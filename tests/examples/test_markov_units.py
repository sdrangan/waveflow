"""markov's kernels in their own harnesses (``plans/unit_harness.md`` Stage 4): each against the
generic credit-stream stand-ins (``waveflow/simulation/credit_tb.py``), generated from its boundary.

pysim: the generator's output, as its credit sink takes it, is exactly what the chain core's harness
plays -- the two workloads describe one stream -- and the chain core answers each job.  At RTL: the
generator alone against the C++ ``CreditSink`` and the chain core alone against the
C++ ``CreditSource`` each record what pysim does.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.markov.markov import (
    CHUNK,
    ChainCore,
    MarkovGen,
    chain_stimulus,
    workload_jobs,
)
from examples.markov.markov_build import HERE, build_dag
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run
from waveflow.hw.harness import harness


def test_the_generator_harness_produces_what_the_core_harness_plays():
    sysm = harness(MarkovGen)
    sysm.run()
    got = [np.asarray(b, np.uint64) for b in sysm.stand_ins["m_u"].words]
    want = [b.words for b in chain_stimulus(workload_jobs("test"))]
    assert len(got) == len(want)
    for g, w in zip(got, want):
        np.testing.assert_array_equal(g, w)
    assert {type(p).__name__ for p in sysm.participants()} == {"StreamDriver", "CreditSink"}


def test_the_core_harness_answers_every_job():
    """Per job: a [MemWCmd | x] pair per chunk, then [MemWCmd(len=0) | MkvResp] -- and the credit
    source never overran the window (it waited for room instead)."""
    sysm = harness(ChainCore)
    sysm.run()
    jobs = workload_jobs("test")
    nchunks = sum(-(-j["n"] // CHUNK) for j in jobs)
    assert len(sysm.stand_ins["m_x"].words) == 2 * nchunks + 2 * len(jobs)
    assert {type(p).__name__ for p in sysm.participants()} == {"CreditSource", "StreamSink"}


@pytest.mark.parametrize("cls", [MarkovGen, ChainCore], ids=["gen", "core"])
def test_a_kernel_is_filed_and_found_under_one_identity(cls):
    """Calibrated in its harness, loaded in the system: one key in both, computed on the module as
    built (a phase loads its model before the run; a record is filed under an un-run copy's key).
    Starting a simulation does not move it: a credit link's counters and a queue view's notifying
    store are context."""
    from examples.markov.markov_build import system
    from waveflow.build.phase_calib import timed_modules
    from waveflow.calib.phases import module_identity

    key = module_identity(harness(cls).dut()).key
    sysm = system()
    (inside,) = [m for m in timed_modules(sysm) if isinstance(m, cls)]
    assert module_identity(inside).key == key
    for obj in sysm.sim._sim_objs:
        obj.pre_sim()
    assert module_identity(inside).key == key


def test_the_system_loads_both_kernels_calibrations():
    """Each kernel's phases in the system come from its harness's record in calib/, not the seed."""
    from examples.markov.markov_build import system
    from waveflow.build.phase_calib import timed_modules

    for m in timed_modules(system()):
        for ph in m.timing_phases.values():
            assert ph._stored_params() is not None, (m.name, ph.name)


def test_a_credit_port_needs_its_window_declared():
    """The window is compiled into a producer's RTL, so the harness refuses to guess it."""
    class Bare(MarkovGen):
        credit_links = {}

    with pytest.raises(ValueError, match="credit_links"):
        harness(Bare)


@pytest.fixture(scope="module")
def gen_run() -> Path:
    return system_run(build_dag(work_dir=WORK), HERE, "ugen_markov_gen_unit",
                      through="ugen_compare")


@pytest.mark.xsi
def test_rtl_generator_alone_matches_pysim(gen_run):
    """The generator at RTL against the C++ CreditSink: what the sink took is what pysim's took."""
    assert recorded_names(gen_run) == ["m_u"]
    assert compared_clean(gen_run)


@pytest.fixture(scope="module")
def core_run() -> Path:
    return system_run(build_dag(work_dir=WORK), HERE, "ucore_markov_core_unit",
                      through="ucore_compare")


@pytest.mark.xsi
def test_rtl_core_alone_matches_pysim(core_run):
    """The chain core at RTL against the C++ CreditSource: its framed output is an AXI-Stream port at
    its own top (``KernelTask.framed``), and what the sink took is what pysim's took."""
    assert recorded_names(core_run) == ["m_x"]
    assert compared_clean(core_run)


# ---------------------------------------------------------------------------
# The memory alone (Stage 4c): a scripted master, the memory's slot behind a direct link
# ---------------------------------------------------------------------------

def test_the_memory_harness_reads_back_what_it_wrote():
    from examples.markov.markov import markov_memory, mem_script
    from waveflow.hw.harness import memory_harness

    sysm = memory_harness(lambda sim: markov_memory(sim, calibrated=False), mem_script("test"))
    sysm.run()
    host = sysm.stand_ins["s_mm"]
    assert host.done()
    writes = [o for o in host.ops if int(o[0]) == 1]
    reads = [r for r in host.rdata if r is not None]
    for w, r in zip(writes, reads):
        np.testing.assert_array_equal(r, w[4:])


def test_the_system_memory_loads_its_harness_calibration():
    """Measured alone, loaded in the system: one identity (contents, allocation, address placement and
    the back door are context), and the record makes the slave serve one transaction at a time."""
    from examples.markov.markov import markov_memory, mem_script
    from examples.markov.markov_build import system
    from waveflow.calib.phases import module_identity
    from waveflow.hw.harness import memory_harness

    sysm = system()
    unit = memory_harness(markov_memory, mem_script("test"))
    assert module_identity(sysm.mem).key == module_identity(unit.dut().sub_comps["mem"]).key
    sysm.mem.pre_sim()
    t = sysm.mem._mem_timing
    assert t is not None and t["serialize"] and sysm.mem.s_mm.serialize_transactions
    assert (round(t["read"]["init"]), round(t["read"]["per_word"])) == (1, 1)
    assert (round(t["write"]["init"]), round(t["write"]["per_word"])) == (1, 0)


@pytest.fixture(scope="module")
def mem_run() -> Path:
    return system_run(build_dag(work_dir=WORK), HERE, "umem_markov_mem_unit", through="umem_compare")


@pytest.mark.xsi
def test_rtl_memory_alone_is_timed_exactly_by_its_calibration(mem_run):
    """The memory at RTL against the scripted master: the read data equal pysim's, and with its
    calibrated timing loaded every op's duration equals the RTL's to the cycle."""
    from examples.markov.markov import mem_script
    from waveflow.simulation.mm_tb import timing_dir
    from waveflow.utils.burst_io import read_burst_bundle

    rtl = read_burst_bundle(timing_dir(mem_run / "traces"))
    pys = read_burst_bundle(timing_dir(mem_run / "pysim_traces"))
    ops = mem_script("test")
    assert len(rtl) == len(pys) == len(ops)
    assert [int(b[1] - b[0]) for b in pys] == [int(b[1] - b[0]) for b in rtl]
