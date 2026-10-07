"""linalg_cal.py — step 7.5: calibrating the cost model of Waveflow's systolic unit.

Study tooling for plan step 7.5 (``plans/mimo_cg/mimo_cg_paper_sims.md``, §14 decision of
2026-10-07).  The component and its model forms are ``waveflow.linalg`` (``systolic.py``,
``cost.py``); this module chooses the builds, runs them, merges the measurements, fits the model and
scores it.  The bench is ``tests/linalg/_unit_bench.py``: the unit fed from memory through the
in-band memory streams, as in the step 7.4 RTL gate.

    python -m examples.mimo_cg.hw.linalg_cal split [--check]     # the pre-registered builds
    python -m examples.mimo_cg.hw.linalg_cal run --role fit [--shard i/n]
    python -m examples.mimo_cg.hw.linalg_cal merge
    python -m examples.mimo_cg.hw.linalg_cal fit                 # writes the packaged platform
    python -m examples.mimo_cg.hw.linalg_cal run --role holdout [--shard i/n]
    python -m examples.mimo_cg.hw.linalg_cal validate

**The builds.**  The space: ``Mmax, Kmax`` in (4, 8, 16), ``Nmax`` in (16, 32), the array ``R × C``
with ``R | Mmax``, ``C`` in (4, 8, 16, 32) with ``C | Nmax`` and ``R·C <= 256``, ``L`` in
(1 .. 16) with ``L | C``, ``W`` in (8 .. 16) for ``A``, ``B`` and ``C`` (3, 4 and 5 integer bits),
the multiply form 3 or 4, and message words of 32 or 64 bits.  The calibration builds are chosen by
rule (:func:`fit_set`: the centre, each knob varied alone, and corners); the held-out builds are a
seeded uniform draw from the rest (:func:`holdout_set`).  Both are written to
``paper_data/linalg_split.csv`` and committed before any build runs.

**A build** (:func:`measure`): generate, csynth (the per-task rows and the remainder, through the
framework's report attribution), then XSI on four job shapes (:func:`shapes`), each issued
:data:`REPEATS` times back to back; every reply is checked bit for bit against the model, and a
shape's steady interval is the time between its last two replies.  Records go to
``results/linalg_points/<build>.json``; build trees to ``hw/build/linalg/`` (pruned to the reports).

**Scoring** (:func:`validate`, AC7): on the held-out builds, DSP and block RAM exact on at least
90% of the builds (the unit and the core), LUT and FF mean absolute percentage error at most 10%
(the unit and the core), and the steady interval within 5% (mean absolute percentage error over
every held-out shape).  The step 7.4 runs (``paper_data/linalg_unit_7_4_cycles.csv``): the span from
the first to the last reply, predicted as the sum of the messages' intervals, within 5%.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
import time
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from examples.mimo_cg.hw.campaign import shard
from examples.mimo_cg.hw.measure import prune_build
from tests.linalg import _unit_bench as UB
from waveflow.calib.synth_report import report_from_solution
from waveflow.linalg import cost
from waveflow.linalg.message import Status
from waveflow.linalg.systolic import MatmulOp, SystolicUnit, cmd_status, core_cycles
from waveflow.simulation.simulation import Simulation
from waveflow.utils.fixputils import Format, OMode, QMode

HERE = Path(__file__).resolve().parent
EX = HERE.parent
PAPER = EX / "paper_data"
SPLIT = PAPER / "linalg_split.csv"
POINTS = EX / "results" / "linalg_points"
BUILDS = HERE / "build" / "linalg"
RUN_7_4 = PAPER / "linalg_unit_7_4_cycles.csv"
PART, PERIOD_NS = cost.PART, 4
TOOL = "Vitis HLS / Vivado xsim 2024.1"

VALUES = {
    "M": (4, 8, 16),
    "K": (4, 8, 16),
    "N": (16, 32),
    "R": (1, 2, 4, 8, 16),
    "C": (4, 8, 16, 32),
    "L": (1, 2, 4, 8, 16),
    "W": (8, 10, 12, 14, 16),
    "form": (3, 4),
    "word": (32, 64),
}
MAX_PES = 256
#: Job shapes per build, each issued this many times back to back.
REPEATS = 4
HOLDOUT_SEED = 75
N_HOLDOUT = 12
#: AC7's thresholds.
MIN_EXACT_PCT, MAX_AREA_MAPE, MAX_CYCLE_MAPE = 90.0, 10.0, 5.0


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


@dataclass(frozen=True)
class UnitConfig:
    M: int
    K: int
    N: int
    R: int
    C: int
    L: int
    W: int
    form: int
    word: int

    @property
    def name(self) -> str:
        c = self
        return (
            f"su_m{c.M}_k{c.K}_n{c.N}_r{c.R}_c{c.C}_l{c.L}_w{c.W}_f{c.form}_b{c.word}"
        )

    def unit(self) -> dict:
        """The ``SystolicUnit`` fields of this configuration."""
        c = self
        return {
            "word_bits": c.word, "Mmax": c.M, "Kmax": c.K, "Nmax": c.N, "L": c.L, "R": c.R,
            "C": c.C, "form": c.form, "sob_depth": 2, "lane_bits": 16,
            "a": reg(c.W, 3), "b": reg(c.W, 4), "c": reg(c.W, 5),
        }  # fmt: skip

    def build(self) -> SystolicUnit:
        return SystolicUnit(name="unit", sim=Simulation(), **self.unit())


def valid(c: UnitConfig) -> bool:
    return (
        all(getattr(c, k) in v for k, v in VALUES.items())
        and c.M % c.R == 0
        and c.N % c.C == 0
        and c.C % c.L == 0
        and c.R * c.C <= MAX_PES
    )


def space() -> list[UnitConfig]:
    keys = list(VALUES)
    return [
        c
        for c in (
            UnitConfig(*vals) for vals in itertools.product(*(VALUES[k] for k in keys))
        )
        if valid(c)
    ]


CENTRE = UnitConfig(8, 8, 32, 4, 8, 4, 12, 4, 64)


def fit_set() -> list[UnitConfig]:
    """The calibration builds, by rule: the centre; each knob varied alone from it; the four
    configurations of step 7.3; and corners of the three-multiply form, 16 lanes, 32-bit words and
    plain multiplies built from LUTs (W <= 10, four-multiply form) at both ends of the array size.
    """
    c0 = CENTRE
    out = [c0]
    for knob, vals in (
        ("R", (1, 2, 8)),
        ("C", (4, 16, 32)),
        ("L", (1, 2, 8)),
        ("W", (8, 10, 14, 16)),
        ("form", (3,)),
        ("word", (32,)),
        ("M", (4, 16)),
        ("K", (4, 16)),
        ("N", (16,)),
    ):
        out += [UnitConfig(**{**asdict(c0), knob: v}) for v in vals]
    out += [
        UnitConfig(16, 16, 32, 16, 16, 4, 16, 4, 64),  # largest
        UnitConfig(16, 16, 32, 1, 4, 1, 8, 3, 64),  # smallest
        UnitConfig(8, 8, 32, 8, 32, 16, 14, 3, 64),  # wide
        UnitConfig(16, 16, 32, 16, 16, 4, 12, 3, 64),
        UnitConfig(8, 8, 32, 4, 16, 16, 12, 4, 64),
        UnitConfig(4, 16, 16, 2, 4, 2, 16, 3, 32),
        UnitConfig(
            16, 16, 32, 16, 16, 4, 10, 4, 64
        ),  # plain multiplies in LUTs, a large array
        UnitConfig(8, 8, 32, 2, 4, 2, 8, 4, 32),  # ... a small one, 32-bit words
    ]
    out = list(dict.fromkeys(out))
    bad = [c.name for c in out if not valid(c)]
    if bad:
        raise ValueError(f"invalid calibration builds: {bad}")
    return out


def holdout_set() -> list[UnitConfig]:
    """:data:`N_HOLDOUT` configurations drawn uniformly (seed :data:`HOLDOUT_SEED`) from the space
    without the calibration builds."""
    fit = set(fit_set())
    rest = [c for c in space() if c not in fit]
    rng = np.random.default_rng(HOLDOUT_SEED)
    picks = sorted(int(i) for i in rng.choice(len(rest), size=N_HOLDOUT, replace=False))
    return [rest[i] for i in picks]


def split_text() -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["role", "build", *VALUES])
    for role, cfgs in (("fit", fit_set()), ("holdout", holdout_set())):
        for c in cfgs:
            w.writerow([role, c.name, *(getattr(c, k) for k in VALUES)])
    return buf.getvalue()


def load_split() -> dict[str, list[UnitConfig]]:
    roles: dict[str, list[UnitConfig]] = {}
    with SPLIT.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            roles.setdefault(row["role"], []).append(
                UnitConfig(*(int(row[k]) for k in VALUES))
            )
    return roles


def shapes(c: UnitConfig) -> list[tuple[int, int, int, int]]:
    """The four job shapes of a build: full ``A·B``, full ``Aᴴ``, one tile, and a short ``k``
    with half the columns."""
    k_short = min(c.K, max(1, c.L // 2))
    n_half = max(c.C, (c.N // 2) // c.C * c.C)
    out = [
        (MatmulOp.MUL, c.M, c.K, c.N),
        (MatmulOp.MUL_AH, c.M, c.K, c.N),
        (MatmulOp.MUL, c.R, c.K, c.C),
        (MatmulOp.MUL, c.M, k_short, n_half),
    ]
    for op, m, k, n in out:
        st = cmd_status(
            op, 1, m, k, n, Mmax=c.M, Kmax=c.K, Nmax=c.N, L=c.L, R=c.R, C=c.C
        )
        assert st == Status.OK, (c.name, op, m, k, n, st)
    return out


# --- one build -----------------------------------------------------------------------------------


def _cycle_budget(c: UnitConfig, unit: SystolicUnit) -> int:
    per = [
        core_cycles(op, 1, m, k, n, L=c.L, R=c.R, C=c.C) + 4 * m * k + 200
        for op, m, k, n in shapes(c)
    ]
    return int(3 * REPEATS * sum(per) + 20_000)


def measure(c: UnitConfig, role: str) -> dict:
    """Build, measure and check one configuration; returns its record."""
    rec: dict = {
        "build": c.name,
        "role": role,
        "config": asdict(c),
        "tool": TOOL,
        "error": None,
    }
    out = BUILDS / c.name
    unit_p = c.unit()
    try:
        t0 = time.time()
        UB.generate(unit_p, out, part=PART, period_ns=PERIOD_NS)
        UB.csynth(out)
        rec["csynth_seconds"] = round(time.time() - t0, 1)
        dut = UB.UnitBench(name=UB.TOP, sim=Simulation(), unit=tuple(unit_p.items()))
        rep = report_from_solution(dut, out / f"{UB.TOP}_proj" / "solution1")
        rec["resources"] = {
            "modules": [
                {
                    "cls": m.cls_name,
                    "rtl_module": m.rtl_module,
                    "counters": dict(m.resources),
                }
                for m in rep.modules
            ],
            "top": dict(rep.top),
            "integration": dict(rep.integration),
        }
        rec["est_ns"] = _est_ns(out)
        rng = np.random.default_rng(zlib.crc32(c.name.encode()))
        jobs = [
            UB.random_job(rng, unit_p, op, m, k, n, edge=(op == MatmulOp.MUL_AH))
            for op, m, k, n in shapes(c)
            for _ in range(REPEATS)
        ]
        sim = UB.UnitBenchSim(unit_p, jobs, n_cycles=_cycle_budget(c, c.build()))
        t0 = time.time()
        sc = UB.generate_tb(out, sim)
        run = UB.run_xsi(out)
        rec["xsi_seconds"] = round(time.time() - t0, 1)
        if run.returncode != 0:
            raise RuntimeError(f"XSI failed: {(run.stdout + run.stderr)[-2000:]}")
        cycles = UB.check_xsi(out, sc, c.word)  # asserts every reply bit for bit
        rec["rtl"] = {
            "bit_exact": True,
            "n_cycles": sim.tb.n_cycles,
            "reply_cycles": cycles,
        }
        rec["shapes"] = []
        for i, (op, m, k, n) in enumerate(shapes(c)):
            r = cycles[i * REPEATS : (i + 1) * REPEATS]
            rec["shapes"].append(
                {
                    "op": int(op),
                    "m": m,
                    "k": k,
                    "n": n,
                    "interval": r[-1] - r[-2],
                    "interval_prev": r[-2] - r[-3],
                    "first": r[0],
                }
            )
    except Exception as e:  # noqa: BLE001 - a failed build is recorded, never dropped
        rec["error"] = f"{type(e).__name__}: {e}"[:3000]
    finally:
        if out.exists():
            prune_build(out, UB.TOP)
    return rec


def _est_ns(out: Path) -> float:
    import xml.etree.ElementTree as ET

    xml = out / f"{UB.TOP}_proj" / "solution1" / "syn" / "report" / "csynth.xml"
    return float(ET.parse(xml).getroot().findtext(".//EstimatedClockPeriod"))


def run(role: str, spec: str | None) -> None:
    cfgs = load_split()[role]
    names = [c.name for c in cfgs]
    todo = shard(names, spec) if spec else names
    POINTS.mkdir(parents=True, exist_ok=True)
    by_name = {c.name: c for c in cfgs}
    for name in todo:
        path = POINTS / f"{name}.json"
        if path.is_file() and json.loads(path.read_text())["error"] is None:
            continue
        rec = measure(by_name[name], role)
        path.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        print(name, "ok" if rec["error"] is None else rec["error"][:200], flush=True)


# --- the tables ----------------------------------------------------------------------------------

TASK_CLS = {
    "SystolicRx": "systolic_rx_task",
    "SystolicLoad": "systolic_load_task",
    "SystolicCore": "systolic_core_task",
    "SystolicStore": "systolic_store_task",
}
COUNTERS = ("lut", "ff", "dsp", "bram")


def records(role: str | None = None) -> list[dict]:
    recs = [json.loads(p.read_text()) for p in sorted(POINTS.glob("*.json"))]
    return [r for r in recs if role is None or r["role"] == role]


def unit_rows(rec: dict) -> dict:
    """Per task, the channels (the remainder) and the unit total, of one record."""
    out = {}
    for m in rec["resources"]["modules"]:
        if m["cls"] in TASK_CLS:
            out[TASK_CLS[m["cls"]]] = {
                k: int(m["counters"].get(k, 0)) for k in COUNTERS
            }
    out[cost.CHANNELS] = {
        k: int(rec["resources"]["integration"].get(k, 0)) for k in COUNTERS
    }
    out["unit"] = {k: sum(out[t][k] for t in out) for k in COUNTERS}
    return out


def merge() -> None:
    builds, modules, cycles = [], [], []
    for rec in records():
        c = rec["config"]
        builds.append(
            {
                "build": rec["build"],
                "role": rec["role"],
                **c,
                "est_ns": rec.get("est_ns", ""),
                "csynth_s": rec.get("csynth_seconds", ""),
                "xsi_s": rec.get("xsi_seconds", ""),
                "bit_exact": int(rec.get("rtl", {}).get("bit_exact", 0)),
                "error": (rec["error"] or "")[:200],
                "tool": rec["tool"],
            }
        )
        if rec["error"] is None:
            for what, row in unit_rows(rec).items():
                modules.append(
                    {"build": rec["build"], "role": rec["role"], "what": what, **row}
                )
            for s in rec["shapes"]:
                cycles.append({"build": rec["build"], "role": rec["role"], **s})
    for name, rows in (
        ("linalg_builds.csv", builds),
        ("linalg_modules.csv", modules),
        ("linalg_cycles.csv", cycles),
    ):
        with (PAPER / name).open("w", encoding="utf-8", newline="") as f:
            f.write(
                f"# {name}: step 7.5 of plans/mimo_cg/mimo_cg_paper_sims.md; tool={TOOL}; part={PART}; period_ns={PERIOD_NS}\n"
            )
            w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        print(f"wrote paper_data/{name}: {len(rows)} rows")


# --- the fit -------------------------------------------------------------------------------------


def _cycle_rows(recs: list[dict]) -> list[dict]:
    rows = []
    for rec in recs:
        unit = UnitConfig(**rec["config"]).build()
        for s in rec["shapes"]:
            feats = cost.message_features(unit, s["op"], s["m"], s["k"], s["n"])
            rows.append({**feats, "interval": s["interval"], "build": rec["build"]})
    return rows


def _fit_all(recs: list[dict]) -> tuple[dict, dict, dict]:
    """Task resource models, the channel model and the message model from ``recs``."""
    from waveflow.calib.vitis_model import VitisResourceModel

    samples: dict = {t: [] for t in cost.TASKS}
    chan_rows = []
    for rec in recs:
        unit = UnitConfig(**rec["config"]).build()
        rows = unit_rows(rec)
        for comp in (unit.rx, unit.load, unit.core, unit.store):
            samples[cost.task_of(comp)].append((comp, rows[cost.task_of(comp)]))
        chan_rows.append({"unit": unit, **rows[cost.CHANNELS]})
    models = {}
    for task, smp in samples.items():
        m = VitisResourceModel(
            name=task, part=PART, platform=cost.platform(), comp_class=type(smp[0][0])
        )
        models[task] = m.fit(samples=smp)
    return (
        models,
        cost.fit_channels(chan_rows),
        cost.fit_message_model(_cycle_rows(recs)),
    )


def fit() -> None:
    recs = [r for r in records("fit") if r["error"] is None]
    if len(recs) != len(load_split()["fit"]):
        raise SystemExit(
            f"{len(recs)} clean calibration records of {len(load_split()['fit'])}"
        )
    models, chan, msg = _fit_all(recs)
    pdir = cost.platform_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "platform.json").write_text(
        json.dumps({"part": PART, "clk_freq_hz": cost.CLK_HZ, "tool": TOOL}, indent=1)
        + "\n",
        encoding="utf-8",
    )
    prov = {"tool": TOOL, "builds": [r["build"] for r in recs], "step": "7.5"}
    files = []
    for task, m in models.items():
        path = pdir / "models" / task / "params.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        m.save_model(path)
        files.append(path)
    for path, obj in (
        (pdir / "models" / cost.CHANNELS / "params.json", chan),
        (pdir / "components" / "systolic_unit" / "params.json", msg),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(obj, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        files.append(path)
    (pdir / "provenance.json").write_text(
        json.dumps(prov, indent=1) + "\n", encoding="utf-8"
    )
    report = {"loo": _loo(recs), "message_meta": msg["meta"], "n_builds": len(recs)}
    report["sha256"] = {
        str(f.relative_to(pdir)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in files
    }
    _write_kv(PAPER / "linalg_fit_report.csv", _flatten(report), "step 7.5c fit")
    print(json.dumps(report, indent=1))


def _loo(recs: list[dict]) -> dict:
    """Leave-one-build-out errors on the calibration builds: unit and core LUT/FF, intervals."""
    errs: dict = {
        "unit_lut": [],
        "unit_ff": [],
        "core_lut": [],
        "core_ff": [],
        "interval": [],
    }
    for i, rec in enumerate(recs):
        rest = recs[:i] + recs[i + 1 :]
        models, chan, msg = _fit_all(rest)
        pred = _predict(UnitConfig(**rec["config"]).build(), models, chan)
        meas = unit_rows(rec)
        for k in ("lut", "ff"):
            errs[f"unit_{k}"].append(_ape(pred["unit"][k], meas["unit"][k]))
            errs[f"core_{k}"].append(
                _ape(pred["systolic_core_task"][k], meas["systolic_core_task"][k])
            )
        for row in _cycle_rows([rec]):
            errs["interval"].append(
                _ape(cost.message_interval(msg, row), row["interval"])
            )
    return {
        k: {"mean_pct": float(np.mean(v)), "max_pct": float(np.max(v))}
        for k, v in errs.items()
    }


def _ape(pred: float, meas: float) -> float:
    return abs(pred - meas) / abs(meas) * 100 if meas else 0.0


def _predict(
    unit: SystolicUnit, models: dict | None = None, chan: dict | None = None
) -> dict:
    """Per task, the channels and the unit total, from given models (or the packaged ones)."""
    if models is None:
        return {**cost.predict_unit(unit), "unit": cost.predict_unit(unit)["total"]}
    out = {}
    for comp in (unit.rx, unit.load, unit.core, unit.store):
        p = models[cost.task_of(comp)].predict(comp)
        out[cost.task_of(comp)] = {k: float(p.get(k, 0.0)) for k in COUNTERS}
    out[cost.CHANNELS] = cost.predict_channels(unit, chan)
    out["unit"] = {k: sum(out[t][k] for t in out) for k in COUNTERS}
    return out


# --- scoring -------------------------------------------------------------------------------------


def validate() -> dict:
    """Score the packaged models on the held-out builds and on the step 7.4 runs (AC7)."""
    msg = cost.message_model()
    rows, cyc = [], []
    for rec in records("holdout"):
        if rec["error"] is not None:
            rows.append({"build": rec["build"], "error": rec["error"][:200]})
            continue
        unit = UnitConfig(**rec["config"]).build()
        pred, meas = _predict(unit), unit_rows(rec)
        row = {"build": rec["build"]}
        for what in ("unit", "systolic_core_task"):
            tag = "unit" if what == "unit" else "core"
            for k in COUNTERS:
                row[f"{tag}_{k}"] = meas[what][k]
                row[f"{tag}_{k}_pred"] = round(pred[what][k], 1)
        rows.append(row)
        for s in _cycle_rows([rec]):
            p = cost.message_interval(msg, s)
            cyc.append(
                {
                    "build": rec["build"],
                    "interval": s["interval"],
                    "pred": round(p, 1),
                    "ape_pct": round(_ape(p, s["interval"]), 2),
                }
            )
    ok = [r for r in rows if "error" not in r]
    metrics = {"n_builds": len(rows), "n_clean": len(ok)}
    for tag in ("unit", "core"):
        for k in ("dsp", "bram"):
            exact = [r[f"{tag}_{k}"] == round(r[f"{tag}_{k}_pred"]) for r in ok]
            metrics[f"{tag}_{k}_exact_pct"] = 100.0 * sum(exact) / len(exact)
        for k in ("lut", "ff"):
            apes = [_ape(r[f"{tag}_{k}_pred"], r[f"{tag}_{k}"]) for r in ok]
            metrics[f"{tag}_{k}_mape_pct"] = float(np.mean(apes))
            metrics[f"{tag}_{k}_max_pct"] = float(np.max(apes))
    metrics["interval_mape_pct"] = float(np.mean([c["ape_pct"] for c in cyc]))
    metrics["interval_max_pct"] = float(np.max([c["ape_pct"] for c in cyc]))
    metrics["run_7_4"] = _score_7_4(msg)
    metrics["ac7"] = {
        "dsp_bram_exact": all(
            metrics[f"{t}_{k}_exact_pct"] >= MIN_EXACT_PCT
            for t in ("unit", "core")
            for k in ("dsp", "bram")
        ),
        "lut_ff_mape": all(
            metrics[f"{t}_{k}_mape_pct"] <= MAX_AREA_MAPE
            for t in ("unit", "core")
            for k in ("lut", "ff")
        ),
        "cycles": metrics["interval_mape_pct"] <= MAX_CYCLE_MAPE,
        "run_7_4": all(
            v["ape_pct"] <= MAX_CYCLE_MAPE for v in metrics["run_7_4"].values()
        ),
    }
    for name, data in (
        ("linalg_validation.csv", rows),
        ("linalg_validation_cycles.csv", cyc),
    ):
        with (PAPER / name).open("w", encoding="utf-8", newline="") as f:
            keys = list(dict.fromkeys(k for r in data for k in r))
            w = csv.DictWriter(f, fieldnames=keys, lineterminator="\n")
            w.writeheader()
            w.writerows(data)
    _write_kv(PAPER / "linalg_validation_metrics.csv", _flatten(metrics), "step 7.5d")
    print(json.dumps(metrics, indent=1))
    return metrics


def load_7_4() -> dict:
    """The step 7.4 runs: per run, its configuration and its jobs in order."""
    runs: dict = {}
    with RUN_7_4.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(line for line in f if not line.startswith("#")))
    for r in rows:
        run_ = runs.setdefault(
            r["run"], {"config": {k: int(r[k]) for k in VALUES}, "jobs": []}
        )
        run_["jobs"].append(
            {
                k: (r[k] if k == "status" else int(r[k]))
                for k in r
                if k not in VALUES and k != "run"
            }
        )
    return runs


def _score_7_4(msg: dict) -> dict:
    """The step 7.4 runs: the span from the first reply to the last against the sum of the
    predicted intervals of the messages after the first."""
    from waveflow.linalg.message import header_words
    from waveflow.linalg.systolic import request_words

    out = {}
    for name, run_ in load_7_4().items():
        unit = UnitConfig(**run_["config"]).build()
        wb = int(unit.word_bits)
        pred = 0.0
        for j in run_["jobs"][1:]:
            if j["status"] == "OK":
                feats = cost.message_features(unit, j["op"], j["m"], j["k"], j["n"])
                pred += cost.message_interval(msg, feats)
            else:
                pay = request_words(
                    j["m_payload"], j["k_payload"], j["n_payload"], 16, wb
                )
                pred += cost.reject_interval(msg, header_words(wb) + pay)
        meas = run_["jobs"][-1]["reply_cycle"] - run_["jobs"][0]["reply_cycle"]
        out[name] = {
            "measured": meas,
            "predicted": round(pred, 1),
            "ape_pct": round(_ape(pred, meas), 2),
        }
    return out


def _write_kv(path: Path, flat: dict, note: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(f"# {path.name}: {note}; tool={TOOL}\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["metric", "value"])
        for k, v in flat.items():
            w.writerow([k, v])


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="step 7.5: the systolic unit's cost model")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("split")
    sp.add_argument("--check", action="store_true")
    rp = sub.add_parser("run")
    rp.add_argument("--role", required=True, choices=("fit", "holdout"))
    rp.add_argument("--shard")
    sub.add_parser("merge")
    sub.add_parser("fit")
    sub.add_parser("validate")
    a = ap.parse_args(argv)
    if a.cmd == "split":
        text = split_text()
        if a.check:
            same = SPLIT.read_text(encoding="utf-8") == text
            print("linalg_split.csv:", "unchanged" if same else "DIFFERS")
            return 0 if same else 1
        SPLIT.write_text(text, encoding="utf-8")
        print(
            f"wrote {SPLIT.relative_to(EX)}: {len(fit_set())} fit, {len(holdout_set())} holdout"
        )
    elif a.cmd == "run":
        run(a.role, a.shard)
    elif a.cmd == "merge":
        merge()
    elif a.cmd == "fit":
        fit()
    elif a.cmd == "validate":
        validate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
