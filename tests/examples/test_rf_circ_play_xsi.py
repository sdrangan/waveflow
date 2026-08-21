"""``rf_repeat_play`` at RTL — the whole transmitter, scheduler included, through real Verilog.

Stage 1's first pass could not reach this at all: the repeat scheduler is *reactive*, and the XSI
testbench is file-driven. Moving the scheduler into the DUT removed that blocker — the testbench now
pushes ``NSAMP`` words once, from the ``AxisMaster`` that already exists — and this file is what that
bought.

**The phase is exact, and that is the point of the whole example.**  The platform's headline use is
channel sounding, where the exactly-known repeat phase *is* the measurement — a drifting phase is a
drifting delay estimate, so "approximately periodic" would not be usable for the thing this exists to
do.  So the strong assertion is the one made: from the first scheduled play onward the stream is the
waveform **tiled bit-exactly**, 15144 consecutive samples, with no gap and no skipped sample.

An earlier revision of this file pinned two symptoms as an open defect — repeats resuming at 1008 and
a ``+2`` in the difference histogram. Both are gone, and their cause is recorded in
``rf_circ_play_task.h``: the HLS body started its train at ``k = 1`` where the Python twin started at
``k = LEAD``. The twins were running different schedules.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.rf_repeat_play.rf_repeat_play import (
    LEAD,
    NSAMP,
    SAMP_BASE,
    SAMP_BW,
    waveform,
)
from examples.rf_repeat_play.rf_repeat_play_build import TOP, generate_tb
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd

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
#: that a deferred response makes unavoidable.  After those, the converter is fed for 15144
#: consecutive samples with no gap at all.
WANT_ZERO_RUNS = 2

#: Length of the startup hole, in slots.  **Exactly one PERIOD**, and that number is derived rather
#: than measured-and-accepted: the train starts at ``k = LEAD``, so the plays at ``k = 1 .. LEAD-1``
#: are never issued, and at ``LEAD = 2`` that is one skipped play.
#:
#: It read **8** before the ``k = LEAD`` fix, which is what a *partially* discarded window looks
#: like — the leading samples of an over-due play taking the player's ``BEFORE`` path — and is the
#: symptom that made a scheduling bug read as a phase error.
WANT_HOLE = 64

#: Where the scheduled train begins, and the length that is then fed without interruption.
WANT_TRAIN_START = 152
WANT_TRAIN_LEN = 15144


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
    # SECOND INSTANCE OF THIS CLASS: `*_proj/` is gitignored build output, and a gate that
    # compares a cycle count against RTL it did not produce reports "a real behaviour change"
    # when the truth is a stale artifact. See rtl_staleness().
    _require(rtl_staleness(ROOT, 'rf_repeat_play') is None, rtl_staleness(ROOT, 'rf_repeat_play') or "")

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
        assert runs[1][1] == WANT_HOLE, (
            f"the startup hole is {runs[1][1]} slots, not {WANT_HOLE}. Exactly one PERIOD is what "
            f"starting the train at k = LEAD costs; anything SHORTER means a window was partly "
            f"discarded rather than never issued, which is what the k = 1 defect looked like.")
        last = runs[1][0] + runs[1][1]
        assert last == WANT_TRAIN_START
        assert played[last:].size == WANT_TRAIN_LEN
        assert played[last:].all(), "not one idle slot after the startup hole"


@pytest.mark.xsi
class TestTheRepeatPhaseIsExact:
    """**The property the platform's headline use depends on.**

    Channel sounding measures delay by the phase of a known repeating waveform. A phase that drifts —
    or that is merely *approximately* periodic — is a delay estimate that drifts, so "the values are
    right and it never starves" is not enough. What has to hold is that play *k* starts at exactly
    ``base + k*PERIOD``, for every *k*, with no sample lost anywhere in between.

    Asserted three ways, because each catches something the others do not: the **spacing** of the
    play starts (a scheduling error), the **difference histogram** (a lost sample anywhere), and a
    **bit-exact tiling** (both, plus any value corruption).
    """

    def test_the_play_starts_are_exactly_one_period_apart(self, played):
        """Read off the data rather than from a counter: the waveform is a ramp from ``SAMP_BASE``,
        so every occurrence of that value **is** a play start, and their spacing is the schedule."""
        starts = np.nonzero(played == SAMP_BASE)[0]
        assert starts.size > 200, f"only {starts.size} play starts in the run"
        assert starts[0] == WANT_LEAD_IN
        assert starts[1] - starts[0] == LEAD * NSAMP, (
            f"the train begins {starts[1] - starts[0]} slots after the start_now window, not "
            f"{LEAD * NSAMP}. The train starts at k = LEAD, so the first scheduled play is LEAD "
            f"periods out — see the k = LEAD note in rf_circ_play_task.h.")
        spacings = set(np.diff(starts[1:]).tolist())
        assert spacings == {NSAMP}, (
            f"consecutive plays are {sorted(spacings)} slots apart, not exactly {NSAMP}. Any value "
            f"but one means the phase moves, and a moving phase is a moving delay estimate.")

    def test_not_one_sample_is_lost_in_the_whole_steady_run(self, played):
        """The difference histogram of a ramp tiled at period ``NSAMP`` is exactly ``{+1, -(NSAMP-1)}``.

        A ``+2`` is a skipped sample — which is how the ``k = 1`` defect showed itself, a single one
        left over from the leading samples of an over-due window. Nothing else can produce one.
        """
        train = played[WANT_TRAIN_START:].astype(np.int64)
        diffs = set(np.diff(train).tolist())
        assert diffs == {1, -(NSAMP - 1)}, (
            f"the steady-region differences are {sorted(diffs)}, not {{1, {-(NSAMP - 1)}}}. "
            f"Anything else is a lost or repeated sample.")

    def test_the_train_is_the_waveform_tiled_bit_exactly(self, played):
        """The whole claim in one comparison, at the far side of the converter.

        15144 consecutive samples, every one equal to ``waveform()[i % NSAMP]``. It covers the
        scheduler's private array, the command path, the in-band payload, the loader's tagging, the
        player's three-way compare and the converter's own unpack — and it is the assertion that
        would fail first if any of them slipped by a single slot.
        """
        train = played[WANT_TRAIN_START:]
        assert train.size == WANT_TRAIN_LEN, (
            f"{train.size} samples after the train start, not {WANT_TRAIN_LEN}")
        want = waveform()[np.arange(train.size) % NSAMP]
        bad = np.nonzero(train != want)[0]
        assert bad.size == 0, (
            f"{bad.size} of {train.size} samples differ from the tiled waveform; first at "
            f"index {WANT_TRAIN_START + int(bad[0])}: got {int(train[bad[0]])}, "
            f"want {int(want[bad[0]])}")


@pytest.mark.xsi
class TestWhatTheCountersDoAndDoNotSee:
    """**A declared limit, recorded because the defect above exposed it.**

    While the ``k = 1`` bug was live, play 1 lost **nine** of its 64 samples — eight never emitted,
    one skipped — and the design's own ``n_late`` would still have read **zero**. That is not a
    counter lying. A ``TxStatus`` is emitted only for the sample carrying ``request_status``, which
    the loader sets on the **last** sample of a window, so the verdict answers *"did the last sample
    make it?"* and not *"did the whole window?"*. The last sample of play 1 was on time.

    ``plans/rf_samp_new.md`` states this ("the RTL verdict already answers 'did the last sample
    make it?' — not 'did the whole frame?'"), and the consequence is worth pinning: **partial window
    loss is invisible to ``n_late`` in both backends.** What catches it is the playout itself, which
    is why the assertions above are made against the samples and not against a counter.
    """

    def test_the_playout_is_the_evidence_not_the_counters(self, played):
        """A structural check, not a measurement: the gate must not rest on a counter that cannot
        see the failure it is supposed to catch."""
        src = (Path(__file__).resolve().parents[2]
               / "waveflow" / "build" / "rf_tx_loader_task.h").read_text(encoding="utf-8")
        assert "t.request_status = (ap_uint<1>)(k == (ap_uint<IDX_W>)(c.nsamp - 1));" in src, (
            "the loader no longer marks exactly the LAST sample of a window. If marking changed, "
            "the scope of the TxResp verdict changed with it, and this file's reliance on the "
            "played samples rather than on n_late needs re-deriving.")
        assert played[WANT_TRAIN_START:].all(), (
            "the played stream is the only thing that sees a partially-lost window")
