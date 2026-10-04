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
    assert committed.meta == refit.meta
    # version 2 (plan step 6.1): the first round's 67 builds and the second round's 19
    assert committed.meta["fit_builds"] == 86 and committed.meta["version"] == 2
    assert committed.meta["fit_roles"] == ["fit", "fit2"]
    assert set(committed.coef) == set(refit.coef) == set(MD.TERMS)
    for name, co in refit.coef.items():
        assert set(co) == {*MD.TERMS[name][1], "intercept"}
        for term, value in co.items():
            assert committed.coef[name][term] == pytest.approx(
                value, rel=1e-6, abs=1e-6
            ), (name, term)


def test_a_held_out_row_cannot_reach_the_fit(tmp_path, refit):
    """The fit reads ``fit`` rows only: absurd rows of the other role change nothing, whichever
    top they belong to.  (The committed tables now hold real held-out rows too, so the refit test
    above is the same guard on real data.)"""
    from examples.mimo_cg.mimo_cg import read_table

    for name in ("hw_builds", "hw_modules", "hw_cycles"):
        src = MD.PAPER_DATA / f"{name}.csv"
        text = src.read_text(encoding="utf-8")
        rows = [r for r in read_table(src) if r["role"] == "fit"]
        fakes = []
        for top in ("vec", "mm", "det"):
            for donor in [r for r in rows if r["top"] == top][:40]:
                fake = dict(donor, build=f"{top}_fake_holdout", role="holdout")
                for k in ("lut", "ff", "dsp", "bram", "cycles"):
                    if k in fake and fake[k] != "":
                        fake[k] = "999999"
                fakes.append(",".join(fake.values()))
        (tmp_path / src.name).write_text(
            text + "\n".join(fakes) + "\n", encoding="utf-8"
        )
    again = MD.fit(tmp_path)
    assert again.table == refit.table
    for name, co in refit.coef.items():
        assert again.coef[name] == pytest.approx(co)


