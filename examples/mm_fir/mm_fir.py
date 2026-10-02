"""mm_fir.py — a free-running FIR whose taps are registers and whose samples are a queue.

The witness example of ``plans/mm_slave_adaptor.md``.  The kernel is an ordinary stream-only
``FreeRunMod``; every memory-mapped view it has is an adaptor module in front of it, reached by a
host through one crossbar:

====================  =========  ===========================================================
view                  base       kernel side
====================  =========  ===========================================================
register bank         0x0000     ``s_cfg`` <- one :class:`FirCfg` per commit;
                                 ``m_status`` -> :class:`FirStatus` (latest-value)
queue in              0x1000     ``s_in``  <- sample packets ``[len | x x len]``
queue out             0x2000     ``m_out`` -> one output word per sample
====================  =========  ===========================================================

(The same 1x3 shape as the Stage 2 RTL gate, ``tests/build/test_mm_regbank_xsi.py``.)

**Cross-view order is carried in the messages** (the plan's decision D5).  A configuration and the
samples travel on different streams, so the kernel could see them in either order.  A config therefore
says *where* it applies -- ``apply_at``, a sample index -- and the kernel switches taps exactly there.
The host makes that happen in time by waiting until the status shows the config was **received**
(``ncfg``) before sending the sample at ``apply_at``.  A config that arrives after its sample has
already been filtered is applied at once and counted in ``late``: detected, never silently misapplied.

The kernel polls both streams, config first -- the pysim model of an HLS ``read_nb`` loop -- so it
picks up a commit even while no samples are arriving, which is exactly what the host's wait needs.

Numbers: samples are int16, one per 64-bit word (low bits); taps are int16; outputs are the exact
integer convolution, int64 two's complement in a 64-bit word.  No rounding anywhere, so the numpy
golden :func:`fir_golden` is bit-exact by construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, DataList, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges
from waveflow.hw.mm_queue import MemSlaveRStream, MemSlaveWStream
from waveflow.hw.mm_regbank import MemSlaveRegBank
from waveflow.simulation.simobj import SimObj
from waveflow.simulation.simulation import Simulation

DW = 64
NTAP_MAX = 16
QDEPTH = 64

S16 = IntField.specialize(bitwidth=16, signed=True)
U32 = IntField.specialize(bitwidth=32, signed=False)
Taps = DataArray.specialize(S16, max_shape=(NTAP_MAX,))


class FirCfg(DataList):
    """One configuration: ``ntaps`` taps from ``coeffs``, in force from sample ``apply_at`` on."""

    elements = {
        "ntaps": {"schema": U32, "description": "active taps (<= NTAP_MAX)"},
        "apply_at": {"schema": U32, "description": "index of the first sample filtered with these taps"},
        "coeffs": {"schema": Taps, "description": "tap k multiplies x[n-k]"},
    }


class FirStatus(DataList):
    """What the kernel publishes after every event (latest value wins)."""

    elements = {
        "nsamp": {"schema": U32, "description": "samples filtered so far"},
        "ncfg": {"schema": U32, "description": "configs received so far"},
        "late": {"schema": U32, "description": "configs that arrived after their apply_at sample"},
    }


REGS, QIN, QOUT = 0x0000, 0x1000, 0x2000


# ---------------------------------------------------------------------------
# Golden
# ---------------------------------------------------------------------------

def fir_golden(x, cfgs) -> np.ndarray:
    """Exact FIR over the whole stream: sample *n* uses the latest config with ``apply_at <= n``.

    The filter history is continuous across a switch (only the taps change), and samples before the
    first config see all-zero taps.  *cfgs* is ``[(apply_at, taps), ...]``.
    """
    x = np.asarray(x, dtype=np.int64)
    y = np.zeros(len(x), dtype=np.int64)
    order = sorted(cfgs, key=lambda c: c[0])
    for n in range(len(x)):
        taps = np.zeros(0, dtype=np.int64)
        for at, t in order:
            if at <= n:
                taps = np.asarray(t, dtype=np.int64)
        for k, c in enumerate(taps):
            if n - k >= 0:
                y[n] += int(c) * int(x[n - k])
    return y


def make_cfg(taps, apply_at: int) -> FirCfg:
    taps = list(taps)
    if not 1 <= len(taps) <= NTAP_MAX:
        raise ValueError(f"1..{NTAP_MAX} taps, got {len(taps)}")
    coeffs = np.zeros(NTAP_MAX, dtype=np.int64)
    coeffs[:len(taps)] = taps
    return FirCfg(ntaps=len(taps), apply_at=int(apply_at), coeffs=coeffs)


def _s16(w: int) -> int:
    w &= 0xFFFF
    return w - 0x10000 if w & 0x8000 else w


# ---------------------------------------------------------------------------
# The kernel
# ---------------------------------------------------------------------------

@dataclass
class MmFir(FreeRunMod):
    """The FIR kernel: streams only.  One firing = one config, one sample packet, or one idle cycle."""

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    ntap_max: HwParam[int] = NTAP_MAX

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_cfg = StreamIFSlave(name=f"{self.name}_s_cfg", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.m_status = StreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=DW,
                                       has_tlast=True)
        for ep in (self.s_cfg, self.s_in, self.m_out, self.m_status):
            self.add_endpoint(ep)
        self.taps = np.zeros(0, dtype=np.int64)
        self.pending: list[tuple[int, np.ndarray]] = []     # received, not yet in force
        self.hist = np.zeros(NTAP_MAX, dtype=np.int64)      # hist[k] = x[n-1-k]
        self.nsamp = 0
        self.ncfg = 0
        self.late = 0

    def _publish(self):
        yield from self.m_status.write(FirStatus(nsamp=self.nsamp, ncfg=self.ncfg, late=self.late))

    def run_iter(self):
        cfg = yield from self.s_cfg.get_schema_nb(FirCfg)
        if cfg is not None:
            self.ncfg += 1
            taps = np.asarray(cfg.coeffs, dtype=np.int64)[:int(cfg.ntaps)]
            at = int(cfg.apply_at)
            if at < self.nsamp:
                self.late += 1
                at = self.nsamp                     # too late for its sample: in force from now
            self.pending.append((at, taps))
            self.pending.sort(key=lambda p: p[0])
            yield from self._publish()
            return
        if self.s_in.data_buffer.items:
            pkt = yield from self.s_in.get()
            out = np.zeros(len(pkt), dtype=np.uint64)
            for i, w in enumerate(np.asarray(pkt).tolist()):
                while self.pending and self.pending[0][0] <= self.nsamp:
                    self.taps = self.pending.pop(0)[1]
                xn = _s16(int(w))
                window = np.concatenate([[xn], self.hist[:NTAP_MAX - 1]])
                acc = int(np.dot(self.taps, window[:len(self.taps)])) if len(self.taps) else 0
                out[i] = np.uint64(acc & 0xFFFF_FFFF_FFFF_FFFF)
                self.hist = window[:NTAP_MAX]
                self.nsamp += 1
            yield from self.m_out.write(out)
            yield from self._publish()
            return
        yield self.timeout(self.clk.period)         # idle: poll again next cycle


# ---------------------------------------------------------------------------
# The host and the system
# ---------------------------------------------------------------------------

@dataclass
class FirHost(SimObj):
    """The host program: configure, stream samples, switch taps mid-stream, drain the results.

    *plan* is ``[(apply_at, taps), ...]`` -- the first entry must have ``apply_at = 0``.  Samples go
    out in packets of ``pkt`` words; before each, the host drains any ready outputs (queue out is
    only ``QDEPTH`` deep, and a full output queue would stop the kernel taking input) and waits for
    room in queue in.
    """

    x: list = field(default_factory=list)
    plan: list = field(default_factory=list)
    pkt: int = 16
    poll_cycles: int = 8
    #: Negative-control knob: commit each config (after the first) this many samples AFTER its
    #: ``apply_at``.  0 is the correct host.  >0 makes the config late, which the kernel must report.
    lag: int = 0
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=DW)
        self.done = self.env.event()
        self.y: list[int] = []
        self.status_reads = 0

    def _status(self):
        words = yield from self.m.read(FirStatus.nwords_per_inst(DW), REGS + 0xC00)
        self.status_reads += 1
        return FirStatus().deserialize(np.asarray(words, dtype=np.uint64), word_bw=DW)

    def _commit(self, cfg: FirCfg, want_ncfg: int):
        yield from self.m.write(np.asarray(cfg.serialize(word_bw=DW), dtype=np.uint64), REGS)
        yield from self.m.write(np.asarray([1], dtype=np.uint64), REGS + 0x800)
        while True:                                   # received, so it cannot miss its sample
            st = yield from self._status()
            if int(st.ncfg) >= want_ncfg:
                return
            yield self.timeout(self.poll_cycles * self.clk.period)

    def _drain(self):
        occ = int((yield from self.m.read(1, QOUT + 0x800))[0])
        while occ > 0:
            n = min(occ, 256)
            words = yield from self.m.read(n, QOUT)
            self.y += [int(np.int64(np.uint64(w))) for w in np.asarray(words)]
            occ -= n

    def run_proc(self):
        cfgs = sorted(self.plan, key=lambda c: c[0])
        if not cfgs or cfgs[0][0] != 0:
            raise ValueError("the first config must apply at sample 0")
        nxt = 0
        n = 0
        def due(i: int) -> int:                       # sample count at which config i is committed
            return cfgs[i][0] + (self.lag if i else 0)

        while n < len(self.x):
            while nxt < len(cfgs) and due(nxt) <= n:
                yield from self._commit(make_cfg(cfgs[nxt][1], cfgs[nxt][0]), nxt + 1)
                nxt += 1
            # A packet never straddles a commit point: the config for it is committed first.
            end = min(n + self.pkt, len(self.x), due(nxt) if nxt < len(cfgs) else len(self.x))
            chunk = [int(v) & 0xFFFF for v in self.x[n:end]]
            yield from self._drain()
            while int((yield from self.m.read(1, QIN))[0]) < len(chunk):
                yield self.timeout(self.poll_cycles * self.clk.period)
            yield from self.m.write(np.asarray([len(chunk)] + chunk, dtype=np.uint64), QIN)
            n = end
        while len(self.y) < len(self.x):
            yield from self._drain()
            if len(self.y) < len(self.x):
                yield self.timeout(self.poll_cycles * self.clk.period)
        self.final_status = yield from self._status()
        self.done.succeed()


@dataclass
class MmFirSystem:
    """Everything wired: host -> crossbar -> {register bank, queue in, queue out} <-> kernel."""

    x: list
    plan: list
    pkt: int = 16
    lag: int = 0
    xbar_latency: float = 4.0
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        sim = self.sim = Simulation()
        clk = self.clk
        self.regs = MemSlaveRegBank(name="regs", sim=sim, cfg_type=FirCfg, status_type=FirStatus,
                                    mem_dwidth=DW, clk=clk)
        self.qin = MemSlaveWStream(name="qin", sim=sim, mem_dwidth=DW, depth=QDEPTH, clk=clk)
        self.qout = MemSlaveRStream(name="qout", sim=sim, mem_dwidth=DW, depth=QDEPTH, clk=clk)
        self.fir = MmFir(name="fir", sim=sim, clk=clk)
        self.host = FirHost(name="host", sim=sim, x=list(self.x), plan=list(self.plan), pkt=self.pkt,
                            lag=self.lag, clk=clk)
        for name, m, s, depth in (
            ("k_cfg", self.regs.m_cfg, self.fir.s_cfg, self.regs.ncfg),
            ("k_stat", self.fir.m_status, self.regs.s_status, 8),
            ("k_in", self.qin.m_out, self.fir.s_in, QDEPTH),
            ("k_out", self.fir.m_out, self.qout.s_in, QDEPTH),
        ):
            si = StreamIF(name=name, sim=sim, clk=clk, bitwidth=DW, depth=depth)
            si.bind(ep_name="master", endpoint=m)
            si.bind(ep_name="slave", endpoint=s)
        self.xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=1, nports_slave=3,
                                    bitwidth=DW, latency_init=self.xbar_latency)
        self.xbar.bind("master_0", self.host.m)
        for k, ep in enumerate((self.regs.s_mem, self.qin.s_mem, self.qout.s_mem)):
            self.xbar.bind(f"slave_{k}", ep)
        assign_address_ranges([self.regs.s_mem, self.qin.s_mem, self.qout.s_mem],
                              [(REGS, 0x1000), (QIN, 0x1000), (QOUT, 0x1000)])

    def run(self) -> np.ndarray:
        self.sim.run_sim(until=self.host.done)
        return np.asarray(self.host.y, dtype=np.int64)


def demo(seed: int = 7, nsamp: int = 200, switch_at: int = 96) -> dict:
    """Rung 2: two tap sets, switched at ``switch_at``.  Returns the run's facts for a caller."""
    rng = np.random.default_rng(seed)
    x = rng.integers(-2000, 2000, size=nsamp)
    plan = [(0, [3, -1, 4, 1, -5]), (switch_at, [2, 7, 1, -8, 2, 8, 1, -8])]
    sysm = MmFirSystem(x=list(x), plan=plan)
    y = sysm.run()
    return {"x": x, "y": y, "golden": fir_golden(x, plan), "status": sysm.host.final_status,
            "cycles": sysm.sim.env.now / sysm.clk.period}


if __name__ == "__main__":
    r = demo()
    ok = np.array_equal(r["y"], r["golden"])
    st = r["status"]
    print(f"bit-exact={ok}  nsamp={int(st.nsamp)} ncfg={int(st.ncfg)} late={int(st.late)}  "
          f"cycles={r['cycles']:.0f}")
