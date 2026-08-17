"""Every declared per-firing cost must equal what csynth measured.

**Three modules have now declared this constant and two of them were wrong**, both justified in
their docstrings by *symmetry* with a module that looked similar — "the same shape, so it costs the
same".  The reports refuted the premise in both cases.  So the guard is worth more than either
correction: a cost is **measured**, never inherited.

The calibration is ``cycles per firing = latency + 1`` — the FSM state count — and it is anchored
rather than assumed.  It is what makes :class:`~waveflow.hw.rf_samp_buf.RfSampBufIngress`'s declared
``2`` correct at a reported latency of 1, and that value is independently corroborated by RTL: at
256 MSa/s the RX design accepted 58.6% of the stream, which is ``0.5 / 0.853`` — exactly the ratio
``fire_cycles = 2`` predicts.  The ingress is therefore the **control** in this file: if its row ever
fails, the calibration is wrong and every other row is meaningless.

Why it is not bookkeeping.  ``fire_cycles`` feeds
:meth:`~waveflow.hw.rf_samp_buf_tx.RfSampBufTx.check_rate`'s threshold,
``samp_per_word * f_axis / fire_cycles``.  Declared too low it permits **more sample rate than the
hardware sustains** — a static check that says yes where the hardware says no, which is precisely
the failure that cost the RX side 1695 of 4096 samples.  The error is optimistic in both places it
lands: the threshold and the pysim charge.

Skips loudly when a report is absent, the way the XSI gates do: a silent skip on a gate like this
reads as a pass in a summary line.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.hw.rf_samp_buf import RfSampBufIngress
from waveflow.hw.rf_samp_buf_tx import RfSampBufLoader, RfSampBufPlayer
from waveflow.utils.csynthparse import loop_pipeline_ii, module_latency

_EX = Path(__file__).resolve().parents[2] / "examples"
RX_REPORT = _EX / "rf_samp_buf_rx" / "rf_samp_buf_rx_proj" / "solution1" / "syn" / "report"
TX_REPORT = _EX / "rf_samp_buf_tx" / "rf_samp_buf_tx_proj" / "solution1" / "syn" / "report"

#: ``(label, report dir, synthesized module, the class attribute that declares the cost)``.
#:
#: The module names carry their template arguments, so they name the **gated geometry** — one sample
#: per word — which is the configuration every declared cost is for.
BOUNDED_BODIES = [
    ("rx ingress", RX_REPORT, "rf_samp_buf_ingress_task_16_1_1024_16_s",
     RfSampBufIngress.fire_cycles),
    ("tx player", TX_REPORT, "rf_samp_buf_player_task_16_1_2048_16_s",
     RfSampBufPlayer.fire_cycles),
]


def _require(report_dir: Path, module: str):
    """The measured latency, or a loud skip — never a silent pass."""
    if not report_dir.is_dir():
        pytest.skip(f"no csynth report dir at {report_dir} — run the example's build --through csynth")
    got = module_latency(report_dir, module)
    if got is None and not (report_dir / f"{module}_csynth.xml").is_file():
        pytest.skip(f"no report for {module} in {report_dir} — re-run csynth")
    return got


@pytest.mark.parametrize("label,report_dir,module,declared",
                         BOUNDED_BODIES, ids=[b[0] for b in BOUNDED_BODIES])
def test_the_declared_firing_cost_is_the_one_csynth_measured(label, report_dir, module, declared):
    """``fire_cycles == latency + 1``, read out of the module's own ``_csynth.xml``.

    A single-firing ``hls::task`` body has a bounded latency, and the state count is what a firing
    costs.  If this fails the declaration is a guess again — correct the constant from the report,
    never the other way round.
    """
    got = _require(report_dir, module)
    assert got is not None, (
        f"{label}: csynth could not bound {module} (latency 'undef'), so it has no cycles-per-firing "
        f"to declare — but {declared} is declared for it. See the loader test below for what to do "
        f"instead.")
    measured = int(got["latency_max"]) + 1
    assert declared == measured, (
        f"{label}: declares fire_cycles={declared}, but {module} reports latency "
        f"{got['latency_max']} -> {measured} FSM states. Correct the DECLARATION from the report; "
        f"raising the threshold back to keep a test green re-introduces the defect the constant "
        f"exists to prevent.")


def test_the_calibration_is_anchored_on_the_rx_ingress():
    """The control.  If this row moves, ``latency + 1`` is wrong and every other row is meaningless.

    Stated separately from the parametrised case above so that a failure says *which* thing broke:
    a wrong calibration and a wrong declaration are different repairs.
    """
    got = _require(RX_REPORT, "rf_samp_buf_ingress_task_16_1_1024_16_s")
    assert got is not None and got["latency_max"] == 1, (
        f"the RX ingress no longer reports latency 1 ({got}); the calibration this whole file rests "
        f"on was derived from that value together with its RTL corroboration (58.6% accepted at "
        f"256 MSa/s == 0.5/0.853), so both need re-deriving before any other row is trusted")
    assert RfSampBufIngress.fire_cycles == 2


def test_the_loader_declares_no_per_firing_cost_because_csynth_cannot_bound_it():
    """A body Vitis cannot bound must not carry a cycles-per-firing constant at all.

    The loader's firing is one whole command and its outer loop's trip count is data-dependent, so
    the report says ``undef``.  Any single number there is a fiction whose *direction* of error is
    unknown — which is worse than no number, because a rate contract built on it looks sourced.

    What it declares instead is a per-**word** cost, and that one is measurable: the payload loop is
    pipelined, so its achieved II is a real cycles-per-iteration figure.
    """
    assert not hasattr(RfSampBufLoader, "fire_cycles"), (
        "RfSampBufLoader declares fire_cycles again. Its overall latency is 'undef' — there is no "
        "per-firing cost to declare. Use a per-word cost sourced from the payload loop's II.")

    if not TX_REPORT.is_dir():
        pytest.skip(f"no csynth report dir at {TX_REPORT} — run rf_samp_buf_tx_build.py --through csynth")
    whole = module_latency(TX_REPORT, "rf_samp_buf_loader_task_16_1_2048_16_16_s")
    assert whole is None, (
        f"csynth can now bound the loader body ({whole}). That is a real change — the payload loop "
        f"must have acquired a fixed trip count — and it means a per-firing cost is declarable "
        f"again. Re-derive rather than assume.")


def test_the_loaders_per_word_cost_is_the_payload_loops_achieved_ii():
    """``word_cycles`` comes from ``PipelineII`` on the payload loop — the **achieved** II.

    Achieved, not target: Vitis reports both, and here they differ (2 achieved against a target of
    1).  A per-word cost taken from the target would be a wish, and it would be optimistic by a
    factor of two in the direction that hides starvation.
    """
    if not TX_REPORT.is_dir():
        pytest.skip(f"no csynth report dir at {TX_REPORT} — run rf_samp_buf_tx_build.py --through csynth")
    mod = "rf_samp_buf_loader_task_16_1_2048_16_16_Pipeline_VITIS_LOOP_99_2"
    if not (TX_REPORT / f"{mod}_csynth.xml").is_file():
        pytest.skip(f"no report for {mod} — re-run csynth")
    ii = loop_pipeline_ii(TX_REPORT, mod, "VITIS_LOOP_99_2")
    assert ii is not None, f"{mod} reports no PipelineII for VITIS_LOOP_99_2"
    assert RfSampBufLoader.word_cycles == ii, (
        f"the loader declares word_cycles={RfSampBufLoader.word_cycles} but the payload loop's "
        f"achieved II is {ii}. Correct the declaration from the report.")


def test_the_rate_ceiling_follows_the_measured_cost():
    """The consequence, asserted so a silent correction cannot pass unnoticed.

    ``fire_cycles = 3`` puts the player's ceiling at 100 MSa/s per sample-per-word on a 300 MHz
    fabric.  The declaration that stood until 2026-08-17 said 2, which permitted 150 — **50% more
    than the hardware sustains**.
    """
    from waveflow.hw.rf_samp_buf_tx import RfSampBufTx
    from waveflow.simulation.simulation import Simulation

    for spw in (1, 2, 4):
        dut = RfSampBufTx(name=f"ceil{spw}", sim=Simulation(), bitwidth=16 * spw,
                          samp_per_word=spw)
        assert dut.max_samp_rate(300e6) == 300e6 * spw / 3
    assert RfSampBufTx(name="c1", sim=Simulation(), bitwidth=16,
                       samp_per_word=1).max_samp_rate(300e6) == 100e6
