"""One transmitter, both opcodes, at RTL — ``plans/rf_shot_unify.md`` Stage A, gates 1-4.

What xsim elaborates is the **wrapper** (``rf_shot_tx_unified_top``): the kernel plus its
hand-written ``bram_t2p`` memory, so the testbench sees only AXI-Stream and the converter model
consumes the playout exactly as it consumes any other design's.

**Two scenarios of ONE design, and that the design is one is the whole point.**

``RfShotTx`` and ``RfShotTxLoop`` are complementary rather than duplicated: one plays a counted
number of passes and refuses a load while it does so, the other plays forever and lets a load
preempt it.  Stage A merges them, and a merge is only proven by driving *both* streams into the
*same* RTL:

* ``vectors/cmd`` — a ``SHOT_LOAD`` of three passes.  A load arriving behind it is answered
  ``SHOT_BUSY``, and when the three passes are spent the design **goes quiet**.
* ``vectors/cmd_loop`` — a ``SHOT_LOOP``.  A load arriving mid-play is **accepted** and preempts it,
  so the waveform on the wire changes; the last one is short, and a short shot is loaded and then
  not played, so this run ends quiet too.

One snapshot, one ``xsimk.dll``, two mains that differ only in three bundle names.  A second
testbench *graph* would be a second model of one design, which is the trap this arc has paid for
more than once.

Why there is no positive control here, and where it lives
---------------------------------------------------------
``rf_shot_loop``'s gate pairs its clean run against a design with the player's ``playing = 0``
removed, because ``bram_t2p.v``'s ``$error`` is discarded by XSI and a scan that finds nothing is
otherwise indistinguishable from a scan bound to the wrong nets.  That control proves the *lock's*
ordering, and this design uses the same ``mem_lock.h``, the same ``LockedT2pMemIF`` and the same
grant sequence.  Shipping a second deliberately broken design to re-prove it would be a second copy
of a finding, not a second finding.  What is new here is the **merge**, and the merge is proven by
the two scenarios above and by :func:`test_both_backends_agree_sample_for_sample`.

What is gated, and how each fails
---------------------------------
* **Gate 1 — the finite path.**  Three whole passes, bit-exact, then filler.  A design that
  preempted the running shot would produce two perfectly good passes and every counter downstream
  would still add up.
* **Gate 2 — the infinite path.**  Waveform A, a handover gap, waveform B — the switch ``SHOT_BUSY``
  would have made impossible.
* **Gate 3 — ``SHOT_BUSY``.**  In the finite stream and *only* there.  A design that set ``busy`` for
  both opcodes would answer ``SHOT_BUSY`` forever after the first loop, which is the defect
  ``rf_shot_loop`` was written to avoid; one that set it for neither would truncate a finite shot.
  Both scenarios in one gate are what separates those.
* **Gate 4 — all five verdicts plus ``SHOT_END``**, across the two streams, each with its own
  ``tid`` and in order.
* **Both backends byte-identical**, sample for sample over the common horizon — so the command
  layer, the lock, the memory, the player, the re-layout and the converter's unpack are covered by
  one comparison, made in **converter codes**, which is what a host wrote.
* **The region is at the top of the memory.**  ``base + offset`` is the shape of the byte-versus-word
  bug ``bram_toy`` stayed green through.
* **The DAC is never starved on either path.**  Filler is a *value*, not a stall.
* **II=1 on every pipelined loop** — including ``await_grant``, the loop that exists because the
  obvious blocking read **deadlocked**.

Needs a prior csynth plus the XSI toolchain; skips **loudly** rather than passing when either is
missing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.rf_shot_unified.rf_shot_unified import (
    BASE,
    BLKSIZE,
    FINITE_FRAMES,
    LOOP_FRAMES,
    NWORD,
    XSI_N_CYCLES,
    blocks_to_codes,
    check_finite_playout,
    check_loop_playout,
    check_responses,
    played_samples,
    run_pysim,
    segments,
)
from examples.rf_shot_unified.rf_shot_unified_build import RTL_FILES, TOP, WRAPPER, generate_tb
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd
from waveflow.hw.rf_shot_tx import SHOT_STATUS_NAMES, ShotTxResp
from waveflow.utils.bram_trace import describe, find_read_during_write, sampled

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_shot_unified"
XSI = ROOT / "xsi"
VERILOG = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
REPORT = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "report"

#: The two hand-written mains, and the bundles each writes.  ONE design and one snapshot: unlike
#: ``rf_shot_loop``'s pairing, these two runs load the same ``xsimk.dll`` on purpose — that they are
#: the same RTL is the claim.
SCENARIOS = (
    ("cmd", f"{TOP}_counters", "resp", "rf_out", FINITE_FRAMES, check_finite_playout),
    ("cmd_loop", f"{TOP}_loop", "resp_loop", "rf_out_loop", LOOP_FRAMES, check_loop_playout),
)

#: Cycle the last verdict reached its sink, per scenario.  **Recorded 2026-09-02 on the first green
#: run.**  Exact, not a bound: a cycle count that moves is either a regression or an improvement, and
#: both deserve a human.
#:
#: The shape of the 269: one accepted load — a one-word header, a grant that costs one poll period of
#: the DAC-paced player, and 64 payload words at one per cycle — plus four refusals, two of which
#: carry a full payload to drain.  The loop stream's 500 is longer because it *accepts* three of its
#: six frames, and each acceptance pays the grant wait again.
WANT_RESP_LAST_CYCLE = {"cmd": 269, "cmd_loop": 500}

#: Words the DAC pulled off the fabric in :data:`XSI_N_CYCLES` cycles at 0.256 words/cycle.  The same
#: in both scenarios, which is the point: the converter's appetite is a property of the converter.
WANT_DAC_WORDS = 359

#: Blocks the converter's grid had to fill **itself**.  **ZERO on both paths, and that is the
#: design's whole claim**: quiet is silence the DESIGN produces — real beats carrying
#: :data:`~waveflow.hw.rf_shot_tx_unified.FILLER` — not silence the grid invents.  A player that
#: blocked while it did not own the region, or that stopped writing when its passes ran out, would be
#: caught here and by nothing else.
WANT_ZERO_FILLED = 0

#: Sample periods that came due with nothing on the wire, and the cycle of the last one.  One, at
#: cycle 4 — before the pipeline has produced its first beat, which no design here avoids.  The cycle
#: is pinned as well as the count, because a count alone cannot separate a startup transient from a
#: steady-state fault with the same total.
WANT_DAC_UNDERRUN = 1
WANT_LAST_UNDERRUN_CYCLE = 4

#: The playout in converter blocks, as ``(is_filler, blocks)``.  **Recorded 2026-09-02.**
#:
#: ``cmd``  — startup filler, then TWELVE blocks of samples (three passes of 256 = 768 samples at 64
#: per block), then SEVEN blocks of quiet.  That trailing run is gate 1: the design stopped on
#: purpose rather than running out of stream.
#:
#: ``cmd_loop`` — startup filler, one block of waveform A, a TWO-block handover, one block of B, then
#: fifteen blocks of quiet.  The handover is the number this plan is about: how long the converter
#: plays silence while the memory changes hands.  The long tail is the merged design's own
#: improvement — the last load is SHORT, and a short shot is loaded and never played;
#: ``rf_shot_loop`` plays the padded result because it has no way to go quiet.
WANT_SEGMENT_BLOCKS = {
    "cmd": [(True, 3), (False, 12), (True, 7)],
    "cmd_loop": [(True, 3), (False, 1), (True, 2), (False, 1), (True, 15)],
}

#: Cycles with **both memory ports live inside the shot's region**, per scenario.  **Recorded
#: 2026-09-02**, and a MEASUREMENT rather than a target — see
#: :func:`test_the_handover_leaves_a_speculative_read_that_the_design_discards` for what it means and
#: what it does not.
WANT_PORT_OVERLAP_CYCLES = {"cmd": 18, "cmd_loop": 55}

#: ``bram_t2p.v``'s own predicate — same address, same cycle, one port writing and the other reading.
#: **Recorded 2026-09-02, and NOT zero on the loop path.**  Read the docstring of
#: :func:`test_the_handover_leaves_a_speculative_read_that_the_design_discards`; the short version is
#: that Vitis reads the BRAM unconditionally at II=1 and muxes the filler in afterwards, so a yielded
#: player still drives its read port, and on a preemption the two addresses eventually coincide.
WANT_RDW_COLLISIONS = {"cmd": 0, "cmd_loop": 2}

#: The elements the writer actually touched.  The region sits at the TOP of the memory on purpose.
WANT_WRITE_RANGE = (BASE, BASE + NWORD - 1)

#: The synthesized pipelined loops, by module.  Named for the **label** on each loop: Vitis names an
#: unlabelled loop ``VITIS_LOOP_<line>_1`` and nests that name into its children, so a comment edit
#: renames the module — and a gate that looks the II up by name then MISSES and skips, which reads as
#: a pass.
#:
#: ``await_grant`` is in this list for a reason that is not throughput.  It exists because the obvious
#: body — one blocking read of the response, right after the request — **deadlocks**: Vitis schedules
#: two ops on two streams with no data dependency into one state, that state stalls on the empty
#: response FIFO, and the request is therefore never sent.
_II_MODULES = (
    "shot_tx_loader_task_64_256_64_4_192_Pipeline_take_shot",
    "shot_tx_loader_task_64_256_64_4_192_Pipeline_drain_tail",
    "shot_tx_loader_task_64_256_64_4_192_Pipeline_await_grant",
    "shot_tx_player_task_64_256_64_192_16_Pipeline_play_chunk",
    # Unlabelled, and it stays that way: `rf_relayout_to_slots_task.h` is shared with the designs
    # this one merges, and adding a label would rename a module their gates name.  Safe because only
    # the MODULE is spelled out here — the loop inside it is discovered.
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


def _responses(bundle: str) -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` off the response **stream**, in arrival order.

    Off the wire rather than off a counter, because the wire is what a host sees: a design that
    decided correctly and serialized wrongly passes every internal check there is.
    """
    from waveflow.utils.burst_io import read_burst_bundle

    d = XSI / "vectors" / bundle
    if not d.is_dir():
        return []
    words = np.concatenate(read_burst_bundle(d)).ravel()
    n = ShotTxResp.nwords_per_inst(64)
    out = []
    for i in range(0, words.size - n + 1, n):
        r = ShotTxResp().deserialize(words[i:i + n], word_bw=64)
        out.append((int(r.tid), int(r.status), int(r.nsamp_loaded)))
    return out


