"""The index is a timestamp on the RECEIVE side, at RTL — ``plans/rf_shot_absolute.md`` S2.

``absolute_index`` is a template argument, so the two settings are two pieces of RTL rather than one
design with a mode register in it.  A gate that only ever elaborated one of them would be asserting
the mode's behaviour against a simulator, so this file drives the *second* snapshot:
``rf_shot_rx_abs_top``, csynth'd from ``gen/rf_shot_rx_abs.cpp``, whose capture instantiates
``pingpong_capture_task<64, 256, 2, 16, 1>``.

**One design, one source, two builds.**
:class:`~examples.rf_shot_rx.rf_shot_rx.RfShotRxAbs` adds a name and a default and overrides nothing
else; the testbench graph is the same ``RfShotRxTB``, cut at a different DUT class; the ramp is the
same ``vectors/rf_in`` the default build's gate drives.  What differs is one integer.

What this run can prove, and what it cannot
-------------------------------------------
It **can** prove the mode synthesizes, keeps ``II=1``, keeps the converter fed, satisfies the
absolute claim on the wire — and the part only an RTL run can say, that **two regions still keep the
writer and the reader apart**.  That is the trap ``plans/rf_shot_absolute.md`` S2 names: absolute
indexing moves *when* a region is claimed, and the property ``plans/t2p_lock_chan.md`` S2 established
(both memory ports live together, never in the same region) is what makes the region enforced at RTL
by construction rather than by an assertion nobody can hear.  So it is **re-measured here rather than
inherited**.

It **cannot** prove what a drop does to an address, because the only thing that loses samples on RX
is a reader that dawdles, and ``stall_blocks`` is a pysim modelling field that reaches no template
argument — a reader that dawdles is not a thing the RTL can be asked to do.  That is the same reason
``plans/t2p_lock_chan.md`` S2 declined to ship a dirty RTL build, and it is recorded rather than
glossed: the drop gates below therefore run in pysim, where the knob does, and the two controls live
beside them in ``tests/hw/test_rf_shot_rx.py``.

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
    N_REGION,
    REGION_WORDS,
    RfShotRxAbs,
    check_addresses_are_the_phase,
    check_windows,
    expected_bases,
    frames_from_sink,
    run_pysim,
    windows_as_codes,
)
from examples.rf_shot_rx.rf_shot_rx_build import (
    ABS_STALL_BLOCKS,
    RTL_FILES_ABS,
    STALL_BLOCKS,
    TOP_ABS,
    WRAPPER_ABS,
    generate_tb,
)
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd
from waveflow.utils.bram_trace import describe, find_read_during_write, sampled

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_shot_rx"
XSI = ROOT / "xsi"
VERILOG = ROOT / f"{TOP_ABS}_proj" / "solution1" / "syn" / "verilog"
REPORT = ROOT / f"{TOP_ABS}_proj" / "solution1" / "syn" / "report"

#: The hand-written main for this build, and the bundle it writes.  ``_abs``-suffixed on the output
#: side so the two builds' captures sit beside each other.
TB = f"{TOP_ABS}_counters"
WIN_BUNDLE = "win_abs"

#: **Recorded 2026-09-08 on the first green run of this build, and every one of them is the default
#: build's number unchanged.**  That is the finding rather than a coincidence: on a run that drops
#: nothing the fill pointer and the block count advance together, so the two builds place every word
#: at the same address on the same cycle.  The mode costs nothing until something is lost.
WANT_ADC_WORDS = 640
WANT_ADC_DROPPED = 0
WANT_ADC_BLOCKS = 40
WANT_WIN_WORDS = 516
WANT_WIN_LAST_CYCLE = 2205

#: Cycles on which both memory ports are live, and how many of those have the two in the **same
#: region**.  **132 and 0, recorded 2026-09-08** — against the default build's 140 and 0.
#:
#: The zero is the property; the 132 is what makes it mean something, and it is **this build's own
#: number**.  The eight-cycle difference is the placement decision being a different piece of logic:
#: Vitis schedules the capture body slightly differently, so the writer's and the reader's sweeps
#: overlap for eight fewer cycles.  Nothing about *which addresses* either touches moved — both ports
#: still visit both regions in equal measure, which is asserted rather than assumed.
WANT_BOTH_LIVE_CYCLES = 132
WANT_SAME_REGION_CYCLES = 0

#: The synthesized pipelined loops of **this** build, by module.  **Read off the report directory,
#: never predicted** — a name that misses makes the II gate skip, which reads as a pass.
#:
#: ``ABS`` is on the CAPTURE's template arguments and not on the window reader's, so exactly one of
#: these four is renamed between the two builds: ``pingpong_capture_task_64_256_2_16_0_*`` there and
#: ``..._64_256_2_16_1_*`` here.  Both had to be re-anchored, including the one whose new argument is
#: zero — S1 lost a run to assuming a trailing zero is dropped from the mangled name.
_II_MODULES = (
    "pingpong_capture_task_64_256_2_16_1_Pipeline_store_block",
    "pingpong_window_task_64_256_2_16_Pipeline_drain_window",
    "pingpong_window_task_64_256_2_16_Pipeline_await_grant",
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


def _frames() -> list[np.ndarray]:
    """The window frames the sink captured — header **and** samples, as a host would see them."""
    from waveflow.utils.burst_io import read_burst_bundle

    d = XSI / "vectors" / WIN_BUNDLE
    if not d.is_dir():
        return []
    return [np.asarray(b, dtype=np.uint64).ravel() for b in read_burst_bundle(d)]


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> dict:
    """One traced RTL run of the absolute-index build, shared by the assertions below.

    Everything the previous run left is removed first — the snapshot, the built TB, the capture
    bundle and the waveform.  A cached snapshot plus a stale bundle is how a broken build passes on
    old output, and a stale VCD is how a scan reports the *previous* run's addresses.
    """
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(VERILOG.is_dir(),
             f"no csynth RTL at {VERILOG} — run rf_shot_rx_build.py --through csynth_abs")
    _require(rtl_staleness(ROOT, TOP_ABS) is None, rtl_staleness(ROOT, TOP_ABS) or "")
    for f in (*RTL_FILES_ABS, f"vcd_dumper_{WRAPPER_ABS}.v", f"{TOP_ABS}_hazard.json"):
        _require((XSI / f).is_file(), f"{XSI / f} — run rf_shot_rx_build.py")

    (XSI / f"rtl_{WRAPPER_ABS}.f").write_text(
        render_rtl_f(TOP_ABS, ROOT, extra=RTL_FILES_ABS, stamp_sources=False), encoding="utf-8")
    shutil.rmtree(XSI / "xsim.dir" / WRAPPER_ABS, ignore_errors=True)
    for stale in (f"{TB}.exe", f"{TB}.bin", f"{TB}.o"):
        (XSI / stale).unlink(missing_ok=True)
    shutil.rmtree(XSI / "vectors" / WIN_BUNDLE, ignore_errors=True)
    trace = XSI / f"{WRAPPER_ABS}_trace.vcd"
    trace.unlink(missing_ok=True)
    generate_tb(ROOT, RfShotRxAbs)

    r = subprocess.run(xsi_runner_cmd(WRAPPER_ABS, TB, trace=True), cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    text = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in text, f"the RTL run did not complete cleanly:\n{text[-3000:]}"
    assert trace.is_file(), (
        f"the traced run produced no {trace.name}. Is vcd_dumper_{WRAPPER_ABS}.v present in {XSI}?")

    keep = tmp_path_factory.mktemp("rf_shot_rx_abs_xsi") / "trace.vcd"
    shutil.copyfile(trace, keep)
    return {
        "counters": _counters(text),
        "frames": _frames(),
        "vcd": keep,
        "manifest": json.loads((XSI / f"{TOP_ABS}_hazard.json").read_text(encoding="utf-8")),
    }


@pytest.fixture(scope="module")
def pysim_runs() -> dict:
    """The SimPy goldens the drop gates need, for **both** builds on one stalled scenario.

    The default build's is the negative control, not a comparison: the whole claim is that the same
    assertion parts the two.  ``stall_blocks`` reaches no template argument, so this is the only tier
    that can produce a drop at all.
    """
    out: dict[str, dict] = {}
    for label, cls in (("abs", RfShotRxAbs), ("default", None)):
        kw = {} if cls is None else {"dut_cls": cls}
        tb = run_pysim(stall_blocks=STALL_BLOCKS, n_blk=ABS_STALL_BLOCKS, **kw)
        out[label] = {"tb": tb, "frames": frames_from_sink(tb)}
    return out


def _both_live(vcd: Path, manifest: dict):
    """``(write addr, read addr)`` on every cycle where **both** memory ports are live.

    ``en && we`` on the write port and ``en`` on the read port — deliberately *not* the cycle-exact
    read-during-write predicate, which is an absence and can be produced by two sweeps that simply
    never line up.
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
# The feature
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_address_is_the_phase_at_rtl(run):
    """**Gate 1, and the whole feature.**  Every captured word sits at the address its index names.

    Two layers, and neither is enough alone.  The **header** claim is that a window's ``base_addr``
    is ``window_abs_index(...) % depth`` — the address the window's own absolute position names,
    rather than wherever a search happened to land.  The **data** claim is that the ramp codes which
    arrived under that claim are the ones whose indices name it.  A design that placed correctly and
    announced wrongly passes the second alone; one that announced correctly and placed wrongly passes
    the first.

    On this run the two builds agree, because nothing was dropped — see
    :func:`test_the_two_builds_are_identical_when_nothing_is_lost`.  What separates them is a lossy
    run, and that is the pair below.
    """
    frames = run["frames"]
    assert frames, "no window frame reached the host"
    idx = run_dut_absolute(frames)
    assert idx == [0, REGION_WORDS, 2 * REGION_WORDS, 3 * REGION_WORDS], (
        f"the windows' absolute word indices are {idx}; four consecutive whole regions were "
        f"captured, so they should be 0, {REGION_WORDS}, {2 * REGION_WORDS}, {3 * REGION_WORDS}.")
    check_addresses_are_the_phase(frames, where="RTL abs: ")


