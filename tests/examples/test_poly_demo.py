import csv
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from examples.stream_inband.poly import (
    Float32,
    PolyAccel,
    PolyCmdHdr,
    PolyCmdType,
    PolyError,
    PolyRespHdr,
    PolySimResult,
    PolyTB,
    connect,
)
from examples.stream_inband.poly_build import build_poly_dag
from waveflow.build.build import BuildConfig
from waveflow.hw.clock import Clock
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain


_CLK_FREQ = 100e6


def _read_event_times(log_file: Path) -> dict[str, float]:
    """Return first occurrence of each event name → simulation time (seconds)."""
    events: dict[str, float] = {}
    with open(log_file, newline='') as f:
        for row in csv.DictReader(f):
            ev = row['event']
            if ev not in events:
                events[ev] = float(row['time'])
    return events


POLY_EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "stream_inband"


def _copy_poly_vitis_resources(dst_dir: Path) -> None:
    """Copy the persistent (non-generated) poly Vitis sources into a temp dir.

    Phase 14 of the codegen pipeline emits ``gen/poly.{hpp,cpp}`` and
    ``gen/poly_tb.cpp`` from Python sources, so only the TCL driver and
    the sticky ``poly_evaluate_impl.tpp`` implementation file need to be
    seeded — the build DAG generates the rest.
    """
    for name in ("run.tcl", "poly_evaluate_impl.tpp"):
        shutil.copy(POLY_EXAMPLE_DIR / name, dst_dir / name)


def test_poly_simulate_matches_expected_outputs(tmp_path: Path) -> None:
    results = build_poly_dag().run(
        BuildConfig(root_dir=tmp_path, params={'nsamp': 100}),
        through='extract_py_timing',
    )

    assert results['build_inputs'].success
    assert results['py_sim'].success

    sim_result = PolySimResult.from_paths(
        cmd_hdr_path=results['build_inputs'].path('data_cmd_hdr'),
        samp_in_path=results['build_inputs'].path('samp_in'),
        resp_dir=results['py_sim'].path('sim_dir'),
    )

    assert sim_result.cmd_hdr is not None
    assert sim_result.samp_in is not None
    assert sim_result.resp_hdr is not None
    assert sim_result.samp_out is not None
    assert sim_result.passed is True
    assert sim_result.halted == 0
    assert sim_result.error is PolyError.NO_ERROR
    assert sim_result.samp_out.dtype == np.float32


