"""The free-running poly (``examples/stream_kernel``, ``plans/unit_harness.md`` Stages 1-2).

pysim against the golden (results and responses, one command ending early), the timed phases it
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
    W,
    default_cmds,
    expected,
    stimulus,
)
from examples.stream_kernel.stream_kernel_build import HERE, TOP, build_dag
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run
from waveflow.hw.harness import harness


def _run(workload="test"):
    """The kernel's harness, run in pysim: ``(system, results bursts, response bursts)``."""
    sysm = harness(PolyKernel, workload=workload)
    sysm.run()
    words = {p: [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[p].words]
             for p in ("m_out", "m_resp")}
    return sysm, words["m_out"], words["m_resp"]


def _resps(bursts) -> list[Resp]:
    return [Resp().deserialize(np.asarray(b, dtype=np.uint64), word_bw=W) for b in bursts]


def test_the_harness_is_generated_from_the_boundary():
    """A driver on the input, a sink on each output -- Stage 1's testbench, written by no one -- and the
    D12 rules hold: the DUT is a root of its simulation, and every participant lowers."""
    sysm = harness(PolyKernel)
    assert sorted(sysm.stand_ins) == ["m_out", "m_resp", "s_in"]
    assert getattr(sysm.dut(), "parent", None) is None
    assert {type(p).__name__ for p in sysm.participants()} == {"StreamDriver", "StreamSink"}


def test_pysim_matches_the_golden():
    """Every result burst and every response, bit for bit."""
    _sysm, got_y, got_r = _run()
    want_y, want_r = expected(default_cmds())
    assert len(got_y) == len(want_y) == len(default_cmds())
    for got, want in zip(got_y, want_y):
        np.testing.assert_array_equal(got, want)
    for got, want in zip(got_r, want_r):
        np.testing.assert_array_equal(got, want)


def test_an_early_tlast_is_answered_not_fatal():
    """The command whose samples end early is answered TLAST_EARLY with the count that arrived, and
    the commands after it run normally (no halt, no status register)."""
    _sysm, _y, got_r = _run()
    resps = _resps(got_r)
    for c, r in zip(default_cmds(), resps):
        k = c.n_sent
        assert int(r.tx_id) == c.tx_id and int(r.nsamp) == k
        assert Status(int(r.status)) == (Status.OK if k == c.nsamp else Status.TLAST_EARLY)
    assert any(Status(int(r.status)) == Status.TLAST_EARLY for r in resps)
    assert Status(int(resps[-1].status)) == Status.OK


def test_timed_phases_record_every_command():
    """Both declared phases record once per command -- the pysim half of the calibration corpus --
    and the early command's sample spans are marked short (they describe the request, not what
    moved)."""
    sysm, _y, _r = _run()
    dut = sysm.dut()
    n = len(default_cmds())
    samp = dut.samp_phase.records
    assert len(samp) == 2 * n                       # latency and fill, per command
    assert len(dut.resp_phase.records) == n
    shorts = {r.occurrence for r in samp if r.short}
    early = {i for i, c in enumerate(default_cmds()) if c.n_sent < c.nsamp}
    assert shorts == early


def test_one_command_of_one_sample():
    """The degenerate command (one sample, one word) still closes with TLAST on its result -- a
    one-off scenario handed to the harness as its stimulus."""
    cmds = [Cmd(tx_id=1, samples=np.array([0.5], np.float32),
                coeffs=np.array([1, 2, 3, 4], np.float32))]
    _sysm, got_y, got_r = _run(workload={"s_in": stimulus(cmds)})
    want_y, want_r = expected(cmds)
    np.testing.assert_array_equal(got_y[0], want_y[0])
    np.testing.assert_array_equal(got_r[0], want_r[0])


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
    the results (float, through the vendor cores) and the responses (including TLAST_EARLY)."""
    assert recorded_names(rtl_run) == ["m_out", "m_resp"]
    assert compared_clean(rtl_run)
