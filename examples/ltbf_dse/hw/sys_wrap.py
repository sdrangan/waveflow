"""sys_wrap.py -- the systolic array on the bus: a Job FIFO, a thin Ctrl, a done record (spec §5.4).

``plans/ltbf_dse/ltbf_dse.md`` step 4.1.  :class:`SysWrap` is a composite of the framework's
:class:`~waveflow.linalg.systolic_engine_mem.SystolicEngineMem`, **unchanged**, and two small leaves:

* :class:`SysTap` turns each ``EngineDone`` (the engine's memory writer forwards it once the job's last
  tile of ``C`` is in memory) into a :class:`~examples.ltbf_dse.hw.records.Resp` record (``src`` 0) for
  the RespHub, and publishes the Ctrl status after every job;
* :class:`SysCtrl` takes the Ctrl configuration (``enable``).

Its memory-mapped views (``mm_views``): ``qjob``, a ``QueueIn`` the host writes ``EngineJobDesc``
words into (passed to the engine's ``s_job`` as they are), and ``ctrl``, a ``RegBank``.  The engine's
read and write masters (``m_in``, ``m_out``) go on the crossbar to Shared Mem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from examples.ltbf_dse.hw.records import SRC_SYS, Resp, SysCfg, SysStatus
from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import (
    FramedStreamIFSlave,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.hw.mm_device import QueueIn, RegBank
from waveflow.linalg.systolic_engine_mem import EngineDone, SystolicEngineMem


@dataclass
class SysTap(FreeRunMod):
    """``EngineDone`` in, a ``Resp`` record and the Ctrl status out: one firing a job."""

    word_bits: int = 128
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_done = FramedStreamIFSlave(
            name=f"{self.name}_s_done", sim=self.sim, bitwidth=int(self.word_bits)
        )
        self.m_resp = StreamIFMaster(
            name=f"{self.name}_m_resp",
            sim=self.sim,
            bitwidth=int(self.word_bits),
            has_tlast=False,
        )
        self.m_status = StreamIFMaster(
            name=f"{self.name}_m_status", sim=self.sim, bitwidth=int(self.word_bits)
        )
        for ep in (self.s_done, self.m_resp, self.m_status):
            self.add_endpoint(ep)
        self.jobs_done = 0

    def kernel_task(self):
        """The hand-written HLS body, ``src/ltbf_tasks.h``: the twin of :meth:`run_iter`."""
        from waveflow.hw.mem_stream import KernelTask

        return KernelTask(
            "ltbf_sys_tap_task",
            "ltbf_tasks.h",
            ("s_done", "m_resp", "m_status"),
            template_args=(int(self.word_bits),),
            framed=("s_done", "m_status"),
        )

    def run_iter(self):
        d = yield from self.s_done.get_schema(EngineDone)
        self.jobs_done += 1
        yield from self.m_resp.write(
            Resp(src=SRC_SYS, status=int(d.status), tx_id=int(d.tx_id), seq=0)
        )
        yield from self.m_status.write(
            SysStatus(
                jobs_done=self.jobs_done,
                last_tx_id=int(d.tx_id),
                last_status=int(d.status),
            )
        )


@dataclass
class SysCtrl(FreeRunMod):
    """The Ctrl configuration: one firing a write."""

    word_bits: int = 128
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_cfg = StreamIFSlave(
            name=f"{self.name}_s_cfg", sim=self.sim, bitwidth=int(self.word_bits)
        )
        self.add_endpoint(self.s_cfg)
        self.enable = 1

    def kernel_task(self):
        """The hand-written HLS body, ``src/ltbf_tasks.h``: the configuration, taken and held."""
        from waveflow.hw.mem_stream import KernelTask

        return KernelTask(
            "ltbf_cfg_sink_task",
            "ltbf_tasks.h",
            ("s_cfg",),
            template_args=(int(self.word_bits),),
            framed=("s_cfg",),
        )

    def run_iter(self):
        cfg = yield from self.s_cfg.get_schema(SysCfg)
        self.enable = int(cfg.enable)


@dataclass
class SysWrap(FreeRunMod):
    """The systolic array's device: ``SystolicEngineMem`` with its Job FIFO, Ctrl and done record."""

    cpp_kernel_name: ClassVar[str | None] = "ltbf_sys"
    #: What the system top re-creates the kernel from (``hw_module.build_params``).
    build_fields: ClassVar[tuple[str, ...]] = ("engine_cfg",)
    mm_views: ClassVar[tuple] = (
        QueueIn("qjob", port="s_job", depth=16),
        RegBank("ctrl", cfg_port="s_cfg", status_port="m_status", cfg_type=SysCfg,
                status_type=SysStatus),
    )  # fmt: skip

    engine_cfg: dict = field(default_factory=dict)
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.engine = SystolicEngineMem(
            name=f"{self.name}_eng", sim=self.sim, clk=self.clk, **self.engine_cfg
        )
        w = int(self.engine.word_bits)
        self.tap = SysTap(
            name=f"{self.name}_tap", sim=self.sim, clk=self.clk, word_bits=w
        )
        self.ctrl = SysCtrl(
            name=f"{self.name}_ctrl", sim=self.sim, clk=self.clk, word_bits=w
        )
        for comp in (self.engine, self.tap, self.ctrl):
            self.add_comp(comp)
        done_if = StreamIF(
            name=f"{self.name}_done_if",
            sim=self.sim,
            clk=self.clk,
            bitwidth=w,
            framed=True,
        )
        done_if.bind("master", self.engine.s_done)
        done_if.bind("slave", self.tap.s_done)
        self.add_if(done_if)
        # In the walk order the top generator pairs names with (children, then their ports).
        self.boundary = ["s_job", "m_in", "m_out", "m_resp", "m_status", "s_cfg"]
        self.s_job = self.engine.s_job
        self.m_in = self.engine.m_in
        self.m_out = self.engine.m_out
        self.m_resp = self.tap.m_resp
        self.s_cfg = self.ctrl.s_cfg
        self.m_status = self.tap.m_status
