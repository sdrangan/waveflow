"""os_filterbank.py -- a multi-channel overlap-save filter bank on ONE shared FFT (``plans/shared_kernel.md``).

``C`` channels, each with its own FIR filter ``h_c``.  Per block of a channel: ``FFT(block)``, then ``x H_c``,
then the inverse FFT **on the same** ``SsrFft``, then the overlap discarded.  It is the curated example of
sharing a kernel (``docs/guide/systems/shared_kernels.md``), and uses each part of the pattern once::

    s_in_c -> front_c --+                                   +-> dmx_ans -> m_out_c
              (credit)  +-> arb -> pre -> fft -> post ------+-> dmx_st  -> m_status_c
                ^       ^   (m_sel)  (s_dir) ^      |       +-> dmx_crd -> front_c   (a credit a block)
                |       +---- fb: x H_c, the feedback frame (input 0: priority) -+
    s_coef ------------------------------------------------------> post (H_c, loaded once)

* **one shared unit**: ``SsrFft(direction="runtime")``, forward for a new block, inverse for its product;
* **an arbiter** in front of it (``StreamArbiter``, priority): input 0 is the feedback, inputs 1..C the
  channels' new blocks, so blocks already in the loop finish first;
* **the direction from the arbiter's index**: ``pre`` reads ``m_sel`` -- input 0 is feedback, so inverse --
  and writes it to the FFT's ``s_dir``;
* **a tag beside the FFT**: ``pre`` -> ``post``, one word a frame (channel, job, block, pass), because the FFT
  frames by count and passes nothing through;
* **resident data**: each ``H_c`` is loaded once into a slot in ``post`` (``s_coef``, before any block);
* **credits**: a channel sends a block only with a credit, ``CR`` a channel; ``post`` returns one per block
  answered, routed back by a ``StreamDemux`` on the credit's channel field.  So at most ``C*CR`` blocks are in
  the loop, every loop link holds what they can put there, and ``post`` never blocks on ``fb``;
* **out-of-order results, each answered**: a block's answer is ``Resp(ch, tx_id, blk)`` then its output
  samples; a job's ``Status`` follows its last block.  Two ``StreamDemux`` (on the ``ch`` field) give each
  channel its own ``m_out_c`` and ``m_status_c``: channels finish in an order that depends on timing, but
  each channel's own outputs do not, so RTL and pysim record the same words on every port.

The arithmetic is bit-exact end to end: :func:`block_golden` is the FFT's own model (``model.pipeline``)
forward, the library's ``complexMultiply`` for ``x H_c`` (into the FFT's input format), and the model
inverse.  :func:`float_check` shows it is a filter: ``16 * y`` against ``np.convolve(x, h)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.dsp.ssr_fft import model as fft_model
from waveflow.dsp.ssr_fft.hw import SsrFft
from waveflow.dsp.ssr_fft.types import EdgeType
from waveflow.hw.arrayutils import read_array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import (FramedStreamIFMaster, FramedStreamIFSlave, StreamIF, StreamIFMaster,
                                   StreamIFSlave)
from waveflow.hw.mem_stream import KernelTask
from waveflow.streams import StreamArbiter, StreamDemux
from waveflow.utils import complexutils as cx
from waveflow.utils import fixputils as fp
from waveflow.utils.burst_io import StreamBurst
from waveflow.vitis_l1.cxquant import complex_multiply

HERE = Path(__file__).resolve().parent
INCLUDE_DIR = "include"

# ---------------------------------------------------------------------------
# 0. The numbers
# ---------------------------------------------------------------------------

#: The FFT: length, samples a word, words a frame.
L, R = 64, 4
NW = L // R
#: Channels, and the credit each holds: blocks of one channel in the loop at once.
C, CR = 3, 2
#: Filter taps, and the overlap a block keeps from the one before: ``P = M - 1`` samples, ``PW`` words.
M = 17
P = M - 1
PW = P // R
#: New samples a block, and words.
B = L - P
BW = B // R
#: ``H_c`` is stored divided by this, so ``X * H_c`` fits the FFT's input format; the output is the
#: filtered signal divided by it too.
H_SCALE = 16

#: The FFT, as every stage sees it.
GEO = fft_model.Geometry(L, runtime=True)
E_IN = EdgeType("in", GEO.in_fmt, R)            # a word of samples into the FFT: 128 bits
E_OUT = EdgeType("out", GEO.out_fmt, R)         # a word of bins out of it: 184 bits
#: The stream widths: the arbiter's loop (headers and sample words), the answers, the small messages.
W_LOOP = E_IN.bitwidth
W_ANS = E_OUT.bitwidth
W_MSG = 32

# ---------------------------------------------------------------------------
# 1. Schemas
# ---------------------------------------------------------------------------

U8 = IntField.specialize(bitwidth=8, signed=False, include_dir=INCLUDE_DIR)
U16 = IntField.specialize(bitwidth=16, signed=False, include_dir=INCLUDE_DIR)


class StatusCode(IntEnum):
    OK = 0


StatusCodeField = EnumField.specialize(enum_type=StatusCode)


class Job(DataList):
    """A channel's command on ``s_in_c``; ``nblk`` bursts of ``BW`` sample words follow, one a block."""
    elements = {
        "tx_id": {"schema": U16, "description": "the sender's id, echoed in every answer and the status"},
        "nblk":  {"schema": U8,  "description": "blocks in the job, 1 to 255"},
    }


