"""cg_cal.py — step 8.4: calibrating the cost model of Waveflow's CG vector unit.

Study tooling for plan step 8.4 (``plans/mimo_cg/mimo_cg_paper_sims.md``, §14 decision of
2026-10-07), the twin of ``linalg_cal.py`` (step 7.5).  The component and its model forms are
``waveflow.linalg`` (``cg_vector.py``, ``cg_cost.py``); this module chooses the builds, runs them,
merges the measurements, fits the model and scores it.  The bench is
``tests/linalg/_cg_unit_bench.py``: the unit fed from memory through the in-band memory streams, as
in the step 8.3 RTL gate.

    python -m examples.mimo_cg.hw.cg_cal split [--check]     # the pre-registered builds
    python -m examples.mimo_cg.hw.cg_cal run --role fit [--shard i/n]
    python -m examples.mimo_cg.hw.cg_cal merge
    python -m examples.mimo_cg.hw.cg_cal fit                 # writes the packaged platform
    python -m examples.mimo_cg.hw.cg_cal run --role holdout [--shard i/n]
    python -m examples.mimo_cg.hw.cg_cal validate

**The builds.**  The space: ``Kmax`` in (4, 8, 16) (``nitmax = Kmax``), ``Nmax`` in (16, 32), ``L``
in (1 .. 16) with ``L | Nmax``, the example's sweep formats ``W`` in (8 .. 16) and ``g`` in (0, 4,
8) (vectors, α and β ``W`` bits, ``ps`` and ``rz`` ``W + g``, ``g_div = 6``), and message words of
32 or 64 bits; the saturation stress set appears in calibration builds only.  The calibration
builds are chosen by rule (:func:`fit_set`), the held-out builds drawn uniformly from the rest
(:func:`holdout_set`); both are written to ``paper_data/cg_split.csv`` and committed before any
build runs.

**A build** (:func:`measure`): generate, csynth (the per-task rows and the remainder), then XSI on
:func:`scenario`: four job shapes, each twice back to back, a job with a rejected request inside
it, and rejected requests right after served ones and back to back, with payloads of three sizes.
Every reply is checked bit for bit against the model.  Each request's interval is the time from the
previous reply to its own; a served request's interval is fitted when the previous request was
served too, a rejected one's by what precedes it (after a rejection, on the previous request's
words in, since the receiver answers a request before draining it).

**Scoring** (:func:`validate`, AC8): on the held-out builds, DSP and block RAM exact on at least
90% of the builds (the unit and the core), LUT and FF mean absolute percentage error at most 10%,
and the served intervals within 5% (mean absolute percentage error); the step 8.3 runs
(``paper_data/cg_unit_8_3_cycles.csv``): the span from the first to the last reply, predicted as the
sum of the requests' intervals, within 5%.
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
from examples.mimo_cg.hw.linalg_cal import _ape, _flatten, _write_kv
from examples.mimo_cg.hw.measure import prune_build
from tests.linalg import _cg_unit_bench as UB
from waveflow.calib.synth_report import report_from_solution
from waveflow.linalg import cg_cost, cost
from waveflow.linalg.cg_vector import (
    CgOp,
    CgVectorUnit,
    iter_cycles,
    start_cycles,
)
from waveflow.linalg.message import Status, header_words
from waveflow.simulation.simulation import Simulation

HERE = Path(__file__).resolve().parent
EX = HERE.parent
PAPER = EX / "paper_data"
SPLIT = PAPER / "cg_split.csv"
POINTS = EX / "results" / "cg_points"
BUILDS = HERE / "build" / "cg"
RUN_8_3 = PAPER / "cg_unit_8_3_cycles.csv"
PART, PERIOD_NS = cost.PART, 4
TOOL = "Vitis HLS / Vivado xsim 2024.1"

VALUES = {
    "K": (4, 8, 16),
    "N": (16, 32),
    "L": (1, 2, 4, 8, 16),
    "W": (8, 10, 12, 14, 16),
    "g": (0, 4, 8),
    "word": (32, 64),
}
HOLDOUT_SEED = 85
N_HOLDOUT = 12
#: The unknown operation of the rejected requests.
REJECT_OP = 7
#: AC8's thresholds.
MIN_EXACT_PCT, MAX_AREA_MAPE, MAX_CYCLE_MAPE = 90.0, 10.0, 5.0


@dataclass(frozen=True)
class CgConfig:
    K: int
    N: int
    L: int
    W: int
    g: int
    word: int
    stress: int = 0

    @property
    def name(self) -> str:
        c = self
        fmt = "stress" if c.stress else f"w{c.W}_g{c.g}"
        return f"cg_k{c.K}_n{c.N}_l{c.L}_{fmt}_b{c.word}"

    def formats(self):
        from examples.mimo_cg.mimo_cg_accuracy_sweep import sweep_format
        from examples.mimo_cg.mimo_cg_conformance import STRESS_FORMATS

        return STRESS_FORMATS if self.stress else sweep_format(self.W, self.g)

    def unit(self) -> dict:
        """The ``CgVectorUnit`` fields of this configuration."""
        c = self
        return {
            "word_bits": c.word, "Kmax": c.K, "Nmax": c.N, "nitmax": c.K, "L": c.L,
            "sob_depth": 2, "lane_bits": 16, "formats": c.formats(),
        }  # fmt: skip

    def build(self) -> CgVectorUnit:
        return CgVectorUnit(name="unit", sim=Simulation(), **self.unit())


def valid(c: CgConfig) -> bool:
    if c.stress:
        ok_vals = all(getattr(c, k) in VALUES[k] for k in ("K", "N", "L", "word"))
    else:
        ok_vals = all(getattr(c, k) in v for k, v in VALUES.items())
    if not ok_vals or c.N % c.L:
        return False
    try:
        c.formats().intermediates(c.K)
    except NotImplementedError:
        return False
    return True


def space() -> list[CgConfig]:
    keys = list(VALUES)
    return [
        c
        for c in (
            CgConfig(*vals) for vals in itertools.product(*(VALUES[k] for k in keys))
        )
        if valid(c)
    ]


CENTRE = CgConfig(8, 32, 4, 12, 8, 64)


def fit_set() -> list[CgConfig]:
    """The calibration builds, by rule: the centre; each knob varied alone from it; the corners of
    step 8.2; the stress set at both word widths; small stream-of-blocks buffers at 32-bit words
    (where step 7.5's block RAM rule was not measured); state arrays on both sides of the block-RAM
    threshold; and other widths at many lanes."""
    c0 = CENTRE
    out = [c0]
    for knob, vals in (
        ("K", (4, 16)),
        ("N", (16,)),
        ("L", (1, 2, 8, 16)),
        ("W", (8, 10, 14, 16)),
        ("g", (0, 4)),
        ("word", (32,)),
    ):
        out += [CgConfig(**{**asdict(c0), knob: v}) for v in vals]
    C = CgConfig
    out += [
        C(16, 32, 16, 16, 8, 64),  # largest
        C(4, 32, 1, 8, 0, 64),  # smallest
        C(16, 32, 2, 12, 0, 64, stress=1),
        C(8, 32, 4, 12, 0, 32, stress=1),
        C(4, 16, 1, 8, 0, 32),  # small buffers, 32-bit words
        C(4, 16, 2, 10, 4, 32),
        C(8, 16, 4, 8, 0, 32),
        C(4, 16, 4, 12, 8, 32),
        C(4, 16, 8, 12, 8, 32),
        C(16, 32, 4, 12, 8, 64),  # state arrays of 128 per lane
        C(8, 16, 1, 12, 8, 64),
        C(16, 16, 2, 10, 4, 64),
        C(4, 32, 2, 14, 0, 64),  # ... of 64
        C(16, 32, 8, 12, 4, 64),
        C(8, 32, 16, 10, 4, 64),  # other widths at many lanes
        C(16, 32, 8, 14, 8, 64),
        C(8, 32, 2, 16, 0, 32),
    ]
    out = list(dict.fromkeys(out))
    bad = [c.name for c in out if not valid(c)]
    if bad:
        raise ValueError(f"invalid calibration builds: {bad}")
    return out


def holdout_set() -> list[CgConfig]:
    """:data:`N_HOLDOUT` configurations drawn uniformly (seed :data:`HOLDOUT_SEED`) from the space
    without the calibration builds."""
    fit = set(fit_set())
    rest = [c for c in space() if c not in fit]
    rng = np.random.default_rng(HOLDOUT_SEED)
    picks = sorted(int(i) for i in rng.choice(len(rest), size=N_HOLDOUT, replace=False))
    return [rest[i] for i in picks]


FIELDS = (*VALUES, "stress")


def split_text() -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["role", "build", *FIELDS])
    for role, cfgs in (("fit", fit_set()), ("holdout", holdout_set())):
        for c in cfgs:
            w.writerow([role, c.name, *(getattr(c, k) for k in FIELDS)])
    return buf.getvalue()


def load_split() -> dict[str, list[CgConfig]]:
    roles: dict[str, list[CgConfig]] = {}
    with SPLIT.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            roles.setdefault(row["role"], []).append(
                CgConfig(*(int(row[k]) for k in FIELDS))
            )
    return roles


# --- the scenario of a build ---------------------------------------------------------------------


def job_shapes(c: CgConfig) -> list[tuple[int, int, int]]:
    """The four job shapes ``(nit, k, n)``: full, half the columns, a short ``k``, one group."""
    n_half = max(c.L, (c.N // 2) // c.L * c.L)
    k_short = max(1, c.K // 2 - 1)
    return [(c.K, c.K, c.N), (2, c.K, n_half), (3, k_short, c.N), (1, c.K, c.L)]


def scenario(c: CgConfig) -> list:
    """The requests of a build: each job shape twice back to back; a job with a rejected request
    inside it; rejected requests right after served ones and back to back (payloads of three
    sizes)."""
    f = c.formats()
    rng = np.random.default_rng(zlib.crc32(c.name.encode()))
    n_half = job_shapes(c)[1][2]

    def reject(k, n):
        lo, hi = -(1 << (f.B.W - 1)), (1 << (f.B.W - 1)) - 1
        pay = (
            rng.integers(lo, hi + 1, size=(k, n)),
            rng.integers(lo, hi + 1, size=(k, n)),
        )
        return UB.Message(REJECT_OP, k, n, 0, pay, f.B)

    items: list = []
    for nit, k, n in job_shapes(c):
        for _ in range(2):
            items.append(UB.random_job(rng, f, nit, k, n))
    inside = UB.random_job(rng, f, 2, c.K, n_half)
    inside.inserts = {2: [reject(c.K, n_half)]}
    items += [
        inside,
        reject(c.K, c.N),  # after a served request
        reject(c.K, n_half),  # back to back
        reject(c.K, c.L),
        reject(c.K, c.N),
        UB.random_job(rng, f, 1, c.K, c.L),
        reject(c.K, n_half),  # after a served request
        reject(c.K, c.N),  # back to back
    ]
    return items


def _cycle_budget(sim: UB.CgUnitBenchSim, c: CgConfig) -> int:
    total = 0
    for m, (_off, words, _out, _h) in zip(sim.msgs, sim.layout, strict=True):
        if m.op == CgOp.START:
            total += start_cycles(m.k, m.n, L=c.L)
        elif m.op == CgOp.STEP:
            total += iter_cycles(m.k, m.n, L=c.L)
        total += 2 * len(words) + 50
    return int(3 * total + 20_000)


# --- one build -----------------------------------------------------------------------------------


def measure(c: CgConfig, role: str) -> dict:
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
        dut = UB.CgUnitBench(name=UB.TOP, sim=Simulation(), unit=tuple(unit_p.items()))
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
        sim = UB.CgUnitBenchSim(unit_p, scenario(c))
        sim = UB.CgUnitBenchSim(
            unit_p, scenario(c), n_cycles=_cycle_budget(sim, c)
        )  # the same scenario, with its budget
        t0 = time.time()
        sc = UB.generate_tb(out, sim)
        run = UB.run_xsi(out)
        rec["xsi_seconds"] = round(time.time() - t0, 1)
        if run.returncode != 0:
            raise RuntimeError(f"XSI failed: {(run.stdout + run.stderr)[-2000:]}")
        cycles = UB.check_xsi(out, sc, c.word)  # asserts every reply bit for bit
        rec["rtl"] = {"bit_exact": True, "n_cycles": sim.tb.n_cycles}
        hw = header_words(c.word)
        rec["requests"] = []
        for i, (m, st, cyc) in enumerate(
            zip(sim.msgs, sim.statuses, cycles, strict=True)
        ):
            rec["requests"].append(
                {
                    "op": int(m.op),
                    "k": m.k,
                    "n": m.n,
                    "nfollow": m.nfollow,
                    "status": st.name,
                    "prev_status": sim.statuses[i - 1].name if i else "",
                    "w_in": hw + len(sim.layout[i][1]),
                    "w_prev": hw + len(sim.layout[i - 1][1]) if i else None,
                    "reply_cycle": cyc,
                    "interval": cyc - cycles[i - 1] if i else None,
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
    "CgVectorRx": "cg_vector_rx_task",
    "CgVectorLoad": "cg_vector_load_task",
    "CgVectorCore": "cg_vector_task",
    "CgVectorStore": "cg_vector_store_task",
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
    out[cg_cost.CHANNELS] = {
        k: int(rec["resources"]["integration"].get(k, 0)) for k in COUNTERS
    }
    out["unit"] = {k: sum(out[t][k] for t in out) for k in COUNTERS}
    return out


def merge() -> None:
    builds, modules, cycles = [], [], []
    for rec in records():
        builds.append(
            {
                "build": rec["build"],
                "role": rec["role"],
                **rec["config"],
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
            for i, q in enumerate(rec["requests"]):
                cycles.append(
                    {"build": rec["build"], "role": rec["role"], "request": i, **q}
                )
    for name, rows in (
        ("cg_builds.csv", builds),
        ("cg_modules.csv", modules),
        ("cg_cycles.csv", cycles),
    ):
        with (PAPER / name).open("w", encoding="utf-8", newline="") as f:
            f.write(
                f"# {name}: step 8.4 of plans/mimo_cg/mimo_cg_paper_sims.md; tool={TOOL}; part={PART}; period_ns={PERIOD_NS}\n"
            )
            keys = list(dict.fromkeys(k for r in rows for k in r))
            w = csv.DictWriter(f, fieldnames=keys, restval="", lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        print(f"wrote paper_data/{name}: {len(rows)} rows")


# --- the fit -------------------------------------------------------------------------------------


def _interval_rows(recs: list[dict]) -> tuple[list, list, list]:
    """Served requests after a served one (features and interval); rejected requests after a
    served one and after a rejection (``w_in`` and interval)."""
    served, after_served, after_reject = [], [], []
    for rec in recs:
        unit = CgConfig(**rec["config"]).build()
        for q in rec["requests"]:
            if q["interval"] is None:
                continue
            prev_ok = q["prev_status"] == "OK"
            if q["status"] == "OK":
                if prev_ok:
                    feats = cg_cost.message_features(unit, q["op"], q["k"], q["n"])
                    served.append(
                        {**feats, "interval": q["interval"], "build": rec["build"]}
                    )
            else:
                row = {
                    "w_in": q["w_in"],
                    "w_prev": q["w_prev"],
                    "interval": q["interval"],
                    "build": rec["build"],
                }
                (after_served if prev_ok else after_reject).append(row)
    return served, after_served, after_reject


def _fit_all(recs: list[dict]) -> tuple[dict, dict, dict]:
    """Task resource models, the channel model and the message model from ``recs``."""
    samples: dict = {t: [] for t in cg_cost.TASKS}
    chan_rows = []
    for rec in recs:
        unit = CgConfig(**rec["config"]).build()
        rows = unit_rows(rec)
        for comp in (unit.rx, unit.load, unit.core, unit.store):
            samples[cost.task_of(comp)].append((comp, rows[cost.task_of(comp)]))
        chan_rows.append({"unit": unit, **rows[cg_cost.CHANNELS]})
    models = {
        task: cost.new_model(task, type(smp[0][0])).fit(samples=smp)
        for task, smp in samples.items()
    }
    msg = cg_cost.fit_message_model(*_interval_rows(recs))
    return models, cg_cost.fit_channels(chan_rows), msg


def fit() -> None:
    split = load_split()
    recs = [r for r in records("fit") if r["error"] is None]
    if len(recs) != len(split["fit"]):
        raise SystemExit(
            f"{len(recs)} clean calibration records of {len(split['fit'])}"
        )
    models, chan, msg = _fit_all(recs)
    pdir = cost.platform_dir()
    files = []
    for task, m in models.items():
        path = pdir / "models" / task / "params.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        m.save_model(path)
        files.append(path)
    for path, obj in (
        (pdir / "models" / cg_cost.CHANNELS / "params.json", chan),
        (pdir / "components" / cg_cost.COMPONENT / "params.json", msg),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(obj, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        files.append(path)
    prov_path = pdir / "provenance.json"
    prov = json.loads(prov_path.read_text(encoding="utf-8"))
    prov["cg_vector"] = {
        "tool": TOOL,
        "builds": [r["build"] for r in recs],
        "step": "8.4",
        "version": 1,
    }
    prov_path.write_text(json.dumps(prov, indent=1) + "\n", encoding="utf-8")
    report = {"loo": _loo(recs), "n_builds": len(recs)}
    report["sha256"] = {
        str(f.relative_to(pdir)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in files
    }
    _write_kv(PAPER / "cg_fit_report.csv", _flatten(report), "step 8.4 fit, model v1")
    print(json.dumps(report, indent=1))


def _predict(
    unit: CgVectorUnit, models: dict | None = None, chan: dict | None = None
) -> dict:
    """Per task, the channels and the unit total, from given models (or the packaged ones)."""
    if models is None:
        p = cg_cost.predict_unit(unit)
        return {**p, "unit": p["total"]}
    out = {}
    for comp in (unit.rx, unit.load, unit.core, unit.store):
        pred = models[cost.task_of(comp)].predict(comp)
        out[cost.task_of(comp)] = {k: float(pred.get(k, 0.0)) for k in COUNTERS}
    out[cg_cost.CHANNELS] = cg_cost.predict_channels(unit, chan)
    out["unit"] = {k: sum(out[t][k] for t in out) for k in COUNTERS}
    return out


def _loo(recs: list[dict]) -> dict:
    """Leave-one-build-out errors on the calibration builds: unit and core LUT/FF, intervals."""
    errs: dict = {
        k: [] for k in ("unit_lut", "unit_ff", "core_lut", "core_ff", "interval")
    }
    for i, rec in enumerate(recs):
        models, chan, msg = _fit_all(recs[:i] + recs[i + 1 :])
        pred = _predict(CgConfig(**rec["config"]).build(), models, chan)
        meas = unit_rows(rec)
        for k in ("lut", "ff"):
            errs[f"unit_{k}"].append(_ape(pred["unit"][k], meas["unit"][k]))
            errs[f"core_{k}"].append(
                _ape(pred["cg_vector_task"][k], meas["cg_vector_task"][k])
            )
        for row in _interval_rows([rec])[0]:
            errs["interval"].append(
                _ape(cg_cost.message_interval(msg, row), row["interval"])
            )
    return {
        k: {"mean_pct": float(np.mean(v)), "max_pct": float(np.max(v))}
        for k, v in errs.items()
    }


# --- scoring -------------------------------------------------------------------------------------


def _run_8_3_formats(run_name: str):
    from tests.linalg.test_cg_vector_unit import UNITS

    return UNITS[run_name]


def _score_8_3(msg: dict) -> dict:
    """The step 8.3 runs: the span from the first reply to the last against the sum of the
    predicted intervals of the requests after the first (rebuilt from the test's scenarios).
    """
    from tests.linalg.test_cg_vector_unit import make_sim

    with RUN_8_3.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(line for line in f if not line.startswith("#")))
    out = {}
    for name in dict.fromkeys(r["run"] for r in rows):
        runs = [r for r in rows if r["run"] == name]
        sim = make_sim(name)
        unit = CgVectorUnit(name="u", sim=Simulation(), **_run_8_3_formats(name))
        hw = header_words(int(unit.word_bits))
        pred = 0.0
        for i in range(1, len(runs)):
            m, st = sim.msgs[i], sim.statuses[i]
            assert st.name == runs[i]["status"], (name, i)
            if st == Status.OK:
                feats = cg_cost.message_features(unit, int(m.op), m.k, m.n)
                pred += cg_cost.message_interval(msg, feats)
            else:
                pred += cg_cost.reject_interval(
                    msg,
                    w_in=hw + len(sim.layout[i][1]),
                    w_prev=hw + len(sim.layout[i - 1][1]),
                    after_served=sim.statuses[i - 1] == Status.OK,
                )
        meas = int(runs[-1]["reply_cycle"]) - int(runs[0]["reply_cycle"])
        out[name] = {
            "measured": meas,
            "predicted": round(pred, 1),
            "ape_pct": round(_ape(pred, meas), 2),
        }
    return out


def score(role: str) -> tuple[dict, list, list]:
    """The packaged models against the builds of ``role``: metrics, resource rows, cycle rows."""
    msg = cg_cost.message_model()
    rows, cyc, rej = [], [], {"after_served": [], "after_reject": []}
    for rec in records(role):
        if rec["error"] is not None:
            rows.append({"build": rec["build"], "error": rec["error"][:200]})
            continue
        unit = CgConfig(**rec["config"]).build()
        pred, meas = _predict(unit), unit_rows(rec)
        row = {"build": rec["build"]}
        for what in ("unit", "cg_vector_task"):
            tag = "unit" if what == "unit" else "core"
            for k in COUNTERS:
                row[f"{tag}_{k}"] = meas[what][k]
                row[f"{tag}_{k}_pred"] = round(pred[what][k], 1)
        rows.append(row)
        served, after_served, after_reject = _interval_rows([rec])
        for s in served:
            p = cg_cost.message_interval(msg, s)
            cyc.append(
                {
                    "build": rec["build"],
                    "start": s["start"],
                    "interval": s["interval"],
                    "pred": round(p, 1),
                    "ape_pct": round(_ape(p, s["interval"]), 2),
                }
            )
        for key, rs, ctx in (
            ("after_served", after_served, True),
            ("after_reject", after_reject, False),
        ):
            for s in rs:
                p = cg_cost.reject_interval(
                    msg, w_in=s["w_in"], w_prev=s["w_prev"], after_served=ctx
                )
                rej[key].append(_ape(p, s["interval"]))
    ok = [r for r in rows if "error" not in r]
    metrics: dict = {"n_builds": len(rows), "n_clean": len(ok)}
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
    for key, v in rej.items():
        if v:
            metrics[f"reject_{key}_mape_pct"] = float(np.mean(v))
            metrics[f"reject_{key}_max_pct"] = float(np.max(v))
    metrics["run_8_3"] = _score_8_3(msg)
    metrics["ac8"] = {
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
        "run_8_3": all(
            v["ape_pct"] <= MAX_CYCLE_MAPE for v in metrics["run_8_3"].values()
        ),
    }
    return metrics, rows, cyc


def validate() -> dict:
    """Score the packaged models on the held-out builds and on the step 8.3 runs (AC8)."""
    metrics, rows, cyc = score("holdout")
    for name, data in (("cg_validation.csv", rows), ("cg_validation_cycles.csv", cyc)):
        with (PAPER / name).open("w", encoding="utf-8", newline="") as f:
            f.write(f"# {name}: step 8.4, model v1; tool={TOOL}\n")
            keys = list(dict.fromkeys(k for r in data for k in r))
            w = csv.DictWriter(f, fieldnames=keys, lineterminator="\n")
            w.writeheader()
            w.writerows(data)
    _write_kv(
        PAPER / "cg_validation_metrics.csv", _flatten(metrics), "step 8.4, model v1"
    )
    print(json.dumps(metrics, indent=1))
    return metrics


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="step 8.4: the CG vector unit's cost model"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("split").add_argument("--check", action="store_true")
    rp = sub.add_parser("run")
    rp.add_argument("--role", required=True, choices=("fit", "holdout"))
    rp.add_argument("--shard")
    for cmd in ("merge", "fit", "validate"):
        sub.add_parser(cmd)
    a = ap.parse_args(argv)
    if a.cmd == "split":
        text = split_text()
        if a.check:
            same = SPLIT.read_text(encoding="utf-8") == text
            print(f"{SPLIT.name}:", "unchanged" if same else "DIFFERS")
            return 0 if same else 1
        SPLIT.write_text(text, encoding="utf-8")
        print(f"wrote {SPLIT.relative_to(EX)}: {text.count(chr(10)) - 1} builds")
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
