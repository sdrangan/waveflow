"""examples/ltbf_dse's DSE machinery on synthetic inputs (plan step 7.2, final review M4).

The job-level simulator's shared port (:class:`~examples.ltbf_dse.hw.jobsim.Port`), its sustained-rate
test, its slots back to back and the Ingest DMA's drops; the DSE's cost of Shared Mem as built, its
decisions and frontiers; AC13's decisions; the pre-registration's overwrite guard.  No build report,
frozen model or table is read, so nothing here skips.
"""

from __future__ import annotations

import pytest
import simpy

from examples.ltbf_dse.hw import dse, fidelity
from examples.ltbf_dse.hw import jobsim as js
from examples.ltbf_dse.hw import space as sp
from examples.ltbf_dse.hw.graphs import Dims

# -- the job-level simulator ------------------------------------------------------------------------


def _port(cycles: list[float], banks: int = 1) -> tuple[dict, float]:
    """Transfers of *cycles* started together: each one's finish (cycles) and the port's busy."""
    env = simpy.Environment()
    port = js.Port(env, 1.0, banks)
    done: dict = {}

    def run(i: int, c: float):
        yield port.transfer(c)
        done[i] = env.now

    for i, c in enumerate(cycles):
        env.process(run(i, c))
    env.run()
    return done, port.busy


def test_the_port_shares_its_one_transaction_at_a_time():
    done, busy = _port([100.0, 300.0])
    # Both at half speed until the first is done, then the second alone: the work is conserved.
    assert done == {0: pytest.approx(200.0), 1: pytest.approx(400.0)}
    assert busy == pytest.approx(400.0)


def test_the_ports_banks_serve_that_many_at_full_speed():
    done, _ = _port([100.0, 300.0], banks=2)
    assert done == {0: pytest.approx(100.0), 1: pytest.approx(300.0)}
    done, _ = _port(
        [100.0] * 3, banks=2
    )  # three on two banks: two thirds of the clock each
    assert all(t == pytest.approx(150.0) for t in done.values())


@pytest.mark.parametrize(
    "run, ok",
    [
        ({"overruns": 0, "finished": True, "oru_late": 0.0}, True),
        ({"overruns": 1, "finished": True, "oru_late": 0.0}, False),
        ({"overruns": 0, "finished": False, "oru_late": 0.0}, False),
        ({"overruns": 0, "finished": True, "oru_late": 50.0}, True),  # 5% of the period
        ({"overruns": 0, "finished": True, "oru_late": 51.0}, False),
    ],
)
def test_sustained_means_no_overrun_every_job_done_and_the_oru_on_pace(run, ok):
    assert bool(js.sustained(run, 1000.0)) is ok


SYMBOL_PORT = 777.0  # a DMA symbol's port work, distinct from every other transfer's


def _jobsim(period_cycles: float, n_slots: int = 1, job: float = 2_000.0) -> dict:
    """A small system (64 antennas, 2 UEs) on constant job times, Shared Mem's port shared."""
    timing = js.Timing(sys_job=lambda g, j: (job / 2, job / 2), vec_job=lambda g, j: (job / 2, job / 2),
                       chunk=lambda k: 100.0, proj=lambda g: 1_000.0, symbol=5_000.0, port=True,
                       symbol_port=SYMBOL_PORT)  # fmt: skip
    cfg = js.Config(
        d=Dims(n_rx=64, U=2), sym_period=period_cycles / js.F_HOST, n_slots=n_slots
    )
    return js.JobSim(cfg, timing).run()


def test_slots_run_back_to_back_at_the_orus_pace():
    period = 200_000.0
    one, three = _jobsim(period), _jobsim(period, n_slots=3)
    for r in (one, three):
        assert r["finished"] and r["overruns"] == 0
    # The last symbol of slot k arrives 14 (k + 1) periods after the long-term update.
    assert (one["end"] - one["t_lt"]) * js.F_HOST >= 14 * period
    assert (three["end"] - three["t_lt"]) * js.F_HOST >= 42 * period


def test_more_slots_never_sustain_a_faster_pace():
    for period in (10_000.0, 20_000.0, 40_000.0, 80_000.0):
        if js.sustained(_jobsim(period, n_slots=3), period):
            assert js.sustained(_jobsim(period), period), period


def test_a_dropped_symbol_charges_the_port_nothing(monkeypatch):
    sizes: list[float] = []
    transfer = js.Port.transfer

    def spy(self, cycles):
        sizes.append(cycles)
        return transfer(self, cycles)

    monkeypatch.setattr(js.Port, "transfer", spy)
    r = _jobsim(
        6_000.0, job=200_000.0
    )  # engines far slower than the pace: the ring fills
    assert r["finished"] and r["overruns"] > 0
    assert sizes.count(SYMBOL_PORT) == 14 - r["overruns"]


# -- the DSE ----------------------------------------------------------------------------------------


