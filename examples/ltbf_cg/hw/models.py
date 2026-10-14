"""models.py -- cost models of the LTBF engine: cycles and resources from the knobs.

``plans/mimo_cg/ltbf_cg_dse.md`` step 4.9 (AC7).  Fitted on the pre-registered fit builds only
(``hw/prereg/fit.json``), scored once on the held-out builds; the frozen parameters go to
``hw/models.json`` (local).

**Cycles** are structural.  The engine's loops are II = 1 and their trip counts are the runtime
sizes, so a job costs

    load:   [load_h] n cols + n m + ceil(m/C) (n + a_rz)       (+ explicit Q: ceil(n/R) ceil(n/C) (cols + a_Q))
    iter:   ceil(m/C) [ ceil(cols/R) (n + a_T) + ceil(n/R) (cols + a_U)          (factored)
                         | ceil(n/R) (n + a_S)                                     (explicit)
                         + 2 n + a_v + C (d_alpha + d_beta) + n ]
    store:  n m

with per-tile overheads ``a_*`` and divider latencies ``d_*`` read from a build's csynth loop table,
or -- for a design not built -- predicted by small linear models in the knobs (fitted on the fit
builds).  The measured quantity a held-out build is scored on is Vitis's own per-iteration latency
at the build's maximum sizes (``ITER`` / its trip count).  RTL runs (XSI) anchor csynth's count:
it is a fixed few cycles pessimistic per product tile (:func:`job_cycles_rtl`).

**Resources**: DSP from multiplier counts (the R x C tiles' 4 RC complex-multiply products per thin
product, 14 per vector lane) times DSPs per multiply by operand widths; BRAM from the banked arrays'
bits; LUT and FF linear in structural features.  All linear least squares with an intercept.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from examples.ltbf_cg.hw import engine as eg
from examples.ltbf_cg.hw import space as sp

REPORTS = eg.HERE / "reports"
MODELS = eg.HERE / "models.json"


# --- the structural cycle count ----------------------------------------------------------------


@dataclass(frozen=True)
class Overheads:
    """A build's per-tile overheads and divider latencies (cycles)."""

    a_T: float
    a_U: float
    a_S: float
    a_Q: float
    a_rz: float
    a_v: float
    d_alpha: float
    d_beta: float


def overheads_from_loops(k: sp.Knobs, loops: dict) -> Overheads:
    """Read a build's overheads off its csynth loop table (at its maximum sizes)."""

    def per(name: str) -> float:
        v = loops.get(name)
        return v["latency"] / v["trip_max"] if v and v["trip_max"] else float("nan")

    n, c = k.nmax, k.cmax
    div_a = per("PS_DIV") if "PS_DIV" in loops else float("nan")
    div_b = per("BETA_DIV") if "BETA_DIV" in loops else float("nan")
    ps_tile = per("PS_T")
    xr_tile = per("XR_T")
    a_v = (ps_tile - n - k.C * div_a) + (xr_tile - n - k.C * div_b)
    return Overheads(
        a_T=per("T_COLS") - n if not k.explicit else float("nan"),
        a_U=per("U_COLS") - c if not k.explicit else float("nan"),
        a_S=per("S_COLS") - n if k.explicit else float("nan"),
        a_Q=per("Q_COLS") - c if k.explicit else float("nan"),
        a_rz=per("RZ0_T") - n,
        a_v=a_v,
        d_alpha=div_a,
        d_beta=div_b,
    )


def job_cycles(
    k: sp.Knobs, o: Overheads, n: int, cols: int, m: int, nit: int, load_h: bool
) -> float:
    """One firing's cycles (see the module docstring)."""
    mt = math.ceil(m / k.C)
    cyc = n * m + mt * (n + o.a_rz) + n * m  # B in, rz0, X out
    if load_h:
        cyc += n * cols
        if k.explicit:
            cyc += math.ceil(n / k.R) * math.ceil(n / k.C) * (cols + o.a_Q)
    if k.explicit:
        prod = math.ceil(n / k.R) * (n + o.a_S)
    else:
        prod = math.ceil(cols / k.R) * (n + o.a_T) + math.ceil(n / k.R) * (cols + o.a_U)
    vec = 2 * n + o.a_v + k.C * (o.d_alpha + o.d_beta) + n
    return cyc + nit * mt * (prod + vec)


