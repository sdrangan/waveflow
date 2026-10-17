"""block_eq.py -- a block MMSE equalizer: a stream-only pipeline of three free-running modules.

One input stream carries, per command, the command, the channel estimates and the received symbols::

    s_in     : CmdHdr(tx_id, nrows, m) | est[0..nrows-1] = (hr, hi, N) | r[0..nrows*m-1]     each a burst
    m_out    : Resp(tx_id, nrows, m)   | z[0..k-1]                                          each a burst
    m_status : Status(tx_id, status)                                                       one per command

and the kernel is ``StreamBroadcast(2)`` (framework) feeding two stages:

* **gain** -- per row, the MMSE equalizer ``g = conj(h) / (|h|^2 + N)``: a division, once a row.  It
  reads the command and the estimates, sends ``GainHdr(tx_id, nvalid, code)`` and the ``g`` values to
  apply, then **keeps consuming**: it discards its copy of the symbols.
* **apply** -- per symbol, ``z = g r``.  It discards its copy of the estimates **first** (the broadcast's
  keep-consuming rule), then takes ``g``, writes the results, then the status: the last stage answers.

Float32 in a fixed operation order (one rounding per operation, no fused multiply-add); results compared
by their bits, signed zeros included.  An error is fatal (``docs/guide/patterns/command_response.md``):

* ``BAD_NOISE`` -- some ``N[i] <= 0``, the first such row ``i``: the results of rows before ``i``;
* ``TLAST_EARLY`` -- the estimates or the symbols end early (TLAST on a word before the last): the results
  of the symbols that arrived and have their row's ``g``.

The status reports the first cut in row-major order; at a tie, ``BAD_NOISE``.  Then the kernel stops.
The words are 64 bits: one complex symbol, or two floats, a word.

What is in this file, in order: the schemas, the golden, the scenarios, the two stages, the composite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.arrayutils import array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, FloatField, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave, StreamIF
from waveflow.streams import StreamBroadcast
from waveflow.utils.burst_io import StreamBurst

HERE = Path(__file__).resolve().parent
INCLUDE_DIR = "include"

#: The stream word: two float32s, or one complex symbol.
W = 64
#: The largest command the hardware buffers: rows of g in apply, rows of estimates in gain.
MAX_ROWS = 64
MAX_M = 256

# ---------------------------------------------------------------------------
# 1. Schemas
# ---------------------------------------------------------------------------

U16 = IntField.specialize(bitwidth=16, signed=False, include_dir=INCLUDE_DIR)
Float32 = FloatField.specialize(bitwidth=32, include_dir=INCLUDE_DIR)


class StatusCode(IntEnum):
    """What a status reports about its command."""
    OK = 0
    TLAST_EARLY = 1      # the estimates or the symbols ended (TLAST) early: fatal
    BAD_NOISE = 2        # some N[i] <= 0: fatal


StatusCodeField = EnumField.specialize(enum_type=StatusCode)


class CmdHdr(DataList):
    """One command: its id and its shape.  The estimates and the symbols follow, each a burst."""
    elements = {
        "tx_id": {"schema": U16, "description": "the sender's command id, echoed in the header and the status"},
        "nrows": {"schema": U16, "description": "rows: one channel estimate each, 1 to 64"},
        "m":     {"schema": U16, "description": "symbols per row, 1 to 256"},
    }


class GainHdr(DataList):
    """gain -> apply: the command's identity, the rows that have a g, and how gain's part ended.  The
    nvalid g values follow, a burst (none when nvalid = 0: a burst cannot be empty)."""
    elements = {
        "tx_id":  {"schema": U16,             "description": "the command it answers"},
        "nvalid": {"schema": U16,             "description": "rows with a g, from row 0"},
        "code":   {"schema": StatusCodeField, "description": "OK, BAD_NOISE (row nvalid) or TLAST_EARLY (estimates)"},
    }


class Resp(DataList):
    """The header of a command's results on m_out."""
    elements = {
        "tx_id": {"schema": U16, "description": "echo of the command's tx_id"},
        "nrows": {"schema": U16, "description": "echo of nrows"},
        "m":     {"schema": U16, "description": "echo of m"},
    }


