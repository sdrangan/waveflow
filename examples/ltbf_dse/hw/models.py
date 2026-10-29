"""models.py -- the DSE's resource and per-job cycle models (plan step 5.5).

``plans/ltbf_dse/ltbf_dse.md`` step 5.5, AC11.  Fitted on the fit set's builds (``hw/reports/``, the
queue's harvest), frozen (``hw/prereg/models.json``, its SHA-256 in §15) before the held-out builds
are read, then scored on them.

* **Resources** (LUT, FF, DSP, BRAM18, URAM) of each part: least squares, linear in a few terms of
  its knobs (:data:`RESOURCE_BASIS`).
* **Per-job cycles** of each part, from its device gate's XSI run (the C++ host's log of each job
  in and each ``Resp`` out): a job's service is its ``Resp`` less the later of its own write and the
  previous ``Resp`` (the engine runs one job at a time); the Ingest DMA's, a symbol's span of
  ``qsym`` writes (:func:`gate_symbols`), linear in its input words and the tiles it writes.  An
  engine job's is linear in the words it moves over the Shared Mem port and its tiles
  (:func:`sys_job_terms`, :func:`vec_job_terms`), so the port's read
  cost -- two cycles a word as built -- is a fitted coefficient, and a one-word-a-cycle leaf a
  what-if on it.
* **Shared Mem** as built (user, 2026-10-29); sizes past the on-chip budget are priced with
  ``DDR4-2400 x64 at 70%`` (:data:`DDR_WORDS_PER_CYCLE`), as ``paper/cg_dse``, and flagged
  *model-only*.

    python -m examples.ltbf_dse.hw.models fit      # fit on the fit set, freeze, print the hash
    python -m examples.ltbf_dse.hw.models score    # the held-out scores (once frozen)
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

from examples.ltbf_dse.hw import space as sp

HERE = Path(__file__).resolve().parent
REPORTS = HERE / "reports"
MODELS = sp.PREREG / "models.json"
COUNTERS = ("LUT", "FF", "DSP", "BRAM_18K", "URAM")
#: The device gates' bus addresses (``devsys``): the device's Job FIFO, and the Ingest DMA's ``qsym``.
JOB_FIFO, QSYM = 0x8000_0000, 0x8000_2000
#: DDR4-2400 x64 at 70%: 2400 MT/s x 8 bytes x 0.7 over a 250 MHz host clock, in 64-bit words.
DDR_WORDS_PER_CYCLE = 2400e6 * 8 * 0.7 / 250e6 / 8


def resource_terms(p: sp.Part) -> dict[str, float]:
    """A part's resource basis: its knobs' terms (the array's PEs and their width for the systolic
    wrapper, the lanes and width for the vector unit, the buffer for the DMA)."""
    w = p.word_bits / 64
    if p.kind == "sys":
        pe = p.R * p.C
        return {"1": 1.0, "pe": pe, "pe_w": pe * p.W, "panels": math.log2(max(p.n_rx, 176) / p.c),
                "c": p.c, "Mmax": p.Mmax, "word": w}  # fmt: skip
    if p.kind == "vec":  # the register files' BRAM18 as the framework's resource model counts them
        from waveflow.vecunit.calib import config_features

        return {"1": 1.0, "L": p.L, "L_w": p.L * p.W, "bram18": config_features(_system(p).vu)["bram18"],
                "c": p.c}  # fmt: skip
    return {"1": 1.0, "ant": p.n_rx / 64, "c": p.c, "J": p.J, "word": w}


@functools.cache
def _system(p: sp.Part):
    """The part's ``LtbfSystem`` (its dimensions, formats and programs), built once."""
    from examples.ltbf_dse.hw import queue as hq
    from examples.ltbf_dse.hw import system as sy

    return sy.LtbfSystem(sy.inputs(hq.params(p)))


def _lstsq(rows: list[dict], y: list[float]) -> dict[str, float]:
    keys = list(rows[0])
    A = np.array([[r[k] for k in keys] for r in rows], float)
    coef, *_ = np.linalg.lstsq(A, np.asarray(y, float), rcond=None)
    return dict(zip(keys, (float(c) for c in coef), strict=True))


