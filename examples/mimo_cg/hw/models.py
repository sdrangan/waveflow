"""models.py — the calibrated resource and cycle models of the CG hardware.

Steps 5.4–5.6 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 5.0 decision record, §14).

One :class:`Models` object prices any configuration of the design space
(:mod:`examples.mimo_cg.hw.space`) with no toolchain: csynth LUT, FF, DSP and BRAM per module and
in total, and the cycles of a job.  It is fitted from the ``fit`` rows of the campaign tables and
from nothing else; a held-out row is never read by :func:`fit`.

Who is calibrated from what (gate 5.0 decision 3)
------------------------------------------------
* the **vector unit** from the ``CgVec`` rows and spans of ``CgVecUnit`` builds;
* the **matmul** from the ``CgMm`` rows and spans of ``CgMmUnit`` builds;
* the **glue** (framer, load, store, control, the two memory streams) and the detector's own
  **channels** (stream-of-blocks, FIFOs, bus adapters) from ``CgDetector`` builds.

Two kinds of term
-----------------
*Counted* terms have no fitted parameter: they follow from what the body contains and from how
the tool binds it on this device.

* **DSP.**  A multiply inside a multiply-add (an accumulate, a subtract, a pre-add) binds to a DSP
  at every width of the space.  A plain multiply binds to a DSP only from
  :data:`PLAIN_MULT_DSP_MIN_BITS` bits; below that it is built from LUTs (csynth reports 40 LUTs at
  8 bits, 62 at 10).  The bodies declare how many of each they have (:func:`vec_mults`,
  :func:`mm_mults`).
* **Block RAM.**  The state arrays of the two blocks follow the framework's rule
  (:func:`waveflow.calib.device_rules.bram_estimate`): one block per bank once a bank holds 1,024
  bits, LUT RAM below.  A stream-of-blocks channel is one simple-dual-port memory over all its
  banks, so it can use the 36-bit shape (:func:`sob_memory`) — unless a memory-side task touches two
  groups per cycle (fewer lanes than elements per word), which makes each bank a true-dual-port
  memory of its own.
* **Registers.**  The matmul holds ``A`` in ``2·K²·W`` flip-flops.

*Fitted* terms are linear regressions (:class:`~waveflow.calib.calib.LinCalibModel`) of what is
left, on terms read off each body's structure (:data:`TERMS`).

Cycles
------
A job of ``nit`` iterations takes ``T0 + nit·T_iter`` cycles between completions.  The detector's
two blocks wait for each other, so ``T_iter`` is the matmul's span plus the vector unit's span, and
``T0`` is the vector unit's start span plus a few cycles that depend on the memory word width.

``python -m examples.mimo_cg.hw.models --fit`` fits and saves the models.
"""

from __future__ import annotations

import argparse
import functools
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from examples.mimo_cg.hw import build as B
from examples.mimo_cg.hw.common import DEFAULT_N
from examples.mimo_cg.hw.space import HwConfig
from examples.mimo_cg.mimo_cg import read_table
from waveflow.calib.calib import LinCalibModel
from waveflow.calib.confidence import Confidence, ConfidenceLevel
from waveflow.calib.device_rules import bram_estimate, lutram_luts
from waveflow.calib.resource_model import ResourceModel

HERE = Path(__file__).resolve().parent
PAPER_DATA = HERE.parent / "paper_data"
#: The tracked platform library: the published module records and the fitted model file.
PLATFORM_DIR = HERE.parent / "calib" / "platforms" / "xczu48dr_250mhz"
MODEL_FILE = PLATFORM_DIR / "models" / "mimo_cg_hw.json"

N = DEFAULT_N
PART = B.PART
COUNTERS = ("lut", "ff", "dsp", "bram")
#: A plain signed multiply binds to a DSP from this operand width (measured on xczu48dr at 4 ns with
#: Vitis HLS 2024.1: in fabric at 8 and 10 bits, in a DSP at 12, 14 and 16).
PLAIN_MULT_DSP_MIN_BITS = 12
#: The shapes of one BRAM18 as a simple-dual-port memory, ``(depth, width)``.
SDP_SHAPES = ((512, 36), (1024, 18), (2048, 9), (4096, 4), (8192, 2), (16384, 1))
#: Below this many bits a stream-of-blocks memory is LUT RAM with an output register.
SOB_BRAM_MIN_BITS = 1024


