"""prach.py -- the PRACH searcher as a Waveflow stream pipeline.

One input stream carries, per PRACH occasion, the command, the roots and the FFT windows::

    s_in     : PrachCmd | roots[2 n_roots] (u, u_inv) U8 | window(s=0,a=0) | window(0,1) | ...    each a burst
               a window = 1024 complex int16 samples (CP removed), 4 a 128-bit word: 256 words
    m_out    : Resp(tx_id, n_det, exp) | DetRec x n_det (one burst; none when n_det = 0)
    m_status : Status(tx_id, status)                                       one per command

and the kernel is a chain of four built stages around two library FFTs::

    s_in -> front ==RadixWord==> fft1024 ==RadixWord==> extract -> correlate ==RadixWord==> fft256 ==> detect
              \\_______________ FwdHdr | roots | FwdTrl ______/       \\______ CorHdr ___________________/

* **front** -- checks the command and the roots, forwards them (``FwdHdr``, ``roots``), copies each window
  to the FFT (an early TLAST: the rest of the job is zeros, so the FFT stays framed by count), then
  ``FwdTrl`` with the job's code.
* **fft1024** -- ``SsrFft(L=1024)``, the library's: a frame every 256 cycles.
* **extract** -- picks the 139 PRACH bins of each window, sums the repetitions, applies the block
  exponent, sends ``ExtHdr | roots | Y``.
* **correlate** -- per root, per antenna, ``conj(Y) R_u`` zero-padded to 256 (the reference generated
  from ``(u, u_inv)`` and a 278-entry ROM), a frame to the second FFT; ``CorHdr`` to detect first.
* **fft256** -- ``SsrFft(L=256)``, the library's, forward (see golden.py, step 4).
* **detect** -- the power-delay profile over antennas, the window peaks, the threshold; answers.

Every arithmetic step is a call into :mod:`prach.golden` -- the same functions the golden runs -- so
this file is about the streams: who reads what, in what order, and how a job's token reaches the
last stage on every path.  An error is fatal (``docs/guide/systems/command_response.md``):

* ``BAD_PARAM`` -- a command field out of range (front): no window is read;
* ``BAD_ROOT`` -- a root with ``u`` outside 1..138 or ``u u_inv != 1 mod 139`` (front);
* ``TLAST_EARLY`` -- the roots or a window ended early (front).

Each is reported by detect (``Resp(n_det = 0)`` then the status) and every stage stops.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.dsp.ssr_fft.hw import SsrFft
from waveflow.dsp.ssr_fft.model import Geometry
from waveflow.hw.arrayutils import array, read_array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import (FramedStreamIFMaster, FramedStreamIFSlave, StreamIF,
                                   StreamIFMaster, StreamIFSlave)
from waveflow.utils import complexutils as cx
from waveflow.utils.fixputils import Format
from waveflow.vitis_l1.cxquant import complex_from_format

from . import golden as g
from .nr import L_RA, N_FFT
from .schemas import (SCHEMA_CLASSES, U8, CorHdr, DetRec, ExtHdr, FwdHdr, FwdTrl,  # noqa: F401
                      PrachCmd, Resp, Status, StatusCode, StatusCodeField)

HERE = Path(__file__).resolve().parent
INCLUDE_DIR = "include"

#: The framed word: 4 complex int16, or one header, or one detection record.
W = 128
R = 4
N_WORDS_1024 = N_FFT // R          # 256 words a window
N_WORDS_256 = g.N_CORR // R        # 64 words a correlation frame
#: Y is stored per antenna padded to 140 bins (35 words), so each antenna starts on a word.
Y_PAD = 140
CLK_HZ = 250e6

# ---------------------------------------------------------------------------
# 1. Schemas: the messages are in schemas.py; here the sample lanes, from the FFTs' geometry
# ---------------------------------------------------------------------------

#: A sample, a bin of Y, a correlation input: ``std::complex<ap_fixed<16, 2>>`` -- the FFT's input lane.
C16_FMT = Format(16, 2, True)
C16 = complex_from_format(C16_FMT)
FFT1_GEO = Geometry(N_FFT)
FFT2_GEO = Geometry(g.N_CORR)
#: The FFTs' output lanes: ``ap_fixed<27, 13>`` and ``ap_fixed<25, 11>`` (read from the geometry).
C27 = complex_from_format(FFT1_GEO.out_fmt)
C25 = complex_from_format(FFT2_GEO.out_fmt)
FFT1_OUT_W = C27.bitwidth * R      # 216
FFT2_OUT_W = C25.bitwidth * R      # 200




def check_params(c) -> StatusCode:
    """Front's header check, in field order.  ``n_roots`` must be what ``n_cs`` and ``n_pre`` imply."""
    n_ant, n_sym, k0, n_cs, n_pre, n_roots = (int(c.n_ant), int(c.n_sym), int(c.k_start), int(c.n_cs),
                                             int(c.n_pre), int(c.n_roots))
    if not (1 <= n_ant <= g.MAX_ANT and 1 <= n_sym <= g.MAX_SYM and k0 < N_FFT and n_cs <= L_RA
            and 1 <= n_pre <= g.MAX_PRE):
        return StatusCode.BAD_PARAM
    per_root = 1 if n_cs == 0 else L_RA // n_cs
    if n_roots != -(-n_pre // per_root):
        return StatusCode.BAD_PARAM
    return StatusCode.OK


def check_roots(roots: np.ndarray) -> StatusCode:
    u, ui = np.asarray(roots[0::2], dtype=np.int64), np.asarray(roots[1::2], dtype=np.int64)
    ok = (u >= 1) & (u <= L_RA - 1) & ((u * ui) % L_RA == 1)
    return StatusCode.OK if bool(ok.all()) else StatusCode.BAD_ROOT


# ---------------------------------------------------------------------------
# 2. Word helpers: every word through a schema or the array utils
# ---------------------------------------------------------------------------


def pack_c(re: np.ndarray, im: np.ndarray, elem, fmt: Format, word_bw: int) -> np.ndarray:
    """Complex stored integers -> words, through the element's array utils."""
    re = np.asarray(re, dtype=np.int64).reshape(-1)
    im = np.asarray(im, dtype=np.int64).reshape(-1)
    return np.asarray(write_array(cx.make_complex(re, im, fmt), elem_type=elem, word_bw=word_bw),
                      dtype=np.uint64)


def unpack_c(words: np.ndarray, elem, word_bw: int, n: int) -> tuple[np.ndarray, np.ndarray]:
    v = read_array(np.asarray(words, dtype=np.uint64), elem_type=elem, word_bw=word_bw, shape=n).val
    return np.asarray(v["re"], dtype=np.int64), np.asarray(v["im"], dtype=np.int64)


def c16_array(re, im):
    return array(C16, cx.make_complex(np.asarray(re, dtype=np.int64), np.asarray(im, dtype=np.int64),
                                      C16_FMT))


def msg_words(msgs: list[DataList]) -> np.ndarray:
    """Several messages as one burst: each serialized by its schema, concatenated."""
    return np.concatenate([np.asarray(m.serialize(word_bw=W), dtype=np.uint64).reshape(-1, W // 64)
                           for m in msgs])


# ---------------------------------------------------------------------------
# 3. The stages
# ---------------------------------------------------------------------------


def _halt(mod):
    """Fatal: an event nothing triggers -- the module stops, as its RTL does, until a reset."""
    yield mod.event()


def _get_words(ep, n: int):
    """Exactly *n* raw words from an unframed (FFT-side) stream, which may deliver them in pieces."""
    parts, have = [], 0
    while have < n:
        got = np.asarray((yield from ep.get(nwords_max=n - have)), dtype=np.uint64)
        if got.shape[0] == 0:
            raise RuntimeError(f"{ep.name}: stream stopped mid-frame")
        parts.append(got.reshape(got.shape[0], -1))
        have += got.shape[0]
    return np.concatenate(parts, axis=0)


#: Root bytes a word (U8 lanes of a 128-bit word).
N_ROOT_BYTES = W // 8


def _as_words(got) -> np.ndarray:
    """A burst as ``(n_words, W / 64)`` uint64."""
    a = np.asarray(got, dtype=np.uint64)
    return a.reshape(a.shape[0], -1) if a.size else a.reshape(0, W // 64)


def _clk():
    return Clock(freq=CLK_HZ)


@dataclass
class Front(FreeRunMod):
    """Stage 1: the command and the roots checked and forwarded; the windows copied to the FFT."""

    cpp_kernel_name: ClassVar[str | None] = "prach_front"
    clk: Clock = field(default_factory=_clk)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=W)
        self.m_cmd = FramedStreamIFMaster(name=f"{self.name}_m_cmd", sim=self.sim, bitwidth=W)
        self.m_fft = StreamIFMaster(name=f"{self.name}_m_fft", sim=self.sim, bitwidth=W)
        for ep in (self.s_in, self.m_cmd, self.m_fft):
            self.add_endpoint(ep)

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("prach_front_task", "prach_front_task.h", ("s_in", "m_cmd", "m_fft"),
                          template_args=(W,), framed=("s_in", "m_cmd"))

    def workload(self, kind: str = "test") -> dict:
        from .scenario import scenario
        return {"s_in": scenario(kind).stimulus()}

    def run_iter(self):
        cw = _as_words((yield from self.s_in.get()))         # the command's burst: one word
        c = PrachCmd().deserialize(cw[:1], word_bw=W)
        code = StatusCode.TLAST_LATE if len(cw) > 1 else check_params(c)
        fwd = dict(tx_id=c.tx_id, n_ant=c.n_ant, n_sym=c.n_sym, k_start=c.k_start, n_cs=c.n_cs,
                   n_pre=c.n_pre, n_roots=c.n_roots, thr_q8=c.thr_q8)
        roots = None
        if code == StatusCode.OK:
            # the whole burst, so that a missing TLAST (more words than announced) is seen: the
            # hardware checks TLAST on the last word it expects (decision 20)
            nr2 = 2 * int(c.n_roots)
            nw = -(-nr2 // N_ROOT_BYTES)
            words = _as_words((yield from self.s_in.get()))
            if len(words) < nw:
                code = StatusCode.TLAST_EARLY
            elif len(words) > nw:
                code = StatusCode.TLAST_LATE
            else:
                roots = np.asarray(read_array(words, elem_type=U8, word_bw=W, shape=nr2).val,
                                   dtype=np.int64)
                code = check_roots(roots)
        if code != StatusCode.OK:
            yield from self.m_cmd.write(FwdHdr(**fwd, nframes=0))
            yield from self.m_cmd.write(FwdTrl(tx_id=c.tx_id, code=code))
            yield from _halt(self)
            return
        nframes = int(c.n_sym) * int(c.n_ant)
        yield from self.m_cmd.write(FwdHdr(**fwd, nframes=nframes))
        yield from self.m_cmd.write_array(U8, len(roots), array(U8, roots))
        code = StatusCode.OK
        zeros = pack_c(np.zeros(N_FFT), np.zeros(N_FFT), C16, C16_FMT, W)
        for _ in range(nframes):
            words = zeros
            if code == StatusCode.OK:
                got = _as_words((yield from self.s_in.get()))
                n = min(len(got), N_WORDS_1024)
                words = np.concatenate([got[:n], zeros[n:]])      # an early TLAST: zeros follow
                if len(got) < N_WORDS_1024:
                    code = StatusCode.TLAST_EARLY
                elif len(got) > N_WORDS_1024:
                    code = StatusCode.TLAST_LATE                  # this window whole, zeros after
            yield from self.m_fft.write(words)
        yield from self.m_cmd.write(FwdTrl(tx_id=c.tx_id, code=code))
        if code != StatusCode.OK:
            yield from _halt(self)


@dataclass
class Extract(FreeRunMod):
    """Stage 2: the 139 PRACH bins of each window, summed over repetitions, block-scaled to int16."""

    cpp_kernel_name: ClassVar[str | None] = "prach_extract"
    clk: Clock = field(default_factory=_clk)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_cmd = FramedStreamIFSlave(name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=W)
        self.s_fft = StreamIFSlave(name=f"{self.name}_s_fft", sim=self.sim, bitwidth=FFT1_OUT_W)
        self.m_y = FramedStreamIFMaster(name=f"{self.name}_m_y", sim=self.sim, bitwidth=W)
        for ep in (self.s_cmd, self.s_fft, self.m_y):
            self.add_endpoint(ep)

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("prach_extract_task", "prach_extract_task.h", ("s_cmd", "s_fft", "m_y"),
                          template_args=(W,), framed=("s_cmd", "m_y"))

    def workload(self, kind: str = "test") -> dict:
        from .scenario import scenario
        sc = scenario(kind)
        return {"s_cmd": sc.link_cmd(), "s_fft": sc.link_fft1_out()}

    def run_iter(self):
        h = yield from self.s_cmd.get_schema(FwdHdr)
        nframes, n_ant = int(h.nframes), int(h.n_ant)
        roots = None
        if nframes:
            roots = np.asarray((yield from self.s_cmd.get_array(U8, 2 * int(h.n_roots))).val,
                               dtype=np.int64)
        xr = np.zeros((max(nframes, 1), N_FFT), dtype=np.int64)
        xi = np.zeros_like(xr)
        for f in range(nframes):
            words = yield from _get_words(self.s_fft, N_WORDS_1024)
            xr[f], xi[f] = unpack_c(words, C27, FFT1_OUT_W, N_FFT)
        t = yield from self.s_cmd.get_schema(FwdTrl)
        code = StatusCode(int(t.code))
        hdr = dict(tx_id=h.tx_id, n_ant=h.n_ant, n_cs=h.n_cs, n_pre=h.n_pre, n_roots=h.n_roots,
                   thr_q8=h.thr_q8)
        if code != StatusCode.OK:
            yield from self.m_y.write(ExtHdr(**hdr, exp=0, code=code))
            yield from _halt(self)
            return
        n_sym = nframes // n_ant
        yr, yi, e = g.extract_combine(xr.reshape(n_sym, n_ant, N_FFT), xi.reshape(n_sym, n_ant, N_FFT),
                                      int(h.k_start))
        pr = np.zeros((n_ant, Y_PAD), dtype=np.int64)
        pi = np.zeros_like(pr)
        pr[:, :L_RA], pi[:, :L_RA] = yr, yi
        yield from self.m_y.write(ExtHdr(**hdr, exp=e, code=code))
        yield from self.m_y.write_array(U8, len(roots), array(U8, roots))
        yield from self.m_y.write_array(C16, n_ant * Y_PAD, c16_array(pr, pi))


@dataclass
class Correlate(FreeRunMod):
    """Stage 3: per root and antenna, ``conj(Y) R_u`` zero-padded to a 256-point frame for fft256."""

    cpp_kernel_name: ClassVar[str | None] = "prach_correlate"
    clk: Clock = field(default_factory=_clk)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_y = FramedStreamIFSlave(name=f"{self.name}_s_y", sim=self.sim, bitwidth=W)
        self.m_cmd = FramedStreamIFMaster(name=f"{self.name}_m_cmd", sim=self.sim, bitwidth=W)
        self.m_fft = StreamIFMaster(name=f"{self.name}_m_fft", sim=self.sim, bitwidth=W)
        for ep in (self.s_y, self.m_cmd, self.m_fft):
            self.add_endpoint(ep)

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("prach_correlate_task", "prach_correlate_task.h", ("s_y", "m_cmd", "m_fft"),
                          template_args=(W,), framed=("s_y", "m_cmd"))

    def workload(self, kind: str = "test") -> dict:
        from .scenario import scenario
        return {"s_y": scenario(kind).link_y()}

    def run_iter(self):
        h = yield from self.s_y.get_schema(ExtHdr)
        code = StatusCode(int(h.code))
        n_ant, n_roots = int(h.n_ant), int(h.n_roots)
        yield from self.m_cmd.write(CorHdr(tx_id=h.tx_id, n_ant=h.n_ant, n_cs=h.n_cs, n_pre=h.n_pre,
                                           n_roots=h.n_roots, thr_q8=h.thr_q8, exp=h.exp, code=code))
        if code != StatusCode.OK:
            yield from _halt(self)
            return
        roots = np.asarray((yield from self.s_y.get_array(U8, 2 * n_roots)).val, dtype=np.int64)
        y = (yield from self.s_y.get_array(C16, n_ant * Y_PAD)).val
        yr = np.asarray(y["re"], dtype=np.int64).reshape(n_ant, Y_PAD)[:, :L_RA]
        yi = np.asarray(y["im"], dtype=np.int64).reshape(n_ant, Y_PAD)[:, :L_RA]
        for r in range(n_roots):
            zr, zi = g.correlate(yr, yi, int(roots[2 * r]), int(roots[2 * r + 1]))
            for a in range(n_ant):
                yield from self.m_fft.write(pack_c(zr[a], zi[a], C16, C16_FMT, W))


@dataclass
class Detect(FreeRunMod):
    """Stage 4: the profile per root, the window peaks against the threshold; the answer."""

    cpp_kernel_name: ClassVar[str | None] = "prach_detect"
    clk: Clock = field(default_factory=_clk)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_cmd = FramedStreamIFSlave(name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=W)
        self.s_fft = StreamIFSlave(name=f"{self.name}_s_fft", sim=self.sim, bitwidth=FFT2_OUT_W)
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=W)
        self.m_status = FramedStreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=W)
        for ep in (self.s_cmd, self.s_fft, self.m_out, self.m_status):
            self.add_endpoint(ep)

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask
        ports = ("s_cmd", "s_fft", "m_out", "m_status")
        return KernelTask("prach_detect_task", "prach_detect_task.h", ports, template_args=(W,),
                          framed=("s_cmd", "m_out", "m_status"))

    def workload(self, kind: str = "test") -> dict:
        from .scenario import scenario
        sc = scenario(kind)
        return {"s_cmd": sc.link_cor(), "s_fft": sc.link_fft2_out()}

    def run_iter(self):
        h = yield from self.s_cmd.get_schema(CorHdr)
        code = StatusCode(int(h.code))
        dets: list[g.Detection] = []
        if code == StatusCode.OK:
            n_ant, n_cs, n_pre = int(h.n_ant), int(h.n_cs), int(h.n_pre)
            prm = g.Params(n_ant, 1, 0, n_cs, n_pre, ((1, 1),) * int(h.n_roots), int(h.thr_q8))
            for r in range(int(h.n_roots)):
                fr = np.zeros((n_ant, g.N_CORR), dtype=np.int64)
                fi = np.zeros_like(fr)
                for a in range(n_ant):
                    words = yield from _get_words(self.s_fft, N_WORDS_256)
                    fr[a], fi[a] = unpack_c(words, C25, FFT2_OUT_W, g.N_CORR)
                dets += g.detect_root(g.power(fr, fi), r, prm)
        yield from self.m_out.write(Resp(tx_id=h.tx_id, n_det=len(dets), exp=h.exp))
        if dets:
            yield from self.m_out.write(msg_words([det_rec(d) for d in dets]))
        yield from self.m_status.write(Status(tx_id=h.tx_id, status=code))
        if code != StatusCode.OK:
            yield from _halt(self)


def det_rec(d: g.Detection) -> DetRec:
    return DetRec(preamble=d.preamble, n_peak=d.n_peak, delay=d.delay, peak=d.peak, noise=d.noise,
                  n_noise=d.n_noise)


# ---------------------------------------------------------------------------
# 4. The composite
# ---------------------------------------------------------------------------

#: Link depths, in words, and why (see plans/blocks/*.md):
#: cmd: FwdHdr + roots (<= 8 words) + FwdTrl, while extract is still reading the previous job's FFT
#:      output: one job's control, rounded up.
DEPTH_CMD = 16
#: y: ExtHdr + roots + Y (<= 1 + 8 + 140 words); correlate buffers all of it before it starts, so
#:    the link need not hold a job -- 32 lets extract finish writing while correlate stores.
DEPTH_Y = 32
#: cor: one CorHdr per job; two jobs' worth.
DEPTH_COR = 4
#: The FFT-side links carry frames by count: two words of slack each (the FFT reads every cycle).
DEPTH_FFT = 4


@dataclass
class PrachSearcher(FreeRunMod):
    """``s_in -> front -> fft1024 -> extract -> correlate -> fft256 -> detect``, with front's control
    beside the first FFT and correlate's beside the second."""

    cpp_kernel_name: ClassVar[str | None] = "prach_searcher"
    clk: Clock = field(default_factory=_clk)
    #: ``SsrFft.pysim``: "flat" (one process per FFT: the same bits and frame times, faster) or "tasks".
    fft_pysim: str = "flat"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.front = Front(name=f"{self.name}_front", sim=self.sim, clk=self.clk)
        self.fft1 = SsrFft(name=f"{self.name}_fft1", sim=self.sim, clk=self.clk, L=N_FFT,
                           reorder="pingpong", pysim=self.fft_pysim)
        self.extract = Extract(name=f"{self.name}_extract", sim=self.sim, clk=self.clk)
        self.correlate = Correlate(name=f"{self.name}_correlate", sim=self.sim, clk=self.clk)
        self.fft2 = SsrFft(name=f"{self.name}_fft2", sim=self.sim, clk=self.clk, L=g.N_CORR,
                           reorder="pingpong", pysim=self.fft_pysim)
        self.detect = Detect(name=f"{self.name}_detect", sim=self.sim, clk=self.clk)
        for c in (self.front, self.fft1, self.extract, self.correlate, self.fft2, self.detect):
            self.add_comp(c)

        def link(name, master, slave, bitwidth, depth, framed):
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk,
                             bitwidth=bitwidth, framed=framed, depth=depth)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        link("cmd", self.front.m_cmd, self.extract.s_cmd, W, DEPTH_CMD, True)
        link("win", self.front.m_fft, self.fft1.s_in, W, DEPTH_FFT, False)
        link("bins", self.fft1.m_out, self.extract.s_fft, FFT1_OUT_W, DEPTH_FFT, False)
        link("y", self.extract.m_y, self.correlate.s_y, W, DEPTH_Y, True)
        link("cor", self.correlate.m_cmd, self.detect.s_cmd, W, DEPTH_COR, True)
        link("zc", self.correlate.m_fft, self.fft2.s_in, W, DEPTH_FFT, False)
        link("pdp", self.fft2.m_out, self.detect.s_fft, FFT2_OUT_W, DEPTH_FFT, False)

        self.boundary = ["s_in", "m_out", "m_status"]
        self.s_in = self.front.s_in
        self.m_out = self.detect.m_out
        self.m_status = self.detect.m_status

    def workload(self, kind: str = "test") -> dict:
        from .scenario import scenario
        return {"s_in": scenario(kind).stimulus()}
