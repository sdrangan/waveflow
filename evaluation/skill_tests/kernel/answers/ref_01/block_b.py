"""Block B: burst energy monitor -- data and settings from different sources, a host on the bus.

The samples come from the ADC; the settings and the reads come from the host over the bus. They are
different sources, so they are different streams, and the module's logic sees only streams::

    s_samp   : x[0] .. x[255]                 a frame from the ADC (TLAST on its last sample)
    s_cfg    : MonCfg(threshold, shift)       <- register bank, one message per commit
    m_report : Report(frame, energy)          -> queue out; the host waits on its interrupt
    m_status : MonStatus(nframes, fault)      -> the register bank's status (it keeps the latest)

No command stream: nothing starts a frame but the ADC.  One firing = one frame.  The settings are
checked once per frame, before its first sample (``get_schema_nb``: a change is taken, no change is
not waited for), so a frame is never processed with a mix; the first frame waits for the first
setting (a blocking read).  A short frame is a fault: reported, then the monitor stops.

The interrupt-enable and clear-on-write status registers asked for are not built in Waveflow: a queue
view's interrupt is a level line with a threshold, high while that many words wait, which the host's
endpoint manages (``interface/axi_mm/slave.md``, "What is not built yet").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

import numpy as np

from waveflow.hw.arrayutils import write_array
from waveflow.hw.clock import Clock
from waveflow.hw.complexfield import ComplexField
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import FramedStreamIFSlave, StreamIFMaster, StreamIFSlave
from waveflow.hw.mm_device import QueueOut, RegBank
from waveflow.utils import complexutils as cx
from waveflow.utils.burst_io import StreamBurst
from waveflow.utils.fixputils import Format

W = 32
FRAME = 256

S16 = IntField.specialize(bitwidth=16, signed=True)
CS16 = ComplexField.specialize(S16)                 # I in the low half, Q in the high
U8 = IntField.specialize(bitwidth=8, signed=False)
U32 = IntField.specialize(bitwidth=32, signed=False)
U64 = IntField.specialize(bitwidth=64, signed=False)


class Fault(IntEnum):
    NONE = 0
    SHORT_FRAME = 1


FaultField = EnumField.specialize(enum_type=Fault)


class MonCfg(DataList):
    elements = {"threshold": {"schema": U32}, "shift": {"schema": U8}}


class Report(DataList):
    elements = {"frame": {"schema": U32}, "energy": {"schema": U64}}


class MonStatus(DataList):
    elements = {"nframes": {"schema": U32}, "fault": {"schema": FaultField}}


# --- the golden ---------------------------------------------------------------------------------

def energy(re: np.ndarray, im: np.ndarray, shift: int) -> int:
    return int((np.sum(re.astype(np.int64) ** 2 + im.astype(np.int64) ** 2)) >> shift)


def frames(n: int, seed: int = 3, short: int | None = None) -> list[np.ndarray]:
    """*n* frames of complex samples (structured re/im), frame *short* cut to 100 samples."""
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n):
        m = 100 if k == short else FRAME
        amp = 300 * (1 + (k % 4))                                   # some frames over threshold
        re, im = rng.integers(-amp, amp, (2, m))
        out.append(cx.make_complex(re, im, Format(16, 16, True)))
    return out


def samp_bursts(fr: list[np.ndarray]) -> list[StreamBurst]:
    return [StreamBurst(np.asarray(write_array(f, elem_type=CS16, word_bw=W), np.uint64), True)
            for f in fr]


def cfg_burst(threshold: int, shift: int) -> StreamBurst:
    return StreamBurst(np.asarray(MonCfg(threshold=threshold, shift=shift).serialize(word_bw=W),
                                  np.uint64), True)


def expected(fr: list[np.ndarray], threshold: int, shift: int):
    """One setting for the whole scenario: the reports, and the statuses (one per frame)."""
    rep, st = [], []
    for k, f in enumerate(fr):
        if len(f) < FRAME:
            st.append(MonStatus(nframes=k, fault=Fault.SHORT_FRAME))
            break
        e = energy(f["re"], f["im"], shift)
        if e >= threshold:
            rep.append(Report(frame=k, energy=e))
        st.append(MonStatus(nframes=k + 1, fault=Fault.NONE))
    return rep, st


#: The scenarios: frames, and the settings the host commits, in order.
SCENARIOS = {
    "test": dict(n=8, short=None, cfgs=[(2_000_000, 4)]),
    "fault": dict(n=6, short=3, cfgs=[(2_000_000, 4)]),
    "retune": dict(n=8, short=None, cfgs=[(0, 4), (0, 6)]),   # every frame reported; shift changes
}


# --- the module -----------------------------------------------------------------------------------

@dataclass
class EnergyMonitor(FreeRunMod):
    """One firing = one frame."""

    cpp_kernel_name: ClassVar[str | None] = "energy_monitor"
    #: How the host reaches it (built by the system step, not here): the settings and the status in a
    #: register bank, the reports in a queue whose interrupt wakes the host.
    mm_views: ClassVar[tuple] = (
        RegBank("regs", cfg_port="s_cfg", status_port="m_status",
                cfg_type=MonCfg, status_type=MonStatus),
        QueueOut("q_report", port="m_report", depth=64),
    )
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_samp = FramedStreamIFSlave(name=f"{self.name}_s_samp", sim=self.sim, bitwidth=W)
        self.s_cfg = StreamIFSlave(name=f"{self.name}_s_cfg", sim=self.sim, bitwidth=W, has_tlast=True)
        # A queue out carries no packet boundary to the bus: unframed, a fixed-size Report each.
        self.m_report = StreamIFMaster(name=f"{self.name}_m_report", sim=self.sim, bitwidth=W,
                                       has_tlast=False)
        self.m_status = StreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=W,
                                       has_tlast=True)
        for ep in (self.s_samp, self.s_cfg, self.m_report, self.m_status):
            self.add_endpoint(ep)
        self.cfg: MonCfg | None = None
        self.nframes = 0

    def workload(self, kind: str = "test") -> dict:
        sc = SCENARIOS[kind]
        return {"s_samp": samp_bursts(frames(sc["n"], short=sc["short"])),
                "s_cfg": [cfg_burst(*c) for c in sc["cfgs"]]}

    def run_iter(self):
        # The settings, before the frame's first sample: wait for the first; afterwards take the
        # latest committed, if any, without waiting.
        if self.cfg is None:
            self.cfg = yield from self.s_cfg.get_schema(MonCfg)
        while True:
            new = yield from self.s_cfg.get_schema_nb(MonCfg)
            if new is None:
                break
            self.cfg = new
        x = yield from self.s_samp.get_array(CS16, FRAME)
        if len(x.val) < FRAME:
            yield from self.m_status.write(MonStatus(nframes=self.nframes, fault=Fault.SHORT_FRAME))
            yield self.event()                                   # fatal: only a reset recovers
        e = energy(x.val["re"], x.val["im"], self.cfg.shift)
        if e >= self.cfg.threshold:
            yield from self.m_report.write(Report(frame=self.nframes, energy=e))
        self.nframes += 1
        yield from self.m_status.write(MonStatus(nframes=self.nframes, fault=Fault.NONE))
