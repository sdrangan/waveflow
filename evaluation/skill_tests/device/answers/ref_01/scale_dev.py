"""The gain-and-shift offload as a device: a kernel between a memory reader and a memory writer, and a
host's command queue and status queue on the bus.

::

    host --QueueIn q_cmd--> s_cmd [ScaleCore] m_rd --> MemRStream --m_axi--> memory
                                              s_rd <-- (the input words)
                                              m_wr --> MemWStream --m_axi--> memory
                                                       m_done --> m_status --QueueOut q_status--> host
                                                                             (its IrqIF wakes the host)

* **ScaleCore** is the kernel, a leaf on streams only: per job it asks the reader for the input
  (``MemRCmd``), computes, and hands the writer ``[MemWCmd(fwd_bursts=1) | ScaleStatus | y]``.
* **The writer echoes the status only once ``y`` is written** (``m_done``), so a status in the host's
  hands means the output is in memory: the answer is "done", not "seen".
* **ScaleDev** is the device: the core and the two framework adaptors, a composite (``add_comp`` /
  ``add_if``).  Its ``mm_views`` say how the host reaches it.  It is not a kernel: it holds adaptors.

**Choices, stated** (the spec leaves them open):

* The status goes to a **queue out**, not a register bank: the spec wants an interrupt when a job is
  done, and Waveflow's register bank raises none (``interface/axi_mm/slave.md``, *What is not built
  yet*; ``systems/command_response.md``, *On a bus*).  The queue out's level interrupt (threshold 1) is
  "a status is waiting".  A register bank would give "the latest status, read any time" but no
  interrupt; it could be added beside the queue if the host also wants that.
* The command carries every parameter (gain, shift, addresses), so there is one input stream and no
  order to keep between two.
* A buffer is whole words: the writer writes the last word whole, so samples past ``n`` in it become
  zero.  The host allocates each output buffer in whole words.
* An over-long job is fatal: its status is sent through the writer with nothing to write
  (``MemWCmd(len=0, fwd_bursts=1)``), and the core takes no more commands until a reset.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.arrayutils import array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import MemRCmd, MemRStream, MemWCmd, MemWStream
from waveflow.hw.mm_device import QueueIn, QueueOut
from waveflow.utils.burst_io import StreamBurst

from scale_msgs import DW, I16, NMAX, Code, ScaleCmd, ScaleStatus, nwords, scale

#: Commands the host may queue ahead of the device.
CMD_DEPTH = 32
#: Statuses the device may hold for the host.  The writer stalls once it is full, so the host keeps at
#: most this many jobs outstanding (its admission window).
STATUS_DEPTH = 16


@dataclass
class ScaleCore(FreeRunMod):
    """The kernel: one firing = one job.  Streams only -- no address leaves it except as a field of a
    ``MemRCmd`` / ``MemWCmd``."""

    cpp_kernel_name: ClassVar[str | None] = "scale_core"
    clk: Clock = field(default_factory=lambda: Clock(freq=200e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.m_rd = StreamIFMaster(name=f"{self.name}_m_rd", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.s_rd = StreamIFSlave(name=f"{self.name}_s_rd", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.m_wr = StreamIFMaster(name=f"{self.name}_m_wr", sim=self.sim, bitwidth=DW, has_tlast=True)
        for ep in (self.s_cmd, self.m_rd, self.s_rd, self.m_wr):
            self.add_endpoint(ep)

    def run_iter(self):
        cmd = yield from self.s_cmd.get_schema(ScaleCmd)
        n = int(cmd.n)
        if n > NMAX:                                              # fatal: report through the writer
            yield from self.m_wr.write(MemWCmd(addr=0, len=0, fwd_bursts=1))
            yield from self.m_wr.write(ScaleStatus(tx_id=cmd.tx_id, code=Code.TOO_LONG, nsat=0))
            yield self.event()                                    # never triggered: stop until reset
        nw = nwords(n)
        yield from self.m_rd.write(MemRCmd(addr=cmd.src, len=nw, fwd_bursts=0))
        x = yield from self.s_rd.get_array(I16, n)
        y, nsat = scale(np.asarray(x.val), cmd.gain, cmd.shift)
        yield from self.m_wr.write(MemWCmd(addr=cmd.dst, len=nw, fwd_bursts=1))
        yield from self.m_wr.write(ScaleStatus(tx_id=cmd.tx_id, code=Code.OK, nsat=nsat))
        yield from self.m_wr.write_array(I16, n, array(I16, y))

    def workload(self, kind: str = "test") -> dict:
        """The core alone: the commands, and what the reader would answer (the input words)."""
        jobs = scenario(kind)
        cmds, rd = [], []
        for j in jobs:
            cmds.append(_burst(j.cmd()))
            if j.n <= NMAX:
                rd.append(StreamBurst(np.asarray(write_array(array(I16, j.x), word_bw=DW),
                                                 dtype=np.uint64)))
        return {"s_cmd": cmds, "s_rd": rd}


@dataclass
class ScaleDev(FreeRunMod):
    """The device: the core between the framework's in-band memory reader and writer.  Its boundary:
    the command stream in (``s_cmd``), the reader's and the writer's ``m_axi`` (``m_rd_mem``,
    ``m_wr_mem``) and the statuses the writer echoes (``m_status``)."""

    cpp_kernel_name: ClassVar[str | None] = "scale_dev"
    mm_views: ClassVar[tuple] = (
        QueueIn("q_cmd", port="s_cmd", depth=CMD_DEPTH),
        QueueOut("q_status", port="m_status", depth=STATUS_DEPTH),
    )
    clk: Clock = field(default_factory=lambda: Clock(freq=200e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.core = ScaleCore(name=f"{self.name}_core", sim=self.sim, clk=self.clk)
        self.reader = MemRStream(name=f"{self.name}_r", sim=self.sim, mem_dwidth=DW, inband=True,
                                 clk=self.clk)
        # The status rides through the writer: it is the writer's whole forward buffer.  Unframed on
        # m_done, because a queue out drops TLAST anyway.
        self.writer = MemWStream(name=f"{self.name}_w", sim=self.sim, mem_dwidth=DW, inband=True,
                                 emit_done=True, done_framed=False,
                                 max_fwd_words=int(ScaleStatus.nwords_per_inst(DW)), clk=self.clk)
        for c in (self.core, self.reader, self.writer):
            self.add_comp(c)
        for name, master, slave in (("rd_cmd", self.core.m_rd, self.reader.s_cmd),
                                    ("rd_data", self.reader.m_out, self.core.s_rd),
                                    ("wr", self.core.m_wr, self.writer.s_in)):
            si = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=DW,
                          framed=True)
            si.bind("master", master)
            si.bind("slave", slave)
            self.add_if(si)
        self.boundary = ["s_cmd", "m_rd_mem", "m_wr_mem", "m_status"]
        self.s_cmd = self.core.s_cmd
        self.m_rd_mem = self.reader.m_mem
        self.m_wr_mem = self.writer.m_mem
        self.m_status = self.writer.m_done

    def workload(self, kind: str = "test") -> dict:
        """The device alone: the commands, and the memory image (every job's input in place)."""
        jobs = scenario(kind)
        return {"s_cmd": [_burst(j.cmd()) for j in jobs], "m_rd_mem": image(jobs)}

    def golden(self, kind: str = "test") -> dict:
        return {"m_status": [s.serialize(word_bw=DW) for s in expected_status(scenario(kind))]}


# --- scenarios ---------------------------------------------------------------------------------------

def _burst(msg) -> StreamBurst:
    return StreamBurst(np.asarray(msg.serialize(word_bw=DW), dtype=np.uint64))


@dataclass
class Job:
    tx_id: int
    x: np.ndarray
    gain: int
    shift: int
    src: int = 0          # word addresses, set by layout()
    dst: int = 0
    n_claimed: int | None = None    # a length other than len(x): the over-long command

    @property
    def n(self) -> int:
        return len(self.x) if self.n_claimed is None else self.n_claimed

    def cmd(self) -> ScaleCmd:
        return ScaleCmd(tx_id=self.tx_id, n=self.n, src=self.src, dst=self.dst, gain=self.gain,
                        shift=self.shift)


#: Where the arena puts buffers: inputs from word 0, outputs after them, each a whole number of words.
OUT_BASE = 2048


def layout(jobs: list[Job]) -> list[Job]:
    src, dst = 0, OUT_BASE
    for j in jobs:
        j.src, j.dst = src, dst
        w = nwords(len(j.x))
        src, dst = src + w, dst + w
    return jobs


def scenario(kind: str = "test") -> list[Job]:
    rng = np.random.default_rng({"test": 3, "too_long": 4}[kind])

    def x(n):
        return rng.integers(-30000, 30000, n).astype(np.int16)

    jobs = [Job(0, x(37), gain=3, shift=1),           # some saturate; n not a multiple of four
            Job(1, x(256), gain=-5, shift=4),
            Job(2, x(1), gain=1, shift=0)]
    if kind == "too_long":
        jobs = [jobs[0], Job(1, x(8), 1, 0, n_claimed=NMAX + 1), Job(2, x(16), 2, 0)]
    return layout(jobs)


def image(jobs: list[Job]) -> np.ndarray:
    """The arena: every job's input at its ``src``; the outputs' region zero."""
    words = np.zeros(4096, dtype=np.uint64)
    for j in jobs:
        w = np.asarray(write_array(array(I16, j.x), word_bw=DW), dtype=np.uint64)
        words[j.src:j.src + len(w)] = w
    return words


def expected_status(jobs: list[Job]) -> list[ScaleStatus]:
    """One status per job, up to and including the first error, which is the last answered."""
    out = []
    for j in jobs:
        if j.n > NMAX:
            out.append(ScaleStatus(tx_id=j.tx_id, code=Code.TOO_LONG, nsat=0))
            break
        _y, nsat = scale(j.x, j.gain, j.shift)
        out.append(ScaleStatus(tx_id=j.tx_id, code=Code.OK, nsat=nsat))
    return out


__all__ = ["ScaleCore", "ScaleDev", "Job", "scenario", "image", "expected_status", "layout",
           "CMD_DEPTH", "STATUS_DEPTH", "OUT_BASE"]
