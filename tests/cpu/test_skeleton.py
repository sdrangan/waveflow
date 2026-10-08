"""Step 3 of ``plans/cpu_model.md``: one task on one core charges exactly ``switch + cycles``."""

from __future__ import annotations

import pytest

from waveflow.calib.calib import LinCalibModel
from waveflow.cpu import CpuConfig, Processor, SwFunction
from waveflow.simulation.simulation import Simulation


def test_one_task_charges_switch_plus_cycles(make_run):
    from tests.cpu.conftest import fixed

    run = make_run(switch_cycles=7).submit(0, fixed("a", 100)).run()
    t_end, result = run.results["a"]
    assert t_end == 107
    assert result == "a"
    rec = run.rec("a")
    assert (rec.t_submit, rec.t_start, rec.t_end) == (0, 0, 107)
    assert (rec.cycles, rec.switch_cycles, rec.busy_s, rec.wait_s) == (100, 7, 107, 0)
    assert rec.core == 0


def test_cycles_follow_the_clock():
    sim = Simulation()
    cpu = Processor(
        name="cpu", sim=sim, config=CpuConfig(f_clk_hz=1.2e9, switch_cycles=0)
    )
    done = {}

    def proc():
        yield from cpu.compute(1200)
        done["t"] = sim.env.now

    sim.env.process(proc())
    sim.env.run()
    assert done["t"] == pytest.approx(1e-6)


def test_the_body_runs_once_at_grant_and_the_result_waits_for_the_charge():
    sim = Simulation()
    cpu = Processor(name="cpu", sim=sim, config=CpuConfig(f_clk_hz=1.0))
    calls = []

    def body(x):
        calls.append(sim.env.now)
        return x * 2, {"n": x}

    model = LinCalibModel(
        basis=["n"], target="cycles", seed={"coeffs": [3.0], "intercept": 10.0}
    )
    model.default_model()
    func = SwFunction(name="double", fn=body, cycles=model)
    seen = {}

    def proc():
        r = yield from cpu.execute(func, 5)
        seen["r"], seen["t"] = r, sim.env.now

    sim.env.process(proc())
    sim.env.run()
    assert calls == [0]
    assert seen == {"r": 10, "t": 25.0}  # 10 + 3*5 cycles at 1 Hz
    assert cpu.records[0].feats == {"n": 5}


def test_a_negative_prediction_is_clamped_to_zero():
    sim = Simulation()
    cpu = Processor(name="cpu", sim=sim, config=CpuConfig(f_clk_hz=1.0))
    model = LinCalibModel(
        basis=["n"], target="cycles", seed={"coeffs": [-1.0], "intercept": 0.0}
    )
    model.default_model()

    def proc():
        yield from cpu.execute(
            SwFunction(name="neg", fn=lambda: (None, {"n": 4}), cycles=model)
        )

    sim.env.process(proc())
    sim.env.run()
    assert cpu.records[0].cycles == 0.0


@pytest.mark.parametrize("bad", [{"n_cores": 0}, {"f_clk_hz": 0}, {"l2_bytes": 0}])
def test_config_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        CpuConfig(**bad)
