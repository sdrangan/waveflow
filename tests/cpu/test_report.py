"""AC7 of ``plans/cpu_model.md`` (report part, step 6): energy is dynamic plus static, exactly."""

from __future__ import annotations

from waveflow.cpu import SwFunction


def test_total_energy_is_dynamic_plus_static_of_every_powered_core(make_run):
    # Two cores at 2 mW each, a 10 s horizon at 1 Hz: static = 2 * 2 mW * 10 s = 0.04 J = 4e10 pJ.
    # Three calls of a 100 pJ function: dynamic = 300 pJ.  Total = 4e10 + 300, exactly.
    func = SwFunction(name="f", fn=lambda: (None, {}), cycles=2, energy_pj=100.0)
    run = make_run(n_cores=2, static_power_mw=2.0)
    for i in range(3):
        run.submit(0, func, key=f"f{i}")
    run.run()
    rep = run.cpu.report(elapsed_s=10)
    assert rep.dynamic_pj == 300.0
    assert rep.static_pj == 4e10
    assert rep.total_pj == 4e10 + 300.0
    assert rep.functions["f"].energy_pj == 300.0


def test_energy_follows_the_counters(make_run):
    func = SwFunction(
        name="f",
        fn=lambda n: (None, {"n": n}),
        cycles=1,
        energy_pj=7.5,  # a fixed number: every call costs the same
    )
    run = make_run()

    def proc():
        yield from run.cpu.execute(func, 1)
        yield from run.cpu.execute(func, 9)

    run.sim.env.process(proc())
    run.run()
    assert [r.energy_pj for r in run.cpu.records] == [7.5, 7.5]
    assert run.cpu.report().static_pj == 0.0