def test_leave_one_out_errors_of_the_regressions(refit):
    """Leave-one-out errors on the fit rows, as mean and worst percent of the predicted quantity,
    for every regression.  The blocks and the cycle models are tight.  The loader, the store and
    the framer's flip-flops are not (M5 review): few samples, small quantities, and errors of tens
    of percent — which the glue's total hides because measured tables dominate it.

    Version 2: the matmul's three regressions are fitted on 45 builds, at every lane count.  With
    v1's terms on those 45 builds the LUT model's leave-one-out error was 7.3% on average and 28%
    at worst; with the output-stage terms it is 2.2% and 14.6%."""
    bounds = {
        "CgVec.lut": (2.0, 4.0),
        "CgVec.ff": (3.0, 9.0),
        "CgMm.lut": (2.5, 15.0),
        "CgMm.ff": (3.0, 16.0),
        "vec.iter": (1.0, 3.0),
        "vec.init": (1.5, 4.0),
        "mm.iter": (1.0, 3.1),
        "CgCmdRx.lut": (1.0, 3.0),
        "CgCtrl.lut": (0.5, 1.0),
        "CgCtrl.ff": (1.0, 3.0),
        # the weak ones
        "CgCmdRx.ff": (13.0, 52.0),
        "load.lut": (12.0, 70.0),
        "load.ff": (46.0, 205.0),
        "store.lut": (11.0, 46.0),
        "store.ff": (71.0, 161.0),
    }
    assert set(bounds) == set(refit.report) == set(MD.TERMS)
    for name, (mean, worst) in bounds.items():
        r = refit.report[name]
        assert r["loo_mape_pct"] <= mean and r["loo_max_pct"] <= worst, (name, r)
    for name in ("CgVec.lut", "CgVec.ff", "CgMm.lut", "CgMm.ff", "vec.iter", "mm.iter"):
        r = refit.report[name]
        assert r["n"] >= 25 and r["terms"] <= r["n"] // 2
    assert {refit.report[n]["n"] for n in ("CgMm.lut", "CgMm.ff", "mm.iter")} == {45}
    # the matmul's cycle model has six fitted parameters: its loops' trip counts are counted
    assert refit.report["mm.iter"]["terms"] == 6
    assert refit.report["mm.iter"]["max_abs_residual"] < 11
    # the loader is a large part of a detector's csynth LUTs (22-35%), so its absolute residuals
    # are what bound its effect on a design
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
    assert worst["lut"] < 2.0 and mean["lut"] < 1.5
    assert worst["ff"] < 4.0 and mean["ff"] < 2.0
    # v2's matmul cycle model is 14 cycles (1.9%) slow on the K = 16, 16-lane, 3-multiply detector
    assert worst["t_iter"] < 2.0 and mean["t_iter"] < 0.6
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
    # since version 2 the matmul is calibrated at every lane count, so nothing inside the space
    # extrapolates (v1 said EXTRAPOLATED at 2 and 16 lanes)
    from waveflow.calib.confidence import ConfidenceLevel

    def level(c):
        top = elaborate(
            M.comp_class("det"), M.elab_params("det", c), name="cg_detector"
        )
        return compose(top, model_for=MD.model_for(models)).level

    assert MD.MM_FIT_LANES == (1, 2, 4, 8, 16)
    for c in (HwConfig(L=4), HwConfig(L=16, C=16), HwConfig(L=2)):
        assert level(c) is ConfidenceLevel.INTERPOLATED


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
    # nor does a span extrapolate: another block size, or knobs outside the space, give None
    assert MD.block_span("vec.iter", c.fmt, N=8, K=4, L=4) is None
    assert MD.block_span("vec.iter", c.fmt, K=32, L=4) is None
    assert MD.block_span("vec.iter", c.fmt, K=8, L=32) is None
    assert MD.block_span("mm.iter", c.fmt, K=8, R=3, C=8, L=4, cmul=4) is None
    assert vec_cycles(IterOp.ITER, 4, 8, 4, c.fmt) == 10 + 2 * (
        12 + 80
    )  # N = 8: the fallback
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


# --- held-out validation (steps 5.7 and 5.8, AC5) ------------------------------------------------

#: The model file as frozen: version 2, in step 6.1, before any build of the second held-out set
#: ran (plan §15).  Version 1 was frozen in step 5.6 as 6450abe0…c513, before any held-out build.
FROZEN_MODEL_SHA256 = "d95510d337e992d3194fa17fff715d15989573e9ea8863553b3998dcfdbb95d9"


def test_models_are_unchanged_since_they_were_frozen():
    from examples.mimo_cg.hw import validate as V

    assert V.model_sha256() == FROZEN_MODEL_SHA256


def test_validation_tables_are_what_the_command_regenerates(tmp_path):
    from examples.mimo_cg.hw import validate as V

    V.validate(out_dir=tmp_path)
    for name in ("model_validation.csv", "model_validation_metrics.csv"):
        assert (tmp_path / name).read_bytes() == (MD.PAPER_DATA / name).read_bytes()


def test_validation_scores_only_held_out_builds_of_the_split():
    from examples.mimo_cg.hw.space import read_split
    from examples.mimo_cg.mimo_cg import read_table

    held = {b: t for b, t, role, _c in read_split() if role == "holdout"}
    detail = read_table(MD.PAPER_DATA / "model_validation.csv")
    assert {r["build"] for r in detail} == set(held)
    blocks = {
        (r["build"], r["scope"]) for r in detail if r["scope"].startswith("block:")
    }
    assert len(blocks) == 34  # 12 vector-unit, 12 matmul, and the glue of 10 detectors
    designs = {r["build"] for r in detail if r["scope"] == "design"}
    assert len(designs) == 10 and all(held[b] == "det" for b in designs)
    jobs = [r for r in detail if r["quantity"].startswith("job_cycles")]
    assert len(jobs) == 50  # five measured intervals per held-out detector


