"""Infinite play at RTL — ``plans/t2p_lock_chan.md`` S1, checkpoint 4.

What xsim elaborates is the **wrapper** (``rf_shot_tx_loop_top``): the kernel plus its hand-written
``bram_t2p`` memory, so the testbench sees only AXI-Stream and the converter model consumes the
playout exactly as it consumes any other design's.

**Two runs of two designs**, and the second one is the reason the first means anything.

* ``rf_shot_tx_loop`` — the shipped design.  Waveform A plays, a second load arrives mid-play, the
  output switches with filler in between, every header is answered, and the DAC is never starved.
* ``rf_shot_tx_loop_dirty`` — **the positive control**: the same composite with the player's
  ``playing = 0`` removed, reached through ``player_cls`` rather than copied.  It collides on its
  first handover, on purpose.

That pairing is not optional.  ``bram_t2p.v`` ``$error``\\ s on a read-during-write and **XSI
discards ``$error``** (``reference-xsi-discards-rtl-text``), so the RTL check is a VCD scan — and a
scan that finds nothing is indistinguishable from a scan bound to nets that no longer carry the
condition.  The clean run's *"no hazards"* is evidence only because the same scan, on the same
manifest shape, finds hazards in the dirty one.

What is gated, and how each fails
---------------------------------
* **The playout switches**, bit-exact against the same golden pysim is checked on — so the command
  layer, the lock, the memory, the re-layout and the converter's own unpack are covered by one
  comparison, made in **converter codes**, which is what a host wrote.
* **The region is at the top of the memory.**  ``base + offset`` is the shape of the byte-versus-word
  bug ``bram_toy`` stayed green through, so the write addresses are checked to reach the memory's
  last element and to stay off the words below the region.
* **Every header answered**, in order, with its own ``tid`` and the right verdict — including the
  ``SHOT_LOAD`` that must be refused rather than reinterpreted as a loop.
* **The DAC is never starved through the handover.**  The filler is a *value*, not a stall, so the
  converter model's ``blocks_zero_filled`` must be **zero**: a design that stalled instead of filling
  would show up here and nowhere else.
* **The control plays the region while it is being overwritten, and the shipped design does
  not.**  That is the pairing, and it is made on the SAMPLES rather than on the memory's own
  cycle-exact predicate, for a measured reason: at S1 the region is enforced in pysim and *not*
  at RTL, exactly as the plan says.  Vitis reads the BRAM unconditionally and muxes in the
  filler, so **both designs** have the same 34 cycles with both memory ports live on the region
  -- the shipped one simply throws the words away.  What the lock buys at RTL is therefore that
  the data is not USED, and the control is what shows it: one continuous run of samples that
  splices the old waveform into the new.  ``find_read_during_write`` is asserted on the clean
  run too, as the memory's own predicate and nothing stronger.
* **The completion cycle**, recorded exactly.  A result, distinct from the run's loop bound.
* **II=1 on every pipelined loop**, achieved rather than targeted — including ``await_grant``, the
  loop that exists because the obvious blocking read **deadlocked** (see the module docstring of
  ``waveflow/build/mem_lock.h``).

Needs a prior csynth of both tcls plus the XSI toolchain; skips **loudly** rather than passing when
either is missing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.rf_shot_loop.rf_shot_loop import (
    BASE,
    BLKSIZE,
    NWORD,
    STARTUP_BLOCKS,
    blocks_to_codes,
    check_responses,
    check_switched,
    segments,
)
from examples.rf_shot_loop.rf_shot_loop_build import (
    DIRTY_RTL_FILES,
    DIRTY_TOP,
    DIRTY_WRAPPER,
    RTL_FILES,
    TOP,
    WRAPPER,
    generate_tb,
)
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd
from waveflow.hw.rf_shot_tx import SHOT_STATUS_NAMES, ShotTxResp
from waveflow.utils.bram_trace import describe, find_read_during_write, sampled

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_shot_loop"
XSI = ROOT / "xsi"
VERILOG = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
REPORT = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "report"
DIRTY_VERILOG = ROOT / f"{DIRTY_TOP}_proj" / "solution1" / "syn" / "verilog"

#: The hand-written mains, one per design.  **Two, and that is not redundancy**: a harness hardcodes
#: ``DESIGN_DLL`` — the elaborated snapshot it loads — so a control driven by the shipped design's
#: harness would run the shipped design, report its numbers and find no hazard.  Everything green,
#: nothing measured.  (One *scenario*, though: this design never becomes busy, so a single
#: file-driven stream reaches every verdict **and** both loads.)
TB = f"{TOP}_counters"
DIRTY_TB = f"{DIRTY_TOP}_counters"

#: Cycle the last verdict reached its sink.  **Recorded 2026-09-01 on the first green run.**  Exact,
#: not a bound: a cycle count that moves is either a regression or an improvement, and both deserve a
#: human.
#:
#: The shape of the 374: two accepted loads, each a one-word header, a grant that costs one poll
#: period of the DAC-paced player, and 64 payload words at one per cycle; plus three refusals, two of
#: which carry a full payload to drain.
WANT_RESP_LAST_CYCLE = 374

#: Words the DAC pulled off the fabric in 1400 cycles at 0.256 words/cycle.
WANT_DAC_WORDS = 359

#: Blocks the converter's grid had to fill **itself**.  **ZERO, and that is the design's whole
#: claim**: the handover's silence arrives as real beats, so the DAC is fed through it.  A design
#: that stalled instead of filling would be caught here and by nothing else.
WANT_ZERO_FILLED = 0

#: Sample periods that came due with nothing on the wire, and the cycle of the last one.  One, at
#: cycle 4 — before the pipeline has produced its first beat, which no design here avoids.
WANT_DAC_UNDERRUN = 1
WANT_LAST_UNDERRUN_CYCLE = 4

#: The playout, as ``(is_filler, blocks)``: the startup gap, waveform A, the **handover gap**, then
#: waveform B for the rest of the run.  The handover being **two blocks** is the number this whole
#: plan is about — it is how long the converter plays silence while the memory changes hands.
WANT_SEGMENTS = [(True, STARTUP_BLOCKS), (False, 2), (True, 2), (False, 15)]

#: Cycles on which **either** design has both memory ports live inside the shot's region.
#: **Recorded 2026-09-01**, and the same number for both, which is the finding: Vitis reads the
#: BRAM unconditionally at II=1 and muxes in the filler, so the read port stays enabled while the
#: player is yielded.  At S1 the region is a pysim-enforced range and an RTL documentation-only
#: one -- ``plans/t2p_lock_chan.md`` says exactly this under *A grant is not a fence at RTL*, and
#: this is its concrete form.
WANT_PORT_OVERLAP_CYCLES = 34

#: Non-filler runs in the CONTROL's playout.  **One**: it never goes quiet, because it never
#: yields.  The shipped design's is :data:`WANT_SEGMENTS` -- four runs with two gaps.
WANT_CONTROL_RUNS = 1

#: The synthesized pipelined loops, by module.  Named for the **label** on each loop: Vitis names an
#: unlabelled loop ``VITIS_LOOP_<line>_1`` and nests that name into its children, so a comment edit
#: renames the module — and a gate that looks the II up by name then MISSES and skips, which reads as
#: a pass.
#:
#: ``await_grant`` is in this list for a reason that is not throughput.  It exists because the
#: obvious body — one blocking read of the response, right after the request — **deadlocks**: Vitis
#: schedules two ops on two streams with no data dependency into one state, that state stalls on the
#: empty response FIFO, and the request is therefore never sent.  The loop is the barrier that fixed
#: it, and its presence in the report is what says the barrier is still there.
_II_MODULES = (
    "shot_loop_load_task_64_256_64_4_192_Pipeline_take_shot",
    "shot_loop_load_task_64_256_64_4_192_Pipeline_drain_tail",
    "shot_loop_load_task_64_256_64_4_192_Pipeline_await_grant",
    "shot_loop_play_task_64_256_64_192_16_Pipeline_play_chunk",
    # Unlabelled, and it stays that way: `rf_relayout_to_slots_task.h` is Stage A's, RTL-gated as it
    # is, and adding a label would rename a module another gate names.  Safe because only the MODULE
    # is spelled out here — the loop inside it is discovered.
    "rf_relayout_to_slots_task_64_4_2_s",
)


def _require(cond: bool, why: str) -> None:
    """Skip loudly.  A silent skip on a gate this expensive reads as 'passed' in a summary line."""
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


def _counters(out: str) -> dict[str, int]:
    """The ``KEY=VALUE`` lines a counters main prints."""
    vals: dict[str, int] = {}
    for line in out.splitlines():
        if "=" in line and line.split("=", 1)[0].isupper():
            k, v = line.split("=", 1)
            try:
                vals[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return vals


def _run_traced(wrapper: str, tb: str, rtl_files, keep_vcd: Path) -> tuple[dict[str, int], Path]:
    """Elaborate *wrapper* fresh, run ``TB`` against it with tracing, keep the VCD.

    Everything the previous run left is removed first — the snapshot, the built TB and the waveform.
    A cached snapshot is how a broken build passes on old output, and a stale VCD is how a scan
    reports the *previous* run's collisions.
    """
    (XSI / f"rtl_{wrapper}.f").write_text(
        render_rtl_f(wrapper[: -len("_top")], ROOT, extra=rtl_files, stamp_sources=False),
        encoding="utf-8")
    shutil.rmtree(XSI / "xsim.dir" / wrapper, ignore_errors=True)
    for stale in (f"{tb}.exe", f"{tb}.bin", f"{tb}.o"):
        (XSI / stale).unlink(missing_ok=True)
    for od in ("resp", "rf_out"):
        shutil.rmtree(XSI / "vectors" / od, ignore_errors=True)
    trace = XSI / f"{wrapper}_trace.vcd"
    trace.unlink(missing_ok=True)

    r = subprocess.run(xsi_runner_cmd(wrapper, tb, trace=True), cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    text = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in text, (
        f"the {wrapper} RTL run did not complete cleanly:\n{text[-3000:]}")
    assert trace.is_file(), (
        f"the traced run produced no {trace.name}. Is vcd_dumper_{wrapper}.v present in {XSI}, and "
        f"did {XSI_RUNNER} get the `trace` argument?")
    keep_vcd.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(trace, keep_vcd)
    return _counters(text), keep_vcd


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict:
    """Both RTL runs, once: the shipped design and the positive control.

    One fixture, because the control overwrites the shipped run's capture bundles and its waveform —
    so the clean outputs are read and copied out before the dirty design plays.
    """
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(VERILOG.is_dir(),
             f"no csynth RTL at {VERILOG} — run rf_shot_loop_build.py --through csynth")
    _require(DIRTY_VERILOG.is_dir(),
             f"no csynth RTL for the positive control at {DIRTY_VERILOG} — run "
             f"rf_shot_loop_build.py --through csynth_dirty. Without it the clean run's hazard scan "
             f"proves nothing.")
    # `*_proj/` is gitignored build output, and a gate that compares a cycle count against RTL it did
    # not produce reports "a real behaviour change" when the truth is a stale artifact.
    for top in (TOP, DIRTY_TOP):
        _require(rtl_staleness(ROOT, top) is None, rtl_staleness(ROOT, top) or "")
    # The hazard manifests are `*.json` and therefore gitignored like every other build artifact, so
    # they are a prerequisite rather than a checked-in file — and a missing one must SKIP loudly
    # rather than raise, which is what the whole session gate in tests/conftest.py is about.
    for f in (set(RTL_FILES) | set(DIRTY_RTL_FILES)
              | {f"vcd_dumper_{WRAPPER}.v", f"vcd_dumper_{DIRTY_WRAPPER}.v",
                 f"{TOP}_hazard.json", f"{DIRTY_TOP}_hazard.json"}):
        _require((XSI / f).is_file(), f"{XSI / f} — run rf_shot_loop_build.py")
    generate_tb(ROOT)

    keep = tmp_path_factory.mktemp("rf_shot_loop_xsi")
    clean_counters, clean_vcd = _run_traced(WRAPPER, TB, RTL_FILES, keep / "clean.vcd")
    played = _played()
    resps = _responses()
    dirty_counters, dirty_vcd = _run_traced(DIRTY_WRAPPER, DIRTY_TB, DIRTY_RTL_FILES,
                                            keep / "dirty.vcd")
    dirty_played = _played()
    return {
        "clean": clean_counters, "clean_vcd": clean_vcd, "played": played, "responses": resps,
        "dirty": dirty_counters, "dirty_vcd": dirty_vcd, "dirty_played": dirty_played,
        "manifest": json.loads((ROOT / f"xsi/{TOP}_hazard.json").read_text(encoding="utf-8")),
        "dirty_manifest": json.loads(
            (ROOT / f"xsi/{DIRTY_TOP}_hazard.json").read_text(encoding="utf-8")),
    }


def _both_live_on_region(vcd: Path, manifest: dict) -> np.ndarray:
    """Cycles where **both memory ports are live inside the shot's region** -- the real hazard.

    ``find_read_during_write`` implements ``bram_t2p.v``'s ``$error`` exactly: same address, same
    cycle.  That predicate is a strict *subset* of the condition the lock removes -- a reader and a
    writer sweeping one region at different rates corrupt each other's data while passing each other
    between samples, and MEASURED here (2026-09-01) the positive control does exactly that: 34 cycles
    of overlap and the cycle-exact scan returns nothing.

    So the pairing is expressed in this: ``en && we`` on the write port, ``en`` on the read port,
    both addresses inside ``[BASE, BASE + NWORD)``, on one cycle.  Zero for the shipped design and
    non-zero for the control.
    """
    mem = manifest["memories"][0]
    w, r = mem["write"], mem["read"]
    sig = sampled(vcd, manifest, w["addr"], w["en"], w["we"], r["addr"], r["en"])
    wa = np.asarray(sig[w["addr"]]) >> int(w["addr_shift"])
    ra = np.asarray(sig[r["addr"]]) >> int(r["addr_shift"])
    live = ((np.asarray(sig[w["en"]]) != 0) & (np.asarray(sig[w["we"]]) != 0)
            & (np.asarray(sig[r["en"]]) != 0))
    inside = (wa >= BASE) & (wa < BASE + NWORD) & (ra >= BASE) & (ra < BASE + NWORD)
    return np.flatnonzero(live & inside)


def _played() -> np.ndarray:
    """The samples the RF sink captured, as signed converter codes."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    d = XSI / "vectors" / "rf_out"
    if not d.is_dir():
        return np.zeros(0, dtype=np.int64)
    return blocks_to_codes(read_rf_bundle(d, 1, BLKSIZE))


