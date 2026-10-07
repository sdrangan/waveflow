"""Step 8.4's study tooling (``examples/mimo_cg/hw/cg_cal.py``): the pre-registered builds."""

from __future__ import annotations

from collections import Counter

from examples.mimo_cg.hw import cg_cal as CC
from tests.linalg import _cg_unit_bench as UB
from waveflow.linalg.message import Status


def test_space_and_split():
    space = CC.space()
    assert all(CC.valid(c) for c in space)
    fit, hold = CC.fit_set(), CC.holdout_set()
    assert len(fit) == len(set(fit)) == 31
    assert len(hold) == CC.N_HOLDOUT == 12 and not set(fit) & set(hold)
    assert set(hold) <= set(space) and CC.CENTRE in fit
    assert sum(c.stress for c in fit) == 2 and not any(c.stress for c in hold)
    assert {c.word for c in fit} == {32, 64}


def test_split_file_is_the_rule():
    assert CC.SPLIT.read_text(encoding="utf-8") == CC.split_text()
    roles = CC.load_split()
    assert roles["fit"] == CC.fit_set() and roles["holdout"] == CC.holdout_set()


def test_scenarios_measure_every_context():
    """Every build serves its four job shapes twice and rejects requests both right after a
    served one (once inside a job) and after another rejection."""
    for c in CC.fit_set() + CC.holdout_set():
        sim = UB.CgUnitBenchSim(c.unit(), CC.scenario(c))
        st = sim.statuses
        assert Counter(s.name for s in st) == {
            "OK": len(st) - 7,
            "BAD_OP": 7,
        }, c.name
        ctx = Counter(
            (st[i] == Status.OK, st[i - 1] == Status.OK) for i in range(1, len(st))
        )
        assert ctx[(False, True)] == 3 and ctx[(False, False)] == 4, (c.name, ctx)
        assert ctx[(True, True)] >= 30, (c.name, ctx)