class Tag(DataList):
    """A block in the loop: the head of every frame into the arbiter, and the tag beside the FFT."""
    elements = {
        "tx_id": {"schema": U16, "description": "the job"},
        "ch":    {"schema": U8,  "description": "the channel, 0 to C-1"},
        "blk":   {"schema": U8,  "description": "the block within the job"},
        "nblk":  {"schema": U8,  "description": "blocks in the job"},
        "npass": {"schema": U8,  "description": "0: forward, a new block; 1: inverse, its product"},
    }


class Coef(DataList):
    """The head of a filter on ``s_coef``: ``NW`` words of ``H_c / H_SCALE`` follow, bins in natural order."""
    elements = {
        "ch": {"schema": U8, "description": "the channel whose slot it fills"},
    }


class Resp(DataList):
    """The head of one block's answer: ``BW`` words of output samples follow.  ``ch`` first: the demux
    routes on bits 0-7."""
    elements = {
        "ch":    {"schema": U8,  "description": "the channel"},
        "tx_id": {"schema": U16, "description": "echo of the job's tx_id"},
        "blk":   {"schema": U8,  "description": "the block within the job"},
    }


class Status(DataList):
    """One a job, after its last block's answer.  ``ch`` first, for the demux."""
    elements = {
        "ch":     {"schema": U8,              "description": "the channel"},
        "tx_id":  {"schema": U16,             "description": "the job it reports on"},
        "status": {"schema": StatusCodeField, "description": "OK"},
    }


class Crd(DataList):
    """``post`` -> the channel's front: one of its blocks has left the loop.  ``ch`` first, for the demux."""
    elements = {
        "ch": {"schema": U8, "description": "the channel"},
    }


#: In dependency order, each with the word widths it travels at.
SCHEMAS = [(StatusCodeField, [W_MSG]), (Job, [W_LOOP]), (Tag, [W_LOOP]), (Coef, [W_LOOP]),
           (Resp, [W_ANS]), (Status, [W_MSG]), (Crd, [W_MSG])]

# ---------------------------------------------------------------------------
# 2. Packing (the schema's, never by hand), the golden and the scenarios
# ---------------------------------------------------------------------------


