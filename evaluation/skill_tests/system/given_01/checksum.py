"""The checksum device: a kernel behind a command queue and a result queue.

::

    host --q_chk (QueueIn)--> s_in  [Checksum]  m_out --q_sum (QueueOut + IrqIF)--> host

    s_in  : ChkCmd(tx_id, n) | x[0..n-1]          int16 samples, four a 64-bit word
    m_out : ChkResp(tx_id, n, sum)                 one per job; sum = (sum of x[i] * (i + 1)) mod 2**32

A burst that ends before ``n`` samples is answered with the samples that arrived (``n`` says how many);
there is no error.  The kernel keeps nothing across jobs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.arrayutils import array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.mm_device import QueueIn, QueueOut
from waveflow.utils.burst_io import StreamBurst

from scale_msgs import DW, I16, U16, U32

CHK_DEPTH = 1024      # words: the largest job's samples (2048 / 4) and its command, twice
SUM_DEPTH = 16


class ChkCmd(DataList):
    elements = {"tx_id": {"schema": U16}, "n": {"schema": U16}}


class ChkResp(DataList):
    elements = {"tx_id": {"schema": U16}, "n": {"schema": U16}, "sum": {"schema": U32}}


def checksum(x: np.ndarray) -> int:
    """The golden."""
    w = np.arange(1, len(x) + 1, dtype=np.int64)
    return int((x.astype(np.int64) * w).sum()) & 0xFFFF_FFFF


@dataclass
class Checksum(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "checksum"
    mm_views: ClassVar[tuple] = (
        QueueIn("q_chk", port="s_in", depth=CHK_DEPTH),
        QueueOut("q_sum", port="m_out", depth=SUM_DEPTH),
    )
    clk: Clock = field(default_factory=lambda: Clock(freq=200e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=DW,
                                    has_tlast=False)
        for ep in (self.s_in, self.m_out):
            self.add_endpoint(ep)

    def run_iter(self):
        cmd = yield from self.s_in.get_schema(ChkCmd)
        x = (yield from self.s_in.get_array(I16, int(cmd.n))).val
        yield from self.m_out.write(ChkResp(tx_id=cmd.tx_id, n=len(x), sum=checksum(np.asarray(x))))

    def workload(self, kind: str = "test") -> dict:
        return {"s_in": stimulus(chk_scenario(kind))}

    def golden(self, kind: str = "test") -> dict:
        return {"m_out": [ChkResp(tx_id=t, n=len(x), sum=checksum(x)).serialize(word_bw=DW)
                          for t, x in chk_scenario(kind)]}


def chk_scenario(kind: str = "test") -> list[tuple[int, np.ndarray]]:
    rng = np.random.default_rng(11)
    return [(t, rng.integers(-32768, 32767, n).astype(np.int16)) for t, n in enumerate((9, 300, 1))]


def stimulus(jobs) -> list[StreamBurst]:
    out = []
    for t, x in jobs:
        out.append(StreamBurst(np.asarray(ChkCmd(tx_id=t, n=len(x)).serialize(word_bw=DW),
                                          dtype=np.uint64)))
        out.append(StreamBurst(np.asarray(array(I16, x).serialize(word_bw=DW), dtype=np.uint64)))
    return out


__all__ = ["Checksum", "ChkCmd", "ChkResp", "checksum", "CHK_DEPTH", "SUM_DEPTH"]
