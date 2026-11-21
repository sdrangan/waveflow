"""The peak picker, bottom-up (pysim): each stage alone in its harness, then the composite.  Every test
counts first.  Two wrong pipelines show what the counts catch -- a deadlock looks like a run that
ended -- and what pysim cannot show: a broadcast stall behind one burst longer than a link."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import peaks as pk  # noqa: E402
from waveflow.hw.harness import harness  # noqa: E402

KINDS = ["test", "bad_len", "short"]


def _bursts(sysm, port):
    return [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[port].words]


def _same(got, want):
    assert len(got) == len(want), (len(got), len(want))          # the count first
    for g, w in zip(got, want):
        np.testing.assert_array_equal(g, w)


@pytest.mark.parametrize("kind", KINDS)
def test_stats_sends_one_token_per_job_even_for_an_error(kind):
    sysm = harness(pk.Stats, workload=kind)
    sysm.run()
    want = [np.asarray(t.serialize(word_bw=pk.W), dtype=np.uint64)
            for t in pk.tokens(pk.scenario(kind))]
    _same(_bursts(sysm, "m_thr"), want)


@pytest.mark.parametrize("cls", [pk.Detect, pk.PeakPicker])
@pytest.mark.parametrize("kind", KINDS)
def test_every_job_is_answered_and_nothing_after_an_error(cls, kind):
    sysm = harness(cls, workload=kind)
    sysm.run()
    want_out, want_status = pk.expected(pk.scenario(kind))
    _same(_bursts(sysm, "m_out"), want_out)
    _same(_bursts(sysm, "m_status"), want_status)
    codes = [pk.PkStatus().deserialize(b, word_bw=pk.W).code for b in _bursts(sysm, "m_status")]
    assert all(c == pk.Code.OK for c in codes[:-1])
    if kind != "test":
        assert codes[-1] != pk.Code.OK and len(codes) < len(pk.scenario(kind))


def test_the_scenario_has_a_job_longer_than_any_link_and_one_with_no_peak():
    jobs = pk.scenario("test")
    words = max(len(pk.stimulus([j])[1].words) for j in jobs)
    assert words > max(pk.COPY_DEPTH, pk.THR_DEPTH)
    assert any(len(pk.peaks(j.x, pk.threshold(j.x, j.mult))) == 0 for j in jobs)


# --- what the counts catch ----------------------------------------------------------------------

class SilentStats(pk.Stats):
    """Wrong: on a bad length it stops without sending a token."""

    def run_iter(self):
        cmd = yield from self.s_in.get_schema(pk.PkCmd)
        if not 1 <= int(cmd.n) <= pk.NMAX:
            yield self.event()
        x = (yield from self.s_in.get_array(pk.I16, int(cmd.n))).val
        yield from self.m_thr.write(pk.PkThr(tx_id=cmd.tx_id, thr=pk.threshold(x, cmd.mult),
                                             code=pk.Code.OK))


class SilentStatsPicker(pk.PeakPicker):
    stats_cls = SilentStats


def test_a_stage_that_sends_no_token_leaves_the_last_stage_waiting_and_the_count_shows_it():
    """The bad command is never answered: the run ends quietly, one status short."""
    sysm = harness(SilentStatsPicker, workload="bad_len")
    sysm.run()
    _out, want_status = pk.expected(pk.scenario("bad_len"))
    assert len(_bursts(sysm, "m_status")) == len(want_status) - 1


class WaitFirstDetect(pk.Detect):
    """Wrong: waits for the token before draining its copy of the samples."""

    def run_iter(self):
        cmd = yield from self.s_in.get_schema(pk.PkCmd)
        tok = yield from self.s_thr.get_schema(pk.PkThr)
        x = (yield from self.s_in.get_array(pk.I16, int(cmd.n))).val
        idx = pk.peaks(x, int(tok.thr))
        yield from self.m_out.write(pk.PkResp(tx_id=cmd.tx_id, count=len(idx)))
        if len(idx):
            yield from self.m_out.write_array(pk.U16, len(idx), pk.array(pk.U16, idx))
        yield from self.m_status.write(pk.PkStatus(tx_id=cmd.tx_id, code=tok.code))


class WaitFirstPicker(pk.PeakPicker):
    detect_cls = WaitFirstDetect


@pytest.mark.xfail(strict=True, reason=(
    "a Waveflow gap: pysim admits a burst longer than a link's depth once the link has room for "
    "min(burst, depth) words (interface.py, _admit_blocking), so the broadcast's lockstep stall -- "
    "512 sample words into a 16-word copy -- never happens in pysim.  At RTL, by the broadcast's "
    "lockstep rule (streams/broadcast.md), the long job hangs (not run here: no Vitis).  "
    "Strict: when pysim models it, this passes and the marker must go."))
def test_waiting_on_a_sibling_before_draining_deadlocks_and_the_count_shows_it():
    """At RTL the broadcast stalls once the waiting stage's copy is full: the long job is never
    answered, and only the count of statuses tells."""
    sysm = harness(WaitFirstPicker, workload="test")
    sysm.run()
    n = len(_bursts(sysm, "m_status"))
    assert n < len(pk.scenario("test")), n
