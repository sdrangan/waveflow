"""Hostile stand-in policies (``plans/unit_harness.md`` Stage 5): the stall pattern, how a policy lands on
each kind of stand-in, and that pysim under a policy still produces the polite run's words."""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.simulation.policy import POLICIES, StandInPolicy, stall_cycles, stalled


def test_the_stall_pattern_is_deterministic_and_about_its_percent():
    a = [stalled(7, c, 30) for c in range(20000)]
    assert a == [stalled(7, c, 30) for c in range(20000)]
    assert 0.28 < sum(a) / len(a) < 0.32
    assert not any(stalled(7, c, 0) for c in range(1000))
    assert a != [stalled(8, c, 30) for c in range(20000)]


def test_the_stall_pattern_matches_the_cpp_hash():
    """Spot values of the C++ wfbfm::policy_stalled, computed by hand from its definition."""
    def cpp(seed, cycle, pct):
        m = 0xFFFFFFFF
        x = ((seed * 2654435761) ^ (cycle * 2246822519)) & m
        x ^= x >> 15; x = (x * 2654435761) & m
        x ^= x >> 13; x = (x * 3266489917) & m
        x ^= x >> 16
        return x % 100 < pct
    assert all(stalled(s, c, p) == cpp(s, c, p) for s in (1, 7) for c in range(500) for p in (10, 90))


def test_stall_cycles_is_the_average_stall():
    assert stall_cycles(16, 0) == 0
    assert stall_cycles(16, 50) == 16
    assert stall_cycles(10, 90) == 90


def test_a_policy_lands_by_role():
    from waveflow.simulation.mm_tb import MmScript
    from waveflow.simulation.simulation import Simulation
    from waveflow.simulation.stream_tb import StreamSink

    sim = Simulation()
    sink, master = StreamSink(name="k", sim=sim), MmScript(name="m", sim=sim)
    p = StandInPolicy("both", stall_pct=40, answer_pct=80, seed=5)
    p.apply(sink)
    p.apply(master)
    assert (sink.stall_pct, master.stall_pct, sink.stall_seed) == (40, 80, 5)
    assert not StandInPolicy("only_answers", answer_pct=50).touches([sink])
    assert StandInPolicy("only_answers", answer_pct=50).touches([master])


def test_bad_policies_are_refused():
    with pytest.raises(ValueError):
        StandInPolicy("x", stall_pct=100)
    with pytest.raises(ValueError):
        StandInPolicy("x", credit_delay=-1)


@pytest.mark.parametrize("policy", POLICIES, ids=[p.name for p in POLICIES])
def test_markov_generator_in_pysim_keeps_its_words_under_each_policy(policy):
    from examples.markov.markov import MarkovGen
    from waveflow.hw.harness import harness

    polite = harness(MarkovGen)
    polite.run()
    hostile = harness(MarkovGen, policy=policy)
    hostile.run()
    got = np.concatenate([np.asarray(b) for b in hostile.stand_ins["m_u"].words])
    want = np.concatenate([np.asarray(b) for b in polite.stand_ins["m_u"].words])
    np.testing.assert_array_equal(got, want)
