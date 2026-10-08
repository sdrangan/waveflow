"""AC4 of ``plans/cpu_model.md`` (seeded part, step 6): a seed never passes for a measurement.

The calibrated part — ``INTERPOLATED`` inside a fitted range, ``EXTRAPOLATED`` outside, and a fitted
pJ energy model that does not claim ``EXACT`` — is added in step 11, against the A53 platform.
"""

from __future__ import annotations

from waveflow.calib.calib import LinCalibModel
from waveflow.calib.confidence import ConfidenceLevel
from waveflow.cpu import CpuAreaModel, CpuConfig, SwFunction


def seeded(coeffs, intercept, basis=("n",), target="cycles"):
    m = LinCalibModel(
        basis=list(basis),
        target=target,
        seed={"coeffs": list(coeffs), "intercept": intercept},
    )
    return m.default_model()


def test_a_seeded_cycle_and_energy_model_report_uncalibrated(make_run):
    func = SwFunction(
        name="f",
        fn=lambda n: (None, {"n": n}),
        cycles=seeded([2.0], 5.0),
        energy_pj=seeded([10.0], 0.0, target="energy_pj"),
    )
    run = make_run()

    def proc():
        yield from run.cpu.execute(func, 3)

    run.sim.env.process(proc())
    run.run()
    rec = run.cpu.records[0]
    assert (rec.cycles, rec.energy_pj) == (11.0, 30.0)
    st = run.cpu.report().functions["f"]
    assert st.confidence.level == ConfidenceLevel.UNCALIBRATED
    assert st.energy_confidence.level == ConfidenceLevel.UNCALIBRATED
    assert st.confidence.facts.get("from_seed") is True


def test_a_function_without_an_energy_model_reports_none():
    run_func = SwFunction.fixed("g", 4)
    assert run_func.energy_pj is None


def test_area_estimates_from_numbers_are_uncalibrated():
    est = CpuAreaModel(area_mm2=1.5, leak_mw=20.0, source="seed").estimate(CpuConfig())
    assert est["area_mm2"].value == 1.5 and est["leak_mw"].value == 20.0
    assert est["area_mm2"].level == ConfidenceLevel.UNCALIBRATED
    assert est["area_mm2"].source == "seed"


def test_area_from_a_seeded_model_reads_the_configuration():
    area = seeded([0.5, 0.01], 0.2, basis=("n_cores", "l2_kb"), target="area_mm2")
    model = CpuAreaModel(area_mm2=area, leak_mw=0.0)
    est = model.estimate(CpuConfig(n_cores=4, l2_bytes=512 * 1024))
    assert est["area_mm2"].value == 0.2 + 0.5 * 4 + 0.01 * 512
    assert est["area_mm2"].level == ConfidenceLevel.UNCALIBRATED
