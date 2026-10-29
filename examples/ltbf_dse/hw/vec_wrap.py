"""vec_wrap.py -- the vector unit on the bus: Job FIFO, Ctrl, memory front end, done record (spec §5.4).

``plans/ltbf_dse/ltbf_dse.md`` step 4.2.  :class:`VecWrap` is a composite around the framework's
:class:`~waveflow.vecunit.unit.VecUnit`, **unchanged** (it only has stream ports):

* :class:`VecDecoder` takes a fixed-size :class:`VecMemJob` from the Job FIFO and turns it into one
  in-band read command for each of the three **program readers** (``MemRStream``, one per program
  stream: no stream waits behind another), the data reader's commands (the load streams' affine
  walks, in the load program's order: for each of ``count`` rounds, each load stream), and the job's
  store plan for the writer;
* a **data reader** (``MemRStream``) feeds ``s_in0``;
* :class:`VecWriter` turns each ``VecResp`` and its data into an in-band memory write (the store
  streams' affine walks), and the job's last store carries the job's done record, which the memory
  writer (``MemWStream``) forwards only once that data is in memory, after the job's ``VecStatus``.

The programs live in Shared Mem, written once by the host; a job's descriptor names their addresses
(the spec's program table, carried in the descriptor). Stored programs carry ``tx_id`` 0; the done
record takes the job's ``tx_id`` from the descriptor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from examples.ltbf_dse.hw.records import SRC_VEC, Resp, SysCfg, SysStatus
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    FramedStreamIFSlave,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.hw.mem_stream import MemRCmd, MemRStream, MemWCmd, MemWStream
from waveflow.hw.mm_device import QueueIn, RegBank
from waveflow.linalg.message import U8, U16, U32
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.isa import VecResp, VecStatus
from waveflow.vecunit.unit import VecUnit

#: Load and store streams a job at most.
MAX_LD, MAX_ST = 4, 4


def _stream_fields(prefix: str, n: int) -> dict:
    out = {}
    for k in range(n):
        out[f"{prefix}{k}_base"] = {
            "schema": U32,
            "description": f"{prefix} stream {k}: first word",
        }
        out[f"{prefix}{k}_stride"] = {
            "schema": U32,
            "description": "words from one round to the next",
        }
        out[f"{prefix}{k}_words"] = {"schema": U32, "description": "words a matrix"}
    return out


class VecMemJob(DataList):
    """One vector-unit job (spec §5.4, fixed size): its programs' addresses and lengths in Shared Mem,
    and the affine walks of its load and store streams (``count`` rounds, each stream once a round).
    """

    include_filename: ClassVar[str | None] = "ltbf_vec_mem_job.h"
    elements: ClassVar[dict] = {
        "tx_id": {
            "schema": U16,
            "description": "the job's id, echoed in its done record",
        },
        "count": {"schema": U16, "description": "rounds of the load and store streams"},
        "n_ld": {"schema": U8, "description": "load streams (<= 4)"},
        "n_st": {"schema": U8, "description": "store streams (<= 4)"},
        "l_addr": {"schema": U32, "description": "the load program's first word"},
        "l_words": {"schema": U32, "description": "its words (VecJob word included)"},
        "x_addr": {"schema": U32, "description": "the execute program"},
        "x_words": {"schema": U32, "description": "its words"},
        "s_addr": {"schema": U32, "description": "the store program"},
        "s_words": {"schema": U32, "description": "its words"},
        **_stream_fields("ld", MAX_LD),
        **_stream_fields("st", MAX_ST),
    }


class StorePlan(DataList):
    """The decoder's word to the writer, a job: its id and its store streams' walk."""

    include_filename: ClassVar[str | None] = "ltbf_store_plan.h"
    elements: ClassVar[dict] = {
        "tx_id": {"schema": U16, "description": "the job's"},
        "count": {"schema": U16, "description": "rounds"},
        "n_st": {"schema": U8, "description": "store streams"},
        **_stream_fields("st", MAX_ST),
    }


def _bits(v) -> int:
    return int(v)


@dataclass
class VecDecoder(FreeRunMod):
    """A ``VecMemJob`` in; read commands to the three program readers and the data reader, the store
    plan to the writer.  One firing a job."""

    word_bits: int = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.word_bits)
        self.s_job = StreamIFSlave(name=f"{self.name}_s_job", sim=self.sim, bitwidth=w)
        self.m_lcmd = FramedStreamIFMaster(
            name=f"{self.name}_m_lcmd", sim=self.sim, bitwidth=w
        )
        self.m_xcmd = FramedStreamIFMaster(
            name=f"{self.name}_m_xcmd", sim=self.sim, bitwidth=w
        )
        self.m_scmd = FramedStreamIFMaster(
            name=f"{self.name}_m_scmd", sim=self.sim, bitwidth=w
        )
        self.m_dcmd = FramedStreamIFMaster(
            name=f"{self.name}_m_dcmd", sim=self.sim, bitwidth=w
        )
        self.m_plan = StreamIFMaster(
            name=f"{self.name}_m_plan", sim=self.sim, bitwidth=w
        )
        for ep in (
            self.s_job,
            self.m_lcmd,
            self.m_xcmd,
            self.m_scmd,
            self.m_dcmd,
            self.m_plan,
        ):
            self.add_endpoint(ep)

    def run_iter(self):
        j = yield from self.s_job.get_schema(VecMemJob)
        for ep, a, n in ((self.m_lcmd, j.l_addr, j.l_words), (self.m_xcmd, j.x_addr, j.x_words),
                         (self.m_scmd, j.s_addr, j.s_words)):  # fmt: skip
            yield from ep.write(MemRCmd(addr=_bits(a), len=_bits(n), fwd_bursts=0))
        plan = StorePlan(tx_id=int(j.tx_id), count=int(j.count), n_st=int(j.n_st))
        for k in range(MAX_ST):
            for f in ("base", "stride", "words"):
                setattr(plan, f"st{k}_{f}", int(getattr(j, f"st{k}_{f}")))
        yield from self.m_plan.write(plan)
        for i in range(int(j.count)):
            for k in range(int(j.n_ld)):
                base, stride = int(getattr(j, f"ld{k}_base")), int(
                    getattr(j, f"ld{k}_stride")
                )
                n = int(getattr(j, f"ld{k}_words"))
                yield from self.m_dcmd.write(
                    MemRCmd(addr=base + i * stride, len=n, fwd_bursts=0)
                )


@dataclass
class VecWriter(FreeRunMod):
    """Each ``VecResp`` + data to an in-band memory write at its store stream's next address; the job's
    last store forwards the job's done record (after its ``VecStatus``)."""

    word_bits: int = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.word_bits)
        self.s_plan = StreamIFSlave(
            name=f"{self.name}_s_plan", sim=self.sim, bitwidth=w
        )
        self.s_out = FramedStreamIFSlave(
            name=f"{self.name}_s_out", sim=self.sim, bitwidth=w
        )
        self.s_status = FramedStreamIFSlave(
            name=f"{self.name}_s_status", sim=self.sim, bitwidth=64
        )
        self.m_w = FramedStreamIFMaster(
            name=f"{self.name}_m_w", sim=self.sim, bitwidth=w
        )
        for ep in (self.s_plan, self.s_out, self.s_status, self.m_w):
            self.add_endpoint(ep)

    def run_iter(self):
        plan = yield from self.s_plan.get_schema(StorePlan)
        count, n_st = int(plan.count), int(plan.n_st)
        for i in range(count):
            for k in range(n_st):
                _resp = yield from self.s_out.get_schema(VecResp)
                data = yield from self.s_out.get()
                base = int(getattr(plan, f"st{k}_base"))
                stride = int(getattr(plan, f"st{k}_stride"))
                last = i == count - 1 and k == n_st - 1
                fwd = 0
                if last:
                    st = yield from self.s_status.get_schema(VecStatus)
                    done = Resp(
                        src=SRC_VEC, status=int(st.status), tx_id=int(plan.tx_id), seq=0
                    )
                    fwd = 1
                cmd = MemWCmd(
                    addr=base + i * stride, len=int(np.shape(data)[0]), fwd_bursts=fwd
                )
                yield from self.m_w.write(cmd)
                if last:
                    yield from self.m_w.write(done)
                yield from self.m_w.write(data)


