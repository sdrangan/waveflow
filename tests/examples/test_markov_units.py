"""markov's kernels in their own harnesses (``plans/unit_harness.md`` Stage 4): each against the
generic credit-stream stand-ins (``waveflow/simulation/credit_tb.py``), generated from its boundary.

pysim: the generator's output, as its credit sink takes it, is exactly what the chain core's harness
plays -- the two workloads describe one stream -- and the chain core answers each job.  At RTL: the
generator alone, against the C++ ``CreditSink``, records what pysim does.
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
