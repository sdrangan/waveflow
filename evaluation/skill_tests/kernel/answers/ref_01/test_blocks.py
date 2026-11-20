"""Each block in its harness (pysim): every output's bursts against the golden, counted first -- a block
that stops early must fail here, not look finished."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import block_a  # noqa: E402
import block_b  # noqa: E402
import block_c  # noqa: E402
from waveflow.hw.harness import harness  # noqa: E402


def _run(cls, kind, ports):
    s = harness(cls, workload=kind)
    s.run()
    return {p: [np.asarray(b, np.uint64) for b in s.stand_ins[p].words] for p in ports}


def _same(got, want):
    assert len(got) == len(want), (len(got), len(want))              # the count first
    for g, w in zip(got, want):
        np.testing.assert_array_equal(g, w)


@pytest.mark.parametrize("kind", ["test", "bad_gain", "short"])
def test_block_a(kind):
    got = _run(block_a.ContrastStretch, kind, ("m_out", "m_status"))
    want_out, want_st = block_a.expected(block_a.scenario(kind))
    _same(got["m_out"], want_out)
    _same(got["m_status"], want_st)
    codes = [block_a.RowStatus().deserialize(b, word_bw=block_a.W).code for b in got["m_status"]]
    assert all(c == block_a.Code.OK for c in codes[:-1])
    if kind != "test":                                    # the error is the last thing answered
        assert codes[-1] != block_a.Code.OK
        assert len(codes) < len(block_a.scenario(kind))


@pytest.mark.parametrize("kind", ["test", "fault"])
def test_block_b(kind):
    sc = block_b.SCENARIOS[kind]
    fr = block_b.frames(sc["n"], short=sc["short"])
    want_rep, want_st = block_b.expected(fr, *sc["cfgs"][0])
    got = _run(block_b.EnergyMonitor, kind, ("m_report", "m_status"))
    _same(got["m_report"], [r.serialize(word_bw=block_b.W) for r in want_rep])
    _same(got["m_status"], [s.serialize(word_bw=block_b.W) for s in want_st])


def test_block_b_takes_a_new_setting_at_a_frame_boundary():
    """Every frame is reported (threshold 0); the shift changes once, between two frames: each frame's
    energy is the golden's at the old shift or the new one, and never back."""
    sc = block_b.SCENARIOS["retune"]
    fr = block_b.frames(sc["n"])
    got = _run(block_b.EnergyMonitor, "retune", ("m_report",))["m_report"]
    reps = [block_b.Report().deserialize(b, word_bw=block_b.W) for b in got]
    assert [r.frame for r in reps] == list(range(sc["n"]))
    (_, s0), (_, s1) = sc["cfgs"]
    which = []
    for r, f in zip(reps, fr):
        e0, e1 = (block_b.energy(f["re"], f["im"], s) for s in (s0, s1))
        assert r.energy in (e0, e1)
        which.append(0 if r.energy == e0 else 1)
    assert which == sorted(which) and which[-1] == 1


@pytest.mark.parametrize("kind", ["test", "too_short", "short_burst"])
def test_block_c(kind):
    got = _run(block_c.Correlator, kind, ("m_out", "m_status"))
    want_out, want_st = block_c.expected(*block_c.scenario(kind))
    _same(got["m_out"], want_out)
    _same(got["m_status"], want_st)


def test_block_c_uses_the_template_each_job_names():
    """Both templates are sent before the first job; the first two jobs still use template 1."""
    got = _run(block_c.Correlator, "test", ("m_out",))["m_out"]
    used = [block_c.JobResp().deserialize(b, word_bw=block_c.W).tmpl_id for b in got[0::2]]
    assert used == [1, 1, 2, 2]