def test_poly_timing_bandwidth_and_unroll(tmp_path: Path) -> None:
    """Timing scales with unroll_factor and interface bitwidth.

    Three configurations at nsamp=100, proc_latency=10, clk=1 GHz:

    Case 1 — uf=1, bw=32: each sample is one 32-bit word; bandwidth-limited
      at 1 sample/cycle.  Expected duration ≈ (nsamp + proc_latency) cycles.

    Case 2 — uf=2, bw=32: unroll=2 but the 32-bit interface only carries one
      Float32 per clock, so still bandwidth-limited at 1 sample/cycle.
      Duration should match case 1.

    Case 3 — uf=2, bw=64: two Float32 samples packed per 64-bit word; both
      compute and bandwidth scale with unroll_factor.
      Expected duration ≈ (nsamp/uf + proc_latency) cycles ≈ half of case 1.
    """
    nsamp = 100
    proc_latency = 40  # matches PolyAccel default (cosim-calibrated)
    period = 1.0 / _CLK_FREQ

    configs = [
        dict(in_bw=32, out_bw=32, unroll_factor=1),
        dict(in_bw=32, out_bw=32, unroll_factor=2),
        dict(in_bw=64, out_bw=64, unroll_factor=2),
    ]

    durations: list[float] = []
    for i, cfg in enumerate(configs):
        run_dir = tmp_path / f"run_{i}"
        run_dir.mkdir()
        results = build_poly_dag().run(
            BuildConfig(root_dir=run_dir, params={
                'clk_freq': _CLK_FREQ,
                'nsamp': nsamp,
                'in_bw': cfg['in_bw'],
                'out_bw': cfg['out_bw'],
                'unroll_factor': cfg['unroll_factor'],
            }),
            through='extract_py_timing',
        )
        sim_result = PolySimResult.from_paths(
            cmd_hdr_path=results['build_inputs'].path('data_cmd_hdr'),
            samp_in_path=results['build_inputs'].path('samp_in'),
            resp_dir=results['py_sim'].path('sim_dir'),
        )
        assert sim_result.passed, f"Simulation failed for config {cfg}"
        log_path = results['py_sim'].path('log')
        events = _read_event_times(log_path)
        durations.append(events['samp_out_write_end'] - events['samp_read_begin'])

    dur_uf1_bw32, dur_uf2_bw32, dur_uf2_bw64 = durations
    tol = 5 * period  # ±5 clock cycles to allow for timing discretisation

    # Cases 1 and 2: bandwidth-limited — timing should be identical
    expected_bw_limited = (nsamp + proc_latency) * period
    assert abs(dur_uf1_bw32 - expected_bw_limited) < tol, (
        f"Case 1 (uf=1, bw=32): expected ~{expected_bw_limited*1e9:.0f} ns, "
        f"got {dur_uf1_bw32*1e9:.1f} ns"
    )
    assert abs(dur_uf2_bw32 - expected_bw_limited) < tol, (
        f"Case 2 (uf=2, bw=32): expected ~{expected_bw_limited*1e9:.0f} ns (BW-limited), "
        f"got {dur_uf2_bw32*1e9:.1f} ns"
    )
    assert abs(dur_uf2_bw32 - dur_uf1_bw32) < tol, (
        f"Cases 1 and 2 should match (both BW-limited): "
        f"{dur_uf1_bw32*1e9:.1f} ns vs {dur_uf2_bw32*1e9:.1f} ns"
    )

    # Case 3: compute + BW not limited — duration halved
    expected_bw64 = (nsamp // 2 + proc_latency) * period
    assert abs(dur_uf2_bw64 - expected_bw64) < tol, (
        f"Case 3 (uf=2, bw=64): expected ~{expected_bw64*1e9:.0f} ns, "
        f"got {dur_uf2_bw64*1e9:.1f} ns"
    )
    assert dur_uf2_bw64 < dur_uf1_bw32 * 0.75, (
        f"Case 3 should be significantly faster than case 1: "
        f"{dur_uf2_bw64*1e9:.1f} ns vs {dur_uf1_bw32*1e9:.1f} ns"
    )


class _FailingPolyAccel(PolyAccel):
    """Accelerator variant whose `evaluate()` always returns WRONG_NSAMP.

    Drives the halt-on-error path through `on_start` end-to-end: the regmap
    latches halted/error/tx_id, the slave clears _busy, and the host reads
    the status back over AXI-Lite. The real evaluate() can't naturally
    return WRONG_NSAMP under SimPy (get_pipelined zero-pads up to ``count``),
    so we inject the failure here while preserving the real stream contract
    (resp_hdr + samp_out written before returning).
    """

    def evaluate(self, cmd_hdr, s_in, m_out, coeffs):  # type: ignore[override]
        from waveflow.hw.arrayutils import array
        del coeffs  # injected for signature parity; this stub forces an error path.
        resp_hdr = PolyRespHdr()
        resp_hdr.tx_id = cmd_hdr.tx_id
        yield from m_out.write(resp_hdr)
        _samp_in, _tstart = yield from s_in.get_pipelined(Float32, count=cmd_hdr.nsamp)
        zero_out = np.zeros(int(cmd_hdr.nsamp), dtype=np.float32)
        yield from m_out.write(array(Float32, zero_out))
        return PolyError.WRONG_NSAMP


def test_poly_halts_on_error(tmp_path: Path) -> None:
    """Kernel latches halted/error/tx_id and returns when evaluate fails."""
    nsamp = 100
    tx_id = 17

    cmd_hdr = PolyCmdHdr()
    cmd_hdr.cmd_type = PolyCmdType.DATA
    cmd_hdr.tx_id    = tx_id
    cmd_hdr.nsamp    = nsamp

    samp_in = np.linspace(0.0, 1.0, nsamp, dtype=np.float32)
    coeffs  = np.array([1.0, -2.0, -3.0, 4.0], dtype=np.float32)

    sim = Simulation()
    clk = Clock(freq=_CLK_FREQ)
    accel = _FailingPolyAccel(name="poly_accel", sim=sim, clk=clk)
    tb = PolyTB(name="poly_tb", sim=sim,
                cmd_hdr=cmd_hdr, samp_in=samp_in, coeffs=coeffs)
    connect(sim, tb, accel, clk)
    sim.run_sim()

    assert tb.halted == 1, f"expected halted=1, got {tb.halted}"
    assert tb.error == PolyError.WRONG_NSAMP, (
        f"expected WRONG_NSAMP, got {tb.error!r}"
    )
    assert tb.tx_id_status == tx_id, (
        f"expected tx_id={tx_id}, got {tb.tx_id_status}"
    )

    sim_result = PolySimResult(
        cmd_hdr=cmd_hdr,
        samp_in=samp_in,
        resp_hdr=tb.resp_hdr,
        samp_out=tb.samp_out,
        halted=tb.halted,
        error=tb.error,
        tx_id=tb.tx_id_status,
    )
    assert sim_result.passed is False


@pytest.mark.vitis
def test_poly_vitis_cosim_matches_python_model(tmp_path: Path) -> None:
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found; skipping poly Vitis co-sim regression.")

    _copy_poly_vitis_resources(tmp_path)

    results = build_poly_dag().run(
        BuildConfig(root_dir=tmp_path, params={'nsamp': 100})
    )

    csim_result = results.get('csim')
    if csim_result is None or not csim_result.success:
        msg = csim_result.message if csim_result else "csim did not run"
        pytest.skip(f"Vitis execution unavailable in current setup: {msg}")

    assert results['validate_csim'].success, results['validate_csim'].message

    cmd_hdr_path = results['build_inputs'].path('data_cmd_hdr')
    samp_in_path = results['build_inputs'].path('samp_in')
    sim_result = PolySimResult.from_paths(
        cmd_hdr_path=cmd_hdr_path, samp_in_path=samp_in_path,
        resp_dir=results['py_sim'].path('sim_dir'),
    )
    vitis_result = PolySimResult.from_paths(
        cmd_hdr_path=cmd_hdr_path, samp_in_path=samp_in_path,
        resp_dir=results['validate_csim'].path('vitis_dir'),
    )
    # Vitis 2025.1 writes poly_cosim.rpt; older versions wrote cosim.log
    sim_report_dir = tmp_path / "waveflow_poly_proj" / "solution1" / "sim" / "report"
    cosim_reports = list(sim_report_dir.glob("*cosim*")) if sim_report_dir.exists() else []

    assert vitis_result.passed is True
    assert vitis_result.halted == 0
    assert vitis_result.error is PolyError.NO_ERROR
    assert np.allclose(vitis_result.samp_out, sim_result.samp_out[:vitis_result.samp_out.size], rtol=1e-6, atol=1e-6)
    assert cosim_reports, f"No cosim report found in {sim_report_dir}"


@pytest.mark.vitis
def test_poly_vitis_cosim_timing_within_tolerance(tmp_path: Path) -> None:
    """End-to-end success gate for the build-DAG-reorg plan.

    Runs the full pipeline through ``validate_timing`` and asserts the
    new ValidateTimingStep's verdict reports ``pass=true``.  Both
    structured timing artifacts (``py_timing.json`` /
    ``cosim_timing.json``) must be present so a future model-training
    workflow can consume them.
    """
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found; skipping poly Vitis cosim timing regression.")

    _copy_poly_vitis_resources(tmp_path)

    results = build_poly_dag().run(
        BuildConfig(root_dir=tmp_path, params={'nsamp': 100}),
        through='validate_timing',
    )

    csim_result = results.get('csim')
    if csim_result is None or not csim_result.success:
        msg = csim_result.message if csim_result else "csim did not run"
        pytest.skip(f"Vitis execution unavailable in current setup: {msg}")

    assert results['validate_timing'].success, results['validate_timing'].message
    verdict_path = results['validate_timing'].path('timing_verdict')
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert verdict['pass'] is True, (
        f"Timing verdict failed: py={verdict['py_cycles']} cycles, "
        f"cosim={verdict['cosim_cycles']} cycles, delta={verdict['delta']}, "
        f"tolerance={verdict['tolerance']}."
    )
    assert verdict['delta'] <= 20, verdict
    # Structured fields must round-trip — see [[project-cycle-model-training]].
    assert 'py_cycles' in verdict and 'cosim_cycles' in verdict
    py_timing = json.loads(
        results['extract_py_timing'].path('py_timing').read_text(encoding="utf-8")
    )
    assert py_timing['source'] == 'py_sim'
    assert isinstance(py_timing['transaction_cycles'], int)
    cosim_timing = json.loads(
        results['extract_cosim_timing'].path('cosim_timing').read_text(encoding="utf-8")
    )
    assert cosim_timing['source'] == 'cosim'
    assert isinstance(cosim_timing['transaction_cycles'], int)
