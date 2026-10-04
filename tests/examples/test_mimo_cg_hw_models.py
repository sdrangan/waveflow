"""Steps 5.4–5.6 of plans/mimo_cg/mimo_cg_paper_sims.md: the calibrated hardware models.

No toolchain is needed: the models are fitted from the committed campaign tables
(``paper_data/hw_*.csv``) and only from their ``fit`` rows.

* The counted DSP and BRAM rules reproduce every ``fit`` row exactly: each vector-unit and matmul
  configuration and every stream-of-blocks channel.
* The committed model file is what a refit gives, and a held-out row cannot change it.
* On the 16 ``fit`` detectors the composed estimate has DSP and BRAM exact, and LUT, FF and cycles
  within the bounds below (the measured errors are in the plan's §15).
* :func:`waveflow.calib.resource_model.compose` over an elaborated detector gives the same totals.
* The Python simulation of the detector, whose blocks now wait their calibrated spans, takes the
  model's job time.
"""

from __future__ import annotations

import numpy as np
import pytest

from examples.mimo_cg.hw import estimate as E
from examples.mimo_cg.hw import measure as M
from examples.mimo_cg.hw import models as MD
from examples.mimo_cg.hw.common import IterOp
from examples.mimo_cg.hw.space import HwConfig, det_fit, full_space, replace

# --- the counted rules -------------------------------------------------------------------------


def test_multiplier_binding_rules():
    assert not MD.plain_in_dsp(8) and not MD.plain_in_dsp(10)
    assert all(MD.plain_in_dsp(w) for w in (12, 14, 16))
    assert MD.vec_mults(8) == (3, 9) and MD.vec_mults(10) == (5, 7) == MD.vec_mults(16)
    assert MD.mm_mults(4) == (2, 2) and MD.mm_mults(3) == (3, 0)
    # DSPs per lane of the vector unit and per element of the matmul, by width
    lane = {W: MD.vec_counted(HwConfig(L=1, W=W))["dsp"] for W in (8, 10, 12, 14, 16)}
    assert lane == {8: 3, 10: 5, 12: 12, 14: 12, 16: 12}
    pe4 = {
        W: MD.mm_counted(HwConfig(R=1, C=4, W=W))["dsp"] // 4 for W in (8, 10, 12, 16)
    }
    pe3 = {
        W: MD.mm_counted(HwConfig(R=1, C=4, W=W, cmul=3))["dsp"] // 4
        for W in (8, 10, 12, 16)
    }
    assert pe4 == {8: 2, 10: 2, 12: 4, 16: 4} and pe3 == {8: 3, 10: 3, 12: 3, 16: 3}
    # a multiply that left the DSPs is counted as a fabric multiply
    assert MD.vec_counted(HwConfig(L=4, W=8))["n_fab"] == 4 * 9
    assert MD.vec_counted(HwConfig(L=4, W=12))["n_fab"] == 0
    assert MD.mm_counted(HwConfig(R=4, C=8, W=10))["n_fab"] == 2 * 32


def test_block_ram_rules():
    # state arrays: one block per bank from 1,024 bits per bank, LUT RAM below
    assert MD.vec_counted(HwConfig(K=8, L=4, W=12))["bram"] == 0  # 64 x 12 = 768 bits
    assert MD.vec_counted(HwConfig(K=8, L=4, W=16))["bram"] == 6 * 4  # 64 x 16 = 1,024
    assert MD.vec_counted(HwConfig(K=16, L=4, W=12))["bram"] == 24
    assert MD.vec_counted(HwConfig(K=8, L=4, W=12))["lut"] == 6 * 4 * 64 * 12 // 64
    assert MD.mm_counted(HwConfig(K=16, R=16, C=4, W=12))["bram"] == 8
    assert (
        MD.mm_counted(HwConfig(K=8, R=4, C=8, W=12))["ff"] == 2 * 64 * 12
    )  # the A registers
    # a stream-of-blocks channel is one simple-dual-port memory over its banks ...
    assert MD.sob_memory(32, 96, 2) == {"bram": 3, "lut": 0, "ff": 0}  # 96 bits / 36
    assert MD.sob_memory(16, 384, 2)["bram"] == 11
    assert MD.sob_memory(512, 16, 3)["bram"] == 2  # 1,536 x 16: the 1,024 x 18 shape
    assert MD.sob_memory(4, 96, 2) == {
        "bram": 0,
        "lut": 12,
        "ff": 96,
    }  # 768 bits: LUT RAM
    assert MD.sob_memory(4, 128, 2)["bram"] == 4  # exactly 1,024 bits
    # ... unless a memory-side task moves two groups per cycle: one true-dual-port memory per bank
    assert MD.sob_memory(128, 24, 2, dual=True)["bram"] == 4
    assert MD.sob_memory(128, 24, 2, dual=False)["bram"] == 1
    dual = {n: d for n, *_r, d in MD.channels(HwConfig(L=1, mem_dw=64))}
    assert dual == {
        "a_blk": False,
        "b_blk": True,
        "p_blk": False,
        "s_blk": False,
        "x_blk": True,
    }
    assert not any(d for *_r, d in MD.channels(HwConfig(L=1, mem_dw=32)))
    assert not any(d for *_r, d in MD.channels(HwConfig(L=2, mem_dw=64)))


