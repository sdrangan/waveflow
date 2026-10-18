"""A harness's stand-in for a module's memory-master ports (``m_axi``): one ``MemoryMod`` behind all of
them on a zero-latency link, its image from the workload, its final arena a recording -- the memory a
kernel's ``m_axi`` bundles face at RTL, where the same ``MemoryMod`` lowers to a shared ``FlatMemory``.
The RTL gate is ``tests/examples/test_mem_copy_unit_xsi.py``."""
from __future__ import annotations

import numpy as np
import pytest

from examples.mem_copy.mem_copy import XSI_JOBS, MemCopy, copy_arena
from waveflow.hw.harness import DEFAULT_MEM_WORDS, MEM_BUNDLE, harness
from waveflow.hw.memory import MemoryMod
from waveflow.hw.mem_stream import MemRStream, MemWStream
from waveflow.utils.burst_io import StreamBurst, read_burst_bundle


def _mr_cmd(k: MemRStream, addr: int, n: int) -> StreamBurst:
    cmd = k.Cmd(addr=addr, len=n, xfer_len=0)
    return StreamBurst(np.asarray(cmd.serialize(word_bw=int(k.mem_dwidth)), dtype=np.uint64))


def test_a_kernel_with_two_memory_masters_gets_one_shared_memory():
    """MemCopy reads on m_in and writes on m_out: both face the one memory, as at RTL."""
    sysm = harness(MemCopy)
    mem = sysm.stand_ins["m_in"]
    assert isinstance(mem, MemoryMod) and sysm.stand_ins["m_out"] is mem
    assert sorted(p.name for p in sysm.participants()) == ["memory", "s_cmd_driver", "s_done_sink"]
    assert MEM_BUNDLE in sysm.compared_recordings()
    # The arena is the workload's image, so it is sized by it.
    assert int(mem.nwords_tot) == copy_arena(XSI_JOBS)[0].size


def test_the_harness_copies_in_pysim(tmp_path):
    """The workload's 16 copies, each destination equal to its source, one completion per job; the
    final arena and the completions are recorded where the RTL run records them."""
    sysm = harness(MemCopy)
    sysm.write_scenario(tmp_path)
    sysm.run()
    _image, golden = copy_arena(XSI_JOBS)
    out = read_burst_bundle(tmp_path / MEM_BUNDLE)[0]
    for j in XSI_JOBS:
        assert np.array_equal(out[j.dst_off:j.dst_off + j.n_words],
                              golden[j.dst_off:j.dst_off + j.n_words])
    assert len(read_burst_bundle(tmp_path / "vectors" / "s_done")) == len(XSI_JOBS)
    run = (tmp_path / "vectors" / "run.json").read_text(encoding="utf-8")
    assert '"mem_words"' in run and '"n_cycles"' in run       # the arena beside the bound


def test_a_read_master_alone_reads_the_image():
    """MemRStream: a command in, the words at [addr, addr+len) of the image out."""
    from waveflow.simulation.simulation import Simulation

    image = np.arange(100, 164, dtype=np.uint64)
    proto = MemRStream(name="proto", sim=Simulation())
    sysm = harness(MemRStream, workload={"s_cmd": [_mr_cmd(proto, 8, 16)], "m_mem": image})
    sysm.run()
    got = np.concatenate([np.asarray(w, dtype=np.uint64) for w in sysm.stand_ins["m_out"].words])
    assert np.array_equal(got, image[8:24])


def test_a_write_master_writes_the_arena(tmp_path):
    """MemWStream: a command and its words in; the arena records them at their address."""
    from waveflow.simulation.simulation import Simulation

    proto = MemWStream(name="proto", sim=Simulation())
    cmd = proto.Cmd(addr=4, len=8, xfer_len=0)
    words = np.arange(1, 9, dtype=np.uint64)
    sysm = harness(MemWStream, workload={
        "s_cmd": [StreamBurst(np.asarray(cmd.serialize(word_bw=64), dtype=np.uint64))],
        "s_in": [StreamBurst(words)]})
    assert int(sysm.stand_ins["m_mem"].nwords_tot) == DEFAULT_MEM_WORDS      # no image: zeros
    sysm.write_scenario(tmp_path)
    sysm.run()
    out = read_burst_bundle(tmp_path / MEM_BUNDLE)[0]
    assert np.array_equal(out[4:12], words) and not out[:4].any() and not out[12:].any()


def test_an_image_under_two_ports_is_refused():
    image = np.zeros(64, dtype=np.uint64)
    with pytest.raises(ValueError, match="share one memory"):
        harness(MemCopy, workload={"s_cmd": [], "m_in": image, "m_out": image})