def knobs(c) -> dict:
    """A configuration as a plain dict (an :class:`HwConfig` or an equivalent mapping)."""
    return (
        dict(c)
        if isinstance(c, dict)
        else {k: getattr(c, k) for k in HwConfig.__dataclass_fields__}
    )


# --- counted structure -----------------------------------------------------------------------


def plain_in_dsp(bits: int) -> bool:
    """Whether a plain ``bits × bits`` multiply is bound to a DSP."""
    return int(bits) >= PLAIN_MULT_DSP_MIN_BITS


def vec_mults(W: int) -> tuple[int, int]:
    """``(multiply-adds, plain multiplies)`` per lane of the vector unit.

    The body has 12 multiplies per lane: two in the start, two in pass 1, six in pass 2 and two in
    pass 3.  Five are in multiply-add patterns (the P·S accumulate, the two R − S·α updates, and the
    two sums of squares).  At W = 8 the tool does not form the two sum-of-squares patterns (measured),
    which leaves three.
    """
    mac = 3 if int(W) == 8 else 5
    return mac, 12 - mac


def mm_mults(cmul: int) -> tuple[int, int]:
    """``(multiply-adds, plain multiplies)`` per element of the systolic array: the 4-multiply form
    has two of each; all three multiplies of the 3-multiply form have a pre-adder."""
    return (3, 0) if int(cmul) == 3 else (2, 2)


def _dsp(mac: int, plain: int, W: int) -> tuple[int, int]:
    """``(DSPs, multiplies built from LUTs)`` for one replicated unit."""
    return (mac + plain, 0) if plain_in_dsp(W) else (mac, plain)


def _array(n_arrays: int, banks: int, depth: int, bits: int) -> dict:
    """Block RAMs of ``n_arrays`` partitioned arrays, or the LUTs they cost as LUT RAM."""
    est = bram_estimate(banks, depth, bits, PART)
    if est.blocks:
        return {"bram": n_arrays * est.blocks, "lut": 0}
    return {"bram": 0, "lut": n_arrays * lutram_luts(banks, depth, bits, PART)}


