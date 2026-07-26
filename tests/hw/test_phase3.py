"""Phase 3 integration tests.

- HwParam detection on PolyAccel.
- End-to-end poly simulation still produces correct results post-VitisRegMap
  migration (kernel runs from ``on_start`` rather than ``run_proc``; halt
  status now lives in the regmap rather than a streamed response footer).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import numpy.testing as npt
import pytest

POLY_DIR = Path(__file__).resolve().parents[2] / "examples" / "stream_inband"
if str(POLY_DIR) not in sys.path:
    sys.path.insert(0, str(POLY_DIR))

from poly import (
    Float32, PolyAccel, PolyCmdHdr, PolyCmdType, PolyError, PolyTB, connect,
)

from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import HwModule, HwParam, SynthContext
from waveflow.hw.interface import StreamDrainStmt
from waveflow.simulation.simulation import Simulation


# ---------------------------------------------------------------------------
# HwParam detection on PolyAccel
# ---------------------------------------------------------------------------

def test_poly_accel_hwparam_fields():
    sim = Simulation()
    comp = PolyAccel(name='p', sim=sim)
    ctx = SynthContext.from_component(comp)
    assert 'in_bw' in ctx.params
    assert 'out_bw' in ctx.params
    assert ctx.params['in_bw'] == 'IN_BW'
    assert ctx.params['out_bw'] == 'OUT_BW'


# ---------------------------------------------------------------------------
# End-to-end simulation correctness
# ---------------------------------------------------------------------------

def _run_sim(nsamp: int = 100):
    coeffs = np.array([1.0, -2.0, -3.0, 4.0], dtype=np.float32)

    cmd_hdr = PolyCmdHdr()
    cmd_hdr.cmd_type = PolyCmdType.DATA
    cmd_hdr.tx_id = 42
    cmd_hdr.nsamp = nsamp

    samp_in = np.linspace(0.0, 1.0, nsamp, dtype=np.float32)

    sim = Simulation()
    clk = Clock(freq=1e9)

    accel = PolyAccel(name='poly_accel', sim=sim)
    tb    = PolyTB(name='poly_tb', sim=sim,
                   cmd_hdr=cmd_hdr, samp_in=samp_in, coeffs=coeffs)

    connect(sim, tb, accel, clk)
    sim.run_sim()
    return tb, cmd_hdr, samp_in, coeffs, sim


def test_sim_tx_id_echoed():
    tb, _, _, _, _ = _run_sim()
    assert int(tb.resp_hdr.tx_id) == 42


def test_sim_samp_out_len_matches_nsamp():
    tb, _, _, _, _ = _run_sim(nsamp=50)
    assert tb.samp_out.size == 50


def test_sim_no_error():
    tb, _, _, _, _ = _run_sim()
    assert tb.halted == 0
    assert tb.error is PolyError.NO_ERROR


def _poly_reference(coeffs: np.ndarray, samp_in: np.ndarray) -> np.ndarray:
    y = np.zeros_like(samp_in)
    power = np.ones_like(samp_in)
    for c in coeffs:
        y += c * power
        power *= samp_in
    return y


def test_sim_output_matches_reference():
    tb, _, samp_in, coeffs, _ = _run_sim(nsamp=100)
    expected_samp = _poly_reference(coeffs, samp_in)
    npt.assert_allclose(np.asarray(tb.samp_out, dtype=np.float32), expected_samp, rtol=1e-5)


def test_sim_different_nsamp():
    tb, _, samp_in, coeffs, _ = _run_sim(nsamp=256)
    assert tb.samp_out.size == 256
    assert tb.halted == 0
    assert tb.error is PolyError.NO_ERROR
    expected_samp = _poly_reference(coeffs, samp_in)
    npt.assert_allclose(np.asarray(tb.samp_out, dtype=np.float32), expected_samp, rtol=1e-5)


def test_sim_timing_is_nonzero():
    """Verify that proc_latency delays the output (sim time advances during run)."""
    _, _, _, _, sim = _run_sim()
    assert sim.env.now > 0


# ---------------------------------------------------------------------------
# StreamDrainStmt is importable and is a SynthCallStmt
# ---------------------------------------------------------------------------

def test_stream_drain_stmt_is_synth_call_stmt():
    from waveflow.hw.hwstmt import SynthCallStmt
    assert issubclass(StreamDrainStmt, SynthCallStmt)


# ---------------------------------------------------------------------------
# @synthesizable on stream methods
# ---------------------------------------------------------------------------

def test_stream_get_is_synthesizable():
    sim = Simulation()
    ep = PolyAccel(name='x', sim=sim).s_in
    assert getattr(ep.get, '_is_synthesizable', False) is True


def test_stream_write_is_synthesizable():
    sim = Simulation()
    ep = PolyAccel(name='y', sim=sim).m_out
    assert getattr(ep.write, '_is_synthesizable', False) is True


def test_stream_drain_is_synthesizable():
    sim = Simulation()
    ep = PolyAccel(name='z', sim=sim).s_in
    assert getattr(ep.drain, '_is_synthesizable', False) is True
