"""tests/examples/test_mem_copy_calibration.py — the calibration loop closes.

The end-to-end proof, toolchain-free: given the writer's MEASURED RTL firing spans (183 cycles at
n_words=128, 615 at n_words=512 — see plans/memcpy_timing_calibration.md), calibrate the writer on
UNCONTENDED firings and check that a re-run of pysim reproduces the RTL *period* at both sizes.

Two things make this a real test and not a tautology:

* The residual is fit on uncontended firings only; the contention (~30 cycles/job at n=128) then
  *emerges* from the depth-2 `copy_data` channel rather than being fitted — so the calibrated period
  matching RTL is a prediction, not an identity.
* It depends on the FIFO being bounded (the depth change).  With an unbounded `copy_data`, the
  writer's trailing delay is absorbed into idle-wait and the period would NOT move — which is exactly
  what an earlier iteration showed before depth-2 became the default.

The synthetic part is only the RTL *numbers* (measured, and gated for real by the -m xsi run and the
scratchpad sweep that produced 0.0% error).  The pysim side — collection, fit, the bounded channel,
the injected delay — is all real.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from examples.mem_copy.mem_copy import CopyJob, xsi_jobs
from examples.mem_copy.mem_copy_sim import MemCopySim
from waveflow.calib.timing_model import StreamTimingModel
from waveflow.hw.clock import Clock

COMPONENT = "mem_w_stream_framed_done_task"
#: The writer's measured RTL firing span (cycles), per n_words — the ap_done-anchored period.
RTL_SPAN = {128: 183.0, 512: 615.0}


def _rtl_events(n_words, span):
    """A minimal ExtractBurstsStep-shaped events dict: one writer firing at this size (blocked==0)."""
    return {"top": "mem_copy", "max_burst_len": 16,
            "firings": [{"component": COMPONENT, "index": 0, "nwords": n_words,
                         "num_trans": math.ceil(n_words / 16), "span": span, "blocked": 0}]}


def _pysim_period(dut):
    """The writer's steady-state firing cadence in cycles = median diff of ap_done-equivalent ends."""
    period = 1.0 / dut.wstream.clk.freq
    ends = np.asarray([r["end"] for r in dut.wstream.firing_records]) / period
    d = np.diff(ends)
    return float(np.median(d[len(d) // 2:]))


def test_calibrated_pysim_reproduces_the_rtl_period(tmp_path):
    calib = str(tmp_path / "wcalib")
    tm = StreamTimingModel(component=COMPONENT, calib_dir=calib, clk=Clock(freq=100e6))

    # --- collect: RTL spans (measured) + pysim firings (real runs), at two sizes ---
    for nw in (128, 512):
        k = 16 if nw == 128 else 4
        tm.collect_rtl(_rtl_events(nw, RTL_SPAN[nw]), run_id=f"n{nw}")
        dut = MemCopySim(jobs=xsi_jobs(nw, k), calib_dir=calib).run()   # writer records firings
        tm.collect_pysim(dut.wstream.firing_records, run_id=f"n{nw}")

    # --- fit the residual on the uncontended firings ---
    tm.fit()
    assert len(tm.coverage["matched"]) == 2, "both sizes must join for the slope"

    # --- validate: re-run pysim with the fitted model; the period must match RTL ---
    for nw in (128, 512):
        k = 16 if nw == 128 else 4
        dut = MemCopySim(jobs=xsi_jobs(nw, k), calib_dir=calib).run()
        got = _pysim_period(dut)
        assert got == pytest.approx(RTL_SPAN[nw], rel=0.03), (
            f"n={nw}: calibrated pysim period {got:.1f} != RTL {RTL_SPAN[nw]} — the fitted delay did "
            f"not reproduce the contended period (is copy_data still unbounded?)")


def test_without_calibration_the_period_is_short(tmp_path):
    """The gap the calibration closes: uncalibrated pysim runs optimistic (the writer's firing
    cadence is well under the RTL 183), so the fix is real, not a no-op."""
    dut = MemCopySim(jobs=xsi_jobs(128, 16), calib_dir=str(tmp_path / "c")).run()  # unfitted seed=0
    assert _pysim_period(dut) < RTL_SPAN[128] - 20   # ~147 measured, comfortably short of 183


def _writer_span(dut):
    period = 1.0 / dut.wstream.clk.freq
    return float(np.median([r["span"] for r in dut.wstream.firing_records]) * dut.wstream.clk.freq)


def test_platform_bus_model_takes_the_burst_term_off_the_component(tmp_path):
    """The two-level split, realized: a PLATFORM bus model charges the m_axi burst cost, so the
    component's residual shrinks to its own control cost.

    Bus law = nwords + 2·(num_trans-1) cycles (the measured burst gaps).  Configuring it on the
    memory grows the writer's pysim span by exactly the burst term (2 cyc × 7 gaps = 14 at n=128),
    so the RTL-vs-pysim residual the *component* must carry drops by that 14 — from ~36 (bus+control
    lumped) to ~22 (control only).  The bus term now lives on the shared platform model, calibrated
    once and reused by every accelerator; only the 22 is per-accelerator."""
    from waveflow.calib.bus_model import BusCalib

    platform = tmp_path / "platform"
    BusCalib(platform_dir=platform, clk_freq=100e6).fit(
        None, [{"num_trans": nt, "nwords": nw, "span": nw + 2 * (nt - 1)}
               for nw, nt in [(128, 8), (512, 32)]])

    no_bus = _writer_span(MemCopySim(jobs=xsi_jobs(128, 16),
                                     calib_dir=str(tmp_path / "a")).run())
    with_bus = _writer_span(MemCopySim(jobs=xsi_jobs(128, 16), calib_dir=str(tmp_path / "b"),
                                       platform_dir=str(platform)).run())

    burst_term = 2 * (8 - 1)                       # 7 gaps × 2 cycles
    assert with_bus - no_bus == pytest.approx(burst_term, abs=1.0)
    # the component residual shrinks by exactly the burst term the platform now owns
    assert (RTL_SPAN[128] - no_bus) - (RTL_SPAN[128] - with_bus) == pytest.approx(burst_term, abs=1.0)


def test_platform_bus_model_preserves_correctness(tmp_path):
    """Charging the calibrated bus span must not corrupt the data (it regressed once: the spanned
    write path defaulted word_bw=32 and truncated 64-bit words)."""
    from waveflow.calib.bus_model import BusCalib

    platform = tmp_path / "platform"
    BusCalib(platform_dir=platform, clk_freq=100e6).fit(
        None, [{"num_trans": 8, "nwords": 128, "span": 142}])
    # MemCopySim.run() asserts every copy is bit-exact; a truncation would raise here.
    MemCopySim(jobs=xsi_jobs(128, 4), calib_dir=str(tmp_path / "c"),
               platform_dir=str(platform)).run()
