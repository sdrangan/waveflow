"""The streaming polynomial example: model, scenarios, pysim timing, and the Vitis flow.

The example is hook-first (plans/hook_first_flow.md): a pure bit-exact model, a body-only
kernel, and shared stimulus files that the Python model, pysim and the hand-written C++
testbench all read.  Every stage is checked against the expected responses scenarios.py
computes from each scenario's intent.  The protocol is the command-response contract of
plans/stream_inband_pattern.md: the coefficients travel in each DATA header, and the
register map holds only status.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from examples.stream_inband import scenarios as S
from examples.stream_inband.poly import (
    POISONED_STATUS,
    PolyAccel,
    PolyCmdHdr,
    PolyError,
    PolyTB,
    connect,
    poly_eval,
    poly_stream_model,
)
from examples.stream_inband.poly_build import WIDTHS, build_poly_dag, width_dir
from waveflow.build.build import BuildConfig
from waveflow.hw.clock import Clock
from waveflow.simulation.logger import Logger
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import StreamBurst, read_bursts, write_bursts

_CLK_FREQ = 100e6


def test_poly_eval_worked_examples() -> None:
    # Every value here is exact in float32.
    # A: y = 1 - 2x - 3x^2 + 4x^3
    assert poly_eval(S.A, [0.0, 1.0, 2.0, -1.0]).tolist() == [1.0, 0.0, 17.0, -4.0]
    # B: y = 0.5 + 0.25x - x^2 + 2x^3
    assert poly_eval(S.B, [0.0, 1.0, 2.0, -1.0]).tolist() == [0.5, 1.75, 13.0, -2.75]
    assert poly_eval(S.A, [0.5]).dtype == np.float32


def test_the_header_carries_the_coefficients() -> None:
    """Rule 2: everything the kernel computes with is on the stream."""
    assert "coeffs" in PolyCmdHdr.elements
    assert PolyCmdHdr().serialize(word_bw=32).size == 6       # tx_id|cmd, nsamp, 4 floats
    assert PolyCmdHdr().serialize(word_bw=64).size == 3
    regmap = PolyAccel(name="a", sim=Simulation()).regmap
    assert {"halted", "error", "tx_id"} <= set(regmap._fields)
    assert "coeffs" not in regmap._fields                      # status only (rule 3)


def _run_model(d: Path, bw: int, names) -> None:
    for name in names:
        res = poly_stream_model(read_bursts(d / name / "in"), word_bw=bw)
        write_bursts(res.out, d / name / "model")
        (d / name / "model" / "status.json").write_text(json.dumps(res.status()))


@pytest.mark.parametrize("bw", WIDTHS)
def test_the_model_matches_every_scenario(tmp_path: Path, bw: int) -> None:
    S.write_scenarios(tmp_path, bw)
    _run_model(tmp_path, bw, S.scenarios())
    assert S.check(tmp_path, "model") == {n: [] for n in S.scenarios()}


@pytest.mark.parametrize("bw", WIDTHS)
def test_the_error_scenarios_halt_and_close_the_output_burst(tmp_path: Path, bw: int) -> None:
    """Rule 6: status set, the burst in progress closed with TLAST, nothing more read."""
    S.write_scenarios(tmp_path, bw)
    for name, code, tx, n_last in (("early_tlast", PolyError.TLAST_EARLY_SAMP_IN, 42, 6),
                                   ("no_tlast", PolyError.NO_TLAST_SAMP_IN, 51, 10),
                                   ("early_tlast_vcd", PolyError.TLAST_EARLY_SAMP_IN, 72, 6)):
        res = poly_stream_model(read_bursts(tmp_path / name / "in"), word_bw=bw)
        assert res.status() == {"halted": 1, "error": int(code), "tx_id": tx}, name
        assert res.out[-1].tlast, name                          # closed
        assert len(res.out[-1].words) == -(-n_last // (bw // 32)), name


def test_the_coefficients_do_not_carry_over(tmp_path: Path) -> None:
    """Rule 4: the same samples under A, B, A give A's results twice and B's between."""
    S.write_scenarios(tmp_path, 32)
    out = read_bursts(tmp_path / "coeff_change" / "expected")
    a1, b, a2 = out[1].words, out[3].words, out[5].words
    assert np.array_equal(a1, a2) and not np.array_equal(a1, b)


def test_the_checker_rejects_wrong_answers(tmp_path: Path) -> None:
    """The checker is worth something only if it rejects a wrong answer."""
    S.write_scenarios(tmp_path, 32)
    _run_model(tmp_path, 32, ["multi_data", "early_tlast"])

    # A wrong result word.
    d = tmp_path / "multi_data" / "model"
    bursts = read_bursts(d)
    bursts[1].words[3] ^= 1
    write_bursts(bursts, d)
    assert S.check(tmp_path, "model", ["multi_data"])["multi_data"]

    # A kernel that did not clear its status (rule 5): the poisoned values survive.
    _run_model(tmp_path, 32, ["multi_data"])
    (d / "status.json").write_text(json.dumps(POISONED_STATUS))
    assert any("status" in p for p in S.check(tmp_path, "model", ["multi_data"])["multi_data"])

    # A kernel that left its output burst open on the error (rule 6).
    d = tmp_path / "early_tlast" / "model"
    bursts = read_bursts(d)
    bursts[-1] = StreamBurst(bursts[-1].words, False)
    write_bursts(bursts, d)
    problems = S.check(tmp_path, "model", ["early_tlast"])["early_tlast"]
    assert any(p.startswith("rule 6") for p in problems), problems


def test_pipeline_python_half(tmp_path: Path) -> None:
    for through in ("check_model", "check_pysim", "extract_py_timing_w32",
                    "extract_py_timing_w64"):
        results = build_poly_dag().run(BuildConfig(root_dir=tmp_path), through=through)
        bad = {k: v.message for k, v in results.items() if not v.success}
        assert not bad, bad
    for bw in WIDTHS:
        pysim = json.loads((tmp_path / "results" / "check_pysim.json").read_text())
        ran = [k for k in pysim if k.startswith(f"w{bw}/")]
        assert f"w{bw}/early_tlast" in ran and f"w{bw}/no_tlast" not in ran
    py = {bw: json.loads((tmp_path / "results" / f"py_timing_w{bw}.json").read_text())
          for bw in WIDTHS}
    assert py[64]["transaction_cycles"] < py[32]["transaction_cycles"]


def _kernel_cycles(stim: Path, bw: int, uf: int, log: Path) -> float:
    sim, clk = Simulation(), Clock(freq=_CLK_FREQ)
    accel = PolyAccel(name="a", sim=sim, clk=clk, in_bw=bw, out_bw=bw, unroll_factor=uf,
                      logger=Logger(name="l", sim=sim, file_path=log, fields=["event", "job"]))
    tb = PolyTB(name="tb", sim=sim, stimulus=stim, n_out=2, word_bw=bw)
    connect(sim, tb, accel, clk)
    sim.run_sim()
    assert tb.status == {"halted": 0, "error": 0, "tx_id": 0}   # cleared over the poison
    ev: dict[str, float] = {}
    with open(log, newline="") as f:
        for row in csv.DictReader(f):
            ev.setdefault(row["event"], float(row["time"]))
    return (ev["proc_end"] - ev["proc_begin"]) * _CLK_FREQ


def test_timing_model_scales_with_width_and_unroll(tmp_path: Path) -> None:
    """The pysim timing model: bandwidth-limited, so 64 bits saves the words, unroll does not."""
    cycles = {}
    for bw, uf in ((32, 1), (32, 2), (64, 2)):
        d = tmp_path / f"w{bw}_{uf}"
        S.write_scenarios(d, bw)
        cycles[(bw, uf)] = _kernel_cycles(d / "timing" / "in", bw, uf, tmp_path / f"{bw}_{uf}.csv")
    assert abs(cycles[(32, 2)] - cycles[(32, 1)]) < 5            # unroll can't beat the bus
    # 64 bits moves the 100 samples in 50 words instead of 100, and each 6-word header in 3.
    saved = cycles[(32, 1)] - cycles[(64, 2)]
    assert 50 <= saved <= 58, cycles


@pytest.mark.vitis
def test_vitis_csim_cosim_and_timing(tmp_path: Path) -> None:
    """csim on every scenario, cosim of the timing scenario, cosim cycles vs pysim, both widths."""
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found")
    results = build_poly_dag().run(BuildConfig(root_dir=tmp_path), through="summary")
    bad = {k: str(v.message)[-2000:] for k, v in results.items() if not v.success}
    assert not bad, bad
    summary = json.loads((tmp_path / "results" / "summary.json").read_text())
    for stage, scenarios in summary["checks"].items():
        assert scenarios and all(scenarios.values()), (stage, scenarios)
    assert set(summary["checks"]["csim"]) == {f"w{bw}/{n}" for bw in WIDTHS for n in S.scenarios()}
    for bw in WIDTHS:
        assert summary["timing"][f"w{bw}"]["pass"], summary["timing"]
        status = json.loads((width_dir(tmp_path, bw) / "timing" / "cosim" / "status.json").read_text())
        assert status == {"halted": 0, "error": 0, "tx_id": 0}
    assert (tmp_path / "vcd" / "error_path.vcd").stat().st_size > 0


def test_the_protocol_page_shows_the_serialized_layout() -> None:
    """docs/examples/stream_inband/protocol.md quotes word layouts; they must be serialize()'s."""
    from waveflow.hw.arrayutils import write_array

    from examples.stream_inband.poly import CoeffArray, Float32, PolyCmdType, PolyRespHdr

    page = (Path(__file__).resolve().parents[2] / "docs" / "examples" / "stream_inband"
            / "protocol.md").read_text(encoding="utf-8")
    for bw, digits in ((32, 8), (64, 16)):
        hdr = PolyCmdHdr()
        hdr.cmd_type, hdr.tx_id, hdr.nsamp = PolyCmdType.DATA, 42, 100
        hdr.coeffs = CoeffArray(S.A)
        resp = PolyRespHdr()
        resp.tx_id = 42
        x = write_array(np.array([0.5, -1.0, 2.0], np.float32), elem_type=Float32, word_bw=bw)
        for words in (hdr.serialize(word_bw=bw), resp.serialize(word_bw=bw), x):
            for w in words:
                assert f"0x{int(w):0{digits}x}" in page, (bw, hex(int(w)))
