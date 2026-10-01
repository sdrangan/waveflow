"""Step 3.2 / AC3.1 of plans/mimo_cg/mimo_cg_paper_sims.md: the accuracy sweep's structure,
determinism, and that it never touches Vitis."""

from __future__ import annotations

import subprocess

import pytest

from examples.mimo_cg import mimo_cg_accuracy_sweep as sweep
from examples.mimo_cg.mimo_cg import read_table
from waveflow.build.sweep import ParamGrid, Stage, SweepRunner
from waveflow.toolchain import toolchain

SMALL = "qpsk_32x4"


def test_grid_is_the_gate_31_grid():
    assert (
        len(sweep.GRID) == 364
    )  # 27 recurrence configs + 1 explicit spot check, x 13 SNRs
    assert len(sweep.FORMATS) == 21
    assert sum(c.explicit for c in sweep.CONFIGS.values()) == 1
    rows = {K: 2 + 22 * len([n for n in sweep.NITS if n <= K]) for K in (4, 8, 16)}
    assert rows == {4: 90, 8: 134, 16: 178}
    for c in sweep.CONFIGS.values():
        assert len(c.detectors) == rows[c.K]
    assert sum(sweep.expected_rows(p["case"]) for p in sweep.GRID) == 13 * (
        9 * 402 + 134
    )


def test_every_sweep_format_fits_the_64_bit_cap():
    for name, fmt in sweep.FORMATS.items():
        fmt.intermediates(16)  # raises if any intermediate exceeds 64 bits
        W, g = (int(x) for x in name[1:].split("g"))
        assert (
            fmt.rz.W == W + g
            and fmt.ps.W == W + g
            and fmt.alpha.W == W
            and fmt.g_div == 6
        )


def test_snr_window_is_integer_and_centred_on_the_zf_crossing():
    for name, c in sweep.CONFIGS.items():
        snrs = [sweep.point_snr(name, o) for o in sweep.SNR_OFFSETS]
        assert snrs == [
            round(sweep.zf_crossing(c.M, c.K, c.modulation)) + o
            for o in sweep.SNR_OFFSETS
        ]


def test_results_do_not_depend_on_the_worker_count():
    kw = {"max_bits": sweep.CHUNK_BITS, "min_errors": 10**9}  # exactly one chunk
    one = sweep.simulate_sweep_point(SMALL, 0, workers=1, **kw)
    two = sweep.simulate_sweep_point(SMALL, 0, workers=2, **kw)
    assert one == two
    assert [r["detector"] for r in one] == sweep.CONFIGS[SMALL].detectors


def test_sweep_never_touches_vitis(tmp_path, monkeypatch):
    """A small sweep through SweepRunner and the DAG, with every toolchain entry point raising."""

    def boom(*_a, **_k):
        raise AssertionError("the accuracy sweep must not run Vitis or a subprocess")

    monkeypatch.setattr(toolchain, "run_vitis_hls", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    runner = SweepRunner(
        dag_factory=sweep.build_accuracy_dag,
        root_dir=tmp_path,
        summary=tmp_path / "results" / "sweep.json",
        extra_params={"workers": 1, "max_bits": sweep.CHUNK_BITS, "min_errors": 10**9},
    )
    grid = ParamGrid(case=(SMALL,), snr_offset=(-1, 1))
    result = runner.run(
        grid, [Stage(through="accuracy_point", use_platform=False)], verbose=False
    )
    assert result.ok, result.failures
    out = sweep.merge_points(tmp_path, grid)
    rows = read_table(out)
    assert len(rows) == 2 * sweep.expected_rows(SMALL)
    assert {r["rho_db"] for r in rows} == {
        f"{sweep.point_snr(SMALL, o):.6e}" for o in (-1, 1)
    }


def test_dry_run_simulates_nothing(tmp_path):
    runner = SweepRunner(dag_factory=sweep.build_accuracy_dag, root_dir=tmp_path,
                         summary=tmp_path / "s.json")  # fmt: skip
    grid = ParamGrid(case=(SMALL,), snr_offset=(0,))
    result = runner.run(
        grid, [Stage(through="dry_point", use_platform=False)], verbose=False
    )
    assert result.ok
    assert not (
        tmp_path / "results" / "accuracy_points" / f"{SMALL}_off+0.csv"
    ).exists()
    assert (
        "90 rows"
        in (tmp_path / "results" / "accuracy_points" / "dry_point.txt").read_text()
    )


@pytest.mark.parametrize("config", ["16qam_64x8", "16qam_64x8_explicit"])
def test_float_rows_count_errors_on_the_chunk_samples(config):
    """The float-CG rows equal a direct float CG on the same chunk (a pairing sanity check)."""
    import numpy as np

    from examples.mimo_cg.detectors import cg_multi_rhs

    c = sweep.CONFIGS[config]
    rows = {
        r["detector"]: r
        for r in sweep.simulate_sweep_point(
            config, 0, max_bits=sweep.CHUNK_BITS, min_errors=10**9
        )
    }
    qam, tx, _, A, B, mu, _ = sweep._chunk(c, sweep.point_snr(config, 0), 0)
    xs = cg_multi_rhs(A, B, c.K, explicit_residual=c.explicit, iterates=[c.K])
    direct = int(
        np.count_nonzero(qam.demodulate(np.swapaxes(xs[c.K] / mu, -1, -2)) != tx)
    )
    assert rows[f"cg{c.K}"]["bit_errors"] == direct
    assert rows[f"cg{c.K}"]["residual"] == ("explicit" if c.explicit else "recurrence")
