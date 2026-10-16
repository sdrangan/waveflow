"""mm_regbank.py — a memory-mapped register bank: shadow-and-commit config, latest-value status.

``plans/mm_slave_adaptor.md``, Stage 2.  The pysim half of the hand-written RTL leaf
``waveflow/build/rtl/mm_regbank.v`` (gate: ``tests/build/test_mm_regbank_xsi.py``).

The bank is **typed**: a config :class:`~waveflow.hw.dataschema.DataSchema` and a status one.  Their
serialized word counts at the bus width are the RTL's ``NCFG`` / ``NSTAT``, so the kernel reads a
whole configuration with ``get_schema(cfg_type)`` and publishes status with ``write(status)``::

    regs = MemSlaveRegBank(name="regs", sim=sim, cfg_type=FirCfg, status_type=FirStatus)
    # host:   write regs.cfg_words(cfg) at the bank base, then write anything to commit_offset()
    # kernel: cfg = yield from s_cfg.get_schema(FirCfg)        (one message per commit)
    #         yield from m_status.write(FirStatus(...))         (the host reads the latest)

Window layout (local byte addresses, ``W = window``):

=====================  ===========================================================================
``[0, W/2)``           config shadow, word *i* at ``i * bytes_per_word``; read/write.  Writing a
                       field changes nothing the kernel sees.
``W/2``                COMMIT: a write snapshots the shadow and sends it as one packet on
                       ``m_cfg``; a read returns the commit count.
``[3W/4, W)``          status: word *i* of the most recent COMPLETE message; read-only.
=====================  ===========================================================================

**Snapshot isolation**: the packet carries the shadow as it was at the commit.  **A commit is never
merged or dropped**: if the previous packet has not been taken, the commit's write waits (the
crossbar's write channel is held, so the host stalls — in RTL, WREADY low).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataSchema
from waveflow.hw.hw_module import HwModule, HwParam
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import MMIFSlave, Words
from waveflow.simulation.simobj import ProcessGen


@dataclass
class MemSlaveRegBank(HwModule):
    """Config + status register bank behind one bus window.  See the module docstring."""

    #: What a bus master reaches this view as -- see :class:`~waveflow.hw.mm_host.MemSlaveMap`.
    view_kind: ClassVar[str] = "regbank"

    cfg_type: type[DataSchema] = None  # type: ignore[assignment]
    status_type: type[DataSchema] = None  # type: ignore[assignment]
    mem_dwidth: HwParam[int] = 64
    window: int = 4096
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.cfg_type is None or self.status_type is None:
            raise ValueError(f"{type(self).__name__} needs both cfg_type and status_type")
        w = int(self.window)
        if w < 4096 or w & (w - 1):
            raise ValueError(f"{type(self).__name__}: window must be a power of two >= 4 KB")
        dw = int(self.mem_dwidth)
        self._bpw = dw // 8
        self._dt = np.dtype(np.uint32) if dw <= 32 else np.dtype(np.uint64)
        self.ncfg = int(self.cfg_type.nwords_per_inst(dw))
        self.nstat = int(self.status_type.nwords_per_inst(dw))
        if self.ncfg * self._bpw > w // 2 or self.nstat * self._bpw > w // 4:
            raise ValueError(f"{self.name}: {self.ncfg} config / {self.nstat} status words do not fit "
                             f"a {w}-byte window")
        self.s_mem = MMIFSlave(name=f"{self.name}_s_mem", sim=self.sim, bitwidth=dw,
                               rx_write_proc=self._on_write, rx_read_proc=self._on_read,
                               peek_read=self._peek)
        self.m_cfg = StreamIFMaster(name=f"{self.name}_m_cfg", sim=self.sim, bitwidth=dw,
                                    has_tlast=True)
        self.s_status = StreamIFSlave(name=f"{self.name}_s_status", sim=self.sim, bitwidth=dw,
                                      has_tlast=True)
        for ep in (self.s_mem, self.m_cfg, self.s_status):
            self.add_endpoint(ep)
        self._shadow = [0] * self.ncfg
        self._live = [0] * self.nstat
        self._pub = [0] * self.nstat
        self._st_i = 0
        self.ncommit = 0
        #: ``(time, words)`` per commit, as sent (observability).
        self.commits: list[tuple[float, list[int]]] = []

    # -- layout --------------------------------------------------------------------------------------
    def commit_offset(self) -> int:
        return int(self.window) // 2

    def status_offset(self) -> int:
        return 3 * int(self.window) // 4

    def cfg_words(self, cfg: DataSchema) -> Words:
        """The words a host writes at the bank base to stage *cfg* (then it writes COMMIT)."""
        return np.asarray(cfg.serialize(word_bw=int(self.mem_dwidth)), dtype=self._dt)

    def status(self) -> DataSchema:
        """The latest complete status message, decoded."""
        return self.status_type().deserialize(np.asarray(self._pub, dtype=self._dt),
                                              word_bw=int(self.mem_dwidth))

    def _region(self, a: int) -> str:
        q = (a * 4) // int(self.window)
        return "cfg" if q < 2 else ("commit" if q == 2 else "status")

    def pre_sim(self) -> None:
        super().pre_sim()
        iface = self.m_cfg.interface
        if iface is None:
            raise RuntimeError(f"{self.name}: m_cfg is not bound to a stream")
        if iface.depth != self.ncfg:
            # The RTL holds exactly ONE packet (the snapshot register), so a second commit stalls
            # until the first has been taken.  A deeper pysim channel would buffer it instead and
            # the two backends would disagree on when the host's commit write completes.
            raise ValueError(f"{self.name}: the stream bound to m_cfg has depth {iface.depth}; it "
                             f"must be {self.ncfg} (one config packet -- the RTL's snapshot "
                             f"register)")

    # -- kernel side: status messages ----------------------------------------------------------------
    def run_proc(self) -> ProcessGen[None]:
        while True:
            burst = yield from self.s_status.get()
            words = [int(x) for x in np.asarray(burst).tolist()]
            for j, w in enumerate(words):
                self._live[self._st_i] = w
                # A message completes on its NSTAT-th word or at the burst's end (TLAST).
                if self._st_i == self.nstat - 1 or j == len(words) - 1:
                    self._pub = list(self._live)
                    self._st_i = 0
                else:
                    self._st_i += 1

    # -- bus side ------------------------------------------------------------------------------------
    def _on_write(self, words: Words, local_addr: int) -> ProcessGen[None]:
        for i, w in enumerate(np.asarray(words).tolist()):
            a = int(local_addr) + i * self._bpw
            region = self._region(a)
            if region == "cfg":
                idx = a // self._bpw
                if idx < self.ncfg:
                    self._shadow[idx] = int(w)
            elif region == "commit":
                # The snapshot is taken now, at the commit.  In RTL the front serves one transaction
                # at a time, so while this commit is stalled no other master can reach the shadow
                # either: taking it before the wait is what the RTL does.
                snap = list(self._shadow)
                # Waits while the previous packet is still untaken: the stall, never a merge.  The
                # commit counts once ACCEPTED, as the RTL's counter does.
                # Early-anchored so only the wait for room is charged to the bus write: in RTL the
                # commit is accepted at once and the packet goes out afterwards, from the snapshot.
                yield from self.m_cfg.write_pipelined(
                    np.asarray(snap, dtype=self._dt),
                    t_out_start=self.env.now - self.ncfg * self.clk.period)
                self.ncommit += 1
                self.commits.append((self.env.now, snap))
        yield self.env.timeout(0)

    def _on_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        yield self.env.timeout(0)
        return self._peek(nwords, local_addr)

    def _peek(self, nwords: int, local_addr: int) -> Words:
        out = np.zeros(int(nwords), dtype=self._dt)
        for i in range(int(nwords)):
            a = int(local_addr) + i * self._bpw
            region = self._region(a)
            if region == "cfg":
                idx = a // self._bpw
                out[i] = self._shadow[idx] if idx < self.ncfg else 0
            elif region == "commit":
                out[i] = self.ncommit
            else:
                idx = (a - self.status_offset()) // self._bpw
                out[i] = self._pub[idx] if idx < self.nstat else 0
        return out


__all__ = ["MemSlaveRegBank"]
