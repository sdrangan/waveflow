"""Step 3.2 / AC3.1 of plans/mimo_cg/mimo_cg_paper_sims.md: the accuracy sweep's structure,
determinism, and that it never touches Vitis."""

from __future__ import annotations

import os
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
    # Three chunks: with 2 workers that is one full wave and a partial one, so the in-order fold
    # and the stop rule across a wave boundary are both exercised.
    kw = {"max_bits": 3 * sweep.SWEEP_CHUNK_BITS - 1, "min_errors": 10**9}
    one = sweep.simulate_sweep_point(SMALL, 0, workers=1, **kw)
    two = sweep.simulate_sweep_point(SMALL, 0, workers=2, **kw)
    three = sweep.simulate_sweep_point(SMALL, 0, workers=3, **kw)
    assert one == two == three
    assert one[0]["bits"] == 3 * sweep.SWEEP_CHUNK_BITS
    assert [r["detector"] for r in one] == sweep.CONFIGS[SMALL].detectors


def test_sweep_never_touches_vitis(tmp_path, monkeypatch):
    """A small sweep through SweepRunner and the DAG, with every toolchain and subprocess entry
    point recording its calls.  Recording (not just raising) matters: ``subprocess_result``
    swallows exceptions, so a raise alone could pass silently.  ``workers=1`` keeps the run in
    this process, where the patches apply."""
    calls = []

    def record(name):
        def fn(*a, **k):
            calls.append(name)
            raise AssertionError(f"the accuracy sweep must not call {name}")

        return fn

    for name in ("run_vitis_hls", "run_vitis_hls_result", "subprocess_result"):
        monkeypatch.setattr(toolchain, name, record(f"toolchain.{name}"))
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, record(f"subprocess.{name}"))
    monkeypatch.setattr(os, "system", record("os.system"))
    runner = SweepRunner(
        dag_factory=sweep.build_accuracy_dag,
        root_dir=tmp_path,
        summary=tmp_path / "results" / "sweep.json",
        extra_params={
            "workers": 1,
            "max_bits": sweep.SWEEP_CHUNK_BITS,
            "min_errors": 10**9,
        },
    )
    grid = ParamGrid(case=(SMALL,), snr_offset=(-1, 1))
    result = runner.run(
        grid, [Stage(through="accuracy_point", use_platform=False)], verbose=False
    )
    assert result.ok, result.failures
    assert calls == []
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
            config, 0, max_bits=sweep.SWEEP_CHUNK_BITS, min_errors=10**9
        )
    }
    qam, tx, _, A, B, mu, _ = sweep._chunk(c, sweep.point_snr(config, 0), 0)
    xs = cg_multi_rhs(A, B, c.K, explicit_residual=c.explicit, iterates=[c.K])
    direct = int(
        np.count_nonzero(qam.demodulate(np.swapaxes(xs[c.K] / mu, -1, -2)) != tx)
    )
    assert rows[f"cg{c.K}"]["bit_errors"] == direct
    assert rows[f"cg{c.K}"]["residual"] == ("explicit" if c.explicit else "recurrence")


def test_refinement_points_bracket_the_loss_region():
    """2-3 consecutive offsets per case, covering 0-0.7 dB above Phase 1's float MMSE crossing."""
    import math

    for name, c in sweep.CONFIGS.items():
        offsets = sweep.refine_offsets(name)
        assert 2 <= len(offsets) <= 4, (name, offsets)
        assert list(offsets) == list(range(offsets[0], offsets[-1] + 1))
        x = sweep.float_mmse_crossing(c.M, c.K, c.modulation)
        snrs = [sweep.point_snr(name, o) for o in offsets]
        assert snrs[0] <= math.floor(x) and math.ceil(x + 0.7) <= snrs[-1], name
    assert len(sweep.refine_points()) == sum(
        len(sweep.refine_offsets(n)) for n in sweep.CONFIGS
    )


def test_merge_refuses_an_unrefined_refinement_point(tmp_path):
    offset = sweep.refine_offsets(SMALL)[0]
    point = {"case": SMALL, "snr_offset": offset}
    rows = sweep.simulate_sweep_point(SMALL, offset, max_bits=sweep.SWEEP_CHUNK_BITS)
    path = sweep._point_csv(tmp_path, SMALL, offset)
    path.parent.mkdir(parents=True)
    for min_errors, ok in ((sweep.MIN_ERRORS, False), (sweep.REFINE_MIN_ERRORS, True)):
        comment = sweep.provenance("accuracy_point", min_errors=min_errors)
        sweep.write_table(path, rows, comment)
        if ok:
            sweep.merge_points(tmp_path, [point])
        else:
            with pytest.raises(RuntimeError, match="refinement point"):
                sweep.merge_points(tmp_path, [point])


committed_grid = pytest.mark.skipif(
    "refined_points"
    not in (sweep.HERE / "paper_data" / "accuracy_grid.csv")
    .read_text(encoding="utf-8")
    .split("\n", 1)[0],
    reason="the committed grid predates the refinement",
)


@committed_grid
def test_committed_refinement_points_ran_to_the_refined_budget():
    rows = read_table(sweep.HERE / "paper_data" / "accuracy_grid.csv")
    refined = {
        (sweep.CONFIGS[p["case"]], sweep.point_snr(p["case"], p["snr_offset"]))
        for p in sweep.refine_points()
    }
    seen = set()
    for r in rows:
        c = sweep.CONFIGS[
            f"{r['modulation']}_{r['M']}x{r['K']}"
            + ("_explicit" if r["residual"] == "explicit" else "")
        ]
        if (c, float(r["rho_db"])) in refined:
            seen.add((c, float(r["rho_db"])))
            assert (
                int(r["bit_errors"]) >= sweep.REFINE_MIN_ERRORS
                or int(r["bits"]) >= sweep.MAX_BITS
            ), r
    assert seen == refined


def test_refinement_redoes_an_under_budget_point_and_skips_a_refined_one(
    tmp_path, monkeypatch
):
    """The point CSV's recorded budget, not a summary, decides what the refinement redoes."""
    monkeypatch.setattr(sweep, "CONFIGS", {SMALL: sweep.CONFIGS[SMALL]})
    monkeypatch.setattr(sweep, "refine_offsets", lambda case: (-1,))
    monkeypatch.setattr(sweep, "MAX_BITS", sweep.SWEEP_CHUNK_BITS)
    path = sweep._point_csv(tmp_path, SMALL, -1)
    path.parent.mkdir(parents=True)
    rows = sweep.simulate_sweep_point(SMALL, -1, max_bits=sweep.SWEEP_CHUNK_BITS)
    comment = sweep.provenance("accuracy_point", min_errors=sweep.MIN_ERRORS)
    sweep.write_table(path, rows, comment)  # as a fresh full run leaves it
    log = tmp_path / "results" / "accuracy_refine" / f"{SMALL}.json"
    assert sweep.run_refinement(1, root=tmp_path, verbose=False) == 0
    assert sweep._recorded_min_errors(path) == sweep.REFINE_MIN_ERRORS and log.exists()
    log.unlink()
    assert sweep.run_refinement(1, root=tmp_path, verbose=False) == 0
    assert not log.exists()  # already refined: nothing ran
