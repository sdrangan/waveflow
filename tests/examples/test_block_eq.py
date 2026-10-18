"""The block equalizer (``examples/block_eq``): a stream-only pipeline of three free-running modules --
the framework's ``StreamBroadcast(2)``, then ``gain`` and ``apply``.

pysim against the golden, bit for bit (signed zeros included): the worked examples, each stage alone in
its own harness, the composite, and the fatal errors (a zero noise; symbols ending early) with nothing
answered after them.  At RTL (``-m xsi``): each stage alone, the composite, and the composite on the
early-TLAST scenario -- every recording equal to pysim's.  One body serves a stage alone and inside the
composite: its port types are template parameters (``KernelTask.framed``).
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.block_eq.block_eq import (
    W,
    Apply,
    BlockEq,
    Gain,
    Status,
    StatusCode,
    apply_golden,
    default_cmds,
    early_cmds,
    expected,
    gain_part,
    link_expected,
    worked_cmd,
)
from examples.block_eq.block_eq_build import APPLY_TOP, GAIN_TOP, HERE, TOP, build_dag
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run
from waveflow.hw.harness import harness

#: The worked examples' twelve result floats, as bits (the retired spec's table, plus row 3's second symbol).
WORKED_BITS = ["3f800000", "40000000", "bf000000", "3e800000", "3ee38e39", "bee38e39",
               "bfaaaaab", "80000000", "3f7d7720", "00000000", "be4ac5b4", "becac5b4"]


def _bursts(sysm, port):
    return [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[port].words]


def _assert_bursts(got, want):
    assert len(got) == len(want)
    for g, w in zip(got, want):
        np.testing.assert_array_equal(g, w)


def test_the_golden_reproduces_the_worked_examples_bit_for_bit():
    c = worked_cmd()
    nvalid, code, gr, gi = gain_part(c)
    assert (nvalid, code) == (3, StatusCode.OK)
    rows = np.arange(6) // 2
    r = c.r.reshape(-1)
    zr, zi = apply_golden(gr[rows], gi[rows], r.real, r.imag)
    bits = [f"{int(np.float32(v).view(np.uint32)):08x}" for v in np.column_stack([zr, zi]).ravel()]
    assert bits == WORKED_BITS


@pytest.mark.parametrize("kind,cmds", [("test", default_cmds), ("early", early_cmds)])
def test_the_composite_matches_the_golden(kind, cmds):
    sysm = harness(BlockEq, workload=kind)
    sysm.run()
    want_out, want_st = expected(cmds())
    _assert_bursts(_bursts(sysm, "m_out"), want_out)
    _assert_bursts(_bursts(sysm, "m_status"), want_st)


def test_errors_are_fatal_and_nothing_follows():
    for cmds, bad_code in ((default_cmds, StatusCode.BAD_NOISE), (early_cmds, StatusCode.TLAST_EARLY)):
        _out, st = expected(cmds())
        codes = [StatusCode(int(Status().deserialize(s, word_bw=W).status)) for s in st]
        assert codes[-1] == bad_code and all(c == StatusCode.OK for c in codes[:-1])
        assert len(st) < len(cmds())                     # a command after the error is never answered


def test_gain_alone_matches_the_golden():
    sysm = harness(Gain, workload="test")
    sysm.run()
    _assert_bursts(_bursts(sysm, "m_g"), link_expected(default_cmds()))


def test_apply_alone_matches_the_golden():
    sysm = harness(Apply, workload="test")
    sysm.run()
    want_out, want_st = expected(default_cmds())
    _assert_bursts(_bursts(sysm, "m_out"), want_out)
    _assert_bursts(_bursts(sysm, "m_status"), want_st)


@pytest.fixture(scope="module")
def dag():
    return build_dag(work_dir=WORK)


@pytest.mark.xsi
@pytest.mark.parametrize("prefix,top,outs", [
    ("", TOP, ["m_out", "m_status"]),
    ("gain_", GAIN_TOP, ["m_g"]),
    ("apply_", APPLY_TOP, ["m_out", "m_status"]),
    ("early_", TOP, ["m_out", "m_status"]),
], ids=["composite", "gain", "apply", "early"])
def test_rtl_recordings_match_pysim(dag, prefix, top, outs):
    """THE gates: each stage alone, the composite, and the composite's fatal early TLAST -- every
    recording at RTL equal to pysim's, word for word and TLAST for TLAST."""
    work = system_run(dag, HERE, f"{prefix}{top}", through=f"{prefix}compare")    # a prefix names the run
    assert recorded_names(work) == outs
    assert compared_clean(work)