def test_ac5_on_the_held_out_builds():
    """AC5: DSP and BRAM exact on at least 90% of the held-out block configurations; LUT and FF
    within 10% and job cycles within 5%, as mean absolute percentage errors, on the held-out
    detectors.  The thresholds were fixed at gate 5.0, before any build."""
    from examples.mimo_cg.mimo_cg import read_table

    summary = {
        r["metric"]: r
        for r in read_table(MD.PAPER_DATA / "model_validation_metrics.csv")
    }
    gates = {m: r for m, r in summary.items() if r["threshold"]}
    assert set(gates) == {
        "blocks: DSP and BRAM both exact (%)",
        "designs: LUT MAPE (%)",
        "designs: FF MAPE (%)",
        "designs: job cycles MAPE (%)",
    }
    assert float(gates["blocks: DSP and BRAM both exact (%)"]["value"]) >= 90.0
    assert float(gates["designs: LUT MAPE (%)"]["value"]) <= 10.0
    assert float(gates["designs: FF MAPE (%)"]["value"]) <= 10.0
    assert float(gates["designs: job cycles MAPE (%)"]["value"]) <= 5.0
    assert all(r["pass"] == "1" for r in gates.values())
    assert gates["blocks: DSP and BRAM both exact (%)"]["n"] == "34"
    assert gates["designs: LUT MAPE (%)"]["n"] == "10"


def test_held_out_builds_that_share_a_block_with_a_fit_build():
    """Three held-out builds share a block with a fit build of the other kind of top (M5 review).
    No fitted model saw their rows, but the overlap is stated, and AC5 holds without them.
    """
    from examples.mimo_cg.hw import validate as V
    from examples.mimo_cg.mimo_cg import read_table

    assert V.overlapping() == {
        "vec_k4_l4_w16g8",  # the vector unit of a fit detector
        "det_k8_l4_r1_c4_m4_w12g4_d64_s2_q4",  # both hold the fit unit vec_k8_l4_w12g4
        "det_k8_l4_r8_c32_m4_w12g4_d32_s4_q2",
    }
    rows = {
        r["metric"]: r
        for r in read_table(MD.PAPER_DATA / "model_validation_metrics.csv")
    }
    disjoint = {m: r for m, r in rows.items() if m.startswith("disjoint from fit")}
    assert all(
        r["threshold"] == "" for r in disjoint.values()
    )  # disclosures, not gates
    blocks = disjoint["disjoint from fit: blocks DSP and BRAM both exact (%)"]
    assert (blocks["n"], float(blocks["value"])) == ("31", 100.0)
    assert disjoint["disjoint from fit: designs LUT MAPE (%)"]["n"] == "8"
    assert float(disjoint["disjoint from fit: designs LUT MAPE (%)"]["value"]) <= 10.0
    assert float(disjoint["disjoint from fit: designs FF MAPE (%)"]["value"]) <= 10.0
    assert (
        float(disjoint["disjoint from fit: designs job cycles MAPE (%)"]["value"])
        <= 5.0
    )


def test_what_exact_rests_on():
    """Most of the 34 exact block comparisons are zero against zero, so the table also states the
    non-trivial ones: blocks whose BRAM is not zero, and every held-out channel memory.
    """
    from examples.mimo_cg.mimo_cg import read_table

    rows = {
        r["metric"]: r
        for r in read_table(MD.PAPER_DATA / "model_validation_metrics.csv")
    }
    nonzero = rows["blocks with BRAM > 0: DSP and BRAM both exact (%)"]
    assert (nonzero["n"], float(nonzero["value"])) == ("11", 100.0)
    mem = rows["channel memories: BRAM, LUT and FF exact (%)"]
    assert (mem["n"], float(mem["value"])) == ("134", 100.0)
    assert float(rows["channel memories with BRAM > 0 (count)"]["value"]) == 124