def product_tiles(
    k: sp.Knobs, n: int, cols: int, m: int, nit: int, load_h: bool
) -> int:
    """The R x C output tiles one firing computes (explicit Q formation included)."""
    mt = math.ceil(m / k.C)
    if k.explicit:
        q = math.ceil(n / k.R) * math.ceil(n / k.C) if load_h else 0
        return q + nit * mt * math.ceil(n / k.R)
    return nit * mt * (math.ceil(cols / k.R) + math.ceil(n / k.R))


def job_cycles_rtl(
    k: sp.Knobs,
    o: Overheads,
    n: int,
    cols: int,
    m: int,
    nit: int,
    load_h: bool,
    delta: float,
) -> float:
    """:func:`job_cycles` as the RTL runs it: csynth's count is ``delta`` cycles pessimistic per
    product tile (fitted on the XSI anchors, :mod:`examples.ltbf_cg.hw.rtl_anchor`)."""
    return job_cycles(k, o, n, cols, m, nit, load_h) - delta * product_tiles(
        k, n, cols, m, nit, load_h
    )


def iter_cycles_at_max(k: sp.Knobs, o: Overheads) -> float:
    """Per-iteration cycles at the build's maximum sizes: what csynth's ``ITER`` reports."""
    return job_cycles(k, o, k.nmax, k.cmax, k.mmax, 1, False) - job_cycles(
        k, o, k.nmax, k.cmax, k.mmax, 0, False
    )


def measured_iter(rec: dict) -> float:
    it = rec["loops"].get("ITER")
    return it["latency"] / it["trip_max"]


# --- features -------------------------------------------------------------------------------------


def _dsp_per_mult(wa: int, wb: int) -> int:
    """DSP48E2s a signed wa x wb product takes: one for 27 x 18, more as the operands grow."""
    a, b = max(wa, wb), min(wa, wb)
    if b <= 18 and a <= 27:
        return 1
    return math.ceil(a / 26) * math.ceil(b / 17)


def features(k: sp.Knobs) -> dict[str, float]:
    f = eg.hw_formats(k.W)
    c = f.cg
    rc = k.R * k.C
    if k.explicit:
        tile_dsp = 4 * rc * (_dsp_per_mult(f.H.W, f.H.W) + _dsp_per_mult(f.Q.W, c.P.W))
        tile_bits = 4 * rc * (f.H.W * f.H.W + f.Q.W * c.P.W)
    else:
        tile_dsp = 4 * rc * (_dsp_per_mult(f.H.W, c.P.W) + _dsp_per_mult(f.H.W, f.T.W))
        tile_bits = 4 * rc * (f.H.W * c.P.W + f.H.W * f.T.W)
    lane_dsp = k.C * (
        2 * _dsp_per_mult(c.P.W, c.S.W) + 4 * _dsp_per_mult(c.P.W, c.alpha.W)
        + 4 * _dsp_per_mult(c.R.W, c.R.W) + 2 * _dsp_per_mult(c.P.W, c.beta.W)
        + 2 * _dsp_per_mult(c.S.W, c.alpha.W)
    )  # fmt: skip
    # storage bits: H (2 x nmax x cmax x W_H), state P R X S (2 x nmax x mmax each), T, Q
    h_bits = 2 * k.nmax * k.cmax * f.H.W
    st_bits = 2 * k.nmax * k.mmax * (c.P.W + c.R.W + c.X.W + c.S.W)
    t_bits = 0 if k.explicit else 2 * k.cmax * k.mmax * f.T.W
    q_bits = 2 * k.nmax * k.nmax * f.Q.W if k.explicit else 0
    acc = eg.type_map(f, k.nmax, k.cmax)
    acc_w = acc["acc_qp_t"].W if k.explicit else acc["acc_hp_t"].W
    div_w = acc["rzw_t"].W + c.ps.W
    return {
        "tile_dsp": float(tile_dsp), "lane_dsp": float(lane_dsp),
        "tile_bits": float(tile_bits), "acc_bits": float(2 * 2 * rc * acc_w),
        "lane_bits": float(k.C * (c.P.W + c.S.W + c.R.W + c.X.W)),
        "div_bits2": float(div_w * div_w),
        "h_banks": float(k.R * k.R), "st_banks": float(k.C),
        "h_bits": float(h_bits), "st_bits": float(st_bits), "t_bits": float(t_bits),
        "q_bits": float(q_bits), "explicit": float(k.explicit), "W": float(k.W),
        "R": float(k.R), "C": float(k.C), "rc": float(rc),
    }  # fmt: skip