def test_counted_rules_reproduce_every_fit_row():
    """Step 5.4's exit: DSP and BRAM exact for every block configuration of the fit builds."""
    assert MD.counted_misses() == []


# --- the fit -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def refit() -> MD.Models:
    return MD.fit()


def test_committed_model_file_is_what_a_refit_gives(refit):
    committed = MD.Models.load()
    assert committed.table == refit.table
    assert committed.meta == refit.meta and committed.meta["fit_builds"] == 67
    assert set(committed.coef) == set(refit.coef) == set(MD.TERMS)
    for name, co in refit.coef.items():
        assert set(co) == {*MD.TERMS[name][1], "intercept"}
        for term, value in co.items():
            assert committed.coef[name][term] == pytest.approx(
                value, rel=1e-6, abs=1e-6
            ), (name, term)


def test_a_held_out_row_cannot_reach_the_fit(tmp_path, refit):
    """The fit reads ``fit`` rows only: absurd rows of the other role change nothing."""
    from examples.mimo_cg.mimo_cg import read_table

    for name in ("hw_builds", "hw_modules", "hw_cycles"):
        src = MD.PAPER_DATA / f"{name}.csv"
        text = src.read_text(encoding="utf-8")
        rows = read_table(src)
        donor = next(r for r in rows if r["top"] == "det")
        fake = dict(donor, build="det_fake_holdout", role="holdout")
        for k in ("lut", "ff", "dsp", "bram", "cycles"):
            if k in fake and fake[k] != "":
                fake[k] = "999999"
        (tmp_path / src.name).write_text(
            text + ",".join(fake.values()) + "\n", encoding="utf-8"
        )
    again = MD.fit(tmp_path)
    assert again.table == refit.table
    for name, co in refit.coef.items():
        assert again.coef[name] == pytest.approx(co)


def test_leave_one_out_errors_of_the_regressions(refit):
    """Leave-one-out errors on the fit rows, as mean and worst percent of the predicted quantity."""
    bounds = {
        "CgVec.lut": (2.0, 4.0),
        "CgVec.ff": (3.0, 9.0),
        "CgMm.lut": (4.0, 14.0),
        "CgMm.ff": (3.0, 20.0),
        "vec.iter": (1.0, 3.0),
        "vec.init": (1.5, 4.0),
        "mm.iter": (1.0, 3.0),
    }
    for name, (mean, worst) in bounds.items():
        r = refit.report[name]
        assert r["loo_mape_pct"] <= mean and r["loo_max_pct"] <= worst, (name, r)
        assert r["n"] >= 25 and r["terms"] <= r["n"] // 2
    # the loaders and the store are small, and so are their absolute residuals
    assert refit.report["load.lut"]["max_abs_residual"] < 500
    assert refit.report["store.lut"]["max_abs_residual"] < 150


def test_cycle_composition_terms(refit):
    """In every fit detector the loop is exactly the two spans, and the job overhead is the vector
    unit's start span plus 1 cycle with 64-bit words or 6 with 32-bit words."""
    assert refit.table["handoff|loop"] == {"cycles": 0.0}
    assert refit.table["t0_extra|64"] == {"cycles": 1}
    assert refit.table["t0_extra|32"] == {"cycles": 6}


# --- the full-design estimate --------------------------------------------------------------------


def test_estimate_on_the_fit_detectors():
    rows = E.errors("fit")
    assert len(rows) == 16
    assert all(
        r["dsp_est"] == r["dsp_meas"] and r["bram_est"] == r["bram_meas"] for r in rows
    )
    worst = {
        q: max(abs(r[f"{q}_err_pct"]) for r in rows)
        for q in ("lut", "ff", "t_iter", "t0")
    }
    mean = {q: float(np.mean([abs(r[f"{q}_err_pct"]) for r in rows])) for q in worst}
    assert worst["lut"] < 7.0 and mean["lut"] < 3.0
    assert worst["ff"] < 4.0 and mean["ff"] < 2.0
    assert worst["t_iter"] < 1.5 and mean["t_iter"] < 0.6
    assert worst["t0"] < 2.5