def _predict(coef: dict[str, float], terms: dict[str, float]) -> float:
    return sum(coef[k] * terms[k] for k in coef)


def load_reports(role: str) -> list[dict]:
    """The queue's reports for the parts of *role* that built (csynth's resources present)."""
    out = []
    for p in sp.load(role):
        f = REPORTS / f"{p.name}.json"
        if f.is_file():
            r = json.loads(f.read_text())
            if "resources" in r:
                out.append(r)
    return out


# -- per-job cycles from a device gate's host log ---------------------------------------------------


def gate_jobs(rep: dict) -> list[float]:
    """Each job's service in RTL cycles from the gate's host log: ``Resp`` out less the later of its
    own write into the Job FIFO and the previous ``Resp``."""
    ops = rep.get("ops") or []
    qjob = [o for o in ops if o[0] == "W" and o[1] == JOB_FIFO and o[2] > 1]
    resp = [o for o in ops if o[0] == "R" and o[1] >= 0x8000_0000 and o[2] == 1]
    out, prev = [], 0.0
    for k, r in enumerate(resp):
        w = qjob[k][4] if k < len(qjob) else 0
        out.append(float(r[3] - max(w, prev)))
        prev = r[3]
    return out


def sys_job_terms(p: sp.Part) -> list[dict[str, float]]:
    """The systolic gate's jobs at *p* (``devsys.sys_system``): per job its tiles and the words it
    moves (``A`` and ``B`` panels read, ``C`` tiles written)."""

    S = _system(p)
    d = S.d
    R, C, c = d.rows, d.cols, d.c
    shapes = [(C, R, c), (R, C, c), (C, R, c), (d.S, R, d.J * c), (R, C, R)][: 5 if d.n_rx <= 64 else 4]
    wa, wb, wc = S.w(d.Mmax, c), S.w(c, c), S.w(d.Mmax, c)
    out = []
    for m, k, n in shapes:
        tiles, P = (m // d.Mmax) * (n // c), k // c
        out.append({"1": 1.0, "rd": tiles * P * (wa + wb), "wr": tiles * wc, "tiles": tiles})
    return out


def vec_job_terms(p: sp.Part) -> list[dict[str, float]]:
    """The vector gate's jobs at *p* (``devsys.vec_system``): a CG step, the last one, an equalizer
    chunk -- the words each loads and stores, and its execute cycles (``VecTiming`` on its program),
    the equalizer's apart (it streams its rounds, the CG step loads, runs, then stores)."""
    from examples.ltbf_dse.hw import programs as pg
    from waveflow.vecunit.unit import VecTiming, job_features

    S = _system(p)
    d, inp = S.d, S.inp
    vt = VecTiming().for_config(S.vu)
    mw = S.w(d.rows, d.c)
    wb = S.w(d.u_pad, d.c)
    progs = [pg.cg_step_programs(S.vu, d.rows, d.c, inp.sh),
             pg.cg_step_programs(S.vu, d.rows, d.c, inp.sh, last_m=S.cfg.m_shift),
             pg.eq_programs(S.vu, d.u_pad, d.c, S.p.r, d.J, off=S.off)]  # fmt: skip
    moves = [(4 * mw, 3 * mw), (4 * mw, 4 * mw), (d.J * 2 * S.p.r * wb, d.J * wb)]
    out = []
    for pr, (ld, st) in zip(progs, moves, strict=True):
        prog = sum(len(x) for x in pr)
        ex = vt.predict(job_features(pr.execute, S.vu))
        eq = pr is progs[2]  # the equalizer streams its rounds: its loads overlap its execution
        out.append({"1": 1.0, "rd": ld + prog, "wr": st, "exec": 0.0 if eq else ex,
                    "exec_eq": ex if eq else 0.0, "eq": float(eq)})  # fmt: skip
    return out


def gate_symbols(rep: dict) -> list[float]:
    """The Ingest DMA's time a symbol in RTL cycles from its gate's host log: the host writes each
    symbol's antennas into ``qsym`` back to back, so a symbol's span is the DMA's rate."""
    q = [o for o in rep.get("ops") or [] if o[0] == "W" and o[1] == QSYM and o[2] > 1]
    per = len(q) // 2  # the gate's two symbols
    return [float(q[(s + 1) * per - 1][4] - q[s * per][3]) for s in range(2)] if per else []


def ing_symbol_terms(p: sp.Part) -> list[dict[str, float]]:
    """A symbol's work for the Ingest DMA at *p*: the words it takes in (an antenna's burst and its
    header) and the tiles it writes (``c`` antennas a group, ``J`` tiles a chunk)."""

    S = _system(p)
    d = S.d
    words = d.n_rx * (d.words(1, d.n_sc) + 1)
    tiles = (d.rows // d.c) * d.n_chunks * d.J
    return [{"1": 1.0, "words": words, "tiles": tiles}] * 2


def job_rows(kind: str, reports: list[dict]) -> tuple[list[dict], list[float]]:
    terms_of = {"sys": sys_job_terms, "vec": vec_job_terms, "ing": ing_symbol_terms}[kind]
    X, y = [], []
    for r in reports:
        if r["part"]["kind"] != kind or not r.get("ops"):
            continue
        cyc = gate_symbols(r) if kind == "ing" else gate_jobs(r)
        terms = terms_of(sp.Part(**r["part"]))
        n = min(len(cyc), len(terms))
        X += terms[:n]
        y += cyc[:n]
    return X, y


# -- fit, freeze, score -------------------------------------------------------------------------------


def fit() -> dict:
    reps = load_reports("fit")
    model: dict = {"resources": {}, "cycles": {}, "fit_parts": sorted(r["name"] for r in reps)}
    for kind in sp.KINDS:
        rk = [r for r in reps if r["part"]["kind"] == kind]
        if not rk:
            continue
        terms = [resource_terms(sp.Part(**r["part"])) for r in rk]
        model["resources"][kind] = {c: _lstsq(terms, [r["resources"][c] for r in rk]) for c in COUNTERS}
    for kind in sp.KINDS:
        X, y = job_rows(kind, reps)
        if X:
            model["cycles"][kind] = _lstsq(X, y)
    return model


def freeze(model: dict) -> str:
    text = json.dumps(model, indent=0, sort_keys=True)
    MODELS.write_bytes(text.encode())
    return hashlib.sha256(text.encode()).hexdigest()


def predict_resources(model: dict, p: sp.Part) -> dict[str, float]:
    t = resource_terms(p)
    return {c: max(0.0, _predict(model["resources"][p.kind][c], t)) for c in COUNTERS}


def score(model: dict, role: str = "heldout") -> dict:
    """Median and worst relative error per counter over *role*'s built parts, and per-job cycles."""
    reps = load_reports(role)
    out: dict = {"parts": len(reps), "resources": {}, "cycles": {}}
    for c in COUNTERS:
        errs = []
        for r in reps:
            meas = r["resources"][c]
            if meas == 0:
                continue
            errs.append(abs(predict_resources(model, sp.Part(**r["part"]))[c] / meas - 1))
        if errs:
            out["resources"][c] = {"median": float(np.median(errs)), "worst": float(max(errs)), "n": len(errs)}
    for kind, coef in model["cycles"].items():
        X, y = job_rows(kind, reps)
        errs = [abs(_predict(coef, x) / v - 1) for x, v in zip(X, y, strict=True) if v > 0]
        if errs:
            out["cycles"][kind] = {"median": float(np.median(errs)), "worst": float(max(errs)), "n": len(errs)}
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fit"
    if cmd == "fit":
        m = fit()
        print("fit parts", len(m["fit_parts"]))
        print(json.dumps({k: m[k] for k in ("resources", "cycles")}, indent=1)[:3000])
        if "--freeze" in sys.argv:
            print("models sha256", freeze(m))
    else:
        print(json.dumps(score(json.loads(MODELS.read_text())), indent=1))