class Status(DataList):
    """The last command seen, on m_status: one per command, after its results."""
    elements = {
        "tx_id":  {"schema": U16,             "description": "the command it reports on"},
        "status": {"schema": StatusCodeField, "description": "OK, TLAST_EARLY or BAD_NOISE (both fatal)"},
    }


#: In dependency order.
SCHEMA_CLASSES = [StatusCodeField, CmdHdr, GainHdr, Resp, Status]

# ---------------------------------------------------------------------------
# 2. The golden: float32, one rounding per operation, in this order
# ---------------------------------------------------------------------------

f32 = np.float32


def gain_golden(hr: np.ndarray, hi: np.ndarray, n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``g = conj(h) / (|h|^2 + N)``, row by row: t1 = hr*hr, t2 = hi*hi, d = t1 + t2, d = d + N,
    q = 1/d, gr = hr*q, gi = -(hi*q)."""
    hr, hi, n = (np.asarray(v, dtype=f32) for v in (hr, hi, n))
    with np.errstate(all="ignore"):
        d = (hr * hr) + (hi * hi)
        d = (d + n).astype(f32)
        q = (f32(1) / d).astype(f32)
        return (hr * q).astype(f32), (-(hi * q)).astype(f32)


def apply_golden(gr, gi, rr, ri) -> tuple[np.ndarray, np.ndarray]:
    """``z = g r``: p1 = gr*rr, p2 = gi*ri, zr = p1 - p2, p3 = gr*ri, p4 = gi*rr, zi = p3 + p4."""
    gr, gi, rr, ri = (np.asarray(v, dtype=f32) for v in (gr, gi, rr, ri))
    with np.errstate(all="ignore"):
        zr = ((gr * rr).astype(f32) - (gi * ri).astype(f32)).astype(f32)
        zi = ((gr * ri).astype(f32) + (gi * rr).astype(f32)).astype(f32)
    return zr, zi


def _interleave(re: np.ndarray, im: np.ndarray) -> np.ndarray:
    out = np.empty(2 * len(re), dtype=f32)
    out[0::2], out[1::2] = re, im
    return out


# ---------------------------------------------------------------------------
# 3. Scenarios: intents -> stimulus, expected outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cmd:
    """One command as intended: per row its channel ``h`` and noise ``n``, the symbols ``r`` (nrows x m),
    and how many **words** of each burst are actually sent (fewer is an early TLAST)."""

    tx_id: int
    h: np.ndarray          # complex, (nrows,)
    n: np.ndarray          # float, (nrows,)
    r: np.ndarray          # complex, (nrows, m)
    est_words: int | None = None
    sym_words: int | None = None

    @property
    def nrows(self) -> int:
        return int(self.r.shape[0])

    @property
    def m(self) -> int:
        return int(self.r.shape[1])

    def est_floats(self) -> np.ndarray:
        e = np.empty(3 * self.nrows, dtype=f32)
        e[0::3], e[1::3], e[2::3] = self.h.real, self.h.imag, self.n
        return e

    def sym_floats(self) -> np.ndarray:
        r = self.r.reshape(-1)
        return _interleave(r.real.astype(f32), r.imag.astype(f32))


def _words(floats: np.ndarray, nwords: int | None) -> np.ndarray:
    """The burst's words, cut to *nwords* -- the unused lanes of the last word are zero."""
    w = np.asarray(write_array(floats, elem_type=Float32, word_bw=W), dtype=np.uint64)
    return w if nwords is None else w[:nwords]


def stimulus(cmds: list[Cmd]) -> list[StreamBurst]:
    """The input stream: per command, three bursts -- the command, the estimates, the symbols."""
    out: list[StreamBurst] = []
    for c in cmds:
        hdr = CmdHdr(tx_id=c.tx_id, nrows=c.nrows, m=c.m)
        out.append(StreamBurst(np.asarray(hdr.serialize(word_bw=W), dtype=np.uint64), True))
        out.append(StreamBurst(_words(c.est_floats(), c.est_words), True))
        out.append(StreamBurst(_words(c.sym_floats(), c.sym_words), True))
    return out


def gain_part(c: Cmd) -> tuple[int, StatusCode, np.ndarray, np.ndarray]:
    """What gain computes from what arrived: ``(nvalid, code, gr, gi)`` -- every lane of every word that
    arrived counts, so a row is valid when its three floats arrived."""
    floats = c.est_floats()
    arrived = len(floats) if c.est_words is None else min(len(floats), 2 * c.est_words)
    complete = arrived // 3
    gr, gi = gain_golden(floats[0::3][:complete], floats[1::3][:complete], floats[2::3][:complete])
    bad = np.flatnonzero(~(floats[2::3][:complete] > 0))
    if len(bad):
        return int(bad[0]), StatusCode.BAD_NOISE, gr[: bad[0]], gi[: bad[0]]
    if complete < c.nrows:
        return complete, StatusCode.TLAST_EARLY, gr, gi
    return c.nrows, StatusCode.OK, gr, gi


def outcome(c: Cmd) -> tuple[int, StatusCode]:
    """``(results written, status)``: the first cut in row-major order wins; at a tie, gain's code."""
    nvalid, code, _gr, _gi = gain_part(c)
    total = c.nrows * c.m
    arrived = total if c.sym_words is None else min(total, c.sym_words)
    need = nvalid * c.m
    if arrived < need:
        return arrived, StatusCode.TLAST_EARLY
    return need, code


def expected(cmds: list[Cmd]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """The two outputs' bursts, from the intents: per command the ``Resp`` header, the results burst (if
    any results), the status -- up to and including the first error, after which nothing appears."""
    out, status = [], []
    for c in cmds:
        k, code = outcome(c)
        _nv, _code, gr, gi = gain_part(c)
        out.append(np.asarray(Resp(tx_id=c.tx_id, nrows=c.nrows, m=c.m).serialize(word_bw=W),
                              dtype=np.uint64))
        if k:
            rows = np.arange(k) // c.m
            r = c.r.reshape(-1)[:k]
            zr, zi = apply_golden(gr[rows], gi[rows], r.real, r.imag)
            out.append(_words(_interleave(zr, zi), None))
        status.append(np.asarray(Status(tx_id=c.tx_id, status=code).serialize(word_bw=W),
                                 dtype=np.uint64))
        if code != StatusCode.OK:
            break
    return out, status


def link_expected(cmds: list[Cmd]) -> list[np.ndarray]:
    """gain -> apply, from the intents: per command ``GainHdr`` and, if any, the g burst -- the stimulus
    of apply's own harness, and gain's expected output."""
    out = []
    for c in cmds:
        nvalid, code, gr, gi = gain_part(c)
        out.append(np.asarray(GainHdr(tx_id=c.tx_id, nvalid=nvalid, code=code).serialize(word_bw=W),
                              dtype=np.uint64))
        if nvalid:
            out.append(_words(_interleave(gr, gi), None))
        if code != StatusCode.OK:
            break
    return out


def worked_cmd() -> Cmd:
    """The worked examples: three rows of two symbols."""
    h = np.array([1 + 0j, 0 + 2j, 3 + 4j], dtype=np.complex64)
    n = np.array([1.0, 0.5, 0.25], dtype=f32)
    r = np.array([[2 + 4j, -1 + 0.5j], [1 + 1j, 0 - 3j], [3 + 4j, 1 - 2j]], dtype=np.complex64)
    return Cmd(tx_id=1, h=h, n=n, r=r)


def random_cmd(rng, tx_id: int, nrows: int, m: int, **kw) -> Cmd:
    h = (rng.normal(size=nrows) + 1j * rng.normal(size=nrows)).astype(np.complex64)
    n = rng.uniform(0.05, 2.0, nrows).astype(f32)
    r = (rng.normal(size=(nrows, m)) + 1j * rng.normal(size=(nrows, m))).astype(np.complex64)
    return Cmd(tx_id=tx_id, h=h, n=n, r=r, **kw)


def default_cmds(seed: int = 3) -> list[Cmd]:
    """The test scenario: the worked examples, commands of every shape, then an error -- a zero noise in
    a middle row, fatal, so the command after it is never answered."""
    rng = np.random.default_rng(seed)
    cmds = [worked_cmd(), random_cmd(rng, 2, 1, 1), random_cmd(rng, 3, 4, 7), random_cmd(rng, 4, 16, 1),
            random_cmd(rng, 5, 2, 33)]
    bad = random_cmd(rng, 6, 5, 3)
    nb = bad.n.copy()
    nb[2] = 0.0
    cmds.append(Cmd(tx_id=6, h=bad.h, n=nb, r=bad.r))
    cmds.append(random_cmd(rng, 7, 3, 3))
    return cmds


def early_cmds(seed: int = 4) -> list[Cmd]:
    """Two good commands, then symbols that end early mid-row (fatal), then one never answered."""
    rng = np.random.default_rng(seed)
    return [random_cmd(rng, 11, 3, 4), random_cmd(rng, 12, 2, 5),
            random_cmd(rng, 13, 4, 6, sym_words=9), random_cmd(rng, 14, 2, 2)]


# ---------------------------------------------------------------------------
# 4. The two stages
# ---------------------------------------------------------------------------


def _halt(mod):
    """Fatal: an event nothing triggers -- the module stops, as its RTL does, until a reset."""
    yield mod.event()


@dataclass
class Gain(FreeRunMod):
    """Stage 1: per command, ``g`` for each row, then the symbols discarded (keep consuming)."""

    cpp_kernel_name: ClassVar[str | None] = "eq_gain"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=W)
        self.m_g = FramedStreamIFMaster(name=f"{self.name}_m_g", sim=self.sim, bitwidth=W)
        for ep in (self.s_in, self.m_g):
            self.add_endpoint(ep)

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("eq_gain_task", "eq_gain_task.h", ("s_in", "m_g"), template_args=(W,),
                          framed=("s_in", "m_g"))

    def workload(self, kind: str = "test") -> dict:
        return {"s_in": stimulus(default_cmds() if kind == "test" else early_cmds())}

    def run_iter(self):
        hdr = yield from self.s_in.get_schema(CmdHdr)
        nrows, m = int(hdr.nrows), int(hdr.m)
        est = (yield from self.s_in.get_array(Float32, 3 * nrows)).val
        complete = len(est) // 3
        gr, gi = gain_golden(est[0::3][:complete], est[1::3][:complete], est[2::3][:complete])
        bad = np.flatnonzero(~(est[2::3][:complete] > 0))
        if len(bad):
            nvalid, code = int(bad[0]), StatusCode.BAD_NOISE
        elif complete < nrows:
            nvalid, code = complete, StatusCode.TLAST_EARLY
        else:
            nvalid, code = nrows, StatusCode.OK
        yield from self.m_g.write(GainHdr(tx_id=hdr.tx_id, nvalid=nvalid, code=code))
        if nvalid:
            yield from self.m_g.write_array(Float32, 2 * nvalid,
                                            array(Float32, _interleave(gr[:nvalid], gi[:nvalid])))
        # Keep consuming: the symbols are apply's, but this copy must be read, or the broadcast stalls.
        sym = (yield from self.s_in.get_array(Float32, 2 * nrows * m)).val
        if code != StatusCode.OK or len(sym) < 2 * nrows * m:
            yield from _halt(self)


@dataclass
class Apply(FreeRunMod):
    """Stage 2: per command, the estimates discarded first, then ``g``, then ``z = g r``, then the status."""

    cpp_kernel_name: ClassVar[str | None] = "eq_apply"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=W)
        self.s_g = FramedStreamIFSlave(name=f"{self.name}_s_g", sim=self.sim, bitwidth=W)
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=W)
        self.m_status = FramedStreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=W)
        for ep in (self.s_in, self.s_g, self.m_out, self.m_status):
            self.add_endpoint(ep)

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask
        ports = ("s_in", "s_g", "m_out", "m_status")
        return KernelTask("eq_apply_task", "eq_apply_task.h", ports, template_args=(W,), framed=ports)

    def workload(self, kind: str = "test") -> dict:
        cmds = default_cmds() if kind == "test" else early_cmds()
        g = [StreamBurst(b, True) for b in link_expected(cmds)]
        return {"s_in": stimulus(cmds), "s_g": g}

    def run_iter(self):
        hdr = yield from self.s_in.get_schema(CmdHdr)
        nrows, m = int(hdr.nrows), int(hdr.m)
        # Keep consuming, first: the estimates are gain's, but this copy must be read before waiting on
        # gain -- waiting first would stall the broadcast, and gain would never get the rest of them.
        yield from self.s_in.get_array(Float32, 3 * nrows)
        gh = yield from self.s_g.get_schema(GainHdr)
        nvalid = int(gh.nvalid)
        g = (yield from self.s_g.get_array(Float32, 2 * nvalid)).val if nvalid else np.zeros(0, f32)
        yield from self.m_out.write(Resp(tx_id=hdr.tx_id, nrows=nrows, m=m))
        sym = (yield from self.s_in.get_array(Float32, 2 * nrows * m)).val
        need, arrived = nvalid * m, len(sym) // 2
        k = min(need, arrived)
        if k:
            rows = np.arange(k) // m
            zr, zi = apply_golden(g[0::2][rows], g[1::2][rows], sym[0:2 * k:2], sym[1:2 * k:2])
            yield from self.m_out.write_array(Float32, 2 * k, array(Float32, _interleave(zr, zi)))
        if arrived < need:
            code = StatusCode.TLAST_EARLY
        else:
            code = StatusCode(int(gh.code))
        yield from self.m_status.write(Status(tx_id=hdr.tx_id, status=code))
        if code != StatusCode.OK:
            yield from _halt(self)