def run_dut_absolute(frames) -> list[int]:
    """:meth:`~waveflow.hw.rf_shot_rx.RfShotRx.assert_windows_absolute` against a bare graph.

    The assertion is a method on the composite because it needs ``depth`` and ``region_words``, and
    the RTL run has no composite object — so one is elaborated purely to be asked its geometry. It
    never runs; nothing about this borrows a pysim result.
    """
    from waveflow.simulation.simulation import Simulation

    from examples.rf_shot_rx.rf_shot_rx import WORD

    dut = RfShotRxAbs.for_word(WORD, depth=REGION_WORDS * N_REGION, sim=Simulation(), name="probe")
    return dut.assert_windows_absolute(frames, where="RTL abs: ")


@pytest.mark.xsi
def test_a_DROP_leaves_a_HOLE_and_not_a_SHIFT(pysim_runs):
    """**The gate that carries S1's correction**, and the one only pysim can run.

    S1 deferred RX with *"a capture that drops a block loses its place in a way a player cannot."*
    That described the implementation, not a necessity. Here the reader is starved until the capture
    runs out of regions, and the windows *after* the loss are asserted to be at the addresses their
    own absolute indices name — not shifted up by what was lost.

    It is in pysim because ``stall_blocks`` reaches no template argument: a reader that dawdles is
    not a thing the RTL can be asked to do, which is the same reason ``plans/t2p_lock_chan.md`` S2
    shipped no dirty RTL build. The RTL half of this file proves the mode *is* the RTL; this proves
    what the mode is for.

    The reachability guard is the first assertion: a run that dropped nothing proves nothing here.
    """
    tb, frames = pysim_runs["abs"]["tb"], pysim_runs["abs"]["frames"]
    assert int(tb.dut.n_dropped), (
        "the stalled reader lost nothing, so this run cannot show what a drop does to an address. "
        "Raise STALL_BLOCKS or ABS_STALL_BLOCKS — this gate is vacuous otherwise.")

    idx = tb.dut.assert_windows_absolute(frames, where="pysim abs lossy: ")
    check_addresses_are_the_phase(frames, where="pysim abs lossy: ")

    hdrs = [(int(h.status), int(h.base_addr), int(h.n_dropped)) for h, _c in windows_as_codes(frames)]
    assert [nd for _s, _b, nd in hdrs] == [0, 0, 256, 256], (
        f"the control run's shape moved: {hdrs}. Recorded as two clean windows, then 256 words — "
        f"exactly two whole windows — lost, then two more at their absolute addresses.")
    assert idx == [0, 128, 512, 640], f"absolute word indices {idx}"
    assert idx[2] % (REGION_WORDS * N_REGION) == hdrs[2][1], (
        "the window after the loss is not at the address its own index names, which is the whole "
        "of what absolute_index buys.")