def _parts_cost(monkeypatch, span: int, each: float = 10.0) -> dict:
    """``dse.cost`` with every part (and no hub) at *each* of every resource, Shared Mem *span*."""
    from examples.ltbf_dse.hw import models as hm

    monkeypatch.setattr(dse, "_model", dict)
    monkeypatch.setattr(dse, "_hub", dict)
    monkeypatch.setattr(
        hm, "predict_resources", lambda m, p: dict.fromkeys(hm.COUNTERS, each)
    )
    monkeypatch.setattr(dse, "_mem", lambda n, form: (span - 1000, span))
    return dse.cost(8, 2, 4, 12, 2, "factored")


def test_shared_mem_is_costed_as_the_block_ram_vivado_builds(monkeypatch):
    c = _parts_cost(monkeypatch, 1 << 17)
    # Three parts, then 2^17 64-bit words as Vivado 2024.1 mapped them in the finalists: 228 RAMB36
    # (seven 9-bit slices of 4K x 9, one 1-bit slice of 32K x 1); no URAM.
    assert dse.shared_mem_bram18(1 << 17) == 2 * 228
    assert c["res"]["BRAM_18K"] == 3 * 10 + 2 * 228
    assert c["res"]["URAM"] == 3 * 10
    assert not c["model_only"] and c["uram_alt"] == (1 << 17) // 4096
    assert c["frac"]["max"] == max(c["res"][k] / v for k, v in dse.AVAILABLE.items())


def test_a_design_over_the_device_is_model_only(monkeypatch):
    assert dse.shared_mem_bram18(1 << 19) == 1824
    c = _parts_cost(
        monkeypatch, 1 << 19, each=120.0
    )  # 1,824 BRAM18 of Shared Mem and 360 more
    assert c["frac"]["BRAM_18K"] > 1 and c["model_only"]
    assert not _parts_cost(monkeypatch, 1 << 19, each=10.0)[
        "model_only"
    ]  # 1,854 of 2,160


def _design(cost: float, *, loss=0.05, t_lt=0.5, model_only=False, R=2) -> dict:
    return {"array": "8x8", "target": "-6_14", "R": R, "C": 4, "W": 12, "L": 2, "form": "factored",
            "t": 3, "median_loss_db": loss, "t_lt_ms": t_lt, "re_per_s": 1.0, "model_only": model_only,
            **{f"cost_{k}": cost for k in dse.COSTS}}  # fmt: skip


def _pick(rows: list[dict], lb: float, tb: float) -> dict:
    return next(d for d in dse.decisions(rows) if d["array"] == "8x8" and d["target"] == "-6_14"
                and d["loss_budget_db"] == lb and d["time_budget_ms"] == tb and d["cost_measure"] == "max")  # fmt: skip


def test_a_decision_is_the_cheapest_design_that_fits_and_meets_both_budgets():
    rows = [_design(0.1, model_only=True, R=1), _design(0.2, t_lt=6.0, R=2), _design(0.3, loss=0.2, R=4),
            _design(0.4, R=8)]  # fmt: skip
    assert (
        _pick(rows, 0.1, 5.0)["R"] == 8
    )  # over the device, over T_LT, over the loss budget
    assert _pick(rows, 0.1, 10.0)["R"] == 2
    assert _pick(rows, 0.25, 5.0)["R"] == 4
    assert "R" not in _pick(rows[:1], 0.5, 10.0)  # none fits: no pick


def test_frontiers_per_budget_hold_designs_that_fit_and_the_unbudgeted_one_flags_the_rest():
    rows = [_design(0.1, model_only=True, R=1), _design(0.2, R=2), _design(0.3, R=4)]
    f = [
        r for r in dse.frontiers(rows) if r["array"] == "8x8" and r["target"] == "-6_14"
    ]
    assert [r["R"] for r in f if r["loss_budget_db"] == "none"] == [1]
    assert [
        r["R"] for r in f if (r["loss_budget_db"], r["time_budget_ms"]) == (0.1, 1.0)
    ] == [2]


def test_ac13_never_picks_a_design_over_the_device():
    rows = [{"n": 8, "target": "-6_14", "R": R, "loss": 0.05, "cost": {"DSP": c, "LUT": c, "FF": c,
             "BRAM_18K": c, "max": c}, "ms": 0.5} for R, c in ((1, 1.2), (2, 0.4))]  # fmt: skip
    pick = fidelity.decide(rows, lambda r: r["cost"], lambda r: r["ms"])
    assert pick[8, "-6_14", 0.1, 1.0, "max"]["R"] == 2
    assert pick[8, "-6_14", 0.1, 1.0, "DSP"]["R"] == 2


# -- pre-registration ---------------------------------------------------------------------------------


def test_a_registered_file_is_never_silently_replaced(tmp_path):
    path = tmp_path / "fit.json"
    sp.guard(path, "anything", force=False)  # nothing registered yet
    path.write_text("registered")
    sp.guard(path, "registered", force=False)  # the same bytes again
    with pytest.raises(FileExistsError):
        sp.guard(path, "changed", force=False)
    sp.guard(path, "changed", force=True)
