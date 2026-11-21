"""scenario.py -- the test scenarios: commands as intents, the stimulus, and every expected message.

Each scenario is a list of :class:`Cmd` (what the sender meant, and how it went wrong, if it did).
From the intents alone -- through the golden's functions, never through the stages -- this module
derives the input stream, the two outputs, and the messages every internal link should carry, which
are the stimulus of each stage's own harness and the expected output of the stage before it.

Scenarios (``scenario(kind)``):

* ``"test"`` -- the worked example (B4, 4 antennas, three UEs, a PUSCH interferer), a two-repetition
  one-antenna occasion with ``N_CS = 0``, a noise-only two-antenna occasion with 22 roots, a B4 occasion
  whose PRACH wraps round bin 1023, then a ``BAD_PARAM`` (fatal) and a command never answered;
* ``"early"`` -- a good command, a window that ends early (``TLAST_EARLY``), one never answered;
* ``"badroot"`` -- a good command, a root whose ``u_inv`` is wrong (``BAD_ROOT``), one never answered;
* ``"late"``, ``"lateroots"``, ``"latecmd"`` -- a good command, then a window / the roots / the command
  burst with no TLAST where it should end (``TLAST_LATE``, decision 20), one never answered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache

import numpy as np

from waveflow.utils.burst_io import StreamBurst

from . import golden as g
from . import nr
from waveflow.hw.arrayutils import write_array

from .prach import (C16, C16_FMT, C25, C27, FFT1_GEO, FFT1_OUT_W, FFT2_GEO, FFT2_OUT_W, L_RA, N_FFT,
                    U8, W, Y_PAD, CorHdr, ExtHdr, FwdHdr, FwdTrl, PrachCmd, Resp, Status, StatusCode,
                    check_params, check_roots, det_rec, msg_words, pack_c)

#: Thresholds: the host's table (decision 18, ``prach/thresholds.py``), per antenna count and N_CS.
from .thresholds import thr_q8 as table_thr_q8  # noqa: E402


@dataclass
class Cmd:
    """One command as intended.  The fields are PrachCmd's (they may be out of range on purpose);
    ``roots`` the ``2 n_roots`` values sent (``roots_sent`` of them, if fewer); ``wr, wi`` the windows
    ``(n_sym, n_ant, 1024)`` int16; ``cut = (frame, words)``: that window ends after ``words`` words."""
    tx_id: int
    n_ant: int
    n_sym: int
    k_start: int
    n_cs: int
    n_pre: int
    n_roots: int
    thr_q8: int
    roots: np.ndarray
    wr: np.ndarray
    wi: np.ndarray
    roots_sent: int | None = None
    cut: tuple[int, int] | None = None
    #: ``(burst, extra_words)``: that burst runs ``extra_words`` past its end with no TLAST where it
    #: should be (decision 20) -- burst -2 the command, -1 the roots, f >= 0 window f.
    late: tuple[int, int] | None = None
    note: str = ""

    def hdr(self) -> PrachCmd:
        return PrachCmd(tx_id=self.tx_id, n_ant=self.n_ant, n_sym=self.n_sym, k_start=self.k_start,
                        n_cs=self.n_cs, n_pre=self.n_pre, n_roots=self.n_roots, thr_q8=self.thr_q8)

    def params(self) -> g.Params:
        rt = tuple((int(self.roots[2 * i]), int(self.roots[2 * i + 1])) for i in range(self.n_roots))
        return g.Params(self.n_ant, self.n_sym, self.k_start, self.n_cs, self.n_pre, rt, self.thr_q8)

    def frames(self) -> list[tuple[np.ndarray, np.ndarray]]:
        """The windows in stream order: symbol-major, then antenna."""
        return [(self.wr[s, a], self.wi[s, a]) for s in range(self.wr.shape[0])
                for a in range(self.wr.shape[1])]


@dataclass
class Outcome:
    """What the hardware does with one command, from the intent."""
    code: StatusCode
    nframes: int                                   # windows front sends the FFT
    fft_in: list[tuple[np.ndarray, np.ndarray]] = field(default_factory=list)
    trace: g.Trace | None = None


def outcome(c: Cmd) -> Outcome:
    late = c.late[0] if c.late is not None else None
    if late == -2:
        return Outcome(StatusCode.TLAST_LATE, 0)
    code = check_params(c.hdr())
    if code != StatusCode.OK:
        return Outcome(code, 0)
    nr2 = 2 * c.n_roots
    sent = nr2 if c.roots_sent is None else min(nr2, c.roots_sent)
    if -(-sent // 16) < -(-nr2 // 16):                 # front counts words, as the hardware does
        return Outcome(StatusCode.TLAST_EARLY, 0)
    if late == -1:
        return Outcome(StatusCode.TLAST_LATE, 0)
    code = check_roots(c.roots)
    if code != StatusCode.OK:
        return Outcome(code, 0)
    frames = c.frames()
    fft_in = []
    stop = StatusCode.OK
    for f, (re, im) in enumerate(frames):
        if stop != StatusCode.OK:
            fft_in.append((np.zeros(N_FFT, np.int64), np.zeros(N_FFT, np.int64)))
            continue
        if c.cut is not None and c.cut[0] == f:
            n = 4 * c.cut[1]
            r2, i2 = np.zeros(N_FFT, np.int64), np.zeros(N_FFT, np.int64)
            r2[:n], i2[:n] = re[:n], im[:n]
            fft_in.append((r2, i2))
            stop = StatusCode.TLAST_EARLY
        else:
            fft_in.append((np.asarray(re, np.int64), np.asarray(im, np.int64)))
            if late == f:
                stop = StatusCode.TLAST_LATE           # this window whole, zeros after
    if stop != StatusCode.OK:
        return Outcome(stop, len(frames), fft_in)
    return Outcome(StatusCode.OK, len(frames), fft_in, g.search_fixed(c.wr, c.wi, c.params()))


def _bursts(arrs) -> list[StreamBurst]:
    return [StreamBurst(np.asarray(a, dtype=np.uint64), True) for a in arrs]


def _hdr_words(m) -> np.ndarray:
    return np.asarray(m.serialize(word_bw=W), dtype=np.uint64)


def _u8_words(vals) -> np.ndarray:
    return np.asarray(write_array(np.asarray(vals, dtype=np.int64), elem_type=U8, word_bw=W),
                      dtype=np.uint64)


@dataclass
class Scenario:
    cmds: list[Cmd]

    def __post_init__(self):
        self.outcomes = [outcome(c) for c in self.cmds]

    def _answered(self):
        """The commands the hardware answers: up to and including the first error."""
        for c, o in zip(self.cmds, self.outcomes):
            yield c, o
            if o.code != StatusCode.OK:
                return

    # -- the input stream --------------------------------------------------------------------
    def stimulus(self) -> list[StreamBurst]:
        out = []
        for c in self.cmds:
            late, extra = c.late if c.late is not None else (None, 0)

            def longer(words, extra=extra):
                """The burst run on past its end: the next words repeat its own (any data will do)."""
                words = np.asarray(words, dtype=np.uint64).reshape(-1, W // 64)
                return np.concatenate([words, words[np.arange(extra) % len(words)]])

            hdr = _hdr_words(c.hdr())
            out.append(longer(hdr) if late == -2 else hdr)
            roots = c.roots if c.roots_sent is None else c.roots[:c.roots_sent]
            if len(roots):
                rw = _u8_words(roots)
                out.append(longer(rw) if late == -1 else rw)
            if late is not None and late < 0:
                continue                                 # front halts here; nothing more is read
            for f, (re, im) in enumerate(c.frames()):
                words = pack_c(re, im, C16, C16_FMT, W)
                if c.cut is not None and c.cut[0] == f:
                    out.append(words[:c.cut[1]])
                    break
                if late == f:
                    out.append(longer(words))
                    break
                out.append(words)
        return _bursts(out)

    # -- the outputs ---------------------------------------------------------------------------
    def expected(self) -> tuple[list[np.ndarray], list[np.ndarray]]:
        out, status = [], []
        for c, o in self._answered():
            dets = o.trace.dets if o.trace is not None else []
            e = o.trace.e if o.trace is not None else 0
            out.append(_hdr_words(Resp(tx_id=c.tx_id, n_det=len(dets), exp=e)))
            if dets:
                out.append(msg_words([det_rec(d) for d in dets]))
            status.append(_hdr_words(Status(tx_id=c.tx_id, status=o.code)))
        return out, status

    # -- the internal links (each stage's harness) ---------------------------------------------
    def _fwd(self, c: Cmd) -> dict:
        return dict(tx_id=c.tx_id, n_ant=c.n_ant, n_sym=c.n_sym, k_start=c.k_start, n_cs=c.n_cs,
                    n_pre=c.n_pre, n_roots=c.n_roots, thr_q8=c.thr_q8)

    def link_cmd_words(self) -> list[np.ndarray]:
        out = []
        for c, o in self._answered():
            out.append(_hdr_words(FwdHdr(**self._fwd(c), nframes=o.nframes)))
            if o.nframes:
                out.append(_u8_words(c.roots))
            out.append(_hdr_words(FwdTrl(tx_id=c.tx_id, code=o.code)))
        return out

    def link_cmd(self) -> list[StreamBurst]:
        return _bursts(self.link_cmd_words())

    def link_fft1_in_words(self) -> list[np.ndarray]:
        return [pack_c(re, im, C16, C16_FMT, W) for _c, o in self._answered() for re, im in o.fft_in]

    def link_fft1_out_words(self) -> list[np.ndarray]:
        out = []
        for _c, o in self._answered():
            for re, im in o.fft_in:
                fr, fi = g.fft1024(re, im)
                out.append(pack_c(fr, fi, C27, FFT1_GEO.out_fmt, FFT1_OUT_W))
        return out

    def link_fft1_out(self) -> list[StreamBurst]:
        return _bursts(self.link_fft1_out_words())

    def _ext(self, c: Cmd, o: Outcome) -> dict:
        return dict(tx_id=c.tx_id, n_ant=c.n_ant, n_cs=c.n_cs, n_pre=c.n_pre, n_roots=c.n_roots,
                    thr_q8=c.thr_q8, exp=o.trace.e if o.trace is not None else 0, code=o.code)

    def link_y_words(self) -> list[np.ndarray]:
        out = []
        for c, o in self._answered():
            out.append(_hdr_words(ExtHdr(**self._ext(c, o))))
            if o.code == StatusCode.OK:
                out.append(_u8_words(c.roots))
                pr = np.zeros((c.n_ant, Y_PAD), np.int64)
                pi = np.zeros_like(pr)
                pr[:, :L_RA], pi[:, :L_RA] = o.trace.yr, o.trace.yi
                out.append(pack_c(pr, pi, C16, C16_FMT, W))
        return out

    def link_y(self) -> list[StreamBurst]:
        return _bursts(self.link_y_words())

    def link_cor_words(self) -> list[np.ndarray]:
        return [_hdr_words(CorHdr(**self._ext(c, o))) for c, o in self._answered()]

    def link_cor(self) -> list[StreamBurst]:
        return _bursts(self.link_cor_words())

    def _fft2(self):
        """``(input, output)`` frames of fft256, per job, root-major then antenna."""
        for c, o in self._answered():
            if o.code != StatusCode.OK:
                continue
            for u, ui in c.params().roots:
                zr, zi = g.correlate(o.trace.yr, o.trace.yi, u, ui)
                for a in range(c.n_ant):
                    yield (zr[a], zi[a]), g.fft256(zr[a], zi[a])

    def link_fft2_in_words(self) -> list[np.ndarray]:
        return [pack_c(zi[0], zi[1], C16, C16_FMT, W) for zi, _ in self._fft2()]

    def link_fft2_out_words(self) -> list[np.ndarray]:
        return [pack_c(fo[0], fo[1], C25, FFT2_GEO.out_fmt, FFT2_OUT_W) for _, fo in self._fft2()]

    def link_fft2_out(self) -> list[StreamBurst]:
        return _bursts(self.link_fft2_out_words())


# ---------------------------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------------------------
def _roots_arr(roots: list[int]) -> np.ndarray:
    return np.array([v for u in roots for v in (u, pow(u, -1, L_RA))], dtype=np.int64)


def make_cmd(tx_id: int, cfg: nr.CellConfig, n_ant: int, ues: list[nr.Ue], rng, *, thr_q8=None,
             pusch_db=None, **kw) -> Cmd:
    rx = nr.occasion(cfg, ues, n_ant, rng, pusch_db=pusch_db)
    wr, wi = nr.quantize_adc(nr.windows(rx, cfg))
    return Cmd(tx_id=tx_id, n_ant=n_ant, n_sym=cfg.n_sym, k_start=cfg.k_start, n_cs=cfg.n_cs,
               n_pre=cfg.n_pre, n_roots=cfg.n_roots, thr_q8=table_thr_q8(n_ant, cfg.n_cs) if thr_q8 is None else thr_q8,
               roots=_roots_arr(cfg.roots), wr=wr, wi=wi, **kw)


B4 = nr.CellConfig(n_cs=23, root0=0, n_pre=64, fmt="B4", k_start=nr.k_start_for(51, 20))


def worked_cmd(tx_id: int = 1) -> Cmd:
    """The worked example: B4, 4 antennas, three UEs at -5, 0 and -8 dB with 2.0, 0.3 and 4.5 us of
    round-trip delay, and a PUSCH interferer 10 dB above the noise on the rest of the carrier."""
    rng = np.random.default_rng(1)
    ues = [nr.Ue(17, 2.0e-6, -5.0), nr.Ue(40, 0.3e-6, 0.0), nr.Ue(63, 4.5e-6, -8.0)]
    return make_cmd(tx_id, B4, 4, ues, rng, pusch_db=10.0, note="worked example")


def test_cmds() -> list[Cmd]:
    rng = np.random.default_rng(7)
    a1 = nr.CellConfig(n_cs=0, root0=10, n_pre=8, fmt="A1", k_start=nr.k_start_for(51, 30))
    c2 = worked_cmd(1)
    c_a1 = make_cmd(2, a1, 1, [nr.Ue(5, 1.0e-6, 0.0)], rng, note="A1, 1 antenna, N_CS 0, 8 roots")
    ncs46 = nr.CellConfig(n_cs=46, root0=3, n_pre=64, fmt="A2", k_start=nr.k_start_for(51, 0))
    c_noise = make_cmd(3, ncs46, 2, [], rng, note="A2, 2 antennas, N_CS 46, 22 roots, noise only")
    wrap = nr.CellConfig(n_cs=23, root0=20, n_pre=64, fmt="B4", k_start=1000)
    c_wrap = make_cmd(4, wrap, 4, [nr.Ue(0, 4.5e-6, -10.0), nr.Ue(33, 0.0, -12.0)], rng,
                      note="B4, PRACH bins wrap 1023 -> 0, a UE at the usable zone's end (zone - 1 us)")
    bad = make_cmd(5, a1, 1, [], rng, note="BAD_PARAM: n_roots does not match n_pre / per_root")
    bad.n_roots = 7
    bad.roots = bad.roots[:14]
    after = make_cmd(6, a1, 1, [], rng, note="never answered")
    return [c2, c_a1, c_noise, c_wrap, bad, after]


def early_cmds() -> list[Cmd]:
    rng = np.random.default_rng(11)
    a2 = nr.CellConfig(n_cs=23, root0=0, n_pre=12, fmt="B1", k_start=nr.k_start_for(51, 10))
    return [make_cmd(21, a2, 2, [nr.Ue(7, 1.5e-6, 0.0)], rng),
            make_cmd(22, a2, 2, [nr.Ue(3, 1.5e-6, 0.0)], rng, cut=(2, 100),
                     note="TLAST_EARLY: window 2 (symbol 1, antenna 0) ends after 100 of 256 words"),
            make_cmd(23, a2, 2, [], rng, note="never answered")]


def badroot_cmds() -> list[Cmd]:
    rng = np.random.default_rng(13)
    a2 = nr.CellConfig(n_cs=23, root0=0, n_pre=12, fmt="B1", k_start=nr.k_start_for(51, 10))
    good = make_cmd(31, a2, 2, [nr.Ue(11, 0.5e-6, 0.0)], rng)
    bad = make_cmd(32, a2, 2, [nr.Ue(1, 0.5e-6, 0.0)], rng, note="BAD_ROOT: u_inv of root 1 wrong")
    bad.roots = bad.roots.copy()
    bad.roots[3] = (bad.roots[3] + 1) % L_RA
    return [good, bad, make_cmd(33, a2, 2, [], rng, note="never answered")]


def _late_cmds(burst: int, what: str, tx0: int) -> list[Cmd]:
    rng = np.random.default_rng(17 + tx0)
    a2 = nr.CellConfig(n_cs=23, root0=0, n_pre=12, fmt="B1", k_start=nr.k_start_for(51, 10))
    return [make_cmd(tx0, a2, 2, [nr.Ue(5, 1.0e-6, 0.0)], rng),
            make_cmd(tx0 + 1, a2, 2, [nr.Ue(8, 2.0e-6, 0.0)], rng, late=(burst, 3),
                     note=f"TLAST_LATE: {what} runs 3 words past its end"),
            make_cmd(tx0 + 2, a2, 2, [], rng, note="never answered")]


def late_cmds() -> list[Cmd]:
    return _late_cmds(1, "window 1 (symbol 0, antenna 1)", 41)


def lateroots_cmds() -> list[Cmd]:
    return _late_cmds(-1, "the roots burst", 51)


def latecmd_cmds() -> list[Cmd]:
    return _late_cmds(-2, "the command burst", 61)


#: Every scenario: the default and its error cases (each error fatal, so one a scenario).
KINDS = ("test", "early", "badroot", "late", "lateroots", "latecmd")


@cache
def scenario(kind: str = "test") -> Scenario:
    cmds = {"test": test_cmds, "early": early_cmds, "badroot": badroot_cmds, "late": late_cmds,
            "lateroots": lateroots_cmds, "latecmd": latecmd_cmds}[kind]()
    return Scenario(cmds)
