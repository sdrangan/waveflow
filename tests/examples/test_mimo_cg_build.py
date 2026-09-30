"""Step 1.4 of plans/mimo_cg/mimo_cg_paper_sims.md: the Phase 1 build's tables and figures.

Seeds are fixed here and must never be changed to make a test pass (the plan's Rules 4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from examples.mimo_cg.mimo_cg import (
    Config,
    mmse_crossing_db,
    read_table,
    run_float_ber,
    simulate_point,
    write_table,
    zf_crossing_db,
)
from examples.mimo_cg.mimo_link import MODULATIONS

PAPER_DATA = Path(__file__).resolve().parents[2] / "examples" / "mimo_cg" / "paper_data"

#: The plan's §9 table: analytical ZF SNR (dB) at BER 1e-3, by modulation, then (M, K).
PLAN_ZF_TABLE = {
    "qpsk": [-4.4, -3.7, -1.8, -7.9, -7.6, -6.9, -11.1, -10.9, -10.6],
    "16qam": [2.3, 3.0, 4.9, -1.1, -0.8, -0.1, -4.3, -4.2, -3.9],
    "64qam": [8.3, 9.0, 10.9, 4.9, 5.2, 5.9, 1.7, 1.8, 2.1],
}
MK = [(M, K) for M in (32, 64, 128) for K in (4, 8, 16)]


def _plan_value(M: int, K: int, modulation: str) -> float:
    return PLAN_ZF_TABLE[modulation][MK.index((M, K))]


@pytest.mark.parametrize("modulation", list(MODULATIONS))
def test_zf_crossings_match_the_plan_table(modulation):
    for M, K in MK:
        got = zf_crossing_db(M, K, MODULATIONS[modulation], tol_db=0.01)
        assert got == pytest.approx(_plan_value(M, K, modulation), abs=0.1), (M, K)


def test_committed_zf_crossings_match_the_plan_table():
    path = PAPER_DATA / "zf_crossings.csv"
    if not path.exists():
        pytest.skip(f"{path} not built yet (python -m examples.mimo_cg.mimo_cg_build)")
    rows = read_table(path)
    assert len(rows) == 27
    for r in rows:
        planned = _plan_value(int(r["M"]), int(r["K"]), r["modulation"])
        assert float(r["zf_crossing_db"]) == pytest.approx(planned, abs=0.1), r


def test_simulate_point_is_a_pure_function_of_its_parameters():
    cfg = Config(32, 8, "16qam")
    assert simulate_point(cfg, 2.0, max_bits=300_000) == simulate_point(
        cfg, 2.0, max_bits=300_000
    )


def test_results_do_not_depend_on_the_worker_count():
    configs = [Config(32, 4, "qpsk"), Config(64, 8, "16qam")]
    snrs = [-6.0, 0.0]
    serial = run_float_ber(configs, snrs, workers=1, max_bits=200_000)
    parallel = run_float_ber(configs, snrs, workers=3, max_bits=200_000)
    assert serial == parallel
    assert [r["detector"] for r in serial[:7]] == [
        "zf",
        "mmse",
        "cg1",
        "cg2",
        "cg3",
        "cg4",
    ] + ["zf"]


def test_cg_at_nit_k_makes_the_same_decisions_as_exact_mmse():
    # About 2 dB below each ZF crossing (4.9 and 5.2 dB), so there are errors to compare.
    for cfg, rho in ((Config(32, 16, "16qam"), 3.0), (Config(64, 8, "64qam"), 3.0)):
        rows = {r["detector"]: r for r in simulate_point(cfg, rho, max_bits=1_000_000)}
        assert rows["mmse"]["bit_errors"] > 50
        assert abs(rows[f"cg{cfg.K}"]["bit_errors"] - rows["mmse"]["bit_errors"]) <= 2


def test_mmse_crossing_interpolates_in_log_ber():
    rows = [{"rho_db": 0.0, "ber": 1e-2}, {"rho_db": 1.0, "ber": 1e-4}]
    assert mmse_crossing_db(rows) == pytest.approx(0.5)
    assert (
        mmse_crossing_db([{"rho_db": 0.0, "ber": 1e-2}, {"rho_db": 1.0, "ber": 5e-3}])
        is None
    )


def test_tables_round_trip_with_fixed_formatting(tmp_path):
    rows = [{"a": 1, "b": 0.1, "c": "x", "d": float("nan")}]
    write_table(tmp_path / "t.csv", rows, "provenance")
    text = (tmp_path / "t.csv").read_text()
    assert text == "# provenance\na,b,c,d\n1,1.000000e-01,x,nan\n"
    assert read_table(tmp_path / "t.csv") == [
        {"a": "1", "b": "1.000000e-01", "c": "x", "d": "nan"}
    ]


def test_figures_are_byte_identical_across_renders(tmp_path):
    from examples.mimo_cg.mimo_cg_figures import write_figures

    rows = []
    for mod in MODULATIONS:
        for M, K in MK:
            cfg = Config(M, K, mod)
            for rho in (-10.0, 0.0, 10.0):
                for det in cfg.detectors:
                    ber = 10 ** (-(rho + 20) / 10) / (2 if det == "mmse" else 1)
                    rows.append(
                        {
                            "M": M,
                            "K": K,
                            "modulation": mod,
                            "rho_db": rho,
                            "detector": det,
                            "ber": ber,
                        }
                    )
    write_table(tmp_path / "ber.csv", rows, "synthetic")
    first = [
        p.read_bytes() for p in write_figures(tmp_path / "ber.csv", tmp_path / "a")
    ]
    second = [
        p.read_bytes() for p in write_figures(tmp_path / "ber.csv", tmp_path / "b")
    ]
    assert len(first) == 4
    assert first == second
