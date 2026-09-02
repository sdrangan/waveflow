"""Continuous capture at RTL — ``plans/t2p_lock_chan.md`` S2, checkpoint 4.

What xsim elaborates is the **wrapper** (``rf_shot_rx_top``): the kernel plus its hand-written
``bram_t2p`` memory, so the testbench sees only AXI-Stream and the converter model feeds it exactly
as it feeds any other design.

**S2's RTL claim is a POSITIVE one, and S1's could not be.**

S1 could only say *"the scan found no read-during-write"* — and its own positive control found none
either, because with one region the shipped design and the deliberately broken one both had the same
34 cycles of both-ports-live and the cycle-exact predicate could separate neither.  Two disjoint
regions change the shape of the evidence entirely:

> Both memory ports are simultaneously live for **140 cycles** of this run, and on **every one of
> them the writer and the reader are in different regions**.

That is not an absence.  The 140 is what makes it non-vacuous — a run where the two were never live
together would prove nothing — and both ports visit both regions (70 cycles each), so it is not one
side sitting still either.  It is also exactly the consequence the plan predicted under *Enable-gating
is CLOSED*: **if the writer and reader never share addresses, both ports staying live is irrelevant
rather than tolerated.**  Vitis still owns the port enable and still reads speculatively; with
disjoint regions that stops mattering.

**No dirty RTL build, and the reason is recorded rather than glossed.**  The fault-injection knob
(``stall_blocks``) is a *pysim* modelling field — it reaches no template argument, because a reader
that dawdles is not a thing the RTL can be asked to do.  A control would need a second design, as S1's
did.  It is not needed here: the claim above is positive and self-witnessing, which is what S1's
absence-of-hazard claim was not.  The clean/dirty pairing lives in pysim, where the knob does
(``tests/hw/test_rf_shot_rx.py``).

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

from examples.rf_shot_rx.rf_shot_rx import (
    CAP_OK,
    CAP_STATUS_NAMES,
    CODE_BASE,
    N_REGION,
    REGION_SAMPLES,
    REGION_WORDS,
    check_windows,
    expected_bases,
    windows_as_codes,
)
from examples.rf_shot_rx.rf_shot_rx_build import (
    RTL_FILES,
    TOP,
    WRAPPER,
    generate_tb,
)
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd
from waveflow.utils.bram_trace import describe, find_read_during_write, sampled

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_shot_rx"
XSI = ROOT / "xsi"
VERILOG = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
REPORT = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "report"

#: The hand-written main.  One, because a capture is asked nothing — there is no command scenario to
#: split and no second run to compare against.
TB = f"{TOP}_counters"

#: Words the converter handed over, and words it could not.  **``ADC_DROPPED`` must be zero**: that
#: is the OTHER loss — a word the fabric would not take, gone before the capture ever saw it — and a
#: run where it is non-zero is measuring the wrong failure.
WANT_ADC_WORDS = 640
WANT_ADC_DROPPED = 0
WANT_ADC_BLOCKS = 40

#: Words the host received: four frames of one header and 128 samples.  **Recorded 2026-09-01 on the
#: first green run.**
WANT_WIN_WORDS = 516
#: Cycle the last window's last word landed.  A **result**, distinct from the run's loop bound.
WANT_WIN_LAST_CYCLE = 2205

#: Cycles on which both memory ports are live, and how many of those have the two in the **same
#: region**.  The first must be non-zero or the second proves nothing; the second must be zero or the
#: design is not doing what two regions are for.  Both recorded, because a change in either is a
#: change in what this gate measures.
WANT_BOTH_LIVE_CYCLES = 140
WANT_SAME_REGION_CYCLES = 0

#: The synthesized pipelined loops, by module.  Named for the **label** on each loop: Vitis names an
#: unlabelled loop ``VITIS_LOOP_<line>_1`` and nests that name into its children, so a comment edit
#: renames the module — and a gate that looks the II up by name then MISSES and skips, which reads as
#: a pass.
_II_MODULES = (
    "pingpong_capture_task_64_256_2_16_Pipeline_store_block",
    "pingpong_window_task_64_256_2_16_Pipeline_drain_window",
    "pingpong_window_task_64_256_2_16_Pipeline_await_grant",
    # Unlabelled, and it stays that way: `rf_relayout_to_dense_task.h` is Stage A's, RTL-gated as it
    # is, and adding a label would rename a module another gate names.  Safe because only the MODULE
    # is spelled out here — the loop inside it is discovered.
    "rf_relayout_to_dense_task_64_4_2_s",
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


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> dict:
    """One traced RTL run, shared by the assertions below.

    Everything the previous run left is removed first — the snapshot, the built TB, the capture
    bundle and the waveform.  A cached snapshot plus a stale bundle is how a broken build passes on
    old output, and a stale VCD is how a scan reports the *previous* run's addresses.
    """
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(VERILOG.is_dir(),
             f"no csynth RTL at {VERILOG} — run rf_shot_rx_build.py --through csynth")
    # `*_proj/` is gitignored build output, and a gate that compares a cycle count against RTL it did
    # not produce reports "a real behaviour change" when the truth is a stale artifact.
    _require(rtl_staleness(ROOT, TOP) is None, rtl_staleness(ROOT, TOP) or "")
    # The hazard manifest is `*.json` and therefore gitignored like every other build artifact, so it
    # is a prerequisite rather than a checked-in file — and a missing one must SKIP loudly rather
    # than raise.
    for f in (*RTL_FILES, f"vcd_dumper_{WRAPPER}.v", f"{TOP}_hazard.json"):
        _require((XSI / f).is_file(), f"{XSI / f} — run rf_shot_rx_build.py")

    # Regenerate the file list from the RTL actually on disk; never trust the committed .f.
    (XSI / f"rtl_{WRAPPER}.f").write_text(
        render_rtl_f(TOP, ROOT, extra=RTL_FILES, stamp_sources=False), encoding="utf-8")
    shutil.rmtree(XSI / "xsim.dir" / WRAPPER, ignore_errors=True)
    for stale in (f"{TB}.exe", f"{TB}.bin", f"{TB}.o"):
        (XSI / stale).unlink(missing_ok=True)
    shutil.rmtree(XSI / "vectors" / "win", ignore_errors=True)
    trace = XSI / f"{WRAPPER}_trace.vcd"
    trace.unlink(missing_ok=True)
    generate_tb(ROOT)

    r = subprocess.run(xsi_runner_cmd(WRAPPER, TB, trace=True), cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    text = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in text, f"the RTL run did not complete cleanly:\n{text[-3000:]}"
    assert trace.is_file(), (
        f"the traced run produced no {trace.name}. Is vcd_dumper_{WRAPPER}.v present in {XSI}, and "
        f"did {XSI_RUNNER} get the `trace` argument?")

    keep = tmp_path_factory.mktemp("rf_shot_rx_xsi") / "trace.vcd"
    shutil.copyfile(trace, keep)
    return {
        "counters": _counters(text),
        "frames": _frames(),
        "vcd": keep,
        "manifest": json.loads((XSI / f"{TOP}_hazard.json").read_text(encoding="utf-8")),
    }


def _frames() -> list[np.ndarray]:
    """The window frames the sink captured — header **and** samples, as a host would see them."""
    from waveflow.utils.burst_io import read_burst_bundle

    d = XSI / "vectors" / "win"
    if not d.is_dir():
        return []
    return [np.asarray(b, dtype=np.uint64).ravel() for b in read_burst_bundle(d)]


def _both_live(vcd: Path, manifest: dict):
    """``(write addr, read addr)`` on every cycle where **both** memory ports are live.

    ``en && we`` on the write port and ``en`` on the read port.  This is the measurement S2's whole
    RTL claim is made from, and it is deliberately *not* the cycle-exact read-during-write predicate:
    that one is an absence, and S1 showed an absence can be produced by two sweeps that simply never
    line up.
    """
    mem = manifest["memories"][0]
    w, r = mem["write"], mem["read"]
    sig = sampled(vcd, manifest, w["addr"], w["en"], w["we"], r["addr"], r["en"])
    wa = np.asarray(sig[w["addr"]]) >> int(w["addr_shift"])
    ra = np.asarray(sig[r["addr"]]) >> int(r["addr_shift"])
    live = ((np.asarray(sig[w["en"]]) != 0) & (np.asarray(sig[w["we"]]) != 0)
            & (np.asarray(sig[r["en"]]) != 0))
    idx = np.flatnonzero(live)
    return wa[idx], ra[idx]


# ---------------------------------------------------------------------------
# The capture, end to end
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_rtl_captures_the_ramp_with_no_gap(run):
    """**The gate.**  Every window whole, every header ``CAP_OK``, and the whole run contiguous.

    One comparison covers the path: converter -> re-layout -> capture -> memory -> window reader ->
    host.  And it is made in **converter codes**, which is what the source played — so a dropped
    block is a *step in the numbers*, visible whether or not anything counted it.
    """
    flat = check_windows(run["frames"], where="XSI: ")
    assert flat.size == 4 * REGION_SAMPLES
    assert int(flat[0]) == CODE_BASE and int(flat[-1]) == CODE_BASE + flat.size - 1


@pytest.mark.xsi
def test_the_windows_alternate_between_the_two_halves(run):
    """The ping-pong, read off the **header** — so it is visible to a host and not only to a test.

    A design handing the same half out twice would move the right number of words, produce
    perfectly contiguous samples if it were lucky, and exercise no second region at all.
    """
    wins = windows_as_codes(run["frames"])
    got = [int(h.base_addr) for h, _c in wins]
    assert got == expected_bases(len(wins)), (
        f"windows came from bases {got}, not the alternating {expected_bases(len(wins))}")
    assert set(got) == {i * REGION_WORDS for i in range(N_REGION)}, (
        "the run never used one of the two regions, so it has not swapped at all")


@pytest.mark.xsi
def test_every_window_carries_CAP_OK_and_zero_lost(run):
    """The verdict, off the wire, at RTL.

    The C++ decides it in the capture and forwards it through the reader untouched, so this is also
    the check that the twin's header layout survived synthesis: a field at the wrong offset would
    come back as a plausible number rather than as an error.
    """
    wins = windows_as_codes(run["frames"])
    assert [int(h.status) for h, _c in wins] == [CAP_OK] * len(wins), (
        f"headers {[CAP_STATUS_NAMES[int(h.status)] for h, _c in wins]}")
    assert [int(h.n_dropped) for h, _c in wins] == [0] * len(wins)


@pytest.mark.xsi
def test_the_converter_never_had_a_word_refused(run):
    """``ADC_DROPPED == 0`` — **the other loss**, and the one that would make this run meaningless.

    A word the fabric would not take is gone before the capture ever sees it, so the design's own
    drop count would stay zero while samples went missing.  The whole premise of a capture front end
    is that it absorbs the converter's rate, and this is where that premise is checked.
    """
    c = run["counters"]
    assert c["ADC_DROPPED"] == WANT_ADC_DROPPED, (
        f"the converter could not hand over {c['ADC_DROPPED']} word(s): the fabric refused them, so "
        f"they never reached the capture and its own counter cannot see them.")
    assert c["ADC_WORDS"] == WANT_ADC_WORDS
    assert c["ADC_BLOCKS_IN"] == WANT_ADC_BLOCKS


@pytest.mark.xsi
def test_the_host_got_the_recorded_words_on_the_recorded_cycle(run):
    """A **result**, distinct from the run's loop bound.  Exact in both directions."""
    c = run["counters"]
    assert c["WIN_WORDS"] == WANT_WIN_WORDS, (
        f"the host got {c['WIN_WORDS']} words, expected {WANT_WIN_WORDS} — four frames of one "
        f"header and {REGION_WORDS} samples.")
    assert c["WIN_LAST_CYCLE"] == WANT_WIN_LAST_CYCLE, (
        f"the last window landed at cycle {c['WIN_LAST_CYCLE']}, gate expects "
        f"{WANT_WIN_LAST_CYCLE}. That is a real behaviour change — either a regression or an "
        f"improvement worth re-recording.")


