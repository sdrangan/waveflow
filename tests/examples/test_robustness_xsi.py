"""Every harness under every hostile stand-in policy that touches it (``plans/unit_harness.md`` Stage 5).

The RTL runs with its stand-ins misbehaving -- sinks dropping TREADY, drivers holding words off, credit
reports held back, a bus master taking its answers late -- and must still finish with exactly the words
pysim (the functional reference) records.  A hang shows as a recording cut short; a protocol slip as
different words.  Each run's policies are confirmed to bite in ``plans/unit_harness.md``'s Stage 5
record (the runs take 2-6x their polite cycles).
"""
from __future__ import annotations

import pytest

from tests.examples._system_run import WORK, compared_clean, system_run

CASES = [
    ("stream_kernel", "rob_stall_", "stream_kernel_top"),
    ("markov", "ugen_rob_stall_", "markov_gen_unit"),
    ("markov", "ugen_rob_starved_credit_", "markov_gen_unit"),
    ("markov", "ucore_rob_stall_", "markov_core_unit"),
    ("markov", "umem_rob_late_answers_", "markov_mem_unit"),
]


def _dag(example: str):
    if example == "markov":
        from examples.markov.markov_build import HERE, build_dag
    else:
        from examples.stream_kernel.stream_kernel_build import HERE, build_dag
    return build_dag(work_dir=WORK), HERE


@pytest.mark.xsi
@pytest.mark.parametrize("example,prefix,top", CASES, ids=[c[1].rstrip("_") for c in CASES])
def test_rtl_survives_hostile_stand_ins(example, prefix, top):
    dag, here = _dag(example)
    work = system_run(dag, here, f"{prefix}{top}", through=f"{prefix}compare")
    assert compared_clean(work)


def test_every_unit_harness_has_a_policy_that_touches_it():
    """The sweep is not empty anywhere: each harness's stand-ins can be made hostile."""
    from examples.markov.markov import ChainCore, MarkovGen, markov_memory, mem_script
    from examples.stream_kernel.stream_kernel import PolyKernel
    from waveflow.hw.harness import harness, memory_harness
    from waveflow.simulation.policy import POLICIES

    units = [harness(PolyKernel), harness(MarkovGen), harness(ChainCore),
             memory_harness(markov_memory, mem_script("test"))]
    for u in units:
        assert any(p.touches(u.stand_ins.values()) for p in POLICIES), u.dut().name