def test_estimate_covers_the_whole_space_and_refuses_the_rest():
    space = list(full_space())
    step = len(space) // 400
    for c in space[::step]:
        est = E.estimate(c)
        assert est.lut > 0 and est.ff > 0 and est.dsp > 0 and est.bram > 0
        assert est.t_iter > 0 and est.t0 > 0
        assert est.job_cycles(2) == pytest.approx(est.t0 + 2 * est.t_iter)
        assert sum(m["lut"] for m in est.modules.values()) == est.lut
    with pytest.raises(ValueError, match="not a configuration"):
        E.estimate(replace(HwConfig(), W=18))
    # more lanes cost DSPs and save cycles; a narrower datapath saves DSPs
    slow, fast = E.estimate(HwConfig(L=1)), E.estimate(HwConfig(L=16, C=16))
    assert fast.dsp > slow.dsp and fast.t_iter < slow.t_iter
    assert E.estimate(HwConfig(W=8)).dsp < E.estimate(HwConfig(W=12)).dsp


def test_compose_walks_the_same_numbers():
    """The framework's composition over an elaborated detector equals the direct estimate."""
    from waveflow.build.elaborate import elaborate
    from waveflow.calib.resource_model import compose

    models = MD.calibrated()
    for c in (
        det_fit()[0],
        det_fit()[-1],
        det_fit()[-2],
        HwConfig(8, 8, 2, 16, 3, 10, 4, 32, 3, 4),
    ):
        top = elaborate(
            M.comp_class("det"), M.elab_params("det", c), name="cg_detector"
        )
        est = compose(top, model_for=MD.model_for(models))
        assert {k: est.total[k] for k in MD.COUNTERS} == models.resources(c)["total"]
        assert len(est.per_module) == 9  # the top's own term and its eight modules
        assert est.own == {k: round(v) for k, v in models.integration(c).items()}


# --- the Python block models use the calibrated spans (step 5.5) ---------------------------------


def test_block_cycles_come_from_the_models_inside_the_space():
    from examples.mimo_cg.hw.mm import mm_cycles
    from examples.mimo_cg.hw.vec import vec_cycles

    models = MD.calibrated()
    c = HwConfig(K=8, L=4, R=4, C=8, cmul=3, W=12, g_s=8)
    spans = models.spans(c)
    assert vec_cycles(IterOp.ITER, 8, 32, 4, c.fmt) == pytest.approx(spans["vec.iter"])
    assert vec_cycles(IterOp.LAST, 8, 32, 4, c.fmt) == pytest.approx(spans["vec.iter"])
    assert vec_cycles(IterOp.INIT, 8, 32, 4, c.fmt) == pytest.approx(spans["vec.init"])
    assert mm_cycles(8, 32, 4, 8, 4, 3, c.fmt) == pytest.approx(spans["mm.iter"])
    # outside the space (the stress format, or no format given) the rough fallback remains
    assert MD.block_span("vec.iter", 2, K=8, L=4) is None
    assert (
        vec_cycles(IterOp.ITER, 8, 32, 4)
        == 10 + 8 * (24 + 80)
        == vec_cycles(IterOp.ITER, 8, 32, 4, 2)
    )
    assert mm_cycles(8, 32, 4, 8) == 2 * 4 * 18 + 10


@pytest.mark.parametrize(
    "c",
    [
        HwConfig(),
        HwConfig(8, 2, 2, 8, 4, 10, 4, 64, 3, 4),
        HwConfig(16, 16, 4, 16, 3, 16, 8, 32, 4, 8),
    ],
    ids=["default", "k8_l2", "k16_l16_32bit"],
)
def test_python_simulation_takes_the_models_job_time(c):
    """Bit-exact as before, and now cycle-approximate: the simulated interval between job
    completions is the model's T0 + nit·T_iter, less the few cycles of job overhead the Python
    glue does not model."""
    from examples.mimo_cg.hw.detector import CgDetectorSim, detector_problems

    jobs = M.job_nits(c.K)
    probs = detector_problems(64 if c.K > 4 else 32, c.K, 32, len(jobs), seed=5)
    kw = M.elab_params("det", c)
    kw.pop("N")
    tb = CgDetectorSim(probs, jobs, **kw).run()  # raises unless every X word is exact
    ends = np.array([end for _start, end in tb.dut.vec.fire_log])
    models = MD.calibrated()
    want = np.array([models.job_cycles(c, nit) for nit in jobs[1:]])
    extra = models.table[f"t0_extra|{c.mem_dw}"]["cycles"]
    assert np.diff(ends) == pytest.approx(want - extra, abs=0.5)
