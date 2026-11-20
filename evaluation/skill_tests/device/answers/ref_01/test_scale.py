"""The offload, bottom-up (pysim): the kernel alone, the device alone, then the device on the bus with
its host.  Every test counts first -- a device that stops early must fail here, not look finished."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scale_dev import OUT_BASE, ScaleCore, ScaleDev, expected_status, scenario  # noqa: E402
from scale_msgs import DW, I16, Code, nwords, scale  # noqa: E402
from scale_system import scale_system  # noqa: E402
from waveflow.hw.arrayutils import read_array  # noqa: E402
from waveflow.hw.harness import MEM_BUNDLE, harness  # noqa: E402
from waveflow.hw.mem_stream import MemRCmd, MemWCmd  # noqa: E402
from waveflow.utils.burst_io import read_burst_bundle  # noqa: E402


def _bursts(sysm, port):
    return [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[port].words]


@pytest.mark.parametrize("kind", ["test", "too_long"])
def test_the_core_alone_asks_for_each_input_and_frames_each_write(kind):
    """Per good job: one MemRCmd out; three bursts to the writer -- MemWCmd(fwd_bursts=1), the status,
    the output.  The error: MemWCmd(len=0) and the status, then nothing."""
    sysm = harness(ScaleCore, workload=kind)
    sysm.run()
    jobs = scenario(kind)
    want_st = expected_status(jobs)
    good = [j for j, w in zip(jobs, want_st) if w.code == Code.OK]
    rd = [MemRCmd().deserialize(b, word_bw=DW) for b in _bursts(sysm, "m_rd")]
    assert [(r.addr, r.len, r.fwd_bursts) for r in rd] == [(j.src, nwords(j.n), 0) for j in good]

    wr = _bursts(sysm, "m_wr")
    n_err = int(want_st[-1].code != Code.OK)
    assert len(wr) == 3 * len(good) + 2 * n_err                     # the count first
    for k, j in enumerate(good):
        cmd = MemWCmd().deserialize(wr[3 * k], word_bw=DW)
        assert (cmd.addr, cmd.len, cmd.fwd_bursts) == (j.dst, nwords(j.n), 1)
        np.testing.assert_array_equal(wr[3 * k + 1], want_st[k].serialize(word_bw=DW))
        y = read_array(wr[3 * k + 2], I16, word_bw=DW, shape=j.n).val
        np.testing.assert_array_equal(y, scale(j.x, j.gain, j.shift)[0])
    if n_err:
        cmd = MemWCmd().deserialize(wr[-2], word_bw=DW)
        assert (cmd.len, cmd.fwd_bursts) == (0, 1)
        np.testing.assert_array_equal(wr[-1], want_st[-1].serialize(word_bw=DW))


@pytest.mark.parametrize("kind", ["test", "too_long"])
def test_the_device_alone_writes_every_output_then_its_status(kind, tmp_path):
    sysm = harness(ScaleDev, workload=kind)
    sysm.write_scenario(tmp_path)
    sysm.run()
    jobs = scenario(kind)
    want = expected_status(jobs)
    got = _bursts(sysm, "m_status")
    assert len(got) == len(want)                                       # the count first
    for g, w in zip(got, want):
        np.testing.assert_array_equal(g, w.serialize(word_bw=DW))

    arena = read_burst_bundle(tmp_path / MEM_BUNDLE)[0]
    written = 0
    for j, st in zip(jobs, want):
        if st.code != Code.OK:
            continue
        w = arena[j.dst:j.dst + nwords(j.n)]
        lanes = read_array(w, I16, word_bw=DW, shape=4 * len(w)).val
        np.testing.assert_array_equal(lanes[:j.n], scale(j.x, j.gain, j.shift)[0])
        assert not lanes[j.n:].any()                     # the last word's spare lanes: zero
        written += len(w)
    assert not arena[OUT_BASE + written:].any()          # nothing written past the outputs


@pytest.mark.parametrize("kind", ["test", "too_long"])
def test_the_host_on_the_bus_gets_every_status_and_output(kind):
    sysm = scale_system(kind)
    status, out = sysm.run()
    want = expected_status(sysm.jobs)
    assert [(s.tx_id, s.code, s.nsat) for s in status] == [(w.tx_id, w.code, w.nsat) for w in want]
    ok = [j for j, w in zip(sysm.jobs, want) if w.code == Code.OK]
    assert sorted(out) == [j.tx_id for j in ok]
    for j in ok:
        np.testing.assert_array_equal(out[j.tx_id], scale(j.x, j.gain, j.shift)[0])
