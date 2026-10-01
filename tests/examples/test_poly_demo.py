"""The streaming polynomial example: model, scenarios, pysim timing, and the Vitis flow.

The example is hook-first (plans/hook_first_flow.md): a pure bit-exact model, a body-only
kernel, and shared stimulus files that the Python model, pysim and the hand-written C++
testbench all read.  Every stage is checked against the expected responses scenarios.py
computes from each scenario's intent.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from examples.stream_inband import scenarios as S
from examples.stream_inband.poly import (
    Float32,
    PolyAccel,
    PolyCmdHdr,
    PolyCmdType,
    PolyError,
    PolyTB,
    connect,
    poly_eval,
    poly_stream_model,
)
from examples.stream_inband.poly_build import build_poly_dag
from waveflow.build.build import BuildConfig
from waveflow.hw.arrayutils import write_array
from waveflow.hw.clock import Clock
from waveflow.simulation.logger import Logger
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts

_CLK_FREQ = 100e6


def test_poly_eval_worked_examples() -> None:
    # y = 1 - 2x - 3x^2 + 4x^3: every value here is exact in float32.
    assert poly_eval(S.COEFFS, [0.0, 1.0, 2.0, -1.0]).tolist() == [1.0, 0.0, 17.0, -4.0]
    assert poly_eval(S.COEFFS, [0.5]).dtype == np.float32


def test_the_model_matches_every_scenario(tmp_path: Path) -> None:
    S.write_scenarios(tmp_path)
    for name in S.scenarios():
        d = tmp_path / name
        res = poly_stream_model(read_bursts(d / "in"), S.COEFFS)
        write_bursts(res.out, d / "model")
        (d / "model" / "status.json").write_text(json.dumps(res.status()))
    assert S.check(tmp_path, "model") == {n: [] for n in S.scenarios()}


def test_the_error_scenarios_halt_with_the_right_code(tmp_path: Path) -> None:
    S.write_scenarios(tmp_path)
    for name, code, tx in (("early_tlast", PolyError.TLAST_EARLY_SAMP_IN, 32),
                           ("no_tlast", PolyError.NO_TLAST_SAMP_IN, 41)):
        res = poly_stream_model(read_bursts(tmp_path / name / "in"), S.COEFFS)
        assert res.status() == {"halted": 1, "error": int(code), "tx_id": tx}, name


def test_a_wrong_model_is_caught(tmp_path: Path) -> None:
    """The checker is worth something only if it rejects a wrong answer."""
    S.write_scenarios(tmp_path)
    d = tmp_path / "nominal"
    res = poly_stream_model(read_bursts(d / "in"), S.COEFFS[::-1])   # coefficients reversed
    write_bursts(res.out, d / "model")
    (d / "model" / "status.json").write_text(json.dumps(res.status()))
    assert S.check(tmp_path, "model", ["nominal"])["nominal"]


def test_pipeline_python_half(tmp_path: Path) -> None:
    for through in ("check_model", "check_pysim", "extract_py_timing"):
        results = build_poly_dag().run(BuildConfig(root_dir=tmp_path), through=through)
        bad = {k: v.message for k, v in results.items() if not v.success}
        assert not bad, bad
    py = json.loads((tmp_path / "results" / "py_timing.json").read_text())
    assert py["transaction_cycles"] == 100 + PolyAccel.proc_latency   # nsamp + latency


def _stimulus(nsamp: int, word_bw: int) -> list[StreamBurst]:
    def hdr(cmd, tx=0, n=0):
        h = PolyCmdHdr()
        h.cmd_type, h.tx_id, h.nsamp = cmd, tx, n
        return h.serialize(word_bw=word_bw)

    x = np.linspace(0.0, 1.0, nsamp, dtype=np.float32)
    return [StreamBurst(hdr(PolyCmdType.DATA, 1, nsamp)),
            StreamBurst(write_array(x, elem_type=Float32, word_bw=word_bw)),
            StreamBurst(hdr(PolyCmdType.END))]


def test_timing_model_scales_with_width_and_unroll(tmp_path: Path) -> None:
    """The pysim timing model: bandwidth-limited at 32 bits, twice as fast at 64 bits."""
    nsamp, period = 100, 1.0 / _CLK_FREQ
    durations = []
    for i, (bw, uf) in enumerate(((32, 1), (32, 2), (64, 2))):
        stim = tmp_path / f"stim_{i}"
        write_bursts(_stimulus(nsamp, bw), stim)
        sim, clk = Simulation(), Clock(freq=_CLK_FREQ)
        log = tmp_path / f"log_{i}.csv"
        accel = PolyAccel(name="a", sim=sim, clk=clk, in_bw=bw, out_bw=bw, unroll_factor=uf,
                          logger=Logger(name="l", sim=sim, file_path=log, fields=["event", "job"]))
        tb = PolyTB(name="tb", sim=sim, stimulus=stim, coeffs=S.COEFFS, n_out=2, word_bw=bw)
        connect(sim, tb, accel, clk)
        sim.run_sim()
        assert tb.status["error"] == 0
        ev: dict[str, float] = {}
        with open(log, newline="") as f:
            for row in csv.DictReader(f):
                ev.setdefault(row["event"], float(row["time"]))
        durations.append(ev["samp_out_write_end"] - ev["samp_read_begin"])
    lat, tol = PolyAccel.proc_latency, 5 * period
    assert abs(durations[0] - (nsamp + lat) * period) < tol      # 1 float per word
    assert abs(durations[1] - durations[0]) < tol                 # unroll can't beat the bus
    assert abs(durations[2] - (nsamp / 2 + lat) * period) < tol   # 2 floats per word


@pytest.mark.vitis
def test_vitis_csim_cosim_and_timing(tmp_path: Path) -> None:
    """csim on every scenario, cosim of the timing scenario, cosim cycles vs pysim."""
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found")
    results = build_poly_dag().run(BuildConfig(root_dir=tmp_path), through="summary")
    bad = {k: str(v.message)[-2000:] for k, v in results.items() if not v.success}
    assert not bad, bad
    summary = json.loads((tmp_path / "results" / "summary.json").read_text())
    for stage, scenarios in summary["checks"].items():
        assert scenarios and all(scenarios.values()), (stage, scenarios)
    assert set(summary["checks"]["csim"]) == set(S.scenarios())     # every scenario, csim
    assert summary["timing"]["pass"], summary["timing"]
