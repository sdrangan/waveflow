"""poly.py -- the streaming polynomial accelerator, written hook-first.

What is in this file, in the order a reader needs it:

1. **Schemas** -- the command header, response header, error codes and coefficient array.
   Waveflow generates their C++ headers and serializers; nothing packs words by hand.
2. **The bit-exact model** -- pure functions with no simulator in them:
   :func:`poly_eval` (the arithmetic, float32 in the C++ operation order) and
   :func:`poly_stream_model` (the whole protocol over a stream of bursts, TLAST rules
   included).  This is what a system simulation calls, and what the C++ kernel is
   checked against, byte for byte.
3. **The module** -- :class:`PolyAccel` declares the ports and the register map and names
   its kernel body (``cpp_body = "body"``).  Waveflow generates the kernel's boundary (the
   prototype and every interface pragma) and a single call to the hand-written C++ body,
   ``poly_body_impl.tpp``.  The Python :meth:`PolyAccel.body` is the same kernel for
   pysim: a thin port wrapper around :func:`poly_eval`, plus a timing model.
4. **The pysim testbench** -- :class:`PolyTB` plays a scenario's stimulus file into the
   module, the same file the hand-written C++ testbench (``poly_tb.cpp``) plays into the
   Vitis kernel.

The scenarios, their independently computed expected outputs, and the checker are in
``scenarios.py``; the build that runs everything is ``poly_build.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

import numpy as np
import numpy.typing as npt

from waveflow.hw.arrayutils import array, read_array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, DataList, EnumField, FloatField, IntField
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.hw_module import HwConst, HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import DirectMMIF, MMIFMaster
from waveflow.hw.regmap import (
    Bit,
    RegAccess,
    RegField,
    VitisRegMap,
    VitisRegMapMMIFSlave,
)
from waveflow.simulation.logger import Logger, NullLogger
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation
from waveflow.utils.burst_io import StreamBurst, read_bursts

# ---------------------------------------------------------------------------
# 1. Schemas
# ---------------------------------------------------------------------------

INCLUDE_DIR = "include"
WORD_BW_SUPPORTED = [32, 64]
TxIdField = IntField.specialize(bitwidth=16, signed=False)
NsampField = IntField.specialize(bitwidth=16, signed=False)
Float32 = FloatField.specialize(bitwidth=32, include_dir=INCLUDE_DIR)


class PolyError(IntEnum):
    NO_ERROR = 0
    TLAST_EARLY_CMD_HDR = 1
    NO_TLAST_CMD_HDR = 2
    TLAST_EARLY_SAMP_IN = 3
    NO_TLAST_SAMP_IN = 4
    WRONG_NSAMP = 5

PolyErrorField = EnumField.specialize(enum_type=PolyError)


class PolyCmdType(IntEnum):
    DATA = 0
    END = 1

PolyCmdTypeField = EnumField.specialize(enum_type=PolyCmdType)


class CoeffArray(DataArray):
    """Array of polynomial coefficients in ascending order (constant term first)."""
    ncoeff: HwConst[int] = 4
    element_type = Float32
    static = True
    max_shape = (ncoeff,)
    cpp_storage = "raw"


class PolyCmdHdr(DataList):
    """Command header: type, transaction ID, and sample count.

    Coefficients are configured separately via the AXI-Lite register map.
    The END variant carries cmd_type=END and nsamp=0; it signals the kernel
    to break the persistent processing loop and return cleanly.
    """
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField, "description": "DATA or END"},
        "tx_id":    {"schema": TxIdField,        "description": "Transaction ID"},
        "nsamp":    {"schema": NsampField,       "description": "Sample count (0 for END)"},
    }


class PolyRespHdr(DataList):
    """Response header: echo of the transaction ID."""
    elements = {
        "tx_id": {"schema": TxIdField, "description": "Echo of the transaction ID"},
    }


SCHEMA_CLASSES = [
    PolyErrorField,
    PolyCmdTypeField,
    CoeffArray,
    PolyCmdHdr,
    PolyRespHdr,
]

# ---------------------------------------------------------------------------
# 2. The bit-exact model (pure functions)
# ---------------------------------------------------------------------------


def poly_eval(coeffs: npt.ArrayLike, x: npt.ArrayLike) -> npt.NDArray[np.float32]:
    """``y = c0 + c1 x + c2 x^2 + c3 x^3`` in float32, in Horner order.

    Bit-exact to the C++ body: the same float32 operations in the same order, each rounded
    to float32 (``y = c3; y = y*x; y = y + c2; ...``).  The C++ keeps every multiply and add
    a separate statement so the compiler cannot fuse them into a multiply-add, which would
    round once instead of twice.
    """
    c = np.asarray(coeffs, dtype=np.float32)
    xs = np.asarray(x, dtype=np.float32)
    y = np.full(xs.shape, c[3], dtype=np.float32)
    for k in (2, 1, 0):
        y = (y * xs).astype(np.float32)
        y = (y + c[k]).astype(np.float32)
    return y


@dataclass
class PolyStreamResult:
    """What the kernel does to one stream: its output bursts and its final register status."""

    out: list[StreamBurst]
    halted: int = 0
    error: int = int(PolyError.NO_ERROR)
    tx_id: int = 0

    def status(self) -> dict[str, int]:
        return {"halted": self.halted, "error": self.error, "tx_id": self.tx_id}


def poly_stream_model(bursts: list[StreamBurst], coeffs: npt.ArrayLike,
                      word_bw: int = 32) -> PolyStreamResult:
    """The whole kernel, as a pure function of its input stream.

    Word by word, exactly what ``poly_body_impl.tpp`` does:

    - read a command header (its word count is fixed by the schema; the header read does
      not inspect TLAST); on ``END``, return;
    - write the response header, TLAST on its last word;
    - read ``nsamp`` sample words, writing ``poly_eval`` of each, TLAST on the output word
      that completes ``nsamp``;
    - a TLAST before the last sample word stops the burst: ``TLAST_EARLY_SAMP_IN``; no TLAST
      on the last one: ``NO_TLAST_SAMP_IN``.  Either error halts the kernel with
      ``halted = 1`` and the offending ``tx_id``.

    The output is split into bursts at TLAST -- the only boundary on the wire -- so it
    compares directly with what ``wf::record_stream`` records from the C++ testbench.
    """
    if word_bw != 32:
        raise NotImplementedError("the model covers WORD_BW = 32 (one float32 per word)")
    words = [(int(w), i + 1 == len(b.words) and b.tlast)
             for b in bursts for i, w in enumerate(np.asarray(b.words, dtype=np.uint64))]
    hdr_words = PolyCmdHdr().serialize(word_bw=word_bw).size
    out: list[tuple[int, bool]] = []
    res = PolyStreamResult(out=[])
    pos = 0
    while True:
        if pos + hdr_words > len(words):
            raise ValueError("the stimulus ends before an END command: the kernel would block")
        hdr = PolyCmdHdr().deserialize(
            np.array([w for w, _ in words[pos:pos + hdr_words]], dtype=np.uint32),
            word_bw=word_bw)
        pos += hdr_words
        if int(hdr.cmd_type) == PolyCmdType.END:
            break
        resp = PolyRespHdr()
        resp.tx_id = int(hdr.tx_id)
        rw = resp.serialize(word_bw=word_bw)
        out += [(int(w), i + 1 == rw.size) for i, w in enumerate(rw)]

        nsamp, nread, tlast_seen, early = int(hdr.nsamp), 0, False, False
        for i in range(nsamp):
            if pos >= len(words):
                raise ValueError("the stimulus ends inside a sample burst: the kernel would block")
            w, last = words[pos]
            pos += 1
            x = read_array(np.array([w], dtype=np.uint32), elem_type=Float32,
                           word_bw=word_bw, shape=1).val
            y = write_array(poly_eval(coeffs, x), elem_type=Float32, word_bw=word_bw)
            out.append((int(y[0]), i + 1 == nsamp))
            nread += 1
            if last:
                tlast_seen, early = True, i + 1 < nsamp
                break
        if nsamp and early:
            err = PolyError.TLAST_EARLY_SAMP_IN
        elif nsamp and not tlast_seen:
            err = PolyError.NO_TLAST_SAMP_IN
        elif nread != nsamp:
            err = PolyError.WRONG_NSAMP
        else:
            err = PolyError.NO_ERROR
        if err != PolyError.NO_ERROR:
            res.halted, res.error, res.tx_id = 1, int(err), int(hdr.tx_id)
            break

    cur: list[int] = []
    for w, last in out:
        cur.append(w)
        if last:
            res.out.append(StreamBurst(np.array(cur, dtype=np.uint64), True))
            cur = []
    if cur:
        res.out.append(StreamBurst(np.array(cur, dtype=np.uint64), False))
    return res


# ---------------------------------------------------------------------------
# 3. The module: ports, register map, and the kernel body
# ---------------------------------------------------------------------------


@dataclass
class PolyAccel(HostActivated):
    """The polynomial accelerator: a body-only kernel.

    The module declares the ports and the register map and names its body.  Waveflow
    generates ``gen/poly.hpp`` / ``gen/poly.cpp`` -- the prototype, every interface pragma,
    and one call ``poly_impl::body(s_in, m_out, halted, error, tx_id, coeffs)`` -- and the
    whole kernel is the hand-written ``poly_body_impl.tpp``.  The register fields reach the
    body by reference, so it writes ``halted`` / ``error`` / ``tx_id`` itself.
    """

    cpp_kernel_name: ClassVar[str | None] = "poly"
    # The hook-wrapping namespace must differ from the kernel function name, or
    # `::poly(...)` at the testbench call site is ambiguous.
    cpp_namespace:   ClassVar[str | None] = "poly_impl"
    cpp_body:        ClassVar[str | None] = "body"

    in_bw:        HwParam[int] = 32
    out_bw:       HwParam[int] = 32
    aximm_bw:     HwParam[int] = 32
    clk:          Clock = field(default_factory=lambda: Clock(freq=1e9))
    # The timing model (pysim only).  Calibrated from RTL cosim: with nsamp=100 the kernel
    # takes nsamp * proc_ii plus a fixed pipeline-fill and handshake overhead.
    proc_ii:      int = 1
    proc_latency: int = 40
    logger:       Logger | NullLogger = field(default_factory=NullLogger)
    unroll_factor: int = 1

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in  = StreamIFSlave( name=f'{self.name}_s_in',  sim=self.sim, bitwidth=self.in_bw)
        self.m_out = StreamIFMaster(name=f'{self.name}_m_out', sim=self.sim, bitwidth=self.out_bw)
        self.regmap = VitisRegMap({
            "halted": RegField(Bit,            RegAccess.R,  description="1 = halted on error"),
            "error":  RegField(PolyErrorField, RegAccess.R,  description="Last error code"),
            "tx_id":  RegField(TxIdField,      RegAccess.R,  description="TX id of halted txn"),
            "coeffs": RegField(CoeffArray,     RegAccess.RW, description="Polynomial coefficients"),
        }, bitwidth=self.aximm_bw)
        self.s_lite = VitisRegMapMMIFSlave(
            name=f'{self.name}_s_lite', sim=self.sim, bitwidth=self.aximm_bw,
            regmap=self.regmap, on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.s_lite):
            self.add_endpoint(ep)
        self._job: int = 0

    def body(self) -> ProcessGen[None]:
        """The kernel for pysim: the port wrapper around :func:`poly_eval`, plus timing.

        Ordinary Python -- this is never translated to C++.  It reads and writes the ports,
        calls the pure model for the arithmetic, and models time: the samples come out
        ``proc_latency`` cycles after they start arriving, one per ``proc_ii`` cycles.
        """
        while True:
            self.logger.log(event='proc_begin', job=self._job)
            cmd_hdr: PolyCmdHdr = yield from self.s_in.get_schema(PolyCmdHdr)
            if cmd_hdr.cmd_type == PolyCmdType.END:
                self.logger.log(event='proc_end', job=self._job)
                return
            coeffs = self.regmap.get("coeffs")

            resp_hdr = PolyRespHdr()
            resp_hdr.tx_id = cmd_hdr.tx_id
            self.logger.log(event='resp_hdr_write_begin', job=self._job)
            yield from self.m_out.write(resp_hdr)
            if cmd_hdr.nsamp == 0:          # no sample burst either way, as in the C++
                self._job += 1
                continue

            self.logger.log(event='samp_read_begin', job=self._job)
            samp_in, tstart = yield from self.s_in.get_pipelined(Float32, count=cmd_hdr.nsamp)
            y = poly_eval(coeffs.val, samp_in)

            t_out_start = tstart + self.proc_latency * self.clk.period
            proc_time = cmd_hdr.nsamp / self.unroll_factor * self.proc_ii * self.clk.period
            proc_time = max(0.0, proc_time + (t_out_start - self.env.now))
            yield self.timeout(proc_time)
            yield from self.m_out.write_pipelined(array(Float32, y), t_out_start)
            self.logger.log(event='samp_out_write_end', job=self._job)
            self._job += 1

            if len(samp_in) != cmd_hdr.nsamp:
                self.regmap.set("error", PolyError.WRONG_NSAMP)
                self.regmap.set("tx_id", cmd_hdr.tx_id)
                self.regmap.set("halted", 1)
                return


# ---------------------------------------------------------------------------
# 4. The pysim testbench
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class PolyTB(SimObj):
    """Plays a scenario's stimulus file into :class:`PolyAccel` and records the response.

    The stimulus is the same burst bundle the C++ testbench plays (``<scenario>/in``).  The
    testbench writes the coefficients, asserts ``ap_start``, and then pushes and pops in
    two concurrent processes, as hardware would: pushing everything first can deadlock,
    because the kernel blocks writing output nobody is reading yet.  pysim runs the
    well-formed scenarios (a pysim stream has no way to omit TLAST); the error paths are
    checked through the pure model and the C++ kernel.
    """

    stimulus:  Path
    coeffs:    npt.NDArray[np.float32]
    n_out:     int                       # output bursts to collect (2 per DATA transaction)
    word_bw:   int = 32
    base_addr: int = 0x0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_in   = StreamIFMaster(name=f'{self.name}_m_in',   sim=self.sim, bitwidth=self.word_bw)
        self.s_out  = StreamIFSlave( name=f'{self.name}_s_out',  sim=self.sim, bitwidth=self.word_bw)
        self.m_lite = MMIFMaster(    name=f'{self.name}_m_lite', sim=self.sim, bitwidth=32)
        self.out: list[StreamBurst] = []
        self.status: dict[str, int] = {}
        self._regmap_ref: VitisRegMap | None = None

    def run_proc(self) -> ProcessGen[None]:
        rm = self._regmap().bind_master(self.m_lite, base_addr=self.base_addr)
        yield from rm.set("coeffs", self.coeffs)
        yield from rm.start()
        reader = self.env.process(self._read_all())
        word_t = np.uint32 if self.word_bw <= 32 else np.uint64
        for b in read_bursts(self.stimulus):
            yield from self.m_in.write(np.asarray(b.words, dtype=word_t))
        yield reader
        yield self.timeout(0)
        for f in ("halted", "error", "tx_id"):
            self.status[f] = int((yield from rm.get(f)))

    def _read_all(self) -> ProcessGen[None]:
        for _ in range(self.n_out):
            words = yield from self.s_out.get()
            self.out.append(StreamBurst(np.asarray(words, dtype=np.uint64), True))

    def _regmap(self) -> VitisRegMap:
        if self._regmap_ref is None:
            raise RuntimeError("PolyTB._regmap_ref is unset; call connect() before run_sim().")
        return self._regmap_ref


def connect(sim: Simulation, tb: PolyTB, accel: PolyAccel, clk: Clock) -> None:
    """Wire the testbench to the accelerator: two streams and the AXI-Lite link."""
    in_stream  = StreamIF(sim=sim, clk=clk)
    out_stream = StreamIF(sim=sim, clk=clk)
    lite_link  = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)
    in_stream.bind( "master", tb.m_in)
    in_stream.bind( "slave",  accel.s_in)
    out_stream.bind("master", accel.m_out)
    out_stream.bind("slave",  tb.s_out)
    lite_link.bind( "master", tb.m_lite)
    lite_link.bind( "slave",  accel.s_lite)
    tb._regmap_ref = accel.regmap