def vec_counted(c) -> dict:
    """The vector unit's counted terms: DSPs, the six state arrays, and its fabric multiplies."""
    k = knobs(c)
    dsp, fab = _dsp(*vec_mults(k["W"]), k["W"])
    mem = _array(6, k["L"], k["K"] * N // k["L"], k["W"])  # X, R, P, each re and im
    return {
        "dsp": k["L"] * dsp,
        "bram": mem["bram"],
        "lut": mem["lut"],
        "ff": 0,
        "n_fab": k["L"] * fab,
    }


def mm_counted(c) -> dict:
    """The matmul's counted terms: DSPs, the two P arrays, the A registers, its fabric multiplies."""
    k = knobs(c)
    dsp, fab = _dsp(*mm_mults(k["cmul"]), k["W"])
    pes = k["R"] * k["C"]
    mem = _array(
        2, k["C"], k["K"] * N // k["C"], k["W"]
    )  # P re and im, partitioned over C
    return {
        "dsp": pes * dsp,
        "bram": mem["bram"],
        "lut": mem["lut"],
        "ff": 2 * k["K"] ** 2 * k["W"],
        "n_fab": pes * fab,
    }


def sob_memory(words: int, bits: int, banks: int, dual: bool = False) -> dict:
    """One stream-of-blocks channel: ``banks`` blocks of ``words`` elements of ``bits`` bits.

    Small channels are LUT RAM (one LUT per 64 bits) with an output register.  Otherwise the banks
    form one simple-dual-port memory in its cheapest BRAM18 shape; with ``dual`` (two accesses per
    cycle) each bank is a true-dual-port memory, priced by the framework's per-bank rule.
    """
    total = words * bits * banks
    if total < SOB_BRAM_MIN_BITS:
        return {"bram": 0, "lut": total // 64, "ff": bits}
    if dual:
        return {
            "bram": bram_estimate(banks, words, bits, PART).blocks,
            "lut": 0,
            "ff": 0,
        }
    blocks = min(
        math.ceil(bits / w) * math.ceil(words * banks / d) for d, w in SDP_SHAPES
    )
    return {"bram": blocks, "lut": 0, "ff": 0}


def channels(c, top: str = "det") -> list[tuple]:
    """The stream-of-blocks channels of a top, as ``(name, words, bits, banks, dual)``."""
    k = knobs(c)
    lanes = (k["K"] * N // k["L"], 2 * k["W"] * k["L"], k["sob_depth"])
    a_rows = (k["K"], 2 * k["W"] * k["K"], k["sob_depth"])
    dual = k["L"] < k["mem_dw"] // 32  # a memory-side task moves two groups per cycle
    if top == "vec":
        return [(n, *lanes, dual) for n in ("b_blk", "s_blk", "p_blk", "x_blk")]
    if top == "mm":
        return [
            ("a_blk", *a_rows, False),
            ("p_blk", *lanes, dual),
            ("s_blk", *lanes, dual),
        ]
    return [
        ("a_blk", *a_rows, False),
        ("b_blk", *lanes, dual),
        ("p_blk", *lanes, False),
        ("s_blk", *lanes, False),
        ("x_blk", *lanes, dual),
    ]


# --- regression terms ------------------------------------------------------------------------


def _vec_terms(k: dict) -> dict:
    L, W, g = k["L"], k["W"], k["g_s"]
    fab = vec_counted(k)["n_fab"]
    return {
        "l": L,
        "lw": L * W,
        "lg": L * g,
        "w": W,
        "g": g,
        "fab": fab * W * W,
        "l2": L * L,
        "l2w": L * L * W,
        "l2wg": L * L * (W + g),
    }


def _mm_terms(k: dict) -> dict:
    K, R, C, W, L = k["K"], k["R"], k["C"], k["W"], k["L"]
    p, m3, out = (
        R * C,
        float(k["cmul"] == 3),
        float(R > 1),
    )  # one row: no output multiplexer
    return {
        "p": p,
        "pw": p * W,
        "p3": p * m3,
        "pw3": p * W * m3,
        "fab": mm_counted(k)["n_fab"] * W * W,
        "k": K,
        "k2": K * K,
        "kw": K * W,
        "c": C,
        "w": W,
        "out": out,
        "out_w": out * W,
        "out_lgl": out * math.log2(L),
        "out_wide": out * float(C >= 16) * R * W,
    }


def _lane_terms(lanes: int, W: int, mem_dw: int) -> dict:
    """One matrix (de)serializer of ``lanes`` lanes: nothing to route when a word holds at least a
    whole group, otherwise a routing network that grows as lanes·W·log2(lanes), with its own
    coefficients per memory word width."""
    lw = mem_dw // 32
    on = float(lanes > lw)
    base, deep = on * lanes * W, on * lanes * W * math.log2(lanes)
    wide = float(lw == 2)
    return {"lw": base, "lwlg": deep, "lw_64": base * wide, "lwlg_64": deep * wide}


def _vec_iter_terms(k: dict) -> dict:
    ng = N // k["L"]
    return {
        "ngk": ng * k["K"],
        "ng": ng,
        "ngw": ng * k["W"],
        "ngg": ng * k["g_s"],
        "w": k["W"],
        "g": k["g_s"],
    }


def _vec_init_terms(k: dict) -> dict:
    return {"ngk": (N // k["L"]) * k["K"], "w": k["W"]}


def _mm_iter_terms(k: dict) -> dict:
    K, R, C, L = k["K"], k["R"], k["C"], k["L"]
    tiles, groups = (K // R) * (N // C), C // L
    flat = R * groups == 1  # the tile loops and the sweep become one pipelined loop
    return {
        "knl": K * N // L,
        "ts": tiles * (K + R + C - 2),
        "t": tiles * (not flat),
        "t3": tiles * (k["cmul"] == 3),
        "t_out1": tiles * ((R == 1) != (groups == 1)),
        "kr": (K // R) * (not flat),
        "kr1": float(K // R == 1),
        "nc1": float(N // C == 1),
    }


def _small_terms(k: dict) -> dict:
    return {"lgk": math.log2(k["K"]), "m32": float(k["mem_dw"] == 32)}


#: The regressions: ``name -> (the terms' function, the terms used)``.
TERMS = {
    "CgVec.lut": (_vec_terms, ("l", "lw", "lg", "w", "g", "fab", "l2", "l2w")),
    "CgVec.ff": (_vec_terms, ("l", "lw", "lg", "w", "g", "fab", "l2wg")),
    "CgMm.lut": (
        _mm_terms,
        ("p", "pw", "p3", "pw3", "fab", "k", "k2", "c", "out", "out_w", "out_lgl"),
    ),
    "CgMm.ff": (_mm_terms, ("p", "pw", "p3", "pw3", "k", "w", "kw", "out_wide")),
    "vec.iter": (_vec_iter_terms, ("ngk", "ng", "ngw", "ngg", "w", "g")),
    "vec.init": (_vec_init_terms, ("ngk", "w")),
    "mm.iter": (_mm_iter_terms, ("knl", "ts", "t", "t3", "t_out1", "kr", "kr1", "nc1")),
}
for _name in ("load.lut", "load.ff", "store.lut", "store.ff"):
    TERMS[_name] = (
        None,
        ("lw", "lwlg", "lw_64", "lwlg_64"),
    )  # rows carry their lane terms
for _cls in ("CgCmdRx", "CgCtrl"):
    for _ctr in ("lut", "ff"):
        TERMS[f"{_cls}.{_ctr}"] = (_small_terms, ("lgk", "m32"))


def _lin(name: str) -> LinCalibModel:
    _fn, names = TERMS[name]
    return LinCalibModel(
        basis=list(names), target="y", name=name, coeff_names=list(names)
    )


# --- the fitted object -----------------------------------------------------------------------


@dataclass
class Models:
    """The fitted models: regression coefficients, measured tables, and how they were fitted."""

    #: ``{regression name: {term: coefficient, …, "intercept": c}}``
    coef: dict = field(default_factory=dict)
    #: Measured, single-valued rows: ``{"<what>|<key>": {counter: value}}``.
    table: dict = field(default_factory=dict)
    #: Per regression: rows used, largest in-sample error, leave-one-out errors.
    report: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    # -- artifact ---------------------------------------------------------------------------
    def save(self, path: Path = MODEL_FILE) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = {
            "meta": self.meta,
            "coef": self.coef,
            "table": self.table,
            "report": self.report,
        }
        path.write_text(
            json.dumps(blob, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: Path = MODEL_FILE) -> Models:
        blob = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            coef=blob["coef"],
            table=blob["table"],
            report=blob["report"],
            meta=blob["meta"],
        )

    # -- evaluation -------------------------------------------------------------------------
    def _reg(self, name: str, terms: dict) -> float:
        co = self.coef[name]
        return co["intercept"] + sum(co[t] * terms[t] for t in TERMS[name][1])

    def _row(self, what: str, *key) -> dict:
        return self.table[f"{what}|{'/'.join(str(x) for x in key)}"]

    def _lanes(self, name: str, lanes: int, W: int, mem_dw: int) -> dict:
        t = _lane_terms(lanes, W, mem_dw)
        return {ctr: self._reg(f"{name}.{ctr}", t) for ctr in ("lut", "ff")}

    def module(self, cls: str, c) -> dict:
        """The csynth row of one module of the detector at configuration ``c``."""
        k = knobs(c)
        if cls in ("CgVec", "CgMm"):
            fn, counted = (
                (_vec_terms, vec_counted) if cls == "CgVec" else (_mm_terms, mm_counted)
            )
            cnt, terms = counted(k), fn(k)
            return {
                "lut": self._reg(f"{cls}.lut", terms) + cnt["lut"],
                "ff": self._reg(f"{cls}.ff", terms) + cnt["ff"],
                "dsp": cnt["dsp"],
                "bram": cnt["bram"],
            }
        if cls in ("MemRStream", "MemWStream"):
            return dict(self._row(cls, k["mem_dw"]), dsp=0, bram=0)
        if cls in ("CgCmdRx", "CgCtrl"):
            t = _small_terms(k)
            return {
                "lut": self._reg(f"{cls}.lut", t),
                "ff": self._reg(f"{cls}.ff", t),
                "dsp": 0,
                "bram": 0,
            }
        own = self._row(f"{cls}.own", k["mem_dw"])
        if cls == "CgLoad":  # the A loader (K lanes) and the B loader (L lanes)
            parts = [
                self._lanes("load", lanes, k["W"], k["mem_dw"])
                for lanes in (k["K"], k["L"])
            ]
        elif cls == "CgStore":
            parts = [self._lanes("store", k["L"], k["W"], k["mem_dw"])]
        else:
            raise KeyError(cls)
        return {
            "lut": own["lut"] + sum(p["lut"] for p in parts),
            "ff": own["ff"] + sum(p["ff"] for p in parts),
            "dsp": 0,
            "bram": 0,
        }

    def integration(self, c) -> dict:
        """What the detector's top holds besides its modules: channels, FIFOs and adapters."""
        k = knobs(c)
        out = {ctr: 0 for ctr in COUNTERS}
        for _name, words, bits, banks, dual in channels(k):
            mem = sob_memory(words, bits, banks, dual)
            for ctr in ("lut", "ff", "bram"):
                out[ctr] += mem[ctr]
        for row in (
            self._row("adapters", k["mem_dw"]),
            self._row("fifos", k["cmd_depth"]),
        ):
            for ctr in ("lut", "ff", "bram"):
                out[ctr] += row.get(ctr, 0)
        return out

    def resources(self, c) -> dict:
        """The detector at ``c``: ``{"total": counters, "modules": {name: counters}}``.

        Module rows are whole numbers, as in a report, and the total is their sum.
        """
        mods = {cls: self.module(cls, c) for cls in DETECTOR_MODULES}
        mods["integration"] = self.integration(c)
        mods = {
            name: {ctr: round(row[ctr]) for ctr in COUNTERS}
            for name, row in mods.items()
        }
        total = {ctr: sum(row[ctr] for row in mods.values()) for ctr in COUNTERS}
        return {"total": total, "modules": mods}

    def spans(self, c) -> dict:
        """The block spans at ``c``, in cycles: ``mm.iter``, ``vec.iter`` and ``vec.init``."""
        k = knobs(c)
        return {
            name: self._reg(name, TERMS[name][0](k))
            for name in ("mm.iter", "vec.iter", "vec.init")
        }

    def cycles(self, c) -> dict:
        """The detector's job timing at ``c``: ``t_iter`` and ``t0``, in cycles."""
        k, s = knobs(c), self.spans(c)
        handoff = self._row("handoff", "loop")["cycles"]
        return {
            "t_iter": s["mm.iter"] + s["vec.iter"] + handoff,
            "t0": s["vec.init"] + self._row("t0_extra", k["mem_dw"])["cycles"],
        }

    def job_cycles(self, c, nit: int) -> float:
        """Cycles between the completions of consecutive ``nit``-iteration jobs."""
        t = self.cycles(c)
        return t["t0"] + nit * t["t_iter"]


@functools.cache
def calibrated() -> Models | None:
    """The committed models, or ``None`` when the model file is not there."""
    return Models.load() if MODEL_FILE.is_file() else None


def block_span(kind: str, fmt: int, **knob) -> float | None:
    """The calibrated span ``kind`` (``vec.init``, ``vec.iter`` or ``mm.iter``) in cycles, for a
    block built with format id ``fmt`` and the given knobs; ``None`` when there is no model for it
    (no model file, or a format outside the space, such as the stress set)."""
    from examples.mimo_cg.hw.common import ALL_FORMAT_NAMES, format_wg

    models = calibrated()
    if models is None or not ALL_FORMAT_NAMES[int(fmt)].startswith("W"):
        return None
    W, g = format_wg(fmt)
    k = knobs(HwConfig()) | {"W": W, "g_s": g} | {n: int(v) for n, v in knob.items()}
    return models._reg(kind, TERMS[kind][0](k))


#: The detector's modules, in graph order.
DETECTOR_MODULES = (
    "CgCmdRx",
    "MemRStream",
    "CgLoad",
    "CgCtrl",
    "CgVec",
    "CgMm",
    "CgStore",
    "MemWStream",
)


# --- the framework's composition ----------------------------------------------------------------


@dataclass
class CgResourceModel(ResourceModel):
    """One module's own cost, as a framework :class:`~waveflow.calib.resource_model.ResourceModel`.

    A thin adapter: it reads the knobs off the elaborated component and asks :class:`Models`, so
    :func:`waveflow.calib.resource_model.compose` over a ``CgDetector`` walks the same numbers as
    :meth:`Models.resources`.
    """

    models: Models | None = None
    cls: str = ""

    def declared_counters(self) -> tuple:
        return COUNTERS

    def get_params(self, comp, **runtime) -> dict:
        from examples.mimo_cg.hw.common import format_wg
        from waveflow.calib.module_key import identify_instance

        p = {
            k: int(v)
            for k, v in identify_instance(comp, require_bound=False).params.items()
        }
        out = {"mem_dw": p.get("mem_dwidth", B.DEFAULT_MEM_DW)}
        if "fmt" in p:
            out["W"], out["g_s"] = format_wg(p["fmt"])
        out |= {
            k: p[k] for k in ("K", "L", "C", "cmul", "sob_depth", "cmd_depth") if k in p
        }
        if "R" in p:
            out["R"] = p["R"] or p["K"]  # 0 means R = K
        return out

    def predict_feat(self, row) -> dict:
        full = knobs(HwConfig()) | dict(row)
        res = (
            self.models.integration(full)
            if self.cls == "integration"
            else self.models.module(self.cls, full)
        )
        return {ctr: round(res[ctr]) for ctr in COUNTERS}

    def confidence_feat(self, row) -> Confidence:
        return Confidence(
            level=ConfidenceLevel.INTERPOLATED,
            facts={
                "summary": "calibrated on the fit builds of paper_data/hw_modules.csv"
            },
        )


def model_for(models: Models):
    """``compose(top, model_for=model_for(models))``: each module's model, by its class."""

    def pick(comp):
        cls = type(comp).__name__
        if cls == "CgDetector":
            return CgResourceModel(name="cg_detector", models=models, cls="integration")
        if cls in DETECTOR_MODULES:
            return CgResourceModel(name=cls, models=models, cls=cls)
        return None

    return pick


# --- fitting ---------------------------------------------------------------------------------


def _fit_tables(data_dir: Path) -> tuple[dict, list[dict], list[dict]]:
    """The campaign tables, cut to the ``fit`` builds: ``(configs by build, module rows, cycles)``."""
    builds = {
        r["build"]: r
        for r in read_table(data_dir / "hw_builds.csv")
        if r["role"] == "fit"
    }
    cfg = {
        b: {k: int(r[k]) for k in HwConfig.__dataclass_fields__}
        for b, r in builds.items()
    }
    modules = [r for r in read_table(data_dir / "hw_modules.csv") if r["role"] == "fit"]
    cycles = [r for r in read_table(data_dir / "hw_cycles.csv") if r["role"] == "fit"]
    assert all(r["build"] in cfg for r in modules + cycles)
    return cfg, modules, cycles


def _regress(
    name: str, samples: list[tuple[dict, float]], *, scale: list[float] | None = None
) -> tuple[dict, dict]:
    """Fit regression ``name`` on ``(terms, target)`` samples; returns ``(coefficients, report)``.

    The report's errors are relative to ``scale`` (the full quantity, when the target had a counted
    part taken out), so they read as errors of what is predicted.
    """
    names = TERMS[name][1]
    df = pd.DataFrame(
        [{**{t: float(terms[t]) for t in names}, "y": float(y)} for terms, y in samples]
    )
    full = (
        [float(y) for _t, y in samples] if scale is None else [float(s) for s in scale]
    )
    model = _lin(name).fit(df)
    insample = [model.predict_feat(r) - r["y"] for r in df.to_dict("records")]
    loo = []
    for i in range(len(df)):
        held = df.iloc[i].to_dict()
        loo.append(_lin(name).fit(df.drop(index=i)).predict_feat(held) - held["y"])
    rel = [abs(e) / f for e, f in zip(loo, full, strict=True) if f]
    report = {
        "n": len(df),
        "terms": len(names) + 1,
        "max_abs_residual": max(abs(e) for e in insample),
        "loo_mape_pct": 100 * sum(rel) / len(rel),
        "loo_max_pct": 100 * max(rel),
    }
    co = model.coeffs
    return {**{t: co[t] for t in names}, "intercept": co["intercept"]}, report


def _single(rows: list[dict], what: str) -> dict:
    """The one value a measured table row takes; raises if the builds disagree."""
    seen = {tuple(sorted(r.items())) for r in rows}
    if len(seen) != 1:
        raise ValueError(
            f"{what}: {len(seen)} different values across the fit builds: {sorted(seen)[:3]}"
        )
    return dict(rows[0])


def fit(data_dir: Path = PAPER_DATA) -> Models:
    """Fit every model from the ``fit`` rows of the campaign tables in ``data_dir``."""
    cfg, modules, cycles = _fit_tables(data_dir)
    m = Models(
        meta={"part": PART, "period_ns": B.PERIOD_NS, "fit_builds": len(cfg), "N": N}
    )

    def rows(top: str, kind: str) -> list[dict]:
        return [r for r in modules if r["top"] == top and r["kind"] == kind]

    def val(r: dict, ctr: str) -> int:
        return int(r[ctr])

    # the two blocks, each from its own unit builds: the counted part comes out first
    for cls, top, terms, counted in (
        ("CgVec", "vec", _vec_terms, vec_counted),
        ("CgMm", "mm", _mm_terms, mm_counted),
    ):
        own = [r for r in rows(top, "module") if r["name"] == cls]
        for ctr in ("lut", "ff"):
            samples = [
                (terms(cfg[r["build"]]), val(r, ctr) - counted(cfg[r["build"]])[ctr])
                for r in own
            ]
            name = f"{cls}.{ctr}"
            m.coef[name], m.report[name] = _regress(
                name, samples, scale=[val(r, ctr) for r in own]
            )

    # the glue and the channels, from detector builds only
    det = rows("det", "module")
    sub = {(r["build"], r["name"]): r for r in rows("det", "subblock")}
    for cls in ("MemRStream", "MemWStream"):
        for mem_dw in sorted({cfg[r["build"]]["mem_dw"] for r in det}):
            got = [
                {"lut": val(r, "lut"), "ff": val(r, "ff")}
                for r in det
                if r["name"] == cls and cfg[r["build"]]["mem_dw"] == mem_dw
            ]
            m.table[f"{cls}|{mem_dw}"] = _single(got, f"{cls} at {mem_dw}-bit words")
    for cls in ("CgCmdRx", "CgCtrl"):
        own = [r for r in det if r["name"] == cls]
        for ctr in ("lut", "ff"):
            name = f"{cls}.{ctr}"
            m.coef[name], m.report[name] = _regress(
                name, [(_small_terms(cfg[r["build"]]), val(r, ctr)) for r in own]
            )
    loaders = {
        "CgLoad": (("WORDS", "K"), ("WORDS1", "L")),
        "CgStore": (("WORDS", "L"),),
    }
    for cls, parts in loaders.items():
        short = "load" if cls == "CgLoad" else "store"
        own = [r for r in det if r["name"] == cls]
        for ctr in ("lut", "ff"):
            samples = {}
            for r in own:
                k = cfg[r["build"]]
                for loop, lanes in parts:
                    key = (
                        k[lanes],
                        k["W"],
                        k["mem_dw"],
                        lanes,
                    )  # one sample per distinct shape
                    samples[key] = (
                        _lane_terms(k[lanes], k["W"], k["mem_dw"]),
                        val(sub[(r["build"], f"{cls}.{loop}")], ctr),
                    )
            name = f"{short}.{ctr}"
            m.coef[name], m.report[name] = _regress(name, list(samples.values()))
        for mem_dw in sorted({cfg[r["build"]]["mem_dw"] for r in own}):
            rest = [
                {
                    ctr: val(r, ctr)
                    - sum(
                        val(sub[(r["build"], f"{cls}.{loop}")], ctr)
                        for loop, _ in parts
                    )
                    for ctr in ("lut", "ff")
                }
                for r in own
                if cfg[r["build"]]["mem_dw"] == mem_dw
            ]
            m.table[f"{cls}.own|{mem_dw}"] = {
                ctr: round(sum(x[ctr] for x in rest) / len(rest))
                for ctr in ("lut", "ff")
            }
    for mem_dw in sorted({k["mem_dw"] for b, k in cfg.items() if b.startswith("det_")}):
        got = []
        for b, k in cfg.items():
            if b.startswith("det_") and k["mem_dw"] == mem_dw:
                inst = [r for r in rows("det", "instance") if r["build"] == b]
                got.append(
                    {
                        ctr: sum(val(r, ctr) for r in inst)
                        for ctr in ("lut", "ff", "bram")
                    }
                )
        m.table[f"adapters|{mem_dw}"] = _single(got, f"adapters at {mem_dw}-bit words")
    for depth in sorted(
        {k["cmd_depth"] for b, k in cfg.items() if b.startswith("det_")}
    ):
        got = []
        for b, k in cfg.items():
            if b.startswith("det_") and k["cmd_depth"] == depth:
                fifo = [
                    r
                    for r in modules
                    if r["build"] == b and r["kind"] in ("fifo", "unitemized")
                ]
                got.append(
                    {
                        ctr: sum(val(r, ctr) for r in fifo)
                        for ctr in ("lut", "ff", "bram")
                    }
                )
        lut = sorted({g["lut"] for g in got})
        m.table[f"fifos|{depth}"] = {
            **_single(
                [{k: v for k, v in g.items() if k != "lut"} for g in got], "FIFOs"
            ),
            "lut": lut[len(lut) // 2],
        }

    # cycles: block spans from the unit builds; the loop and job offsets from the detectors

    def span(top: str, q: str) -> list[tuple[dict, float]]:
        return [
            (cfg[r["build"]], float(r["cycles"]))
            for r in cycles
            if r["top"] == top and r["quantity"] == q
        ]

    for name, top in (("vec.iter", "vec"), ("vec.init", "vec"), ("mm.iter", "mm")):
        m.coef[name], m.report[name] = _regress(
            name, [(TERMS[name][0](k), y) for k, y in span(top, name)]
        )
    by = {
        (r["build"], r["quantity"]): float(r["cycles"])
        for r in cycles
        if r["top"] == "det" and r["quantity"] != "job_interval"
    }
    dets = sorted({b for b, _q in by})
    gaps = {by[(b, "t_iter")] - by[(b, "mm.iter")] - by[(b, "vec.iter")] for b in dets}
    m.table["handoff|loop"] = {
        "cycles": _single([{"cycles": g} for g in gaps], "loop handoff")["cycles"]
    }
    for mem_dw in sorted({cfg[b]["mem_dw"] for b in dets}):
        extra = [
            {"cycles": round(by[(b, "t0")] - by[(b, "vec.init")])}
            for b in dets
            if cfg[b]["mem_dw"] == mem_dw
        ]
        m.table[f"t0_extra|{mem_dw}"] = _single(
            extra, f"job overhead at {mem_dw}-bit words"
        )
    return m


# --- checks on the fit builds ----------------------------------------------------------------


def counted_misses(data_dir: Path = PAPER_DATA) -> list[str]:
    """Every ``fit`` row whose DSP or BRAM the counted rules do not reproduce exactly."""
    cfg, modules, _ = _fit_tables(data_dir)
    out = []
    for r in modules:
        k = cfg[r["build"]]
        if r["kind"] == "module" and (r["top"], r["name"]) in (
            ("vec", "CgVec"),
            ("mm", "CgMm"),
        ):
            want = vec_counted(k) if r["name"] == "CgVec" else mm_counted(k)
            for ctr in ("dsp", "bram"):
                if int(r[ctr]) != want[ctr]:
                    out.append(
                        f"{r['build']} {r['name']} {ctr}: measured {r[ctr]}, counted {want[ctr]}"
                    )
        elif r["kind"] == "memory":
            dual = {n: d for n, _w, _b, _k, d in channels(k, r["top"])}[
                r["name"].removesuffix("_U")
            ]
            want = sob_memory(int(r["words"]), int(r["bits"]), int(r["banks"]), dual)
            got = {ctr: int(r[ctr]) for ctr in ("bram", "lut", "ff")}
            if got != want:
                out.append(f"{r['build']} {r['name']}: measured {got}, counted {want}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--fit",
        action="store_true",
        help=f"fit from paper_data/ and write {MODEL_FILE.name}",
    )
    args = ap.parse_args(argv)
    models = fit() if args.fit else Models.load()
    if args.fit:
        print("wrote", models.save())
    misses = counted_misses()
    print(f"counted DSP / BRAM rules: {len(misses)} miss(es) on the fit builds")
    for line in misses:
        print("  ", line)
    print(f"{'regression':12s}  n terms  max|res|  LOO mean  LOO max")
    for name, r in models.report.items():
        print(
            f"{name:12s} {r['n']:2d} {r['terms']:5d} {r['max_abs_residual']:9.1f} {r['loo_mape_pct']:8.2f}% {r['loo_max_pct']:7.2f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