def _played(bundle: str) -> np.ndarray:
    """The samples the RF sink captured, as signed converter codes."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    d = XSI / "vectors" / bundle
    if not d.is_dir():
        return np.zeros(0, dtype=np.int64)
    return blocks_to_codes(read_rf_bundle(d, 1, BLKSIZE))


def _run_traced(tb: str, keep_vcd: Path) -> tuple[dict[str, int], Path]:
    """Run *tb* against a freshly elaborated wrapper, with tracing, and keep the VCD.

    Everything the previous run left is removed first — the built TB and the waveform.  A stale VCD
    is how a scan reports the *previous* run's collisions, and with two scenarios writing one trace
    file that is not a hypothetical.
    """
    for stale in (f"{tb}.exe", f"{tb}.bin", f"{tb}.o"):
        (XSI / stale).unlink(missing_ok=True)
    trace = XSI / f"{WRAPPER}_trace.vcd"
    trace.unlink(missing_ok=True)

    r = subprocess.run(xsi_runner_cmd(WRAPPER, tb, trace=True), cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    text = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in text, (
        f"the {tb} RTL run did not complete cleanly:\n{text[-3000:]}")
    assert trace.is_file(), (
        f"the traced run produced no {trace.name}. Is vcd_dumper_{WRAPPER}.v present in {XSI}, and "
        f"did {XSI_RUNNER} get the `trace` argument?")
    keep_vcd.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(trace, keep_vcd)
    return _counters(text), keep_vcd


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict:
    """Both scenarios, once, against the SAME snapshot.

    One fixture, because the second run overwrites the first's waveform — so each run's outputs are
    read and copied out before the next one plays.
    """
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(VERILOG.is_dir(),
             f"no csynth RTL at {VERILOG} — run rf_shot_unified_build.py --through csynth")
    # `*_proj/` is gitignored build output, and a gate that compares a cycle count against RTL it did
    # not produce reports "a real behaviour change" when the truth is a stale artifact.
    _require(rtl_staleness(ROOT, TOP) is None, rtl_staleness(ROOT, TOP) or "")
    # The hazard manifest is `*.json` and therefore gitignored like every other build artifact, so it
    # is a prerequisite rather than a checked-in file — and a missing one must SKIP loudly rather
    # than raise, which is what the whole-session gate in tests/conftest.py is about.
    for f in (*RTL_FILES, f"vcd_dumper_{WRAPPER}.v", f"{TOP}_hazard.json"):
        _require((XSI / f).is_file(), f"{XSI / f} — run rf_shot_unified_build.py")
    generate_tb(ROOT)
    (XSI / f"rtl_{WRAPPER}.f").write_text(
        render_rtl_f(TOP, ROOT, extra=RTL_FILES, stamp_sources=False), encoding="utf-8")
    shutil.rmtree(XSI / "xsim.dir" / WRAPPER, ignore_errors=True)

    keep = tmp_path_factory.mktemp("rf_shot_unified_xsi")
    out: dict[str, dict] = {}
    for name, tb, resp_b, rf_b, frames, check in SCENARIOS:
        for od in (resp_b, rf_b):
            shutil.rmtree(XSI / "vectors" / od, ignore_errors=True)
        counters, vcd = _run_traced(tb, keep / f"{name}.vcd")
        out[name] = {"counters": counters, "vcd": vcd, "played": _played(rf_b),
                     "responses": _responses(resp_b), "frames": frames, "check": check}
    out["manifest"] = json.loads((XSI / f"{TOP}_hazard.json").read_text(encoding="utf-8"))
    return out


def _port_pins(vcd: Path, manifest: dict):
    """``(write_addr, read_addr, write_live, read_live)`` sampled off the memory's own pins."""
    mem = manifest["memories"][0]
    w, r = mem["write"], mem["read"]
    sig = sampled(vcd, manifest, w["addr"], w["en"], w["we"], r["addr"], r["en"])
    wa = np.asarray(sig[w["addr"]]) >> int(w["addr_shift"])
    ra = np.asarray(sig[r["addr"]]) >> int(r["addr_shift"])
    wl = (np.asarray(sig[w["en"]]) != 0) & (np.asarray(sig[w["we"]]) != 0)
    rl = np.asarray(sig[r["en"]]) != 0
    return wa, ra, wl, rl