# ---------------------------------------------------------------------------
# 5. The composite: StreamBroadcast(2) -> gain, apply
# ---------------------------------------------------------------------------


@dataclass
class BlockEq(FreeRunMod):
    """``s_in -> StreamBroadcast(2) -> gain -> apply``, the broadcast's second copy into apply; apply
    writes ``m_out`` and ``m_status``."""

    cpp_kernel_name: ClassVar[str | None] = "block_eq"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.bcast = StreamBroadcast(name=f"{self.name}_bcast", sim=self.sim, n_out=2, bitwidth=W,
                                     clk=self.clk)
        self.gain = Gain(name=f"{self.name}_gain", sim=self.sim, clk=self.clk)
        self.apply = Apply(name=f"{self.name}_apply", sim=self.sim, clk=self.clk)
        for c in (self.bcast, self.gain, self.apply):
            self.add_comp(c)

        def link(name, master, slave, depth):
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=W,
                             framed=True, depth=depth)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        # The two copies are deep enough for a command's estimates (3 * 64 floats = 96 words), so the
        # broadcast never waits on apply while gain still needs estimates; the g link holds one command.
        link("est_gain", self.bcast.m_out_0, self.gain.s_in, 128)
        link("est_apply", self.bcast.m_out_1, self.apply.s_in, 128)
        link("g", self.gain.m_g, self.apply.s_g, 128)

        self.boundary = ["s_in", "m_out", "m_status"]
        self.s_in = self.bcast.s_in
        self.m_out = self.apply.m_out
        self.m_status = self.apply.m_status

    def workload(self, kind: str = "test") -> dict:
        return {"s_in": stimulus(default_cmds() if kind == "test" else early_cmds())}