@dataclass
class VecCtrl(FreeRunMod):
    """The vector unit's Ctrl: configuration in, status after every job."""

    word_bits: int = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.word_bits)
        self.s_cfg = StreamIFSlave(name=f"{self.name}_s_cfg", sim=self.sim, bitwidth=w)
        self.add_endpoint(self.s_cfg)
        self.enable = 1

    def run_iter(self):
        cfg = yield from self.s_cfg.get_schema(SysCfg)
        self.enable = int(cfg.enable)


@dataclass
class VecWrap(FreeRunMod):
    """The vector unit's device (spec §5.4)."""

    mm_views: ClassVar[tuple] = (
        QueueIn("qjob", port="s_job", depth=32),
        RegBank("ctrl", cfg_port="s_cfg", status_port="m_status", cfg_type=SysCfg,
                status_type=SysStatus),
    )  # fmt: skip

    vu_cfg: VecUnitConfig | None = None
    word_bits: int = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, sim, clk = int(self.word_bits), self.sim, self.clk
        self.unit = VecUnit(name=f"{self.name}_vu", sim=sim, clk=clk, cfg=self.vu_cfg)
        self.dec = VecDecoder(name=f"{self.name}_dec", sim=sim, clk=clk, word_bits=w)
        self.readers = {
            p: MemRStream(
                name=f"{self.name}_r{p}", sim=sim, mem_dwidth=w, inband=True, clk=clk
            )
            for p in ("l", "x", "s", "d")
        }
        self.writer = VecWriter(name=f"{self.name}_wr", sim=sim, clk=clk, word_bits=w)
        self.wstream = MemWStream(name=f"{self.name}_memw", sim=sim, mem_dwidth=w, inband=True,
                                  emit_done=True, done_framed=False, max_fwd_words=1, clk=clk)  # fmt: skip
        self.ctrl = VecCtrl(name=f"{self.name}_ctrl", sim=sim, clk=clk, word_bits=w)
        for comp in (
            self.unit,
            self.dec,
            *self.readers.values(),
            self.writer,
            self.wstream,
            self.ctrl,
        ):
            self.add_comp(comp)
        u = self.unit
        links = (
            ("lcmd", self.dec.m_lcmd, self.readers["l"].s_cmd, True),
            ("xcmd", self.dec.m_xcmd, self.readers["x"].s_cmd, True),
            ("scmd", self.dec.m_scmd, self.readers["s"].s_cmd, True),
            ("dcmd", self.dec.m_dcmd, self.readers["d"].s_cmd, True),
            ("lprog", self.readers["l"].m_out, u.s_lprog, True),
            ("xprog", self.readers["x"].m_out, u.s_xprog, True),
            ("sprog", self.readers["s"].m_out, u.s_sprog, True),
            ("data", self.readers["d"].m_out, u.s_in0, True),
            ("out", u.s_out0, self.writer.s_out, True),
            ("status", u.m_status, self.writer.s_status, True),
            ("plan", self.dec.m_plan, self.writer.s_plan, False),
            ("w", self.writer.m_w, self.wstream.s_in, True),
        )
        for name, m, s, framed in links:
            bits = 64 if name == "status" else w
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=sim, clk=clk, bitwidth=bits,
                             **({"framed": True} if framed else {}))  # fmt: skip
            iface.bind("master", m)
            iface.bind("slave", s)
            self.add_if(iface)
        self.boundary = ["s_job", "m_rl", "m_rx", "m_rs", "m_rd", "m_w", "m_resp", "s_cfg",
                         "m_status"]  # fmt: skip
        self.s_job = self.dec.s_job
        self.m_rl, self.m_rx = self.readers["l"].m_mem, self.readers["x"].m_mem
        self.m_rs, self.m_rd = self.readers["s"].m_mem, self.readers["d"].m_mem
        self.m_w = self.wstream.m_mem
        self.m_resp = self.wstream.s_done
        self.s_cfg = self.ctrl.s_cfg
        self.m_status = StreamIFMaster(
            name=f"{self.name}_m_status", sim=sim, bitwidth=w
        )
        self.add_endpoint(self.m_status)