def test_results_files_name_their_tool_version():
    """Rules 10: every results file of Phase 5 records the tool version it was measured with."""
    import json

    for name in (
        "hw_builds",
        "hw_modules",
        "hw_cycles",
        "model_validation",
        "model_validation_metrics",
    ):
        head = (
            (MD.PAPER_DATA / f"{name}.csv").read_text(encoding="utf-8").splitlines()[0]
        )
        assert "tool=vitis_hls 2024.1" in head, name
    for name in ("impl_check", "impl_check_modules"):
        head = (
            (MD.PAPER_DATA / f"{name}.csv").read_text(encoding="utf-8").splitlines()[0]
        )
        assert "Vivado v.2024.1" in head and "hls=vitis_hls 2024.1" in head, name
    side = json.loads(
        MD.MODEL_FILE.with_suffix(".provenance.json").read_text(encoding="utf-8")
    )
    assert side["sha256"] == FROZEN_MODEL_SHA256 and side["tool"] == "vitis_hls 2024.1"
    assert side["fit_builds"] == MD.Models.load().meta["fit_builds"] == 86
    assert side["version"] == MD.Models.load().meta["version"] == 2


# --- the implementation reality check (step 5.9) -------------------------------------------------

_EXPORT_RPT = """\
Implementation tool: Xilinx Vivado v.2024.1
Project:             probe_proj
Solution:            solution1
Device target:       xczu48dr-ffvg1517-2-e
Report date:         Sun Oct 04 12:10:39 EDT 2026

#=== Post-Implementation Resource usage ===
SLICE:            0
LUT:            340
FF:             279
DSP:             26
BRAM:             0
URAM:             0
LATCH:            0
SRL:              0
CLB:             51

#=== Final timing ===
CP required:                     4.000
CP achieved post-synthesis:      1.548
CP achieved post-implementation: 2.363
Timing met
"""


def test_implementation_report_parser():
    from examples.mimo_cg.hw.impl_check import parse_report

    got = parse_report(_EXPORT_RPT)
    assert (got["lut"], got["ff"], got["dsp"], got["bram"], got["srl"], got["clb"]) == (
        340,
        279,
        26,
        0,
        0,
        51,
    )
    assert (got["cp_required"], got["cp_post_synth"], got["cp_post_impl"]) == (
        4.0,
        1.548,
        2.363,
    )
    assert got["timing_met"] == 1 and got["tool"] == "Xilinx Vivado v.2024.1"
    assert (
        parse_report(_EXPORT_RPT.replace("Timing met", "Timing not met"))["timing_met"]
        == 0
    )
    with pytest.raises(ValueError, match="no 'dsp'"):
        parse_report(_EXPORT_RPT.replace("DSP:", "D5P:"))


def test_committed_implementation_check():
    """Five detectors implemented by Vivado 2024.1 on xczu48dr at the 4 ns target: the default
    knobs at K = 4, 8, 16 (step 5.9), a W = 8 design and the largest held-out design (step 5.9a).
    Timing is met on all five."""
    from examples.mimo_cg.hw import impl_check as I
    from examples.mimo_cg.mimo_cg import read_table

    path = I.PAPER_DATA / "impl_check.csv"
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert "part=xczu48dr-ffvg1517-2-e" in header and "period_ns=4" in header
    rows = read_table(path)
    assert [r["build"] for r in rows] == list(I.BUILDS)
    assert [r["K"] for r in rows] == ["4", "8", "16", "4", "4"]
    builds = {r["build"]: r for r in read_table(I.PAPER_DATA / "hw_builds.csv")}
    for r in rows:
        assert "Vivado v.2024.1" in r["tool"]
        assert all(
            int(r[f"csynth_{k}"]) == int(builds[r["build"]][k])
            for k in ("lut", "ff", "dsp", "bram")
        )
        assert int(r["impl_lut"]) > 0 and int(r["impl_ff"]) > 0
        assert float(r["lut_ratio"]) == pytest.approx(
            int(r["csynth_lut"]) / int(r["impl_lut"]), abs=1e-3
        )
        assert float(r["cp_required_ns"]) == 4.0
        assert r["timing_met"] == "1" and float(r["cp_post_impl_ns"]) < 4.0
        assert (
            2.9 <= float(r["lut_ratio"]) <= 4.5
        )  # csynth's LUT count is 3 to 4.4 times higher
    assert max(float(r["cp_post_impl_ns"]) for r in rows) == pytest.approx(
        3.738
    )  # the largest