# ---------------------------------------------------------------------------
# Gates 1 and 2 — the two playouts, from one RTL
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_rtl_plays_a_finite_shot_three_times_and_then_goes_quiet(runs):
    """**Gate 1.**  ``SHOT_LOAD`` with ``nrepeat = 3``: three whole passes, bit-exact, then filler.

    One comparison covers the whole path: header -> lock -> memory -> player -> re-layout ->
    converter -> codes.  ``RfShotTxLoop`` cannot pass this test at all — it has no repeat count and
    no way to stop.
    """
    runs["cmd"]["check"](runs["cmd"]["played"], where="XSI cmd: ")


@pytest.mark.xsi
def test_the_rtl_switches_waveform_mid_play_on_the_infinite_path(runs):
    """**Gate 2.**  ``SHOT_LOOP``, then a load that PREEMPTS it: A, a gap, B — and then quiet.

    Under ``RfShotTx`` the preempting frame would be :data:`~waveflow.hw.rf_shot_tx.SHOT_BUSY` and
    there would be no waveform B at all.  The trailing quiet is this design's own improvement over
    ``RfShotTxLoop``: the last load is SHORT, and a short shot is loaded and then not played.
    """
    runs["cmd_loop"]["check"](runs["cmd_loop"]["played"], where="XSI cmd_loop: ")


