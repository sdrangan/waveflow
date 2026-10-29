"""Spike 1.2 c (plans/ltbf_dse/ltbf_dse.md): the vector unit's two LTBF programs, on the interpreter.

* **The CG step, one iteration a job, its state through Shared Mem.**  In: ``X, R, D`` and the
  systolic engine's product ``U`` (``K D`` explicit, ``H_alpha (H_alpha^H D)`` factored); out:
  ``X, R, D``.  ``S = D + U``; ``ps = Re(D^H S)``; ``rz = |R|^2`` recomputed (scalars cannot be
  loaded with ``scalar_ext`` off); ``alpha = rz / ps``; ``X += alpha D``; ``R -= alpha S``;
  ``rz' = |R|^2``; ``beta = rz' / rz``; ``D = beta D + R``.  A column's dot product spans all its
  ``k = N_rx`` rows, so a job is one block of ``k`` rows and the build needs ``VLMAX >= N_rx``.
* **The equalizer at r = 1, a weight per subcarrier.**  Lanes are subcarriers.  ``[h, sqrt(Rt)]``
  (VL 2) gives ``|h|^2 + Rt`` in one ``wf.vdotr``; ``wf.sdivz`` inverts it (the dividend 1 from a
  ``wf.vdotr`` of a loaded ones vector); ``wf.vmacc.vs`` scales ``h`` into the weight ``w``; the
  slot's 12 data symbols take ``x = conj(w) z``, broadcast.  One divide a subcarrier a slot.

Each program is checked on the interpreter (bit-exact model) at a small size against numpy, then
priced at the LTBF sizes with the calibrated execute-time model (``VecTiming``, extrapolated beyond
its fit range) and the words it moves through Shared Mem.  Output ``data/spikes/spike_c.json``.

    python -m examples.ltbf_dse.spikes.spike_c_vecunit
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from examples.vecunit_cg.cg_unit import reg
from waveflow.linalg.lanes import elems_per_word
from waveflow.utils import fixputils as fxu
from waveflow.vecunit import isa as I
from waveflow.vecunit.asm import Programs, assemble
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.model import run_job
from waveflow.vecunit.unit import _calibrated, job_features

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data" / "spikes"
PORT = 0
FREQ = 250e6

# -- the CG step -------------------------------------------------------------------------------------

#: Input queues X, R, D, U; output queues X, R, D.
QX, QR, QD, QU = 0, 1, 2, 3
OX, OR, OD = 0, 1, 2
#: Formats: the vector unit's CG centre (examples/vecunit_cg), U in S's.
FX, FR, FD, FS = reg(12, 3), reg(12, 4), reg(12, 4), reg(12, 5)
FPS, FRZ, FAL, FBE = reg(20, 10), reg(20, 9), reg(12, 5), reg(12, 3)


def cg_step_config(k: int, n: int, L: int, word_bits: int = 64) -> VecUnitConfig:
    ng = -(-n // L)
    return VecUnitConfig(
        group_formats=(FX, FR, FD, FS) * ng,
        scalar_formats=(FPS, FAL, FBE, FRZ, FRZ) * ng,
        in_queue_formats=(FX, FR, FD, FS),
        out_queue_formats=(FX, FR, FD),
        L=L,
        VLMAX=k,
        Kmax=k,
        Nmax=n,
        NB=4,
        g_div=6,
        rs_max=0,
        word_bits=word_bits,
    )


def cg_step_programs(cfg: VecUnitConfig, k: int, n: int) -> Programs:
    G = cfg.groups_per_row(n)
    ex = [I.setvl(k)]
    for g in range(G):
        X, R, D, S = 4 * g, 4 * g + 1, 4 * g + 2, 4 * g + 3
        ps, al, be, rz, rz2 = 5 * g, 5 * g + 1, 5 * g + 2, 5 * g + 3, 5 * g + 4
        ex += [I.vlse(R, QR, g, G).c, I.vdotr(rz, R, R)]
        # A pass writes one group a bank once: load D and U, then S = D + U and its dot product.
        ex += [I.vlse(D, QD, g, G).c, I.vlse(S, QU, g, G)]
        ex += [I.vadd_vv(S, S, D).c, I.vdotr(ps, D, S)]
        ex += [I.vlse(X, QX, g, G)]
        ex += [I.sdivz(al, rz, ps)]
        ex += [I.vmacc_vs(X, al, D).c, I.vnmsac_vs(R, al, S).c, I.vdotr(rz2, R, R)]
        ex += [I.sdivz(be, rz2, rz)]
        ex += [I.vmadd_vs(D, be, R).c, I.vsse(OD, D, g, G)]
        ex += [I.vsse(OX, X, g, G), I.vsse(OR, R, g, G)]
    ex += [I.qpop(q) for q in (QX, QR, QD, QU)] + [I.qpush(q) for q in (OX, OR, OD)]
    load = [I.ld(q, PORT, k, n) for q in (QX, QR, QD, QU)]
    store = [I.st(PORT, q, q, k, n) for q in (OX, OR, OD)]
    return Programs(load=load, execute=ex, store=store)


def _q(z: np.ndarray, f) -> tuple[np.ndarray, np.ndarray]:
    return fxu.quantize_real(z.real, f), fxu.quantize_real(z.imag, f)


def _deq(re: np.ndarray, im: np.ndarray, f) -> np.ndarray:
    return (np.asarray(re, float) + 1j * np.asarray(im, float)) * 2.0 ** -(
        f.W - f.int_bits
    )


def check_cg_step(k: int = 64, n: int = 8, L: int = 8, seed: int = 3) -> dict:
    """One iteration on the interpreter against numpy (float), relative errors per output."""
    rng = np.random.default_rng(seed)
    m = 4 * k
    H = (rng.standard_normal((k, m)) + 1j * rng.standard_normal((k, m))) / np.sqrt(
        2 * m
    )
    Q = np.eye(k) + H @ H.conj().T / 4
    B = (rng.standard_normal((k, n)) + 1j * rng.standard_normal((k, n))) / 4
    X = np.zeros((k, n), complex)
    R = B.copy()
    D = B.copy()
    U = (Q - np.eye(k)) @ D
    ins = {
        f: _q(v, fm)
        for f, (v, fm) in {
            "X": (X, FX),
            "R": (R, FR),
            "D": (D, FD),
            "U": (U, FS),
        }.items()
    }
    cfg = cg_step_config(k, n, L)
    progs = cg_step_programs(cfg, k, n)
    res = run_job(cfg, *progs, {PORT: [ins["X"], ins["R"], ins["D"], ins["U"]]})
    S = D + U
    al = np.sum(np.abs(R) ** 2, 0) / np.real(np.sum(D.conj() * S, 0))
    X1 = X + D * al
    R1 = R - S * al
    be = np.sum(np.abs(R1) ** 2, 0) / np.sum(np.abs(R) ** 2, 0)
    D1 = D * be + R1
    got = {r["tag"]: (re, im) for r, re, im in res.outputs[PORT]}
    err = {}
    for tag, (want, f) in {OX: (X1, FX), OR: (R1, FR), OD: (D1, FD)}.items():
        g = _deq(*got[tag], f)
        err["XRD"[tag]] = float(np.linalg.norm(g - want) / np.linalg.norm(want))
    return {"status": int(res.status), "rel_err": err}


# -- the equalizer -----------------------------------------------------------------------------------

#: Input queues: [h, sqrt(Rt)] (2 x m), ones (1 x m), h per RE (12 x m), Z (12 x m); output x.
QH, Q1, QHM, QZ = 0, 1, 2, 3
OXH = 0
FH, FONE, FZ, FY, FXH = reg(16, 3), reg(16, 2), reg(16, 4), reg(16, 5), reg(16, 4)
FDEN, FINV = reg(24, 8), reg(24, 8)
N_SYM = 12
#: Groups per column group: Hv, E, Hm, Z in banks 0-3, then Y, X in banks 0-1 (a pass reads and
#: writes one group a bank); 8 a column group so NB = 4 divides NG.
GPG = 8


def eq_config(m: int, L: int, word_bits: int = 64) -> VecUnitConfig:
    G = -(-m // L)
    return VecUnitConfig(
        group_formats=(FH, FONE, FH, FZ, FY, FXH, FONE, FONE) * G,
        scalar_formats=(FONE, FDEN, FINV) * G,
        in_queue_formats=(FH, FONE, FH, FZ),
        out_queue_formats=(FXH,),
        L=L,
        VLMAX=N_SYM,
        Kmax=N_SYM,
        Nmax=m,
        NB=4,
        g_div=8,
        rs_max=0,
        word_bits=word_bits,
    )


def eq_programs(cfg: VecUnitConfig, m: int) -> Programs:
    """``y = conj(h) z`` a resource element, ``x = y / (|h|^2 + Rt)`` with one divide a subcarrier.
    The broadcast form copies lane 0 to every lane and the scalar forms take a real scalar, so ``h``
    arrives a resource element (constant over the slot) for the elementwise product."""
    G = cfg.groups_per_row(m)
    ex = []
    for g in range(G):
        b = GPG * g
        Hv, E, Hm, Z, Y, X = b, b + 1, b + 2, b + 3, b + 4, b + 5
        one, den, inv = 3 * g, 3 * g + 1, 3 * g + 2
        ex += [I.setvl(1), I.vlse(E, Q1, g, G).c, I.vdotr(one, E, E)]
        ex += [I.setvl(2), I.vlse(Hv, QH, g, G).c, I.vdotr(den, Hv, Hv)]
        ex += [I.sdivz(inv, one, den)]
        ex += [I.setvl(N_SYM), I.vlse(Hm, QHM, g, G).c, I.vlse(Z, QZ, g, G).c]
        ex += [I.vmul_vv(Y, Hm, Z, conj=True)]
        ex += [I.vmv_v_i(X, 0)]
        ex += [I.vmacc_vs(X, inv, Y).c, I.vsse(OXH, X, g, G)]
    ex += [I.qpop(q) for q in (QH, Q1, QHM, QZ)] + [I.qpush(OXH)]
    load = [I.ld(QH, PORT, 2, m), I.ld(Q1, PORT, 1, m), I.ld(QHM, PORT, N_SYM, m)]
    load += [I.ld(QZ, PORT, N_SYM, m)]
    store = [I.st(PORT, OXH, 0, N_SYM, m)]
    return Programs(load=load, execute=ex, store=store)


def check_eq(m: int = 32, L: int = 8, seed: int = 5) -> dict:
    rng = np.random.default_rng(seed)
    h = (rng.standard_normal(m) + 1j * rng.standard_normal(m)) / 2
    rt = 0.3
    z = (rng.standard_normal((N_SYM, m)) + 1j * rng.standard_normal((N_SYM, m))) / 2
    hr = np.vstack([h, np.full(m, np.sqrt(rt))])
    hm = np.tile(h, (N_SYM, 1))
    ins = [_q(hr, FH), _q(np.ones((1, m)), FONE), _q(hm, FH), _q(z, FZ)]
    cfg = eq_config(m, L)
    res = run_job(cfg, *eq_programs(cfg, m), {PORT: ins})
    want = np.conj(h / (np.abs(h) ** 2 + rt))[None, :] * z
    _r, re, im = res.outputs[PORT][0]
    g = _deq(re, im, FXH)
    return {
        "status": int(res.status),
        "rel_err": float(np.linalg.norm(g - want) / np.linalg.norm(want)),
    }


# -- pricing -----------------------------------------------------------------------------------------


def price(cfg: VecUnitConfig, progs: Programs, mem_words: int) -> dict:
    t = _calibrated().for_config(cfg)
    feats = job_features(progs.execute, cfg)
    words = assemble(cfg, progs, tx_id=1)
    prog_words = sum(int(np.size(v)) for v in words.values())
    return {
        "exec_cycles": round(t.predict(feats), 1),
        "instr": int(feats["instr"]),
        "program_words_64b": prog_words,
        "data_words": int(mem_words),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out: dict = {
        "cg_step_check": check_cg_step(),
        "eq_check": check_eq(),
        "cg_step": {},
        "eq": {},
    }
    for k in (64, 256, 1024):
        for n in (8, 16, 80):
            for L in (4, 8, 16):
                try:
                    cfg = cg_step_config(k, n, L, word_bits=128)
                except ValueError as e:
                    out["cg_step"][f"k{k}_n{n}_L{L}"] = {"invalid": str(e)[:200]}
                    continue
                epw = elems_per_word(cfg.lane_bits, cfg.word_bits)
                mem = 7 * -(-k * n // epw)
                p = price(cfg, cg_step_programs(cfg, k, n), mem)
                p["us_at_250MHz"] = round(p["exec_cycles"] / FREQ * 1e6, 2)
                out["cg_step"][f"k{k}_n{n}_L{L}"] = p
    for m in (64, 128, 256):
        for L in (8, 16):
            cfg = eq_config(m, L, word_bits=128)
            epw = elems_per_word(cfg.lane_bits, cfg.word_bits)
            mem = -(-2 * m // epw) + -(-m // epw) + 3 * -(-N_SYM * m // epw)
            p = price(cfg, eq_programs(cfg, m), mem)
            p["cycles_per_subcarrier"] = round(p["exec_cycles"] / m, 2)
            p["re_per_cycle"] = round(N_SYM * m / p["exec_cycles"], 3)
            out["eq"][f"m{m}_L{L}"] = p
    (OUT / "spike_c.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()


# -- the equalizer of the spec's gate E: weights from the estimator, the Z-tile layout -------------

#: Input queues W0, Z0, W1, Z1 (each U_pad x c: streams x resource elements); output X.
QW0, QZ0, QW1, QZ1 = 0, 1, 2, 3


def apply_config(
    u_pad: int, c: int, L: int, r: int, word_bits: int = 128
) -> VecUnitConfig:
    G = -(-c // L)
    fz, fw = reg(16, 4), reg(16, 3)
    return VecUnitConfig(
        group_formats=(fw, fz, FXH, fw, fz, FXH, fw, fz) * G,
        scalar_formats=(FONE,),
        in_queue_formats=(fw, fz, fw, fz)[: 2 * r],
        out_queue_formats=(FXH,),
        L=L,
        VLMAX=u_pad,
        Kmax=u_pad,
        Nmax=c,
        NB=4,
        rs_max=0,
        word_bits=word_bits,
    )


def apply_programs(
    cfg: VecUnitConfig, u_pad: int, c: int, r: int, tiles: int
) -> Programs:
    """``x = sum_r conj(w_r) z_r`` on *tiles* tiles of ``u_pad`` streams x ``c`` REs: lanes are REs,
    elements are UEs (a pass writes a group once and one group a bank)."""
    G = cfg.groups_per_row(c)
    ex = [I.setvl(u_pad)]
    for _ in range(tiles):
        for g in range(G):
            b = 8 * g
            W0, Z0, Y, W1, Z1 = b, b + 1, b + 2, b + 3, b + 4
            ex += [I.vlse(W0, QW0, g, G).c, I.vlse(Z0, QZ0, g, G).c]
            ex += [I.vmul_vv(Y, W0, Z0, conj=True)]
            if r == 2:
                ex += [I.vlse(W1, QW1, g, G).c, I.vlse(Z1, QZ1, g, G).c]
                ex += [I.vmacc_vv(Y, W1, Z1, conj=True)]
            ex += [I.vsse(OXH, Y, g, G)]
        ins = (QW0, QZ0, QW1, QZ1)[: 2 * r]
        ex += [I.qpop(q) for q in ins] + [I.qpush(OXH)]
    load = [
        I.ld(q, PORT, u_pad, c)
        for _ in range(tiles)
        for q in (QW0, QZ0, QW1, QZ1)[: 2 * r]
    ]
    store = [I.st(PORT, OXH, t, u_pad, c) for t in range(tiles)]
    return Programs(load=load, execute=ex, store=store)


def check_apply(
    u_pad: int = 16, c: int = 8, L: int = 8, r: int = 2, seed: int = 7
) -> dict:
    rng = np.random.default_rng(seed)
    z = (
        rng.standard_normal((r, u_pad, c)) + 1j * rng.standard_normal((r, u_pad, c))
    ) / 3
    w = (
        rng.standard_normal((r, u_pad, c)) + 1j * rng.standard_normal((r, u_pad, c))
    ) / 3
    cfg = apply_config(u_pad, c, L, r)
    ins = []
    for k in range(r):
        ins += [_q(w[k], reg(16, 3)), _q(z[k], reg(16, 4))]
    res = run_job(cfg, *apply_programs(cfg, u_pad, c, r, 1), {PORT: ins})
    want = np.sum(np.conj(w) * z, axis=0)
    _r, re, im = res.outputs[PORT][0]
    g = _deq(re, im, FXH)
    return {
        "status": int(res.status),
        "rel_err": float(np.linalg.norm(g - want) / np.linalg.norm(want)),
    }