def test_committed_implementation_check_per_module():
    """Per module, csynth against the implemented instance.  The rows add up to the build totals.
    DSP differs in two ways: at W = 12 the vector unit gets 4 more per lane (16 at 4 lanes, 64 at
    16); at W = 8 it gets none, but the matmul gets most of the multiplies csynth built from LUTs
    back into DSPs (32 by csynth, 60 implemented)."""
    from examples.mimo_cg.hw import impl_check as I
    from examples.mimo_cg.mimo_cg import read_table

    totals = {r["build"]: r for r in read_table(I.PAPER_DATA / "impl_check.csv")}
    rows = read_table(I.PAPER_DATA / "impl_check_modules.csv")
    extra_dsp = {
        I.BUILDS[0]: {"CgVec": 16},
        I.BUILDS[1]: {"CgVec": 16},
        I.BUILDS[2]: {"CgVec": 16},
        I.BUILDS[3]: {"CgMm": 28},  # W = 8
        I.BUILDS[4]: {"CgVec": 64},  # 16 lanes
    }
    for build in I.BUILDS:
        mine = {r["module"]: r for r in rows if r["build"] == build}
        assert set(mine) == {*MD.DETECTOR_MODULES, "integration"}
        for k in ("lut", "ff", "dsp", "bram"):
            assert sum(int(r[f"csynth_{k}"]) for r in mine.values()) == int(
                totals[build][f"csynth_{k}"]
            )
        assert sum(int(r["impl_dsp"]) for r in mine.values()) == int(
            totals[build]["impl_dsp"]
        )
        assert sum(int(r["impl_bram"]) for r in mine.values()) == int(
            totals[build]["impl_bram"]
        )
        # a LUT shared by two instances is counted in both, so the instances add up to a few more
        # (8 to 58 over these builds)
        shared = sum(int(r["impl_lut"]) for r in mine.values()) - int(
            totals[build]["impl_lut"]
        )
        assert 0 <= shared < 100
        differ = {
            m: int(r["impl_dsp"]) - int(r["csynth_dsp"])
            for m, r in mine.items()
            if r["csynth_dsp"] != r["impl_dsp"]
        }
        assert differ == extra_dsp[build], build
        # csynth's LUT estimate is closest for the channels and furthest for the matrix loader
        assert (
            float(mine["CgLoad"]["lut_ratio"]) > 19
            and 0.75 < float(mine["integration"]["lut_ratio"]) < 1.1
        )


# --- the supplementary held-out set (step 5.8a, M5 review) ---------------------------------------


def _supplement():
    from examples.mimo_cg.mimo_cg import read_table

    detail = read_table(MD.PAPER_DATA / "model_validation_supplement.csv")
    metrics = {
        r["metric"]: r
        for r in read_table(MD.PAPER_DATA / "model_validation_supplement_metrics.csv")
    }
    return detail, metrics


