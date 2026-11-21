"""The ring stub (``examples/ring_stub``, ``plans/shared_kernel.md`` Stage 1): a composite with a cycle --
one unit K used twice per job, its output fed back to its own input through a mux, a tag beside K and a
credit on the loop.

pysim against the golden: K alone, Post alone, and the composite on two scenarios (more jobs than credits,
so the loop runs full).  The credit is what keeps the loop alive: without it the same composite deadlocks,
and the run still returns cleanly -- only the counts show it.  The generated top carries the cycle as two
ordinary channels at their stated depths.  At RTL (``-m xsi``): K alone, Post alone, the composite on both
scenarios -- every recording equal to pysim's.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import examples.ring_stub.ring_stub as rs
from examples.ring_stub.ring_stub import Inc, Post, RingStub, jobs
from examples.ring_stub.ring_stub_build import (
    HERE,
    INC_TOP,
    POST_TOP,
    TOP,
    build_dag,
    gen_top,
)
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run
from waveflow.hw.harness import harness


def _bursts(sysm, port):
    return [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[port].words]


def _assert_golden(sysm):
    for port, want in sysm.golden_outputs().items():
        got = _bursts(sysm, port)
        assert len(got) == len(want), port
        for g, w in zip(got, want):
            np.testing.assert_array_equal(g, np.asarray(w, dtype=np.uint64))


@pytest.mark.parametrize("kind", ["test", "long"])
def test_the_composite_matches_the_golden(kind):
    sysm = harness(RingStub, workload=kind)
    sysm.run()
    _assert_golden(sysm)
    assert len(_bursts(sysm, "m_status")) == len(jobs(kind))      # every job answered


@pytest.mark.parametrize("cls", [Inc, Post], ids=["k", "post"])
def test_each_stage_alone_matches_the_golden(cls):
    sysm = harness(cls, workload="test")
    sysm.run()
    _assert_golden(sysm)


def test_more_jobs_than_credits():
    """The scenarios put more jobs in than the loop admits, so the mux waits on credit."""
    assert len(jobs("test")) > rs.FMAX and len(jobs("long")) > rs.FMAX


def test_without_the_credit_the_loop_deadlocks_and_the_run_still_returns(monkeypatch):
    """The witness for the credit, and for asserting counts: with no effective credit and shallow loop
    links, Post blocks on the feedback link while the mux blocks on K -- a cycle of full FIFOs.  pysim
    returns as if it had finished; only the missing statuses say otherwise."""
    monkeypatch.setattr(rs, "FMAX", 100)
    monkeypatch.setattr(rs, "DEPTH", {**rs.DEPTH, "fb": 8, "k_in": 8, "k_out": 8, "tag": 2})
    sysm = harness(RingStub, workload="long")
    sysm.run()                                                     # returns: no error, no hang
    assert len(_bursts(sysm, "m_status")) < len(jobs("long"))


def test_the_generated_top_carries_the_cycle_as_channels(tmp_path: Path):
    """The generator needs nothing special for a cycle: fb and crd are channels like any other, at the
    depths the composite states, and the mux task takes them as inputs while Post writes them."""
    gen_top(RingStub, tmp_path)
    top = (tmp_path / "gen" / "ring_stub.cpp").read_text()
    for name, depth in rs.DEPTH.items():
        assert f"hls::stream<streamutils::framed_word<64> > {name};" in top
        assert f"#pragma HLS STREAM variable={name} depth={depth}" in top
    assert "s_in, fb, crd, k_in, tag);" in top                   # the mux reads the feedback
    assert "tag, k_out, fb, m_out, m_status, crd);" in top       # Post writes it


@pytest.fixture(scope="module")
def dag():
    return build_dag(work_dir=WORK)


@pytest.mark.xsi
@pytest.mark.parametrize("prefix,top,outs", [
    ("", TOP, ["m_out", "m_status"]),
    ("long_", TOP, ["m_out", "m_status"]),
    ("inc_", INC_TOP, ["m_out"]),
    ("post_", POST_TOP, ["m_crd", "m_fb", "m_out", "m_status"]),
], ids=["composite", "long", "k", "post"])
def test_rtl_recordings_match_pysim(dag, prefix, top, outs):
    """THE gates: K alone, Post alone, and the composite with its cycle on both scenarios -- every
    recording at RTL equal to pysim's, word for word and TLAST for TLAST."""
    work = system_run(dag, HERE, f"{prefix}{top}", through=f"{prefix}compare")
    assert sorted(recorded_names(work)) == sorted(outs)
    assert compared_clean(work)


@pytest.mark.xsi
@pytest.mark.parametrize("prefix,kind,last", [("", "test", 934), ("long_", "long", 6282)],
                         ids=["composite", "long"])
def test_rtl_loop_timing_is_exact(dag, prefix, kind, last):
    """The loop's timing, pinned: the cycle the last job's status leaves the composite at RTL (Vitis
    2025.1, xc7z020 at 100 MHz), every job answered.  A change to the mux, the credit or a loop depth
    moves it."""
    work = system_run(dag, HERE, f"{prefix}{TOP}", through=f"{prefix}compare")
    cyc = np.fromfile(work / "traces" / "m_status" / "cycles.bin", dtype="<u8")
    assert len(cyc) == len(jobs(kind))
    assert int(cyc[-1]) == last