#: Each resource's model: the features it is linear in.
RESOURCE_TERMS = {
    "DSP": ("tile_dsp", "lane_dsp"),
    "BRAM_18K": ("h_bits", "st_bits", "t_bits", "q_bits", "h_banks", "st_banks"),
    "LUT": (
        "tile_bits",
        "acc_bits",
        "lane_bits",
        "div_bits2",
        "h_banks",
        "st_banks",
        "explicit",
    ),
    "FF": (
        "tile_bits",
        "acc_bits",
        "lane_bits",
        "div_bits2",
        "h_banks",
        "st_banks",
        "explicit",
    ),
}
#: The overheads' models (cycles): linear in the tile shape and the width.
OVERHEAD_TERMS = ("R", "C", "W", "explicit")


# --- fit and predict ------------------------------------------------------------------------------


def load_reports(role: str) -> list[tuple[sp.Knobs, dict]]:
    out = []
    for k in sp.load(role):
        p = REPORTS / f"{k.name}.json"
        if p.is_file():
            rec = json.loads(p.read_text())
            if rec.get("ok"):
                out.append((k, rec))
    return out


def _lstsq(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Least squares with an intercept, weighted by 1/y (relative error), as the A53 models."""
    a = np.column_stack([np.ones(len(x)), x])
    w = 1.0 / np.maximum(np.abs(y), 1.0)
    coef, *_ = np.linalg.lstsq(a * w[:, None], y * w, rcond=None)
    return coef


def fit(builds: list[tuple[sp.Knobs, dict]]) -> dict:
    params: dict = {"resources": {}, "overheads": {}}
    feats = [features(k) for k, _ in builds]
    for res, terms in RESOURCE_TERMS.items():
        x = np.array([[f[t] for t in terms] for f in feats])
        y = np.array([float(rec["resources"][res]) for _, rec in builds])
        params["resources"][res] = {"terms": list(terms), "coef": _lstsq(x, y).tolist()}
    ovs = [overheads_from_loops(k, rec["loops"]) for k, rec in builds]
    for name in Overheads.__dataclass_fields__:
        rows = [(f, getattr(o, name)) for f, o in zip(feats, ovs, strict=True)]
        rows = [(f, v) for f, v in rows if np.isfinite(v)]
        x = np.array([[f[t] for t in OVERHEAD_TERMS] for f, _ in rows])
        y = np.array([v for _, v in rows])
        params["overheads"][name] = {
            "terms": list(OVERHEAD_TERMS),
            "coef": _lstsq(x, y).tolist(),
        }
    return params


def _apply(p: dict, f: dict) -> float:
    c = p["coef"]
    return float(c[0] + sum(ci * f[t] for ci, t in zip(c[1:], p["terms"], strict=True)))


def predict_resources(params: dict, k: sp.Knobs) -> dict[str, float]:
    f = features(k)
    return {res: max(0.0, _apply(p, f)) for res, p in params["resources"].items()}


def predict_overheads(params: dict, k: sp.Knobs) -> Overheads:
    f = features(k)
    return Overheads(**{n: _apply(p, f) for n, p in params["overheads"].items()})


def save(params: dict) -> Path:
    MODELS.write_text(json.dumps(params, indent=1))
    return MODELS


def load() -> dict:
    return json.loads(MODELS.read_text())
