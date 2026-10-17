"""A harness carries stream words wider than 64 bits: each word is ``ceil(W/64)`` uint64 chunks in the
bundles both backends read (the stimulus the harness writes and the capture its sink writes)."""
from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.harness import harness
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.utils.burst_io import StreamBurst, read_burst_bundle

W = 128


@dataclass
class Pass128(FreeRunMod):
    cpp_kernel_name: ClassVar[str] = "pass128"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self):
        super().__post_init__()
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=W, has_tlast=True)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=W, has_tlast=True)
        for ep in (self.s_in, self.m_out):
            self.add_endpoint(ep)

    def run_iter(self):
        x = yield from self.s_in.get()
        yield from self.m_out.write(x)


def _bursts():
    a = np.arange(1, 7, dtype=np.uint64).reshape(3, 2)          # 3 words of 2 chunks
    b = np.array([[0xFFFFFFFFFFFFFFFF, 7]], dtype=np.uint64)     # 1 word
    return [StreamBurst(words=a), StreamBurst(words=b)]


def test_a_128_bit_harness_runs_and_writes_two_chunk_bundles(tmp_path):
    sysm = harness(Pass128, workload={"s_in": _bursts()})
    sysm.write_scenario(tmp_path)
    meta = read_burst_bundle(tmp_path / "vectors" / "s_in")
    assert [np.asarray(m).shape for m in meta] == [(3, 2), (1, 2)]
    sysm.run()
    got = sysm.stand_ins["m_out"].words
    assert len(got) == 2
    for g, b in zip(got, _bursts()):
        assert np.array_equal(np.asarray(g, dtype=np.uint64).reshape(-1, 2), b.words)
    cap = read_burst_bundle(tmp_path / "vectors" / "m_out")
    assert [np.asarray(c).shape for c in cap] == [(3, 2), (1, 2)]