@pytest.mark.xsi
def test_the_same_assertion_FAILS_at_absolute_index_0(pysim_runs):
    """**THE NEGATIVE CONTROL**, and it names its run rather than sweeping.

    Without it ``absolute_index`` could do nothing and every gate here would still pass. Two things
    it has to get right, and both were measured rather than assumed:

    * **it must be a lossy run.** With nothing dropped the fill pointer and the block count advance
      together, so the default build satisfies the absolute assertions honestly — which is exactly
      what :func:`test_the_two_builds_are_identical_when_nothing_is_lost` records.
    * **it must be a *named* lossy run.** After a drop a default-mode window can still land on its
      absolute address by coincidence. On this scenario the default build gets three in a row wrong.

    It runs in pysim on both halves, because what it must isolate is the **parameter**: the two
    pysim runs differ in nothing else, where the two RTL builds differ in a snapshot as well.
    """
    tb, frames = pysim_runs["default"]["tb"], pysim_runs["default"]["frames"]
    assert int(tb.dut.n_dropped), "the control run must actually drop something"

    # The default design is CORRECT — every window is whole, and its samples are contiguous.
    check_windows(frames, where="pysim default lossy: ", expect_loss=True)

    with pytest.raises(AssertionError):
        tb.dut.assert_windows_absolute(frames, where="pysim default lossy: ")
    with pytest.raises(AssertionError, match="supposed to BE the timestamp"):
        check_addresses_are_the_phase(frames, where="pysim default lossy: ")


