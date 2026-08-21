"""``rf_repeat_play`` at RTL — the whole transmitter, scheduler included, through real Verilog.

Stage 1's first pass could not reach this at all: the repeat scheduler is *reactive*, and the XSI
testbench is file-driven. Moving the scheduler into the DUT removed that blocker — the testbench now
pushes ``NSAMP`` words once, from the ``AxisMaster`` that already exists — and this file is what that
bought.

**What is asserted here, and what is not.**  The run completes, the ``start_now`` window plays
bit-exact, and the converter does not starve after the startup transient. The *phase* of the repeats
does **not** match the schedule exactly, and that is recorded as an open finding rather than
asserted around — see :class:`TestTheRepeatPhaseIsNotYetBitExact`. Weakening a property until it
passes is how a gate stops being one.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.rf_repeat_play.rf_repeat_play import NSAMP, SAMP_BW, waveform
from examples.rf_repeat_play.rf_repeat_play_build import TOP, generate_tb
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, xsi_runner_cmd

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_repeat_play"
TB = f"{TOP}_bfm_tb"

#: Samples the DAC pulled over the run — 60000 fabric cycles at 0.256 samples/cycle.  A result of the
#: run bound and the converter's rate, not of the design; it moves if either does.
WANT_TOTAL_SAMPLES = 15296

#: Slots gone before the ``start_now`` window reaches the converter.  **Measured, then pinned**: the
#: waveform must cross the wave port, the scheduler must issue a command, and the loader must stream
#: 64 tagged samples.  A change here is a change in the design's startup latency and wants a human.
WANT_LEAD_IN = 24

#: Zero runs in the whole playout.  **Two, and only two**: the lead-in above, and the ``LEAD`` hole
#: that a deferred response makes unavoidable.  This is the strong steady-state property — after
#: those, the converter is fed for 15200 consecutive samples with no gap at all.
WANT_ZERO_RUNS = 2


def _require(cond: bool, why: str) -> None:
    """Skip loudly — a silent skip on a gate this expensive reads as 'passed' in a summary line."""
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


def _played(xsi: Path) -> np.ndarray:
    """The samples the RF sink captured, as unsigned 16-bit words.

    The bundle holds **doubles** — real amplitudes on the far side of the converter — so this
    reverses the same quantization ``played_samples`` applies to the pysim sink, and the two are
    therefore comparable numbers rather than two different views.
    """
    from waveflow.utils.burst_io import read_burst_bundle

    bursts = read_burst_bundle(xsi / "vectors" / "rf_out")
    packed = np.concatenate([np.asarray(b, dtype=np.uint64) for b in bursts])
    reals = np.frombuffer(packed.tobytes(), dtype=np.float64)
    ints = np.rint(reals * float(1 << (SAMP_BW - 1))).astype(np.int64)
    return (ints % (1 << SAMP_BW)).astype(np.uint64)


def _zero_runs(a: np.ndarray) -> list[tuple[int, int]]:
    """``(start, length)`` for every run of zeros — the gaps the converter was not fed."""
    out, i = [], 0
    z = a == 0
    while i < a.size:
        if z[i]:
            j = i
            while j < a.size and z[j]:
                j += 1
            out.append((i, j - i))
            i = j
        else:
            i += 1
    return out


@pytest.fixture(scope="module")
def played() -> np.ndarray:
    """One RTL run, shared by the assertions below."""
    xsi = ROOT / "xsi"
    _require((xsi / XSI_RUNNER).exists(), f"{xsi / XSI_RUNNER}")
    proj = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
    _require(proj.is_dir(),
             f"no csynth RTL at {proj} — run rf_repeat_play_build.py --through csynth")

    # Regenerate the file list FROM THE RTL ON DISK; never trust a committed `.f`.  A stale one names
    # files that may no longer be this design, and xsim would elaborate them and PASS.
    (xsi / f"rtl_{TOP}.f").write_text(render_rtl_f(TOP, ROOT), encoding="utf-8")
    # Force a clean elaboration: a cached snapshot proves nothing about this design.
    shutil.rmtree(xsi / "xsim.dir" / TOP, ignore_errors=True)
    for stale in (f"{TB}.exe", f"{TB}.bin", f"{TB}.o"):
        (xsi / stale).unlink(missing_ok=True)
    shutil.rmtree(xsi / "vectors" / "rf_out", ignore_errors=True)
    generate_tb(ROOT)

    r = subprocess.run(xsi_runner_cmd(TOP, TB), cwd=str(xsi),
                       capture_output=True, text=True, errors="replace", timeout=2400)
    out = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in out, f"the RTL run did not complete cleanly:\n{out[-3000:]}"
    return _played(xsi)


@pytest.mark.xsi
class TestTheDesignPlaysAtRtl:
    """The properties the RTL run establishes."""

    def test_the_run_completed_and_the_converter_was_driven(self, played):
        assert played.size == WANT_TOTAL_SAMPLES, (
            f"the DAC pulled {played.size} samples, not {WANT_TOTAL_SAMPLES}. That is the run bound "
            f"times the converter's rate — if it moved, one of those did.")
        assert played.any(), "the converter played nothing at all"

    def test_the_start_now_window_is_bit_exact(self, played):
        """The first play, sample for sample, through the whole TX path.

        It covers the scheduler's private array, the ``TxCmd``, the in-band payload, the loader's
        tagging, the player's ``now`` handling and the converter's own unpack — one comparison for
        all of it. And it is the play whose *slot* nothing chose: ``start_now`` means the player
        assigned it, so this is also the evidence that the assignment reached the DAC.
        """
        lead_in = int(np.argmax(played != 0))
        assert lead_in == WANT_LEAD_IN, (
            f"the first sample reached the converter at slot {lead_in}, not {WANT_LEAD_IN} — the "
            f"design's startup latency changed")
        got = played[lead_in:lead_in + NSAMP]
        assert np.array_equal(got, waveform()), (
            f"the start_now window is not the waveform: {got[:8].tolist()} vs "
            f"{waveform()[:8].tolist()}")

    def test_the_converter_never_starves_after_the_startup_transient(self, played):
        """**The property this design exists for.**

        A DAC consumes a word every sample period whether or not one is ready. Two zero runs in
        15296 samples — the lead-in and the ``LEAD`` hole — means that after startup the scheduler,
        loader and player kept the converter fed for 15200 consecutive slots with no gap at all.
        A third run would be a steady-state underrun, which is the failure this whole arc is about.
        """
        runs = _zero_runs(played)
        assert len(runs) == WANT_ZERO_RUNS, (
            f"the converter starved {len(runs)} times, not {WANT_ZERO_RUNS}: {runs[:6]}. Runs after "
            f"the second are steady-state underruns.")
        assert runs[0][0] == 0 and runs[0][1] == WANT_LEAD_IN
        assert runs[1][1] <= NSAMP, (
            f"the startup hole is {runs[1][1]} slots — it must be under one play, because a "
            f"deferred response costs at most the periods LEAD covers")
        last = runs[1][0] + runs[1][1]
        assert played[last:].size == 15200
        assert played[last:].all(), "not one idle slot after the startup hole"


@pytest.mark.xsi
class TestTheRepeatPhaseIsNotYetBitExact:
    """**An open finding, asserted as the defect it is rather than tidied away.**

    pysim says the schedule holds exactly: every play lands at ``base + k*PERIOD`` and every block
    carries the waveform at the right phase. RTL says it very nearly does — the converter is fed
    continuously and the values are the waveform's — but the repeats after the startup hole are
    **not** at the phase the schedule names, and at least one sample is skipped.

    Measured: the ramp's differences over the steady region are ``{1, -63}`` for a clean tiling, and
    the run shows a **2** as well — one value stepped over. The repeats also resume at ``1008``
    rather than ``1000``.

    Two candidate causes, neither confirmed:

    * the **player's slot granularity** against a block-granular pysim twin — the plan declares this
      as fidelity limit 1, and it explains a phase *offset* but not a skipped sample;
    * the player's ``BEFORE`` path, which discards a stale sample **without advancing the slot**. If
      the loader and player disagree by one about which slot a window starts on, that path fires once
      per repeat and eats exactly one sample.

    This test pins the defect so it cannot regress unnoticed and so **fixing it is a visible
    change**. It does not assert the property is met, because it is not.
    """

    def test_the_steady_repeats_show_one_skipped_sample_per_wrap(self, played):
        runs = _zero_runs(played)
        steady = played[runs[1][0] + runs[1][1]:].astype(np.int64)
        diffs = set(np.diff(steady).tolist())
        assert diffs == {1, -63, 2}, (
            f"the steady-region differences are {sorted(diffs)}. A clean tiling of a ramp is "
            f"{{1, -63}}; the extra 2 is a skipped sample. If this is now {{1, -63}} the defect is "
            f"FIXED — delete this test and assert the phase in TestTheDesignPlaysAtRtl instead.")

    def test_the_repeats_do_not_resume_at_the_scheduled_phase(self, played):
        runs = _zero_runs(played)
        steady = played[runs[1][0] + runs[1][1]:]
        assert int(steady[0]) != int(waveform()[0]), (
            "the repeats now resume at the waveform's first sample, which is the scheduled phase — "
            "the defect this test records is fixed, so assert the phase properly and remove this")