def _chunks(bw: int) -> int:
    return -(-bw // 64)


def pack(re: np.ndarray, im: np.ndarray, e: EdgeType) -> np.ndarray:
    """``(n, R)`` stored samples -> ``n`` words of edge *e* (``k`` 64-bit chunks each)."""
    words = write_array(cx.make_complex(np.asarray(re).reshape(-1), np.asarray(im).reshape(-1), e.fmt),
                        elem_type=e.elem, word_bw=e.bitwidth)
    return np.asarray(words, dtype=np.uint64).reshape(-1, _chunks(e.bitwidth))


def unpack(words: np.ndarray, e: EdgeType) -> tuple[np.ndarray, np.ndarray]:
    """``n`` words of edge *e* -> ``(n, R)`` stored samples, re and im."""
    words = np.asarray(words, dtype=np.uint64).reshape(-1, _chunks(e.bitwidth))
    v = read_array(words, elem_type=e.elem, word_bw=e.bitwidth, shape=words.shape[0] * e.R).val
    return (np.asarray(v["re"], dtype=np.int64).reshape(-1, e.R),
            np.asarray(v["im"], dtype=np.int64).reshape(-1, e.R))


def hdr(m, bw: int) -> np.ndarray:
    """A message as one *bw*-bit word."""
    return np.asarray(m.serialize(word_bw=bw), dtype=np.uint64).reshape(-1, _chunks(bw))


def multiply(xr, xi, hr, hi) -> tuple[np.ndarray, np.ndarray]:
    """``X * H_c`` as ``post`` computes it: the library's ``complexMultiply`` -- partial products in the
    bins' format -- into the FFT's input format (``ssr_cmul<out_t, in_t, in_t>``)."""
    return complex_multiply(xr, xi, GEO.out_fmt, hr, hi, GEO.in_fmt, GEO.in_fmt)


def block_golden(xr, xi, hr, hi) -> tuple[np.ndarray, np.ndarray]:
    """One block, ``(NW, R)`` stored input samples and ``H_c / H_SCALE`` bins -> its ``(BW, R)`` output
    samples, as the hardware computes them: the FFT's model forward, the multiply, the model inverse
    (whose bits read as ``GEO.inv_out_fmt``), the first ``PW`` words dropped."""
    fr, fi = fft_model.pipeline(GEO.direction(False), xr, xi)
    yr, yi = multiply(fr, fi, hr, hi)
    zr, zi = fft_model.pipeline(GEO.direction(True), yr, yi)
    return zr[PW:], zi[PW:]


def taps(ch: int) -> np.ndarray:
    """Channel *ch*'s filter: ``M`` complex taps, ``sum |h| = 0.9`` so ``|H| < 1``.  Each channel's own."""
    rng = np.random.default_rng(100 + ch)
    n = np.arange(M)
    h = np.sinc((n - P / 2) * (0.25 + 0.15 * ch)) * np.hamming(M) * np.exp(0.4j * ch * n)
    h = h + 0.05 * (rng.standard_normal(M) + 1j * rng.standard_normal(M))
    return 0.9 * h / np.abs(h).sum()


def coef_bins(ch: int) -> tuple[np.ndarray, np.ndarray]:
    """``H_c / H_SCALE`` quantized to the FFT's input format, ``(NW, R)`` natural-order bins."""
    hf = np.fft.fft(taps(ch), L) / H_SCALE
    re, im = fp.quantize_real(hf.real, GEO.in_fmt), fp.quantize_real(hf.imag, GEO.in_fmt)
    return np.asarray(re, dtype=np.int64).reshape(NW, R), np.asarray(im, dtype=np.int64).reshape(NW, R)


#: Jobs per channel, in blocks: channels of different lengths, so they finish out of order.
JOBS = {"test": {0: [3, 1], 1: [5], 2: [1, 1, 2]},
        "long": {0: [6, 4, 2], 1: [12], 2: [3, 3, 3, 3]}}


def jobs(kind: str = "test") -> dict[int, list[tuple[int, np.ndarray]]]:
    """Per channel: ``(tx_id, samples)`` per job, ``nblk * B`` stored samples each."""
    return {ch: [(10 * ch + j, signal(ch, j, nb, kind)) for j, nb in enumerate(lens)]
            for ch, lens in JOBS[kind].items()}


def signal(ch: int, j: int, nblk: int, kind: str) -> np.ndarray:
    """Job *j* of channel *ch*: ``nblk * B`` stored complex samples of noise at ``|x| <= 0.25``, so
    ``|X| < 16`` and ``X * H_c / H_SCALE`` fits the FFT's input format."""
    rng = np.random.default_rng(({"test": 7, "long": 8}[kind] * 100) + 10 * ch + j)
    scale = 1 << GEO.in_fmt.frac_bits
    lim = int(0.25 * scale / np.sqrt(2))
    n = nblk * B
    return rng.integers(-lim, lim + 1, n) + 1j * rng.integers(-lim, lim + 1, n)


def job_golden(ch: int, x: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """A job's blocks, each ``(BW, R)`` output samples: the overlap is zero before the job's first block
    (each job is its own signal)."""
    hr, hi = coef_bins(ch)
    xr = np.concatenate([np.zeros(P, dtype=np.int64), np.real(x).astype(np.int64)])
    xi = np.concatenate([np.zeros(P, dtype=np.int64), np.imag(x).astype(np.int64)])
    out = []
    for b in range(len(x) // B):
        seg = slice(b * B, b * B + L)
        out.append(block_golden(xr[seg].reshape(NW, R), xi[seg].reshape(NW, R), hr, hi))
    return out


def float_check(ch: int, x: np.ndarray) -> float:
    """The worst error, relative to the output's peak, of ``H_SCALE * y`` against ``np.convolve(x, h)``
    in floating point: how close the bit-exact chain is to the filter it implements."""
    scale = float(1 << GEO.in_fmt.frac_bits)
    inv = float(1 << GEO.inv_out_fmt.frac_bits)
    y = np.concatenate([zr.reshape(-1) + 1j * zi.reshape(-1) for zr, zi in job_golden(ch, x)]) / inv
    ref = np.convolve(x / scale, taps(ch))[:len(x)]
    return float(np.max(np.abs(H_SCALE * y - ref)) / np.max(np.abs(ref)))


def coef_stimulus() -> list[StreamBurst]:
    out = []
    for ch in range(C):
        hr, hi = coef_bins(ch)
        out += [StreamBurst(hdr(Coef(ch=ch), W_LOOP), True), StreamBurst(pack(hr, hi, E_IN), True)]
    return out


def channel_stimulus(js) -> list[StreamBurst]:
    out = []
    for tx, x in js:
        nblk = len(x) // B
        out.append(StreamBurst(hdr(Job(tx_id=tx, nblk=nblk), W_LOOP), True))
        re = np.real(x).astype(np.int64).reshape(-1, R)
        im = np.imag(x).astype(np.int64).reshape(-1, R)
        for b in range(nblk):
            out.append(StreamBurst(pack(re[b * BW:(b + 1) * BW], im[b * BW:(b + 1) * BW], E_IN), True))
    return out


def channel_expected(ch: int, js) -> dict[str, list[np.ndarray]]:
    ans, st = [], []
    for tx, x in js:
        blocks = job_golden(ch, x)
        for b, (zr, zi) in enumerate(blocks):
            ans += [hdr(Resp(ch=ch, tx_id=tx, blk=b), W_ANS), pack(zr, zi, E_OUT)]
        st.append(hdr(Status(ch=ch, tx_id=tx, status=StatusCode.OK), W_MSG))
    return {f"m_out_{ch}": ans, f"m_status_{ch}": st}


# ---------------------------------------------------------------------------
# 3. The stages
# ---------------------------------------------------------------------------


def _ep(cls, owner, name: str, bw: int):
    ep = cls(name=f"{owner.name}_{name}", sim=owner.sim, bitwidth=bw)
    setattr(owner, name, ep)
    owner.add_endpoint(ep)
    return ep


@dataclass
class OsFront(FreeRunMod):
    """Channel ``ch``'s front: per block, wait for a credit, read its ``BW`` new words, and send the frame
    ``Tag`` + ``PW`` overlap words + the new words to the arbiter -- the whole block read first, so the
    frame is announced only once complete.  The overlap is the last ``PW`` words of the block before,
    zero at a job's start."""

    cpp_kernel_name: ClassVar[str | None] = "os_front"
    #: Each port at its own width (128-bit words, 32-bit credits), here and in the system flow.
    endpoint_port_widths: ClassVar[bool] = True
    ch: HwParam[int] = 0
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        _ep(FramedStreamIFSlave, self, "s_in", W_LOOP)
        _ep(FramedStreamIFSlave, self, "s_crd", W_MSG)
        _ep(FramedStreamIFMaster, self, "m_blk", W_LOOP)
        #: State across firings, as the HLS body's statics: the job, the overlap, blocks in the loop.
        self.left = 0
        self.job: Job | None = None
        self.blk = 0
        self.inflight = 0
        self.hist = np.zeros((PW, _chunks(W_LOOP)), dtype=np.uint64)

    def kernel_task(self) -> KernelTask:
        ports = ("s_in", "s_crd", "m_blk")
        return KernelTask("os_front_task", "os_front_task.h", ports, template_args=(int(self.ch), CR),
                          framed=ports)

    def run_iter(self):
        if self.left == 0:
            self.job = yield from self.s_in.get_schema(Job)
            self.left, self.blk = int(self.job.nblk), 0
            self.hist = np.zeros_like(self.hist)
        if self.inflight == CR:
            yield from self.s_crd.get_schema(Crd)
            self.inflight -= 1
        new = np.asarray((yield from self.s_in.get()), dtype=np.uint64)
        frame = np.concatenate([self.hist, new])
        yield from self.m_blk.write(Tag(tx_id=self.job.tx_id, ch=int(self.ch), blk=self.blk,
                                        nblk=self.job.nblk, npass=0))
        yield from self.m_blk.write(frame)
        self.hist = frame[-PW:]
        self.inflight += 1
        self.blk += 1
        self.left -= 1

    def workload(self, kind: str = "test") -> dict:
        js = jobs(kind)[int(self.ch)]
        nblk = sum(len(x) // B for _tx, x in js)
        return {"s_in": channel_stimulus(js),
                "s_crd": [StreamBurst(hdr(Crd(ch=int(self.ch)), W_MSG), True) for _ in range(nblk)]}

    def golden(self, kind: str = "test") -> dict:
        out = []
        for tx, x in jobs(kind)[int(self.ch)]:
            nblk = len(x) // B
            words = np.concatenate([np.zeros((PW, _chunks(W_LOOP)), dtype=np.uint64)]
                                   + [b.words for b in channel_stimulus([(tx, x)])[1:]])
            for b in range(nblk):
                out += [hdr(Tag(tx_id=tx, ch=int(self.ch), blk=b, nblk=nblk, npass=0), W_LOOP),
                        words[b * BW:b * BW + NW]]
        return {"m_blk": out}


@dataclass
class OsPre(FreeRunMod):
    """In front of the FFT: per frame, the arbiter's index (``s_sel``: 0 is the feedback, so the inverse)
    and the frame; the direction to the FFT's ``s_dir``, the tag to ``post`` beside the FFT, the ``NW``
    sample words to the FFT -- raw words, already the FFT's input word."""

    cpp_kernel_name: ClassVar[str | None] = "os_pre"
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        _ep(FramedStreamIFSlave, self, "s_frm", W_LOOP)
        _ep(FramedStreamIFSlave, self, "s_sel", W_LOOP)
        _ep(StreamIFMaster, self, "m_x", W_LOOP)
        _ep(StreamIFMaster, self, "m_dir", 8)
        _ep(FramedStreamIFMaster, self, "m_tag", W_LOOP)

    def kernel_task(self) -> KernelTask:
        return KernelTask("os_pre_task", "os_pre_task.h", ("s_frm", "s_sel", "m_x", "m_dir", "m_tag"),
                          template_args=(NW,), framed=("s_frm", "s_sel", "m_tag"))

    def run_iter(self):
        sel = np.asarray((yield from self.s_sel.get()), dtype=np.uint64).reshape(-1)
        tag = yield from self.s_frm.get_schema(Tag)
        words = yield from self.s_frm.get()
        yield from self.m_tag.write(tag)
        yield from self.m_dir.write(np.asarray([1 if int(sel[0]) == 0 else 0], dtype=np.uint64))
        yield from self.m_x.write(np.asarray(words, dtype=np.uint64))


@dataclass
class OsPost(FreeRunMod):
    """After the FFT.  First, once: the ``C`` filters off ``s_coef``, each into its slot.  Then per frame:
    its tag (beside the FFT), then its ``NW`` words.  Pass 0: ``x H_c`` into the FFT's input format, the
    whole product first, then back to the arbiter as a feedback frame (``Tag`` with ``npass`` 1, the
    words).  Pass 1: the answer -- ``Resp``, the last ``BW`` words (the overlap dropped) -- a ``Status``
    after a job's last block, and a credit to the channel."""

    cpp_kernel_name: ClassVar[str | None] = "os_post"
    #: Each port at its own width (128-bit words, 184-bit answers, 32-bit messages).
    endpoint_port_widths: ClassVar[bool] = True
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        _ep(FramedStreamIFSlave, self, "s_coef", W_LOOP)
        _ep(FramedStreamIFSlave, self, "s_tag", W_LOOP)
        _ep(StreamIFSlave, self, "s_y", W_ANS)
        _ep(FramedStreamIFMaster, self, "m_fb", W_LOOP)
        _ep(FramedStreamIFMaster, self, "m_ans", W_ANS)
        _ep(FramedStreamIFMaster, self, "m_st", W_MSG)
        _ep(FramedStreamIFMaster, self, "m_crd", W_MSG)
        #: The slots: each channel's ``H_c / H_SCALE``, loaded once.
        self.h_re = np.zeros((C, NW, R), dtype=np.int64)
        self.h_im = np.zeros((C, NW, R), dtype=np.int64)
        self.nloaded = 0

    def kernel_task(self) -> KernelTask:
        ports = ("s_coef", "s_tag", "s_y", "m_fb", "m_ans", "m_st", "m_crd")
        return KernelTask("os_post_task", "os_post_task.h", ports, template_args=(C, NW, PW),
                          framed=("s_coef", "s_tag", "m_fb", "m_ans", "m_st", "m_crd"))

    def _frame(self):
        """Exactly ``NW`` words off the FFT, which frames by count (``get`` may deliver them in pieces)."""
        parts, have = [], 0
        while have < NW:
            got = np.asarray((yield from self.s_y.get(nwords_max=NW - have)), dtype=np.uint64)
            parts.append(got.reshape(-1, _chunks(W_ANS)))
            have += parts[-1].shape[0]
        return np.concatenate(parts)

    def run_iter(self):
        if self.nloaded < C:
            c = yield from self.s_coef.get_schema(Coef)
            hr, hi = unpack((yield from self.s_coef.get()), E_IN)
            self.h_re[int(c.ch)], self.h_im[int(c.ch)] = hr, hi
            self.nloaded += 1
            return
        tag = yield from self.s_tag.get_schema(Tag)
        y = yield from self._frame()
        ch = int(tag.ch)
        if int(tag.npass) == 0:
            yr, yi = unpack(y, E_OUT)
            pr, pi = multiply(yr, yi, self.h_re[ch], self.h_im[ch])
            yield from self.m_fb.write(Tag(tx_id=tag.tx_id, ch=ch, blk=tag.blk, nblk=tag.nblk, npass=1))
            yield from self.m_fb.write(pack(pr, pi, E_IN))
            return
        yield from self.m_ans.write(Resp(ch=ch, tx_id=tag.tx_id, blk=tag.blk))
        yield from self.m_ans.write(y[PW:])
        if int(tag.blk) == int(tag.nblk) - 1:
            yield from self.m_st.write(Status(ch=ch, tx_id=tag.tx_id, status=StatusCode.OK))
        yield from self.m_crd.write(Crd(ch=ch))

    # -- alone in its harness: the FFT's two passes of every block, as the loop would hand them over --
    def workload(self, kind: str = "test") -> dict:
        tags, ys = [], []
        for ch, js in jobs(kind).items():
            hr, hi = coef_bins(ch)
            for tx, x in js:
                xr = np.concatenate([np.zeros(P, dtype=np.int64), np.real(x).astype(np.int64)])
                xi = np.concatenate([np.zeros(P, dtype=np.int64), np.imag(x).astype(np.int64)])
                nblk = len(x) // B
                for b in range(nblk):
                    seg = slice(b * B, b * B + L)
                    fr, fi = fft_model.pipeline(GEO.direction(False), xr[seg].reshape(NW, R),
                                                xi[seg].reshape(NW, R))
                    pr, pi = multiply(fr, fi, hr, hi)
                    zr, zi = fft_model.pipeline(GEO.direction(True), pr, pi)
                    for p, (a, bb) in ((0, (fr, fi)), (1, (zr, zi))):
                        tags.append(StreamBurst(hdr(Tag(tx_id=tx, ch=ch, blk=b, nblk=nblk, npass=p),
                                                    W_LOOP), True))
                        ys.append(StreamBurst(pack(a, bb, E_OUT), False))
        return {"s_coef": coef_stimulus(), "s_tag": tags, "s_y": ys}

    def golden(self, kind: str = "test") -> dict:
        fb, ans, st, crd = [], [], [], []
        for ch, js in jobs(kind).items():
            hr, hi = coef_bins(ch)
            for tx, x in js:
                blocks = job_golden(ch, x)
                xr = np.concatenate([np.zeros(P, dtype=np.int64), np.real(x).astype(np.int64)])
                xi = np.concatenate([np.zeros(P, dtype=np.int64), np.imag(x).astype(np.int64)])
                nblk = len(x) // B
                for b in range(nblk):
                    seg = slice(b * B, b * B + L)
                    fr, fi = fft_model.pipeline(GEO.direction(False), xr[seg].reshape(NW, R),
                                                xi[seg].reshape(NW, R))
                    pr, pi = multiply(fr, fi, hr, hi)
                    fb += [hdr(Tag(tx_id=tx, ch=ch, blk=b, nblk=nblk, npass=1), W_LOOP), pack(pr, pi, E_IN)]
                    ans += [hdr(Resp(ch=ch, tx_id=tx, blk=b), W_ANS), pack(*blocks[b], E_OUT)]
                    crd.append(hdr(Crd(ch=ch), W_MSG))
                st.append(hdr(Status(ch=ch, tx_id=tx, status=StatusCode.OK), W_MSG))
        return {"m_fb": fb, "m_ans": ans, "m_st": st, "m_crd": crd}


# ---------------------------------------------------------------------------
# 4. The composite
# ---------------------------------------------------------------------------

#: Link depths, in words.  The loop's are what the credits let into it: ``C*CR`` blocks at most, each with
#: one frame (a tag word + ``NW`` words) in flight.
FRAME = NW + 1
DEPTH = {
    "blk": 2 * FRAME,             # a front's frames into the arbiter
    "fb": C * CR * FRAME,         # every feedback frame the loop can hold: post never blocks on it
    "frm": 2 * FRAME,             # the arbiter's output
    "sel": C * CR + 1,            # the arbiter's index, one a frame
    "tag": C * CR + 1,            # the tag beside the FFT, one a frame in it
    "x": 4,                       # into the FFT
    "dir": 4,
    "y": 2 * NW,                  # out of the FFT
    "ans": 2 * (BW + 1),
    "st": 4,
    "crd": C * CR + 1,            # credits on their way back
    "crd_c": CR + 1,
}


@dataclass
class OsFilterBank(FreeRunMod):
    """The filter bank: ``C`` fronts, the arbiter, ``pre``, the shared ``SsrFft``, ``post`` and three demuxes;
    boundary ``s_in_0 .. s_in_{C-1}``, ``s_coef``, ``m_out_0 ..``, ``m_status_0 ..``."""

    cpp_kernel_name: ClassVar[str | None] = "os_filterbank"
    #: Each boundary port at its own width (128-bit sample words, 184-bit answers, 32-bit statuses).
    endpoint_port_widths: ClassVar[bool] = True
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        n = self.name
        self.fronts = [OsFront(name=f"{n}_front{c}", sim=self.sim, clk=self.clk, ch=c) for c in range(C)]
        self.arb = StreamArbiter(name=f"{n}_arb", sim=self.sim, clk=self.clk, n_in=C + 1, bitwidth=W_LOOP,
                                 frame_bursts=2, policy="priority")
        self.pre = OsPre(name=f"{n}_pre", sim=self.sim, clk=self.clk)
        self.fft = SsrFft(name=f"{n}_fft", sim=self.sim, clk=self.clk, L=L, reorder="pingpong",
                          direction="runtime")
        self.post = OsPost(name=f"{n}_post", sim=self.sim, clk=self.clk)
        self.dmx_ans = StreamDemux(name=f"{n}_dmx_ans", sim=self.sim, clk=self.clk, n_out=C, bitwidth=W_ANS,
                                   frame_bursts=2, route="header", sel_lsb=0, sel_bits=8)
        self.dmx_st = StreamDemux(name=f"{n}_dmx_st", sim=self.sim, clk=self.clk, n_out=C, bitwidth=W_MSG,
                                  frame_bursts=1, route="header", sel_lsb=0, sel_bits=8)
        self.dmx_crd = StreamDemux(name=f"{n}_dmx_crd", sim=self.sim, clk=self.clk, n_out=C,
                                   bitwidth=W_MSG, frame_bursts=1, route="header", sel_lsb=0, sel_bits=8)
        for c in (*self.fronts, self.arb, self.pre, self.fft, self.post, self.dmx_ans, self.dmx_st,
                  self.dmx_crd):
            self.add_comp(c)

        def link(name, master, slave, bw, framed=True, depth_key=None):
            iface = StreamIF(name=f"{n}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=bw, framed=framed,
                             depth=DEPTH[depth_key or name])
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        for c, f in enumerate(self.fronts):
            link(f"blk{c}", f.m_blk, self.arb.s_in[c + 1], W_LOOP, depth_key="blk")
            link(f"crd{c}", self.dmx_crd.m_out[c], f.s_crd, W_MSG, depth_key="crd_c")
        link("fb", self.post.m_fb, self.arb.s_in[0], W_LOOP)          # the cycle: the product back in
        link("frm", self.arb.m_out, self.pre.s_frm, W_LOOP)
        link("sel", self.arb.m_sel, self.pre.s_sel, W_LOOP)
        link("x", self.pre.m_x, self.fft.s_in, W_LOOP, framed=False)
        link("dir", self.pre.m_dir, self.fft.s_dir, 8, framed=False)
        link("tag", self.pre.m_tag, self.post.s_tag, W_LOOP)           # beside the FFT
        link("y", self.fft.m_out, self.post.s_y, W_ANS, framed=False)
        link("ans", self.post.m_ans, self.dmx_ans.s_in, W_ANS)
        link("st", self.post.m_st, self.dmx_st.s_in, W_MSG)
        link("crd", self.post.m_crd, self.dmx_crd.s_in, W_MSG)

        self.boundary = ([f"s_in_{c}" for c in range(C)] + ["s_coef"]
                         + [f"m_out_{c}" for c in range(C)] + [f"m_status_{c}" for c in range(C)])
        for c in range(C):
            setattr(self, f"s_in_{c}", self.fronts[c].s_in)
            setattr(self, f"m_out_{c}", self.dmx_ans.m_out[c])
            setattr(self, f"m_status_{c}", self.dmx_st.m_out[c])
        self.s_coef = self.post.s_coef

    def workload(self, kind: str = "test") -> dict:
        js = jobs(kind)
        return {"s_coef": coef_stimulus(), **{f"s_in_{c}": channel_stimulus(js[c]) for c in range(C)}}

    def golden(self, kind: str = "test") -> dict:
        out = {}
        for c, js in jobs(kind).items():
            out.update(channel_expected(c, js))
        return out