@pytest.mark.xsi
def test_absolute_indexing_costs_a_WHOLE_WINDOW_where_the_default_costs_a_BLOCK(pysim_runs):
    """**The cost, measured** — the RX mirror of the deferral TX pays.

    A region busy at its boundary is skipped **entirely**, because the claim is made once and held;
    the default mode retries every block and recovers as soon as a region frees. So on the same
    stalled run the absolute build loses more, in coarser units.

    That coarseness is not only a cost: it is what keeps an announced window from ever being part
    stale, and therefore what lets the header localize a hole with **no per-block valid mask**
    (``plans/rf_shot_absolute.md`` S2, *what is in a hole*).
    """
    lost = {k: int(v["tb"].dut.n_dropped) for k, v in pysim_runs.items()}
    assert (lost["default"], lost["abs"]) == (160, 448), (
        f"the recorded cost moved: {lost}. Both are measurements; a change in either is a finding.")
    for label, whole in (("abs", True), ("default", False)):
        nd = [int(h.n_dropped) for h, _c in windows_as_codes(pysim_runs[label]["frames"])]
        if whole:
            assert all(x % REGION_WORDS == 0 for x in nd), (
                f"the absolute build published losses {nd} that are not whole windows")
        else:
            assert any(x % REGION_WORDS for x in nd), (
                f"the default build lost whole windows only ({nd}), so this gate is not "
                f"distinguishing the two granularities.")