@pytest.mark.xsi
@pytest.mark.parametrize("name", ["cmd", "cmd_loop"])
def test_the_playout_has_the_recorded_block_shape(runs, name):
    """The shape of each playout, block by block, **recorded exactly** rather than bounded.

    The handover gap and the length of the trailing quiet are numbers a user of this design feels:
    longer means the player is not being told to resume promptly, shorter means the transfer got
    faster and somebody should know.
    """
    got = [(bool(f), int(s.size) // BLKSIZE) for f, s in segments(runs[name]["played"])]
    assert got == WANT_SEGMENT_BLOCKS[name], (
        f"{name}: the playout is {got} (filler?, blocks), expected {WANT_SEGMENT_BLOCKS[name]}. A "
        f"filler run between two sample runs is a handover; the one at the END is the design going "
        f"quiet on purpose.")


@pytest.mark.xsi
@pytest.mark.parametrize("name", ["cmd", "cmd_loop"])
def test_both_backends_agree_sample_for_sample(runs, name):
    """**Byte-identical**, RTL against the SimPy golden, in converter codes.

    Compared over the common horizon: the RTL run is a fixed number of *cycles* and the pysim run a
    fixed number of converter *blocks*, so the two tails differ in length by construction and
    nothing else.  Everything before that is one comparison of the whole path.

    This is also the honest half of
    :func:`test_the_handover_leaves_a_speculative_read_that_the_design_discards`: pysim takes the
    region out of the owner's hands inside ``grant()`` and RAISES on the very next access, so if the
    RTL player were *using* the words it reads while yielded, these two sequences could not agree.
    """
    rtl = runs[name]["played"]
    py = played_samples(run_pysim(frames=runs[name]["frames"], in_bundle=f"vectors/{name}"))
    assert rtl.size and py.size, f"{name}: one of the backends produced no samples at all"
    n = min(int(rtl.size), int(py.size))
    assert np.array_equal(rtl[:n], py[:n]), (
        f"{name}: the two backends disagree — first mismatch at sample "
        f"{int(np.flatnonzero(rtl[:n] != py[:n])[0])} of {n}.")


# ---------------------------------------------------------------------------
# Gates 3 and 4 — the verdicts
# ---------------------------------------------------------------------------

@pytest.mark.xsi
@pytest.mark.parametrize("name", ["cmd", "cmd_loop"])
def test_every_header_is_answered_in_order_with_its_own_verdict(runs, name):
    """**Gate 4.**  One response per header, in order, each with its own ``tid``.

    The ordering is the evidence the in-band frame stayed aligned: a loader that mis-counted a
    payload would answer the *next* header with this one's verdict and every count would still be
    right.
    """
    check_responses(runs[name]["responses"], runs[name]["frames"], where=f"XSI {name}: ")


@pytest.mark.xsi
def test_shot_busy_answers_a_finite_shot_and_only_a_finite_shot(runs):
    """**Gate 3, and it needs BOTH streams to mean anything.**

    ``SHOT_BUSY`` appears in the finite stream — preempting a counted shot would silently truncate
    what the host asked for — and must NOT appear in the loop stream, where an infinite player would
    otherwise refuse every load forever.  A design that set ``busy`` for both opcodes passes the
    first half and fails the second; one that set it for neither passes the second and fails the
    first.  Only running one RTL against both streams separates them.
    """
    names = {k: [SHOT_STATUS_NAMES[s] for _t, s, _n in runs[k]["responses"]]
             for k in ("cmd", "cmd_loop")}
    assert names["cmd"].count("SHOT_BUSY") == 1, (
        f"the finite stream's verdicts are {names['cmd']}; exactly one SHOT_BUSY is expected. A "
        f"load accepted mid-shot would truncate the three passes the host asked for, and the "
        f"shorter signal it produced would look perfectly good.")
    assert "SHOT_BUSY" not in names["cmd_loop"], (
        f"the loop stream's verdicts are {names['cmd_loop']}; a SHOT_BUSY here means `busy` is set "
        f"by SHOT_LOOP too, and an infinite player that refuses every later load can never be "
        f"replaced — the defect rf_shot_loop was written to avoid.")


@pytest.mark.xsi
def test_all_five_verdicts_and_the_fence_appear_across_the_two_streams(runs):
    """**Gate 4's other half.**  Every legal answer is exercised by one RTL, and none is a guess.

    ``SHOT_END`` is answered rather than acted on: an ``hls::task`` has no loop to break, so what the
    fence is worth is what its RESPONSE proves — headers are answered strictly in order, so the
    ``SHOT_LOADED`` closing each stream says everything ahead of it has been processed.
    """
    seen = {SHOT_STATUS_NAMES[s] for k in ("cmd", "cmd_loop") for _t, s, _n in runs[k]["responses"]}
    want = {"SHOT_LOADED", "SHOT_SHORT", "SHOT_WRONG_LEN", "SHOT_BUSY", "SHOT_ZERO_LEN"}
    assert want <= seen, (
        f"the two streams together produced {sorted(seen)}; missing {sorted(want - seen)}. A "
        f"verdict no scenario reaches is a verdict no backend has ever compared.")


# ---------------------------------------------------------------------------
# The converter edge, and the run's completion
# ---------------------------------------------------------------------------

@pytest.mark.xsi
@pytest.mark.parametrize("name", ["cmd", "cmd_loop"])
def test_the_dac_is_never_starved_on_either_path(runs, name):
    """``blocks_zero_filled == 0``: quiet arrives as **real beats**.

    Two ways to fail, and this design has both in a way neither predecessor did.  A player that
    blocked while it did not own the memory back-pressures the converter through the handover — the
    infinite path's failure.  A player that simply *stopped writing* when its passes ran out starves
    it forever after — the finite path's, and the one ``rf_shot_loop`` never had to survive because
    it never stops.  Either way the grid fills the gap itself, which is what this counter counts.
    """
    c = runs[name]["counters"]
    assert c["DAC_BLOCKS_ZERO_FILLED"] == WANT_ZERO_FILLED, (
        f"{name}: the converter's grid zero-filled {c['DAC_BLOCKS_ZERO_FILLED']} block(s). Quiet is "
        f"supposed to be silence the DESIGN produces, not silence the grid invents.")
    assert c["DAC_WORDS_RECV"] == WANT_DAC_WORDS, (
        f"{name}: the DAC took {c['DAC_WORDS_RECV']} words, expected {WANT_DAC_WORDS}.")
    assert (c["DAC_UNDERRUN"], c["DAC_LAST_UNDERRUN_CYCLE"]) == (WANT_DAC_UNDERRUN,
                                                                 WANT_LAST_UNDERRUN_CYCLE), (
        f"{name}: {c['DAC_UNDERRUN']} sample period(s) came due with nothing, last at cycle "
        f"{c['DAC_LAST_UNDERRUN_CYCLE']}; expected {WANT_DAC_UNDERRUN} at "
        f"{WANT_LAST_UNDERRUN_CYCLE}.")


@pytest.mark.xsi
@pytest.mark.parametrize("name", ["cmd", "cmd_loop"])
def test_the_scenario_was_consumed_and_the_last_verdict_landed_on_the_recorded_cycle(runs, name):
    """A **result**, distinct from the run's loop bound of :data:`XSI_N_CYCLES`.  Exact both ways.

    ``CMD_SENT == CMD_TOTAL`` is the half that catches a stall rather than a wrong answer: this
    family's first RTL run deadlocked after two words and every other counter still looked plausible.
    """
    c = runs[name]["counters"]
    assert c["CMD_SENT"] == c["CMD_TOTAL"], (
        f"{name}: the driver placed {c['CMD_SENT']} of {c['CMD_TOTAL']} words — the design stopped "
        f"taking the command stream, which is a stall rather than a wrong answer.")
    assert c["RESP_LAST_CYCLE"] == WANT_RESP_LAST_CYCLE[name], (
        f"{name}: the last response landed at cycle {c['RESP_LAST_CYCLE']}, gate expects "
        f"{WANT_RESP_LAST_CYCLE[name]} (of {XSI_N_CYCLES} run). That is a real behaviour change — "
        f"either a regression or an improvement worth re-recording.")


# ---------------------------------------------------------------------------
# The memory's own pins
# ---------------------------------------------------------------------------

@pytest.mark.xsi
@pytest.mark.parametrize("name", ["cmd", "cmd_loop"])
def test_the_write_addresses_reach_the_last_element_and_no_further(runs, name):
    """``base + offset``, measured on the memory's own pins.

    The byte-versus-word bug had every BRAM design mis-addressed and ``bram_toy`` stayed green
    through it, because consistently mis-scaled addressing round-trips perfectly right up to the top
    of the address space.  So the assertion is not "the data came back" — it is *which elements the
    writer actually touched*, and the gated region ends at the memory's last.
    """
    wa, _ra, wl, _rl = _port_pins(runs[name]["vcd"], runs["manifest"])
    touched = np.unique(wa[wl])
    assert touched.size, f"{name}: the writer never wrote a word; there is no addressing to check"
    assert (int(touched.min()), int(touched.max())) == WANT_WRITE_RANGE, (
        f"{name}: the writer touched elements {int(touched.min())}..{int(touched.max())}, expected "
        f"exactly {WANT_WRITE_RANGE}. A base that is scaled wrongly lands somewhere plausible and "
        f"round-trips perfectly — only the address range says so.")


@pytest.mark.xsi
@pytest.mark.parametrize("name", ["cmd", "cmd_loop"])
def test_the_handover_leaves_a_speculative_read_that_the_design_discards(runs, name):
    """**The honest gate, and it is not zero.**  What the lock buys at RTL, measured.

    ``mem_lock``'s ordering is *set the state, then grant*: the player clears ``playing`` before it
    answers an ``ACQUIRE``.  In pysim that is enforced — ``LockedMemSlaveIF.grant()`` takes the
    region out of the owner's hands and the next access RAISES — and the pysim gate is what proves
    the ordering.

    At RTL it buys something weaker, and this test records exactly what.  ``play_chunk`` is pipelined
    at II=1 and reads ``buf[BASE + rd + i]`` **unconditionally**, muxing the filler in afterwards; a
    register guard was measured not to quiet the port (``plans/t2p_lock_chan.md``, *enable-gating is
    closed*).  So a yielded player keeps driving its read address, and:

    * ``cmd``      — one grant, taken before anything has played: ``18`` cycles of both-ports-live on
      the region and **no** address collision.
    * ``cmd_loop`` — three grants, two of them mid-play: ``55`` cycles of overlap and **two** cycles
      where the two addresses coincide.  ``bram_t2p.v`` ``$error``\\ s on those, and XSI throws the
      ``$error`` away (``reference-xsi-discards-rtl-text``), so this scan is the only witness.

    Those two collisions are **not** a defect, and the evidence is in a different test:
    :func:`test_both_backends_agree_sample_for_sample` compares this run against a pysim run where
    reading a yielded region raises, and they are byte-identical.  The word is fetched and thrown
    away.  What would make it a defect is the count *changing*, which is why both numbers are pinned
    rather than bounded — a rise means the player started reading somewhere it should not, and a fall
    means the read port stopped being unconditional, which changes what the region does and does not
    enforce at RTL.
    """
    wa, ra, wl, rl = _port_pins(runs[name]["vcd"], runs["manifest"])
    inside = (wa >= BASE) & (wa < BASE + NWORD) & (ra >= BASE) & (ra < BASE + NWORD)
    overlap = int(np.flatnonzero(wl & rl & inside).size)
    assert overlap == WANT_PORT_OVERLAP_CYCLES[name], (
        f"{name}: both memory ports are live on [{BASE}, {BASE + NWORD}) for {overlap} cycle(s), "
        f"expected {WANT_PORT_OVERLAP_CYCLES[name]}. Vitis reads speculatively and muxes, so this "
        f"is not zero by design; a CHANGE is what matters.")
    hz = find_read_during_write(runs[name]["vcd"], runs["manifest"])
    assert len(hz) == WANT_RDW_COLLISIONS[name], (
        f"{name}: {describe(hz)}; expected exactly {WANT_RDW_COLLISIONS[name]} collision(s). The "
        f"words are discarded — the two backends are byte-identical — but the COUNT is pinned, "
        f"because a rise means the player is reading a region it was told to yield.")


# ---------------------------------------------------------------------------
# The II, achieved rather than targeted
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_every_pipelined_loop_reaches_ii_1():
    """Cycles per word, **measured**: the achieved ``PipelineII`` of every pipelined body.

    Achieved, not target — Vitis reports both and they differ whenever it missed.  Five loops: the
    loader's store/drain pass, its residue drain, the grant wait, the player's chunk, and the
    re-layout.  **The merge cost none of them.**  Both predecessors reach II=1 on their own bodies,
    and the merged loader carries a ``busy`` register and a four-way verdict chain the loop-only one
    did not, while the merged player carries a repeat counter and an exit test.
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
    writes, so the request is never sent.  Measured on the first RTL run of this family: the loader's
    ``ap_CS_fsm`` sat in state 1 for 1400 cycles and ``lock_if_cmd_write`` never asserted once.

    A module named for that loop is what says the barrier is still in ``mem_lock.h``.
    """
    _require(REPORT.is_dir(), f"no csynth report dir at {REPORT}")
    await_mod = "shot_tx_loader_task_64_256_64_4_192_Pipeline_await_grant"
    assert (REPORT / f"{await_mod}_csynth.xml").is_file(), (
        f"no synthesized module for the grant wait ({await_mod}). If mem_lock_await went back to a "
        f"single blocking read, this design deadlocks at RTL and csynth says nothing about it.")
