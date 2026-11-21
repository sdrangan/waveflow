"""The vector-multiply device in its own harness, in pysim: counts and values."""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.harness import harness
from waveflow.utils.burst_io import read_burst_bundle

from vmul import W, Vmul, VmulCode, VmulResp, decode_z, expected, scenario, vmul_golden, write_array, XElem


def test_golden_worked_examples():
    x = np.array([3, -2, -32768, 32767], np.int16)
    y = np.array([4, 5, -32768, 32767], np.int16)
    assert list(vmul_golden(x, y)) == [12, -10, 2**30, 32767 * 32767]


def test_word_counts_from_schema():
    from vmul import x_words, z_words
    assert [x_words(n) for n in (1, 4, 5, 37)] == [1, 1, 2, 10]
    assert [z_words(n) for n in (1, 2, 3, 37)] == [1, 1, 2, 19]


def _run(kind, tmp_path):
    sysm = harness(Vmul, workload=kind, n_cycles=10**6)
    sysm.write_scenario(tmp_path)
    sysm.run()
    done = [np.asarray(b, np.uint64) for b in sysm.stand_ins["m_done"].words]
    mem = np.asarray(read_burst_bundle(tmp_path / "vectors" / "mem")[0], np.uint64)
    return done, mem


def _resps(bursts):
    return [VmulResp().deserialize(b, word_bw=W) for b in bursts]


@pytest.mark.parametrize("kind", ["test", "error"])
def test_pysim_matches_golden(kind, tmp_path):
    done, mem = _run(kind, tmp_path)
    want_resp, want_mem = expected(kind)
    # one response per command up to the fatal one, and nothing after it
    assert len(done) == len(want_resp)
    for b in done:
        assert len(b) == VmulResp.nwords_per_inst(W)
    got = _resps(done)
    assert [(r.tx_id, r.n, r.code) for r in got] == [(r.tx_id, r.n, r.code) for r in want_resp]
    # the whole arena, word for word (z regions written, guards and the post-error z untouched)
    assert mem.shape[0] >= want_mem.shape[0]
    assert np.array_equal(mem[:want_mem.shape[0]], want_mem)
    jobs, _ = scenario(kind)
    for cmd, x, y in jobs[:len(want_resp)]:
        if int(cmd.n):
            assert np.array_equal(decode_z(mem, cmd), vmul_golden(x, y))


def test_error_stops_the_device(tmp_path):
    done, mem = _run("error", tmp_path)
    got = _resps(done)
    assert got[-1].code == VmulCode.BAD_LEN and len(got) == 3
    jobs, image = scenario("error")
    last = jobs[3][0]
    za = int(last.z_addr)
    assert not mem[za:za + 4].any()          # the job after the error never ran