# ---------------------------------------------------------------------------
# The claim two regions make, and S1 could not
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_both_ports_are_live_together_and_never_in_the_same_region(run):
    """**S2's RTL claim, and it is a positive one.**

    S1 could only report an *absence* — the cycle-exact scan found nothing — and its own positive
    control found nothing either, because with one region the shipped design and the deliberately
    broken one had the same 34 cycles of both-ports-live and the predicate separated neither.

    Here the statement is: the two ports were simultaneously live for a recorded number of cycles,
    and on **every one of them** the writer and the reader were in different regions.  The first
    number is what makes the second non-vacuous — a run where the two were never live together would
    prove nothing at all — and both ports visit both regions, so it is not one side sitting still.

    It is also the consequence the plan predicted under *Enable-gating is CLOSED*: Vitis still owns
    the port enable and still reads speculatively, and with disjoint regions that stops mattering.
    """
    wa, ra = _both_live(run["vcd"], run["manifest"])
    assert wa.size == WANT_BOTH_LIVE_CYCLES, (
        f"both memory ports were live together on {wa.size} cycle(s), expected "
        f"{WANT_BOTH_LIVE_CYCLES}. ZERO would make the disjointness claim below vacuous — the two "
        f"would simply never have been on the memory at the same time.")
    same = (wa // REGION_WORDS) == (ra // REGION_WORDS)
    assert int(same.sum()) == WANT_SAME_REGION_CYCLES, (
        f"the writer and the reader were in the same region on {int(same.sum())} of those cycles, "
        f"expected {WANT_SAME_REGION_CYCLES}. Two disjoint regions are the whole mechanism; if they "
        f"share one, the lock is arbitrating something the design is ignoring.")
    # ... and neither side is sitting still, which is the other way this could be vacuous.
    assert set(np.unique(wa // REGION_WORDS)) == set(range(N_REGION))
    assert set(np.unique(ra // REGION_WORDS)) == set(range(N_REGION))


@pytest.mark.xsi
def test_the_memorys_own_predicate_also_finds_nothing(run):
    """``find_read_during_write`` — the memory's cycle-exact ``$error``, checked where XSI discards it.

    Asserted for what it is worth and no more.  It is a strict *subset* of the claim above: a same
    address on a same cycle.  S1 measured that this predicate can return nothing on a design that
    genuinely overlaps, so it is corroboration here rather than the evidence.
    """
    hz = find_read_during_write(run["vcd"], run["manifest"])
    assert not hz, f"the design collided cycle-exactly: {describe(hz)}."


# ---------------------------------------------------------------------------
# The II, achieved rather than targeted
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_every_pipelined_loop_reaches_ii_1():
    """Cycles per element, **measured**: the achieved ``PipelineII`` of every pipelined body.

    ``store_block`` is the interesting one: it reads its input unconditionally and writes the memory
    *conditionally*, which is the shape that makes a drop possible without back-pressuring an ADC —
    and exactly the shape one might expect to cost a cycle.
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
            f"throughput claim of both sides of this lock.")


@pytest.mark.xsi
def test_the_grant_wait_is_still_a_loop_and_not_a_blocking_read():
    """``await_grant`` exists, and its existence is the fix for a deadlock rather than a style.

    A write followed by a blocking read on a **different** stream has no data dependency, so Vitis
    schedules both into one state — and a state that stalls on the empty response FIFO performs none
    of its writes, so the request is never sent.  That cost S1 a full RTL debug; ``mem_lock_await``
    polls with ``read_nb`` inside a loop, which is a scheduling barrier.  A module named for that
    loop is what says the barrier is still in ``mem_lock.h``.
    """
    _require(REPORT.is_dir(), f"no csynth report dir at {REPORT}")
    await_mod = "pingpong_window_task_64_256_2_16_Pipeline_await_grant"
    assert (REPORT / f"{await_mod}_csynth.xml").is_file(), (
        f"no synthesized module for the grant wait ({await_mod}). If mem_lock_await went back to a "
        f"single blocking read, this design deadlocks at RTL and csynth says nothing about it.")