def test_supplement_is_scored_with_the_frozen_models_and_has_no_gates(tmp_path):
    from examples.mimo_cg.hw import validate as V
    from examples.mimo_cg.hw.space import read_supplement

    V.validate(out_dir=tmp_path, role="supplement")
    for name in (
        "model_validation_supplement.csv",
        "model_validation_supplement_metrics.csv",
    ):
        assert (tmp_path / name).read_bytes() == (MD.PAPER_DATA / name).read_bytes()
        head = (MD.PAPER_DATA / name).read_text(encoding="utf-8").splitlines()[0]
        assert (
            f"model_sha256={FROZEN_MODEL_SHA256[:16]}" in head
            and "role=supplement" in head
        )
    detail, metrics = _supplement()
    assert {r["build"] for r in detail} == {b for b, _t, _r, _c in read_supplement()}
    assert all(r["threshold"] == "" and r["pass"] == "" for r in metrics.values())
    # Disjoint from the first calibration round by construction.  The second round (step 6.1)
    # repeats the first design's corners at 2 lanes, and one of them is this detector's matmul;
    # the table gives the metrics without it too.
    assert V.overlapping(role="supplement") == {"det_k16_l2_r16_c16_m4_w8g0_d32_s4_q4"}
    assert metrics["disjoint from fit: designs LUT MAPE (%)"]["n"] == "1"


def test_supplement_confirms_the_resource_models_at_k16():
    """K = 16 vector units, a 16-lane matmul, an 8-row matmul and two K = 16 detectors: DSP and
    BRAM exact everywhere, and the detectors' LUT and FF within the AC5 bounds."""
    detail, metrics = _supplement()
    blocks = metrics["blocks: DSP and BRAM both exact (%)"]
    assert (blocks["n"], float(blocks["value"])) == ("6", 100.0)
    mem = metrics["channel memories: BRAM, LUT and FF exact (%)"]
    assert (mem["n"], float(mem["value"])) == ("24", 100.0)
    assert float(metrics["designs: DSP exact (%)"]["value"]) == 100.0
    assert float(metrics["designs: BRAM exact (%)"]["value"]) == 100.0
    assert float(metrics["designs: LUT MAPE (%)"]["worst"]) < 2.0
    assert float(metrics["designs: FF MAPE (%)"]["worst"]) < 7.0
    designs = [r for r in detail if r["scope"] == "design" and r["quantity"] == "lut"]
    assert sorted(int(r["measured"]) for r in designs) == [80124, 100104]
    # the 16-lane matmul: v1's LUT model, calibrated at 1, 4 and 8 lanes, was 15% low here; this
    # build is what prompted version 2, which is 4.3% low
    lut = {
        r["build"]: float(r["error_pct"])
        for r in detail
        if r["scope"] == "block:CgMm" and r["quantity"] == "lut"
    }
    assert -5.0 < lut["mm_k16_r2_c32_m4_w14_l16"] < -4.0
    assert abs(lut["mm_k16_r8_c4_m4_w16_l1"]) < 3.0


def test_supplement_finds_the_memory_bound_regime_the_cycle_model_lacks():
    """The vector unit's spans hold at K = 16 (within 0.5% on the unit builds).  Job time does not,
    on one detector: with 16 lanes an iteration is so short that a short job is limited by loading
    its matrices through 32-bit memory words, and the interval between completions then depends on
    the jobs around it, not on nit alone.  The model has no term for that; since gate 6.0 the DSE
    guards against it instead (``dse.GUARD``).

    The matmul's span is where version 2 is worse than version 1 on this set: 4.4% (23 cycles) slow
    on the two-row, 32-column, 16-lane build, which v1 had within 0.5%.  v2 fits one overhead per
    tile that grows with log2 of the columns whenever the array has more than one row; at two rows
    it does not grow.  The models were frozen before this was scored, so it stays as measured.
    """
    detail, metrics = _supplement()
    for q in ("vec.iter", "vec.init"):
        assert float(metrics[f"unit builds: {q} span MAPE (%)"]["worst"]) < 0.5
    assert 4.0 < float(metrics["unit builds: mm.iter span MAPE (%)"]["worst"]) < 5.0
    jobs = {}
    for r in detail:
        if r["quantity"].startswith("job_cycles"):
            jobs.setdefault(r["build"], []).append(float(r["error_pct"]))
    fast, slow = (
        "det_k16_l16_r8_c16_m3_w10g0_d32_s3_q2",
        "det_k16_l2_r16_c16_m4_w8g0_d32_s4_q4",
    )
    assert (
        max(abs(e) for e in jobs[slow]) < 0.1
    )  # the loop is the bottleneck: the model holds
    assert 14.0 < max(abs(e) for e in jobs[fast]) < 16.0  # memory-bound short jobs
    # the large errors are all one way: the model is too fast (a negative error), and never more
    # than 2.4% too slow
    assert all(e < 2.5 for e in jobs[fast])
    assert sorted(jobs[fast])[1] < -4.0 < sorted(jobs[fast])[2]
    # the same nit takes two different times in that run
    cycles = _committed_cycles(fast)
    assert len(cycles[2]) == 2 and max(cycles[2]) - min(cycles[2]) > 200