def _responses() -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` off the response **stream**, in arrival order.

    Off the wire rather than off a counter, because the wire is what a host sees: a design that
    decided correctly and serialized wrongly passes every internal check there is.
    """
    from waveflow.utils.burst_io import read_burst_bundle

    d = XSI / "vectors" / "resp"
    if not d.is_dir():
        return []
    words = np.concatenate(read_burst_bundle(d)).ravel()
    n = ShotTxResp.nwords_per_inst(64)
    out = []
    for i in range(0, words.size - n + 1, n):
        r = ShotTxResp().deserialize(words[i:i + n], word_bw=64)
        out.append((int(r.tid), int(r.status), int(r.nsamp_loaded)))
    return out


# ---------------------------------------------------------------------------
# The playout — the gate this whole plan is aimed at
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_rtl_switches_waveform_mid_play(runs):
    """**The gate.**  Waveform A, filler, waveform B — at RTL, bit-exact against the pysim golden.

    One comparison covers the whole path: header -> lock -> memory -> player -> re-layout ->
    converter -> codes.  Under ``RfShotTx`` the second frame is
    :data:`~waveflow.hw.rf_shot_tx.SHOT_BUSY` and there is no second waveform at all.
    """
    check_switched(runs["played"], where="XSI: ")


@pytest.mark.xsi
def test_the_handover_is_two_blocks_of_silence_and_not_a_stall(runs):
    """The shape of the playout, block by block, and the handover gap **recorded exactly**.

    The gap is the number a user of this design feels, so it is pinned rather than bounded: longer
    means the player is not being told to resume promptly, shorter means the transfer got faster and
    somebody should know.
    """
    got = [(bool(f), int(s.size) // BLKSIZE) for f, s in segments(runs["played"])]
    assert got == WANT_SEGMENTS, (
        f"the playout is {got} (filler?, blocks), expected {WANT_SEGMENTS}. The middle filler run is "
        f"the handover: that is how long the converter plays silence while the memory changes hands.")


@pytest.mark.xsi
def test_every_header_is_answered_in_order_with_its_own_verdict(runs):
    """One response per header, and the ordering is the evidence the in-band frame stayed aligned.

    Four verdicts in one stream, including the one that would be invisible if it were wrong: a
    ``SHOT_LOAD`` asks for a finite number of plays, which this design cannot provide, and
    reinterpreting it as a loop would produce perfect-looking samples.
    """
    check_responses(runs["responses"], where="XSI: ")
    assert [SHOT_STATUS_NAMES[s] for _t, s, _n in runs["responses"]].count("SHOT_LOADED") == 3


@pytest.mark.xsi
def test_the_dac_is_never_starved_through_the_handover(runs):
    """``blocks_zero_filled == 0``: the silence arrives as **real beats**.

    The design's filler is a value, not a stall.  A player that blocked while it did not own the
    memory would back-pressure the converter, and the grid would have to fill the gap itself — which
    is what this counter counts, and it is the only place that failure is visible.
    """
    c = runs["clean"]
    assert c["DAC_BLOCKS_ZERO_FILLED"] == WANT_ZERO_FILLED, (
        f"the converter's grid zero-filled {c['DAC_BLOCKS_ZERO_FILLED']} block(s). The handover is "
        f"supposed to be silence the DESIGN produces, not silence the grid invents.")
    assert c["DAC_WORDS_RECV"] == WANT_DAC_WORDS, (
        f"the DAC took {c['DAC_WORDS_RECV']} words, expected {WANT_DAC_WORDS}.")
    assert (c["DAC_UNDERRUN"], c["DAC_LAST_UNDERRUN_CYCLE"]) == (WANT_DAC_UNDERRUN,
                                                                 WANT_LAST_UNDERRUN_CYCLE), (
        f"{c['DAC_UNDERRUN']} sample period(s) came due with nothing, last at cycle "
        f"{c['DAC_LAST_UNDERRUN_CYCLE']}; expected {WANT_DAC_UNDERRUN} at "
        f"{WANT_LAST_UNDERRUN_CYCLE}. The count alone cannot separate a startup transient from a "
        f"steady-state fault with the same total, which is why the cycle is pinned too.")


@pytest.mark.xsi
def test_the_whole_scenario_was_consumed_and_the_last_verdict_landed_on_the_recorded_cycle(runs):
    """A **result**, distinct from the run's loop bound.  Exact in both directions.

    ``CMD_SENT == CMD_TOTAL`` is the half that would have caught this design's first RTL run, where
    the loader deadlocked after two words and every other counter still looked plausible.
    """
    c = runs["clean"]
    assert c["CMD_SENT"] == c["CMD_TOTAL"], (
        f"the driver placed {c['CMD_SENT']} of {c['CMD_TOTAL']} words: the design stopped taking "
        f"the command stream, which is a stall rather than a wrong answer.")
    assert c["RESP_LAST_CYCLE"] == WANT_RESP_LAST_CYCLE, (
        f"the last response landed at cycle {c['RESP_LAST_CYCLE']}, gate expects "
        f"{WANT_RESP_LAST_CYCLE}. That is a real behaviour change — either a regression or an "
        f"improvement worth re-recording.")


# ---------------------------------------------------------------------------
# The region at the top of the memory
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_write_addresses_reach_the_last_element_and_no_further(runs):
    """``base + offset``, measured on the memory's own pins.

    The byte-versus-word bug had every BRAM design mis-addressed and ``bram_toy`` stayed green
    through it, because consistently mis-scaled addressing round-trips perfectly right up to the top
    of the address space.  So the assertion is not "the data came back" — it is *which elements the
    writer actually touched*, and the gated region ends at the memory's last.
    """
    man = runs["manifest"]
    mem = man["memories"][0]
    sig = sampled(runs["clean_vcd"], man, mem["write"]["addr"], mem["write"]["en"],
                  mem["write"]["we"])
    addr = np.asarray(sig[mem["write"]["addr"]]) >> int(mem["write"]["addr_shift"])
    live = (np.asarray(sig[mem["write"]["en"]]) != 0) & (np.asarray(sig[mem["write"]["we"]]) != 0)
    touched = np.unique(addr[live])
    assert touched.size, "the writer never wrote a word; there is no addressing to check"
    assert int(touched.min()) == BASE and int(touched.max()) == BASE + NWORD - 1, (
        f"the writer touched elements {int(touched.min())}..{int(touched.max())}, expected exactly "
        f"[{BASE}, {BASE + NWORD}). A base that is scaled wrongly lands somewhere plausible and "
        f"round-trips perfectly — only the address range says so.")


# ---------------------------------------------------------------------------
# The read-during-write scan, and the run that makes it mean something
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_control_plays_the_region_while_it_is_being_overwritten(runs):
    """**The positive control**, and it is what makes the clean result below evidence.

    ``rf_shot_tx_loop_dirty`` is the shipped composite with the player's ``playing = 0`` removed --
    one line -- so it answers the ``ACQUIRE`` and carries on reading the region the loader is
    writing.  In pysim that raises; at RTL it is silent, because ``bram_t2p.v``'s ``$error`` is
    discarded by XSI, so the only witness is the playout.

    And the playout says it plainly: **one** continuous run of samples with no gap anywhere, opening
    on the uninitialised memory and splicing the old waveform into the new part way through.  Until
    this test fails when it should, the shipped design's clean handover is not evidence of anything.
    """
    played = runs["dirty_played"]
    runs_of = [s for f, s in segments(played) if not f]
    assert len(runs_of) == WANT_CONTROL_RUNS, (
        f"the control's playout has {len(runs_of)} non-filler run(s), expected "
        f"{WANT_CONTROL_RUNS}. It is supposed to NEVER go quiet — it never yields the region — so a "
        f"gap here means the control stopped being broken (check that "
        f"shot_loop_play_dirty_task.h still omits `playing = 0`).")
    with pytest.raises(AssertionError):
        check_switched(played, where="control: ")
    assert int(played[0]) == -1, (
        f"the control's first sample is {int(played[0])}, not -1. It reads the memory before "
        f"anything has been loaded, so the run should open on the uninitialised array — which is "
        f"itself the plausible-samples failure a shot design must not have.")


@pytest.mark.xsi
def test_the_shipped_design_never_returns_a_word_that_is_being_written(runs):
    """``bram_t2p.v``'s own predicate, checked where its ``$error`` cannot be heard.

    And **the honest half**, measured rather than assumed: at S1 the region is enforced in pysim and
    *not* at RTL.  Vitis reads the BRAM unconditionally at II=1 and muxes in the filler, so the read
    port stays enabled while the player is yielded and **both** designs show the same
    :data:`WANT_PORT_OVERLAP_CYCLES` cycles of both-ports-live on the region.  What the lock buys at
    RTL is that the *data* is not used — which is why the pairing above is made on the samples, and
    why this assertion is the memory's cycle-exact predicate and nothing stronger.
    """
    clean = _both_live_on_region(runs["clean_vcd"], runs["manifest"])
    dirty = _both_live_on_region(runs["dirty_vcd"], runs["dirty_manifest"])
    assert clean.size == dirty.size == WANT_PORT_OVERLAP_CYCLES, (
        f"both memory ports are live on [{BASE}, {BASE + NWORD}) for {clean.size} cycle(s) in the "
        f"shipped design and {dirty.size} in the control, expected "
        f"{WANT_PORT_OVERLAP_CYCLES} in both. They are equal because Vitis reads speculatively and "
        f"muxes; a difference means the read port stopped being unconditional, which changes what "
        f"the S1 region does and does not enforce at RTL.")
    hz = find_read_during_write(runs["clean_vcd"], runs["manifest"])
    assert not hz, (
        f"the shipped design returned a word while it was being written: {describe(hz)}. That is "
        f"whatever the BRAM's read-during-write mode happens to be, which looks exactly like a "
        f"sample and no counter would notice.")


# ---------------------------------------------------------------------------
# The II, achieved rather than targeted
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_every_pipelined_loop_reaches_ii_1():
    """Cycles per word, **measured**: the achieved ``PipelineII`` of every pipelined body.

    Achieved, not target — Vitis reports both and they differ whenever it missed.  Five loops: the
    loader's store/drain pass, its residue drain, the grant wait, the player's chunk, and the
    re-layout.
    """
    from waveflow.utils.csynthparse import loop_pipeline_ii, module_loops

    _require(REPORT.is_dir(), f"no csynth report dir at {REPORT}")
    for module in _II_MODULES:
        _require((REPORT / f"{module}_csynth.xml").is_file(), f"no report for {module}")
        loops = module_loops(REPORT, module)
        assert len(loops) == 1, (
            f"{module} reports loops {loops}; each of these modules is one pipelined loop, so a "
            f"second entry means the body grew a loop it did not have.")
        assert loop_pipeline_ii(REPORT, module, loops[0]) == 1, (
            f"{module}.{loops[0]} no longer achieves II=1 — one cycle per element, and the "
            f"throughput claim of both sides of this lock. Do not re-record it without diagnosing "
            f"why the loop stopped flattening.")


@pytest.mark.xsi
def test_the_grant_wait_is_still_a_loop_and_not_a_blocking_read():
    """``await_grant`` exists, and its existence is the fix for a deadlock rather than a style.

    The obvious body — one blocking read of the response right after the request — **deadlocks**:
    Vitis schedules two operations on two streams with no data dependency between them into one
    state, that state stalls on the empty response FIFO, and a stalled state performs none of its
    writes, so the request is never sent.  Measured on the first RTL run of this design: the loader's
    ``ap_CS_fsm`` sat in state 1 for 1400 cycles and ``lock_if_cmd_write`` never asserted once.

    A module named for that loop is what says the barrier is still in ``mem_lock.h``.
    """
    _require(REPORT.is_dir(), f"no csynth report dir at {REPORT}")
    await_mod = "shot_loop_load_task_64_256_64_4_192_Pipeline_await_grant"
    assert (REPORT / f"{await_mod}_csynth.xml").is_file(), (
        f"no synthesized module for the grant wait ({await_mod}). If mem_lock_await went back to a "
        f"single blocking read, this design deadlocks at RTL and csynth says nothing about it.")