# ---------------------------------------------------------------------------
# The trap: the lock is on the critical path here in a way it was not on TX
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_both_ports_are_live_together_and_never_in_the_same_region(run):
    """**The property absolute indexing must not break, re-measured rather than inherited.**

    ``plans/t2p_lock_chan.md`` S2's whole RTL claim: both memory ports are simultaneously live for
    many cycles of this run, and on **every one of them** the writer and the reader are in different
    regions. That is what makes the region enforced at RTL *by construction* — Vitis still owns the
    port enable and still reads speculatively, and with disjoint regions that stops mattering.

    TX's player owns one region and yields it; this design holds two and hands them over
    continuously, so ``absolute_index`` changing **when** a region is claimed is the one edit in this
    plan that could plausibly break it. It does not, and the argument is the same one as before: a
    region is claimed only while its ``full`` flag is clear, and only this task ever sets that flag —
    at the *end* of filling. So the reader cannot acquire a region mid-fill in either mode.

    The count is non-zero or the zero proves nothing, and both ports visit both regions or it is one
    side sitting still.
    """
    wa, ra = _both_live(run["vcd"], run["manifest"])
    assert wa.size == WANT_BOTH_LIVE_CYCLES, (
        f"both ports were live together on {wa.size} cycle(s), recorded "
        f"{WANT_BOTH_LIVE_CYCLES}. A run where they never are proves nothing about regions, so this "
        f"count is asserted before the zero below is allowed to mean anything.")
    same = int(np.sum((wa // REGION_WORDS) == (ra // REGION_WORDS)))
    assert same == WANT_SAME_REGION_CYCLES, (
        f"{same} of {wa.size} both-live cycles have the writer and the reader in the SAME region "
        f"under absolute_index. The disjoint-region property is what makes the lock enforced at RTL "
        f"rather than by convention, and trading it for absolute addressing would be a decision "
        f"rather than a detail. Writer regions "
        f"{sorted(set((wa // REGION_WORDS).tolist()))}, reader "
        f"{sorted(set((ra // REGION_WORDS).tolist()))}.")
    assert set((wa // REGION_WORDS).tolist()) == set(range(N_REGION)), (
        f"the writer only ever visited region(s) {sorted(set((wa // REGION_WORDS).tolist()))} while "
        f"the reader was live; one side sitting still would satisfy the zero above for the wrong "
        f"reason.")
    assert set((ra // REGION_WORDS).tolist()) == set(range(N_REGION))


@pytest.mark.xsi
def test_the_memorys_own_predicate_also_finds_nothing(run):
    """``bram_t2p.v``'s one-sided predicate — same address, same cycle, one writing one reading.

    Weaker than the region claim above and asserted anyway, because it is the condition the memory
    itself would flag and XSI throws ``$error`` away (``reference-xsi-discards-rtl-text``).
    """
    hits = find_read_during_write(run["vcd"], run["manifest"])
    assert not hits, (
        f"{len(hits)} read-during-write collision(s) under absolute_index: "
        f"{describe(hits[:5])}")


# ---------------------------------------------------------------------------
# What must NOT have moved
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_ramp_is_captured_with_no_gap_at_rtl(run):
    """The end-to-end claim, unchanged by the mode: every window whole, ``CAP_OK``, contiguous."""
    flat = check_windows(run["frames"], where="RTL abs: ")
    assert flat.size == 4 * REGION_WORDS * 4, f"{flat.size} samples reached the host"


@pytest.mark.xsi
def test_the_windows_alternate_between_the_two_halves(run):
    """The ping-pong, on a run that lost nothing.

    **Alternation is a property of a clean run**, and under ``absolute_index`` that is not a
    technicality: a skipped region does not lose its turn, so a lossy run may announce the same base
    twice in a row and be working correctly. This run drops nothing, so it must alternate.
    """
    bases = [int(h.base_addr) for h, _c in windows_as_codes(run["frames"])]
    assert bases == expected_bases(len(bases)), (
        f"windows came from bases {bases}, not the alternating {expected_bases(len(bases))}.")
    assert all(int(h.status) == CAP_OK and not int(h.n_dropped)
               for h, _c in windows_as_codes(run["frames"]))


@pytest.mark.xsi
def test_the_two_builds_are_identical_when_nothing_is_lost(run):
    """**The finding worth writing down**, and the reason the negative control needs a lossy run.

    Every counter this build produces is the default build's number unchanged — the same 640 words
    in, the same 516 words out, on the same cycle. On a run that drops nothing the fill pointer and
    the block count advance together, so the two builds place every word at the same address at the
    same instant. **The mode costs nothing until something is lost**, and it distinguishes nothing
    either, which is why every gate that separates the two modes above is on a stalled run.
    """
    c = run["counters"]
    assert (c["ADC_WORDS"], c["ADC_DROPPED"], c["ADC_BLOCKS_IN"]) == (
        WANT_ADC_WORDS, WANT_ADC_DROPPED, WANT_ADC_BLOCKS), (
        f"the converter side moved: {c}. ADC_DROPPED must be zero — that is the OTHER loss, a word "
        f"the fabric would not take, and a run where it is non-zero measures the wrong failure.")
    assert (c["WIN_WORDS"], c["WIN_LAST_CYCLE"]) == (WANT_WIN_WORDS, WANT_WIN_LAST_CYCLE), (
        f"the host side moved: {c['WIN_WORDS']} word(s), last at cycle {c['WIN_LAST_CYCLE']}; "
        f"recorded {WANT_WIN_WORDS} at {WANT_WIN_LAST_CYCLE} — which are the DEFAULT build's "
        f"numbers. A difference here is the mode costing something on a clean run, which it should "
        f"not.")


@pytest.mark.xsi
def test_every_pipelined_loop_still_reaches_ii_1_in_the_absolute_build():
    """**The mode costs no throughput**, measured rather than argued.

    Four loops, and all four achieve ``II=1`` in this build exactly as they do in the default one —
    including ``store_block``, the one whose body now reads a fifth static and computes its region
    from the pointer. The names carry the template arguments and were read off the report directory
    rather than predicted: see :data:`_II_MODULES`.
    """
    from waveflow.utils.csynthparse import loop_pipeline_ii, module_loops

    _require(REPORT.is_dir(), f"no csynth report dir at {REPORT}")
    for module in _II_MODULES:
        _require((REPORT / f"{module}_csynth.xml").is_file(), f"no report for {module}")
        loops = module_loops(REPORT, module)
        assert len(loops) == 1, (
            f"{module} reports loops {loops}; each of these modules is one pipelined loop.")
        assert loop_pipeline_ii(REPORT, module, loops[0]) == 1, (
            f"{module}.{loops[0]} no longer achieves II=1 under absolute_index. The mode is "
            f"supposed to be free at RTL; do not re-record this without diagnosing why.")


@pytest.mark.xsi
def test_the_synthesized_capture_carries_the_MODE_in_its_template_arguments():
    """The two builds are two designs, and the generated C++ says which is which.

    ``ABS`` is a template argument so Vitis folds it and each build synthesizes exactly the design it
    asked for. A build that had lost the argument would still run, still capture, and still pass
    every counter gate here — because it would be the *default* design under an absolute-index name.

    The window reader takes no such argument and must not: it follows the ``base_addr`` it is handed,
    which is still an address and still 28 bits wide.
    """
    d, nr, bw = REGION_WORDS * N_REGION, N_REGION, 16
    cpp = (ROOT / "gen" / f"{TOP_ABS}.cpp").read_text(encoding="utf-8")
    assert f"pingpong_capture_task<64, {d}, {nr}, {bw}, 1>" in cpp, (
        f"{TOP_ABS}.cpp does not instantiate the capture with ABS=1; this whole file would then be "
        f"gating the default design under another name.")
    assert f"pingpong_window_task<64, {d}, {nr}, {bw}>" in cpp, (
        "the window reader gained a template argument it has no use for.")
    default = (ROOT / "gen" / "rf_shot_rx.cpp").read_text(encoding="utf-8")
    assert f"pingpong_capture_task<64, {d}, {nr}, {bw}, 0>" in default, (
        "rf_shot_rx.cpp does not instantiate the capture with ABS=0 — the default build must be "
        "today's behaviour, spelled out rather than defaulted.")


@pytest.mark.xsi
def test_the_pysim_rung_recorded_both_builds():
    """The toolchain-free rung ran the absolute build too, and filed what it measured.

    ``results/rf_shot_rx_pysim.json`` is what a reader without Vivado has, so a mode that only exists
    inside a gate nobody can run is a mode nobody can check. It carries the negative control's own
    failure message, which is the part a reader most needs and the part a green suite hides.
    """
    p = ROOT / "results" / "rf_shot_rx_pysim.json"
    _require(p.is_file(), f"{p} — run rf_shot_rx_build.py --through pysim")
    got = json.loads(p.read_text(encoding="utf-8"))
    assert {"clean", "stalled", "clean_abs", "stalled_abs", "stalled_ctl"} <= set(got)
    assert got["stalled_abs"]["n_dropped"] == 448 and got["stalled_ctl"]["n_dropped"] == 160
    assert got["stalled_ctl"]["absolute_control_failed_with"], (
        "the pysim rung's negative control did not fail, so the file records a control that "
        "asserted nothing.")
    assert got["clean_abs"]["headers"] == got["clean"]["headers"], (
        "the two builds disagree on a clean run, where they are supposed to be identical.")