def test_second_held_out_set_judges_the_refit(tmp_path):
    """Step 6.1's exit: six fresh matmul builds at 2 and 16 lanes, drawn before the second
    calibration round ran and built only after version 2 was frozen.  The matmul LUT error is at
    most 10% on average (it is 1.7%, and 4.5% at worst); DSP and BRAM are exact on all six.
    """
    from examples.mimo_cg.hw import validate as V
    from examples.mimo_cg.hw.space import read_v2
    from examples.mimo_cg.mimo_cg import read_table

    V.validate(out_dir=tmp_path, role="supplement2")
    names = (
        "model_validation_supplement2.csv",
        "model_validation_supplement2_metrics.csv",
    )
    for name in names:
        assert (tmp_path / name).read_bytes() == (MD.PAPER_DATA / name).read_bytes()
        head = (MD.PAPER_DATA / name).read_text(encoding="utf-8").splitlines()[0]
        assert f"model_sha256={FROZEN_MODEL_SHA256[:16]}" in head
        assert "role=supplement2" in head and "tool=vitis_hls 2024.1" in head
    detail = read_table(MD.PAPER_DATA / names[0])
    metrics = {r["metric"]: r for r in read_table(MD.PAPER_DATA / names[1])}
    held = {b for b, _t, role, _c in read_v2() if role == "supplement2"}
    assert {r["build"] for r in detail} == held and len(held) == 6
    assert V.overlapping(role="supplement2") == set()
    assert all(
        r["threshold"] == "" for r in metrics.values()
    )  # evidence, not an AC5 gate
    assert not any(m.startswith("designs:") for m in metrics)  # unit builds only

    lut = metrics["CgMm: LUT MAPE (%)"]
    assert lut["n"] == "6" and float(lut["value"]) <= 10.0  # the step's exit condition
    assert float(lut["value"]) < 2.0 and float(lut["worst"]) < 5.0
    blocks = metrics["blocks: DSP and BRAM both exact (%)"]
    assert (blocks["n"], float(blocks["value"])) == ("6", 100.0)
    assert float(metrics["CgMm: FF MAPE (%)"]["worst"]) < 7.0
    assert float(metrics["unit builds: mm.iter span MAPE (%)"]["worst"]) < 2.5
    # both lane counts, each within 5%
    by_lane = {}
    for r in detail:
        if r["quantity"] == "lut":
            lanes = int(r["build"].rsplit("_l", 1)[1])
            by_lane.setdefault(lanes, []).append(abs(float(r["error_pct"])))
    assert set(by_lane) == {2, 16} and all(max(e) < 5.0 for e in by_lane.values())


def _committed_cycles(build: str) -> dict:
    from examples.mimo_cg.mimo_cg import read_table

    out: dict = {}
    for r in read_table(MD.PAPER_DATA / "hw_cycles.csv"):
        if r["build"] == build and r["quantity"] == "job_interval":
            out.setdefault(int(r["nit"]), []).append(int(float(r["cycles"])))
    return out
