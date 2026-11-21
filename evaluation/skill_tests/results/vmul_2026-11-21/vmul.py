"""vmul.py -- a vector-multiply device: z[i] = x[i] * y[i], x, y and z in memory.

Written as a test of the waveflow-* skills (waveflow-start -> waveflow-kernel -> waveflow-device ->
waveflow-schema -> waveflow-kernel-hls).  A device of five children, all in one HLS top:

    s_cmd -> VmulIssue --MemRCmd--> MemRStream (x) --x words--> VmulMul --[MemWCmd|VmulResp|z]--> MemWStream -> m_done
                       --MemRCmd--> MemRStream (y) --y words-->
                       --VmulCmd (the command travels with the data)-->

VmulIssue and VmulMul are the kernels (streams only); the readers and the writer are the framework
adaptors, in-band.  Two readers, so x and y arrive side by side and the multiply never buffers a whole
vector; the issue stage never waits for a reply it asked for (rule 7 of waveflow-kernel-hls).
(vmul_v1_single_core.py is the first try, the device skill's one-core shape: it passes pysim, but its
HLS body would have to hold all of x before y arrives, and would write then block-read in one task.)

Choices (the user left them open):

* Elements: x, y are int16; z is int32, the exact product (no rounding, no saturation).
* Bus / memory word: 64 bits -- 4 x or y elements per word, 2 z elements per word.
* Addresses and lengths are **bus words** from the arena base (offset=off), as MemRCmd / MemWCmd take.
* Commands: one input stream ``s_cmd`` carrying ``VmulCmd(tx_id, n, x_addr, y_addr, z_addr)``; there is
  no data on it (the data is in memory, so a command stream on its own is the kernel skill's case).
* Response: one ``VmulResp(tx_id, n, code)`` per command on ``m_done``, sent by the memory writer only
  after z is stored (done means stored).
* Error: ``n == 0`` is BAD_LEN, fatal: the core writes nothing, answers BAD_LEN through the writer
  (``MemWCmd(len=0, fwd_bursts=1)``) and stops taking commands until a reset.
* Whole words: when ``n`` is odd, z's last word is written whole -- its spare lane is overwritten with 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

import numpy as np

from waveflow.hw.arrayutils import array, read_array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import (
    FramedStreamIFSlave,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.hw.mem_stream import KernelTask, MemRCmd, MemRStream, MemWCmd, MemWStream
from waveflow.simulation.simobj import ProcessGen

W = 64                                                   # memory / stream word width

# ---------------------------------------------------------------- schemas
U32 = IntField.specialize(bitwidth=32, signed=False)
XElem = IntField.specialize(bitwidth=16, signed=True, include_dir="include")  # x[i], y[i]
ZElem = IntField.specialize(bitwidth=32, signed=True, include_dir="include")  # z = x*y, exact


class VmulCode(IntEnum):
    OK = 0
    BAD_LEN = 1                                          # n == 0


VmulCodeField = EnumField.specialize(enum_type=VmulCode, bitwidth=8)


class VmulCmd(DataList):
    elements = {
        "tx_id":  {"schema": U32, "description": "echoed in the response"},
        "n":      {"schema": U32, "description": "number of elements, n >= 1"},
        "x_addr": {"schema": U32, "description": "x's first word (bus words from the arena base)"},
        "y_addr": {"schema": U32, "description": "y's first word"},
        "z_addr": {"schema": U32, "description": "z's first word"},
    }


class VmulResp(DataList):
    elements = {
        "tx_id": {"schema": U32, "description": "the command's tx_id"},
        "n":     {"schema": U32, "description": "elements written to z"},
        "code":  {"schema": VmulCodeField, "description": "OK, or why the command failed (fatal)"},
    }


#: Schemas whose C++ headers the top includes; all ride framed internal edges.
SCHEMA_CLASSES = [VmulCodeField, VmulCmd, VmulResp, MemRCmd, MemWCmd]


def x_words(n: int) -> int:
    """Bus words holding n int16 elements (from the schema layout, not ceil by hand)."""
    return len(write_array(np.zeros(n, np.int16), elem_type=XElem, word_bw=W))


def z_words(n: int) -> int:
    return len(write_array(np.zeros(n, np.int32), elem_type=ZElem, word_bw=W))


# ---------------------------------------------------------------- golden
def vmul_golden(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """The pure function: z = x * y, int16 x int16 -> int32, exact."""
    return x.astype(np.int32) * y.astype(np.int32)


# ---------------------------------------------------------------- the kernels
@dataclass
class VmulIssue(FreeRunMod):
    """Stage 1, streams only: take a command, ask the two readers for x and y, pass the command on.
    It never waits for a reply (the data goes to the next stage), so no write-then-read in one task."""

    cpp_kernel_name: ClassVar[str | None] = "vmul_issue"
    mem_dwidth: HwParam[int] = W
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = self.mem_dwidth
        self.s_cmd = FramedStreamIFSlave(name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=w)
        self.m_rx = StreamIFMaster(name=f"{self.name}_m_rx", sim=self.sim, bitwidth=w, has_tlast=True)
        self.m_ry = StreamIFMaster(name=f"{self.name}_m_ry", sim=self.sim, bitwidth=w, has_tlast=True)
        self.m_desc = StreamIFMaster(name=f"{self.name}_m_desc", sim=self.sim, bitwidth=w,
                                     has_tlast=True)
        for ep in (self.s_cmd, self.m_rx, self.m_ry, self.m_desc):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask("vmul_issue_task", "vmul_issue_task.h",
                          ("s_cmd", "m_rx", "m_ry", "m_desc"), template_args=(int(self.mem_dwidth),))

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.s_cmd.get_schema(VmulCmd)
        n = int(cmd.n)                                   # a Python int: word counts below
        if n == 0:                                       # fatal: no reads; the next stage answers
            yield from self.m_desc.write(cmd)
            yield self.event()                           # stop taking input until a reset
            return
        nwx = x_words(n)
        yield from self.m_rx.write(MemRCmd(addr=cmd.x_addr, len=nwx, fwd_bursts=0))
        yield from self.m_ry.write(MemRCmd(addr=cmd.y_addr, len=nwx, fwd_bursts=0))
        yield from self.m_desc.write(cmd)


@dataclass
class VmulMul(FreeRunMod):
    """Stage 2, streams only: the command, then x and y side by side; z and the answer to the writer."""

    cpp_kernel_name: ClassVar[str | None] = "vmul_mul"
    mem_dwidth: HwParam[int] = W
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = self.mem_dwidth
        self.s_desc = StreamIFSlave(name=f"{self.name}_s_desc", sim=self.sim, bitwidth=w,
                                    has_tlast=True)
        self.s_x = StreamIFSlave(name=f"{self.name}_s_x", sim=self.sim, bitwidth=w, has_tlast=True)
        self.s_y = StreamIFSlave(name=f"{self.name}_s_y", sim=self.sim, bitwidth=w, has_tlast=True)
        self.m_wr = StreamIFMaster(name=f"{self.name}_m_wr", sim=self.sim, bitwidth=w, has_tlast=True)
        for ep in (self.s_desc, self.s_x, self.s_y, self.m_wr):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask("vmul_mul_task", "vmul_mul_task.h",
                          ("s_desc", "s_x", "s_y", "m_wr"), template_args=(int(self.mem_dwidth),))

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.s_desc.get_schema(VmulCmd)
        n = int(cmd.n)
        if n == 0:
            # fatal: nothing to write; answer through the writer so the answer still means "stored"
            yield from self.m_wr.write(MemWCmd(addr=cmd.z_addr, len=0, fwd_bursts=1))
            yield from self.m_wr.write(VmulResp(tx_id=cmd.tx_id, n=0, code=VmulCode.BAD_LEN))
            yield self.event()
            return
        x = yield from self.s_x.get_array(XElem, n)
        y = yield from self.s_y.get_array(XElem, n)
        z = vmul_golden(x.val, y.val)
        yield from self.m_wr.write(MemWCmd(addr=cmd.z_addr, len=z_words(n), fwd_bursts=1))
        yield from self.m_wr.write(VmulResp(tx_id=cmd.tx_id, n=n, code=VmulCode.OK))
        yield from self.m_wr.write_array(ZElem, n, array(ZElem, z))


# ---------------------------------------------------------------- the device
@dataclass
class Vmul(FreeRunMod):
    """The device: issue -> (x reader, y reader) -> mul -> writer.  The command travels issue -> mul on
    its own stream; the writer echoes the answer once z is stored."""

    cpp_kernel_name: ClassVar[str | None] = "vmul"
    mem_dwidth: HwParam[int] = W
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.issue = VmulIssue(name=f"{self.name}_issue", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.rx = MemRStream(name=f"{self.name}_rx", sim=self.sim, mem_dwidth=w, inband=True,
                             clk=self.clk)
        self.ry = MemRStream(name=f"{self.name}_ry", sim=self.sim, mem_dwidth=w, inband=True,
                             clk=self.clk)
        self.mul = VmulMul(name=f"{self.name}_mul", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.wr = MemWStream(name=f"{self.name}_w", sim=self.sim, mem_dwidth=w, inband=True,
                             emit_done=True, max_fwd_words=VmulResp.nwords_per_inst(w),
                             clk=self.clk)
        for c in (self.issue, self.rx, self.ry, self.mul, self.wr):
            self.add_comp(c)
        links = (("rx_cmd", self.issue.m_rx, self.rx.s_cmd),
                 ("ry_cmd", self.issue.m_ry, self.ry.s_cmd),
                 ("desc", self.issue.m_desc, self.mul.s_desc),
                 ("x", self.rx.m_out, self.mul.s_x),
                 ("y", self.ry.m_out, self.mul.s_y),
                 ("wr", self.mul.m_wr, self.wr.s_in))
        for nm, m, s in links:
            link = StreamIF(name=f"{self.name}_{nm}_if", sim=self.sim, clk=self.clk, bitwidth=w,
                            framed=True)
            link.bind("master", m)
            link.bind("slave", s)
            self.add_if(link)
        self.boundary = ["s_cmd", "m_x_mem", "m_y_mem", "m_z_mem", "m_done"]
        self.s_cmd, self.m_done = self.issue.s_cmd, self.wr.m_done
        self.m_x_mem, self.m_y_mem, self.m_z_mem = self.rx.m_mem, self.ry.m_mem, self.wr.m_mem
        self.cmd_headers = tuple(dict.fromkeys(c.resolved_include_filename() for c in SCHEMA_CLASSES))
        self.extra_includes = ("streamutils_hls.h",)

    # --- the scenario -------------------------------------------------------------------------
    def workload(self, kind: str = "test") -> dict:
        from waveflow.utils.burst_io import StreamBurst

        jobs, image = scenario(kind)
        cmds = [StreamBurst(np.asarray(c.serialize(word_bw=W), dtype=np.uint64)) for c, *_ in jobs]
        return {"s_cmd": cmds, "m_x_mem": image}


# ---------------------------------------------------------------- scenarios
#: (n per job) for each scenario; 0 is the fatal BAD_LEN; a job after it must never run.
SCENARIOS = {
    "test":  [1, 4, 37, 128, 3],
    "error": [16, 5, 0, 8],
}


def scenario(kind: str):
    """``(jobs, image)``: jobs = [(VmulCmd, x, y)], image = the arena (uint64 words) with x, y loaded.
    Each job's x, y, z get their own word-aligned regions, 4 words of guard between."""
    if kind not in SCENARIOS:
        raise ValueError(f"Vmul.workload: no scenario {kind!r}")
    rng = np.random.default_rng(0x5EED)
    jobs, regions, a = [], [], 16
    for k, n in enumerate(SCENARIOS[kind]):
        x = rng.integers(-2**15, 2**15, size=n, dtype=np.int16)
        y = rng.integers(-2**15, 2**15, size=n, dtype=np.int16)
        if n >= 2:                                       # the corner: (-32768)^2 = 2^30
            x[0] = y[0] = -2**15
        xa = a; a += max(x_words(n), 1) + 4
        ya = a; a += max(x_words(n), 1) + 4
        za = a; a += max(z_words(n), 1) + 4
        jobs.append((VmulCmd(tx_id=100 + k, n=n, x_addr=xa, y_addr=ya, z_addr=za), x, y))
        regions.append((xa, ya, za))
    image = np.zeros(a + 16, dtype=np.uint64)
    for (cmd, x, y), (xa, ya, _za) in zip(jobs, regions):
        if len(x):
            image[xa:xa + x_words(len(x))] = write_array(x, elem_type=XElem, word_bw=W)
            image[ya:ya + x_words(len(y))] = write_array(y, elem_type=XElem, word_bw=W)
    return jobs, image


def expected(kind: str):
    """``(responses, final_image)`` from the golden alone: one VmulResp per command up to and including
    the first fatal one; z of every OK job before it, written whole-word (spare lane 0)."""
    jobs, image = scenario(kind)
    final = image.copy()
    resps = []
    for cmd, x, y in jobs:
        n = int(cmd.n)
        if n == 0:
            resps.append(VmulResp(tx_id=cmd.tx_id, n=0, code=VmulCode.BAD_LEN))
            break
        za = int(cmd.z_addr)
        final[za:za + z_words(n)] = write_array(vmul_golden(x, y), elem_type=ZElem, word_bw=W)
        resps.append(VmulResp(tx_id=cmd.tx_id, n=n, code=VmulCode.OK))
    return resps, final


def decode_z(words: np.ndarray, cmd: VmulCmd) -> np.ndarray:
    n, za = int(cmd.n), int(cmd.z_addr)
    return read_array(words[za:za + z_words(n)], elem_type=ZElem, word_bw=W, shape=n).val
