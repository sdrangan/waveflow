"""The free-running poly (``examples/stream_kernel``, ``plans/unit_harness.md`` Stages 1-2).

pysim against the golden (headers, results and statuses; one command ending early, fatal), the timed phases it
records, and the RTL gate: the kernel under XSI between a stream driver and two sinks, its recordings
word for word against pysim's.  The testbench is not written anywhere: it is the kernel's **harness**,
generated from its boundary (:func:`~waveflow.hw.harness.harness`).  The RTL is the first in the system
flow with vendor cores (the float add and multiply are ``floating_point`` IP, ``waveflow.build.hls_ip``)
-- a wrong core or a clock too fast for its model shows here as wrong results, not as an error.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.stream_kernel.stream_kernel import (
    Cmd,
    PolyKernel,
    Resp,
    Status,
    StatusCode,
    W,
    default_cmds,
    expected,
    stimulus,
)
from examples.stream_kernel.stream_kernel_build import HERE, TOP, build_dag
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run
from waveflow.hw.harness import harness


def _run(workload="test"):
    """The kernel's harness, run in pysim: ``(system, m_out bursts, m_status bursts)``."""
    sysm = harness(PolyKernel, workload=workload)
    sysm.run()
    words = {p: [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[p].words]
             for p in ("m_out", "m_status")}
    return sysm, words["m_out"], words["m_status"]


def _statuses(bursts) -> list[Status]:
    return [Status().deserialize(np.asarray(b, dtype=np.uint64), word_bw=W) for b in bursts]


def test_the_harness_is_generated_from_the_boundary():
    """A driver on the input, a sink on each output -- Stage 1's testbench, written by no one -- and the
    D12 rules hold: the DUT is a root of its simulation, and every participant lowers."""
    sysm = harness(PolyKernel)
    assert sorted(sysm.stand_ins) == ["m_out", "m_status", "s_in"]
    assert getattr(sysm.dut(), "parent", None) is None
    assert {type(p).__name__ for p in sysm.participants()} == {"StreamDriver", "StreamSink"}


def test_pysim_matches_the_golden():
    """Every burst on both outputs, bit for bit: per command a header and its results on ``m_out``, a
    status on ``m_status`` -- up to and including the command that ends early, and nothing after."""
    _sysm, got_out, got_st = _run()
    want_out, want_st = expected(default_cmds())
    assert len(got_out) == len(want_out) and len(got_st) == len(want_st)
    for got, want in zip(got_out + got_st, want_out + want_st):
        np.testing.assert_array_equal(got, want)


def test_an_early_tlast_is_fatal():
    """The command whose samples end early: its header announces what was asked, its results burst
    holds what arrived, closed short; its status is TLAST_EARLY -- and the command after it is never
    answered, on either stream (``plans/guide_system_first.md`` D9)."""
    _sysm, got_out, got_st = _run()
    cmds = default_cmds()
    bad = next(i for i, c in enumerate(cmds) if c.n_sent < c.nsamp)
    assert bad < len(cmds) - 1                      # a command follows it, to be ignored
    st = _statuses(got_st)
    assert [int(s.tx_id) for s in st] == [c.tx_id for c in cmds[: bad + 1]]
    assert [StatusCode(int(s.status)) for s in st] == [StatusCode.OK] * bad + [StatusCode.TLAST_EARLY]
    hdr = Resp().deserialize(got_out[2 * bad], word_bw=W)
    assert int(hdr.nsamp) == cmds[bad].nsamp        # announced
    assert len(got_out[2 * bad + 1]) == cmds[bad].n_sent    # delivered: one float32 a word
    assert len(got_out) == 2 * (bad + 1)


def test_timed_phases_record_every_command_answered():
    """Both declared phases record once per command the kernel answered -- the pysim half of the
    calibration corpus -- and the early command's sample spans are marked short (they describe the
    request, not what moved)."""
    sysm, _o, _s = _run()
    dut = sysm.dut()
    cmds = default_cmds()
    bad = next(i for i, c in enumerate(cmds) if c.n_sent < c.nsamp)
    n = bad + 1
    samp = dut.samp_phase.records
    assert len(samp) == 2 * n                       # latency and fill, per command
    assert len(dut.status_phase.records) == n
    assert {r.occurrence for r in samp if r.short} == {bad}


def test_one_command_of_one_sample():
    """The degenerate command (one sample, one word) still closes with TLAST on its result -- a
    one-off scenario handed to the harness as its stimulus."""
    cmds = [Cmd(tx_id=1, samples=np.array([0.5], np.float32),
                coeffs=np.array([1, 2, 3, 4], np.float32))]
    _sysm, got_out, got_st = _run(workload={"s_in": stimulus(cmds)})
    want_out, want_st = expected(cmds)
    for got, want in zip(got_out + got_st, want_out + want_st):
        np.testing.assert_array_equal(got, want)


def test_the_calibration_workload_comes_from_the_kernel():
    """D10: the kernel names its calibration workload and its held-out lengths; the harness turns them
    into the calibrating system."""
    wl = harness(PolyKernel).calibration_workload()
    assert wl is not None and wl.holdout == {"nwords": (48, 128)}
    assert len(wl.system.dut().workload("calib")["s_in"]) == 2 * 9      # a header and samples each


@pytest.fixture(scope="module")
def rtl_run() -> Path:
    """One RTL run of the system, shared by the gates below."""
    return system_run(build_dag(work_dir=WORK), HERE, TOP)


@pytest.mark.xsi
def test_rtl_recordings_match_pysim(rtl_run):
    """THE gate: what both sinks recorded at RTL is what they recorded in pysim, word for word --
    the headers and results (float, through the vendor cores) and the statuses -- including the
    TLAST_EARLY that stops the kernel, and the silence after it."""
    assert recorded_names(rtl_run) == ["m_out", "m_status"]
    assert compared_clean(rtl_run)
