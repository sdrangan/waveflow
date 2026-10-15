"""The free-running poly (``examples/stream_kernel``, ``plans/unit_harness.md`` Stage 1).

pysim against the golden (results and responses, one command ending early), the timed phases it
records, and the RTL gate: the kernel under XSI between a stream driver and two sinks, its recordings
word for word against pysim's.  The RTL is the first in the system flow with vendor cores (the float
add and multiply are ``floating_point`` IP, ``waveflow.build.hls_ip``) -- a wrong core or a clock too
fast for its model shows here as wrong results, not as an error.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.stream_kernel.stream_kernel import (
    Cmd,
    PolyKernelSystem,
    Resp,
    Status,
    W,
    default_cmds,
)
from examples.stream_kernel.stream_kernel_build import HERE, TOP, build_dag
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run


def _resps(bursts) -> list[Resp]:
    return [Resp().deserialize(np.asarray(b, dtype=np.uint64), word_bw=W) for b in bursts]


def test_pysim_matches_the_golden():
    """Every result burst and every response, bit for bit."""
    out = PolyKernelSystem().run()
    assert len(out["results"]) == len(out["want_results"]) == len(default_cmds())
    for got, want in zip(out["results"], out["want_results"]):
        np.testing.assert_array_equal(got, want)
    for got, want in zip(out["resps"], out["want_resps"]):
        np.testing.assert_array_equal(got, want)


def test_an_early_tlast_is_answered_not_fatal():
    """The command whose samples end early is answered TLAST_EARLY with the count that arrived, and
    the commands after it run normally (no halt, no status register)."""
    sysm = PolyKernelSystem()
    out = sysm.run()
    cmds = default_cmds()
    resps = _resps(out["resps"])
    for c, r in zip(cmds, resps):
        k = c.n_sent
        assert int(r.tx_id) == c.tx_id and int(r.nsamp) == k
        assert Status(int(r.status)) == (Status.OK if k == c.nsamp else Status.TLAST_EARLY)
    assert any(Status(int(r.status)) == Status.TLAST_EARLY for r in resps)
    assert Status(int(resps[-1].status)) == Status.OK


def test_timed_phases_record_every_command():
    """Both declared phases record once per command -- the pysim half of the calibration corpus --
    and the early command's sample spans are marked short (they describe the request, not what
    moved)."""
    sysm = PolyKernelSystem()
    sysm.run()
    dut = sysm.tb.dut
    n = len(default_cmds())
    samp = dut.samp_phase.records
    assert len(samp) == 2 * n                       # latency and fill, per command
    assert len(dut.resp_phase.records) == n
    shorts = {r.occurrence for r in samp if r.short}
    early = {i for i, c in enumerate(default_cmds()) if c.n_sent < c.nsamp}
    assert shorts == early


def test_one_command_of_one_sample():
    """The degenerate command (one sample, one word) still closes with TLAST on its result."""
    sysm = PolyKernelSystem(cmds=[Cmd(tx_id=1, samples=np.array([0.5], np.float32),
                                      coeffs=np.array([1, 2, 3, 4], np.float32))])
    out = sysm.run()
    np.testing.assert_array_equal(out["results"][0], out["want_results"][0])
    np.testing.assert_array_equal(out["resps"][0], out["want_resps"][0])


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
