"""calibrate.py — from a measured corpus to fitted, persisted cost models, under pre-registration.

Four stages, each a function and a sub-command (``python -m waveflow.cpu.calib.calibrate <stage>``):

``energy``
    Run McPAT on every corpus row that has no energy yet: ``energy_pj = E(region) - E(empty region)``,
    the same subtraction the cycles get.
``fit``
    Fit each family's cycle and energy model on its **fit** rows only, save them under
    ``cpu/models/<family>/``, and write the **validation** report (``cpu/validation.csv``).  Model
    structure (:data:`FAMILIES`) may be revised on this report and nothing else.
``test``
    Evaluate the **test** rows once and write ``cpu/accuracy.csv``.  Refuses to run a second time:
    a model changed after its test evaluation needs a freshly registered test set
    (``plans/cpu_model.md`` rule 11).
``area``
    Run McPAT on every configuration of ``cpu/area_plan.csv`` and fit the area and leakage models
    on its fit rows (validation and test reported the same way).

Accuracy is ``|predicted - measured| / measured`` per point; a family passes at median <= 10 % and
max <= 25 % (``plans/cpu_model.md`` §2).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]  # no pandas-stubs in the dev deps

from waveflow.calib.calib import LinCalibModel
from waveflow.cpu.calib.gem5 import Gem5Runner, pick_stats
from waveflow.cpu.calib.kernels import EMPTY, KERNELS
from waveflow.cpu.calib.mcpat import MCPAT_COMMIT, TECH_NODE_NM, Mcpat, McpatConfig

MEDIAN_BOUND = 0.10
MAX_BOUND = 0.25
TARGETS = ("cycles", "energy_pj")


def _gather_features(row: dict) -> dict:
    """``gather_hist``'s basis: increments, and increments weighted by the share of the working set
    beyond L1 and beyond L2 -- the expected misses of a uniform random access, from regime features
    alone (no cache sizes), so the form carries to another configuration."""
    n, ws = float(row["n"]), float(row["ws"])
    frac1 = float(row["ws_over_l1"]) / ws if ws > 0 else 0.0
    frac2 = float(row["ws_over_l2"]) / ws if ws > 0 else 0.0
    return {"n": n, "n_l1": n * frac1, "n_l2": n * frac2}


@dataclass(frozen=True)
class Family:
    """One cost-model family: a kernel (or one operation of it) and its model's form."""

    name: str
    kernel: str
    #: The raw feature columns the model reads (and records ranges over).
    raw: tuple[str, ...]
    #: The basis names (equal to *raw* when there is no transform).
    basis: tuple[str, ...]
    op: str | None = None
    transform: Callable[[dict], dict] | None = None
    informational: bool = False

    def select(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df[df["kernel"] == self.kernel]
        if self.op is not None:
            df = df[df["point"].map(lambda p: json.loads(p).get("op")) == self.op]
        return df

    def model(self, target: str) -> LinCalibModel:
        return LinCalibModel(
            basis=list(self.basis),
            target=target,
            name=f"{self.name}.{target}",
            transform_fn=self.transform,
        )


_SCHED = ("n_scanned", "n_moved", "n_tasks")
#: The model forms, fixed before the validation report is read; revisions are logged in the plan.
FAMILIES: tuple[Family, ...] = (
    *(
        Family(f"sched_ops.{op}", "sched_ops", _SCHED, _SCHED, op=op)
        for op in ("add", "delete", "reprio", "sort")
    ),
    Family(
        "cdot_q15",
        "cdot_q15",
        ("n", "ws_over_l1", "ws_over_l2"),
        ("n", "ws_over_l1", "ws_over_l2"),
    ),
    Family(
        "gather_hist",
        "gather_hist",
        ("n", "ws", "ws_over_l1", "ws_over_l2"),
        ("n", "n_l1", "n_l2"),
        transform=_gather_features,
    ),
    Family("dispatch", "dispatch", ("n_dispatch",), ("n_dispatch",)),
    Family("ctx_switch", "ctx_switch", ("n_switches",), ("n_switches",)),
    Family(
        "swapcontext",
        "swapcontext",
        ("n_switches",),
        ("n_switches",),
        informational=True,
    ),
)


def load_corpus(cpu_dir: Path) -> pd.DataFrame:
    frames = [
        pd.read_csv(p) for k in KERNELS if (p := cpu_dir / k / "corpus.csv").is_file()
    ]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def rel_errors(model: LinCalibModel, df: pd.DataFrame, target: str) -> np.ndarray:
    pred = np.array([model.predict_feat(r) for r in df.to_dict("records")], dtype=float)
    meas = df[target].to_numpy(dtype=float)
    return np.abs(pred - meas) / np.abs(meas)


# ---------------------------------------------------------------------------
# energy
# ---------------------------------------------------------------------------


def annotate_energy(platform_dir: str | Path, *, workers: int = 4, log=print) -> int:
    """Add ``energy_pj`` (and its provenance) to every corpus row that lacks it; return the count."""
    cpu_dir = Path(platform_dir) / "cpu"
    mcpat = Mcpat()
    why = mcpat.unavailable()
    if why:
        raise RuntimeError(why)
    runner = Gem5Runner()
    cfg0 = runner.config
    cfg = McpatConfig(
        n_cores=cfg0.num_cores,
        f_clk_hz=cfg0.f_clk_hz,
        l1i_bytes=cfg0.l1i_bytes,
        l1d_bytes=cfg0.l1d_bytes,
        l2_bytes=cfg0.l2_bytes,
    )
    _, empty_stats, _ = runner.run(EMPTY, {})
    e_empty = mcpat.region_energy_pj(cfg, pick_stats(empty_stats))
    log(f"empty region: {e_empty:.1f} pJ")
    total = 0
    for kernel in KERNELS:
        path = cpu_dir / kernel / "corpus.csv"
        if not path.is_file():
            continue
        df = pd.read_csv(path)
        need = df.index if "energy_pj" not in df else df.index[df["energy_pj"].isna()]
        if len(need) == 0:
            continue
        rows = [df.loc[i].to_dict() for i in need]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            raw = list(pool.map(lambda r: mcpat.region_energy_pj(cfg, r), rows))
        df.loc[need, "energy_pj_raw"] = raw
        df.loc[need, "empty_region_energy_pj"] = e_empty
        df.loc[need, "energy_pj"] = np.array(raw) - e_empty
        df.loc[need, "mcpat_commit"] = MCPAT_COMMIT
        df.loc[need, "mcpat_node_nm"] = TECH_NODE_NM
        df.to_csv(path, index=False)
        total += len(need)
        log(f"{kernel}: {len(need)} rows")
    return total


# ---------------------------------------------------------------------------
# fit + validation
# ---------------------------------------------------------------------------


def _fit_frame(fam: Family, df: pd.DataFrame, target: str) -> pd.DataFrame:
    return df[list(fam.raw) + [target]].astype(float)


def _summ(errs: np.ndarray) -> dict[str, float]:
    return {
        "n": len(errs),
        "median": float(np.median(errs)) if len(errs) else float("nan"),
        "max": float(np.max(errs)) if len(errs) else float("nan"),
    }


def fit_and_validate(platform_dir: str | Path, *, log=print) -> pd.DataFrame:
    """Fit every family on its fit rows, save the models, return (and write) the validation report."""
    cpu_dir = Path(platform_dir) / "cpu"
    corpus = load_corpus(cpu_dir)
    rows = []
    for fam in FAMILIES:
        df = fam.select(corpus)
        fit, val = df[df["role"] == "fit"], df[df["role"] == "validation"]
        for target in TARGETS:
            model = fam.model(target).fit(_fit_frame(fam, fit, target))
            model.save_model(cpu_dir / "models" / fam.name / f"{target}.json")
            fit_s = _summ(rel_errors(model, fit, target))
            val_s = (
                _summ(rel_errors(model, val, target))
                if len(val)
                else _summ(np.array([]))
            )
            ok = len(val) == 0 or (
                val_s["median"] <= MEDIAN_BOUND and val_s["max"] <= MAX_BOUND
            )
            rows.append(
                {
                    "family": fam.name,
                    "target": target,
                    "coeffs": json.dumps(model.coeffs),
                    "fit_n": fit_s["n"],
                    "fit_median": fit_s["median"],
                    "fit_max": fit_s["max"],
                    "val_n": val_s["n"],
                    "val_median": val_s["median"],
                    "val_max": val_s["max"],
                    "val_pass": ok,
                    "informational": fam.informational,
                }
            )
            log(
                f"{fam.name:18s} {target:9s} fit med {fit_s['median']:.3f} max {fit_s['max']:.3f} | "
                f"val med {val_s['median']:.3f} max {val_s['max']:.3f} {'ok' if ok else 'MISS'}"
            )
    report = pd.DataFrame(rows)
    report.to_csv(cpu_dir / "validation.csv", index=False)
    return report


# ---------------------------------------------------------------------------
# test, once
# ---------------------------------------------------------------------------


class TestAlreadyEvaluated(RuntimeError):
    """The test set has been evaluated; a second evaluation needs a fresh registration."""


def evaluate_test(
    platform_dir: str | Path, *, commit: str = "", log=print
) -> pd.DataFrame:
    """Evaluate the saved models on the test rows, once; write ``cpu/accuracy.csv``."""
    cpu_dir = Path(platform_dir) / "cpu"
    out = cpu_dir / "accuracy.csv"
    if out.exists():
        raise TestAlreadyEvaluated(f"{out} exists: the test set was already evaluated")
    corpus = load_corpus(cpu_dir)
    rows = []
    for fam in FAMILIES:
        test = fam.select(corpus)
        test = test[test["role"] == "test"]
        for target in TARGETS:
            model = fam.model(target)
            if (
                model.load_model(cpu_dir / "models" / fam.name / f"{target}.json")
                is None
            ):
                raise RuntimeError(
                    f"no fitted model for {fam.name} {target}: run 'fit' first"
                )
            for rec, err in zip(
                test.to_dict("records"), rel_errors(model, test, target)
            ):
                rows.append(
                    {
                        "family": fam.name,
                        "target": target,
                        "point": rec["point"],
                        "measured": rec[target],
                        "predicted": model.predict_feat(rec),
                        "rel_err": err,
                        "level": model.confidence_feat(rec).level.value,
                        "informational": fam.informational,
                        "evaluated_at": commit,
                    }
                )
    acc = pd.DataFrame(rows)
    acc.to_csv(out, index=False)
    for (fam, target), g in acc.groupby(["family", "target"], sort=False):
        s = _summ(g["rel_err"].to_numpy())
        ok = s["median"] <= MEDIAN_BOUND and s["max"] <= MAX_BOUND
        log(
            f"{fam:18s} {target:9s} test n {s['n']:2d} med {s['median']:.3f} max {s['max']:.3f} {'PASS' if ok else 'FAIL'}"
        )
    return acc


def summarize(acc: pd.DataFrame) -> pd.DataFrame:
    """Per family and target: n, median, max, pass."""
    out = []
    for (fam, target), g in acc.groupby(["family", "target"], sort=False):
        s = _summ(g["rel_err"].to_numpy())
        out.append(
            {
                "family": fam,
                "target": target,
                **s,
                "pass": s["median"] <= MEDIAN_BOUND and s["max"] <= MAX_BOUND,
                "informational": bool(g["informational"].iloc[0]),
            }
        )
    return pd.DataFrame(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "stage", choices=("energy", "fit", "test", "area", "area-fit", "area-test")
    )
    ap.add_argument("--platform-dir", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--commit", default="", help="the commit the test evaluation runs at"
    )
    args = ap.parse_args(argv)
    log: Any = lambda s: print(s, flush=True)
    if args.stage == "energy":
        annotate_energy(args.platform_dir, workers=args.workers, log=log)
    elif args.stage == "fit":
        fit_and_validate(args.platform_dir, log=log)
    elif args.stage == "test":
        evaluate_test(args.platform_dir, commit=args.commit, log=log)
    elif args.stage == "area":
        area_campaign(args.platform_dir, workers=args.workers, log=log)
    elif args.stage == "area-fit":
        fit_area(args.platform_dir, log=log)
    else:
        evaluate_area_test(args.platform_dir, commit=args.commit, log=log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# area
# ---------------------------------------------------------------------------

#: The raw configuration features an area or leakage model records ranges over.
AREA_RAW = ("n_cores", "l1i_kb", "l1d_kb", "l2_kb")
AREA_TARGETS = ("area_mm2", "leak_mw")


def _area_features(row: dict) -> dict:
    """Private cores with their L1s, plus one shared L2: the structure of a cluster."""
    c = float(row["n_cores"])
    return {
        "n_cores": c,
        "core_l1_kb": c * (float(row["l1i_kb"]) + float(row["l1d_kb"])),
        "l2_kb": float(row["l2_kb"]),
    }


def area_model(target: str) -> LinCalibModel:
    return LinCalibModel(
        basis=["n_cores", "core_l1_kb", "l2_kb"],
        target=target,
        name=f"area.{target}",
        transform_fn=_area_features,
    )


def area_campaign(
    platform_dir: str | Path, *, workers: int = 4, log=print
) -> pd.DataFrame:
    """McPAT on every configuration of the committed ``area_plan.csv``; write ``cpu/area/corpus.csv``."""
    from waveflow.cpu.calib.prereg import require_committed

    cpu_dir = Path(platform_dir) / "cpu"
    plan_path = cpu_dir / "area_plan.csv"
    prereg = require_committed(plan_path)
    plan = pd.read_csv(plan_path)
    mcpat = Mcpat()
    why = mcpat.unavailable()
    if why:
        raise RuntimeError(why)
    f_clk = Gem5Runner().config.f_clk_hz

    def one(r: dict) -> dict:
        cfg = McpatConfig(
            n_cores=int(r["n_cores"]),
            f_clk_hz=f_clk,
            l1i_bytes=int(r["l1_kb"]) * 1024,
            l1d_bytes=int(r["l1_kb"]) * 1024,
            l2_bytes=int(r["l2_kb"]) * 1024,
        )
        res = mcpat.run(cfg, None)
        return {
            "n_cores": int(r["n_cores"]),
            "l1i_kb": int(r["l1_kb"]),
            "l1d_kb": int(r["l1_kb"]),
            "l2_kb": int(r["l2_kb"]),
            "f_mhz": f_clk / 1e6,
            "role": r["role"],
            "area_mm2": res.area_mm2,
            "leak_mw": res.leakage_w * 1e3,
            "mcpat_commit": MCPAT_COMMIT,
            "mcpat_node_nm": TECH_NODE_NM,
            "prereg_commit": prereg,
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(one, plan.to_dict("records")))
    df = pd.DataFrame(rows)
    out = cpu_dir / "area" / "corpus.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    log(f"area: {len(df)} configurations")
    return df


def fit_area(platform_dir: str | Path, *, log=print) -> pd.DataFrame:
    """Fit area and leakage on the fit configurations; write ``cpu/area/validation.csv``."""
    cpu_dir = Path(platform_dir) / "cpu"
    df = pd.read_csv(cpu_dir / "area" / "corpus.csv")
    fit, val = df[df["role"] == "fit"], df[df["role"] == "validation"]
    rows = []
    for target in AREA_TARGETS:
        model = area_model(target).fit(fit[list(AREA_RAW) + [target]].astype(float))
        model.save_model(cpu_dir / "models" / "area" / f"{target}.json")
        f_s, v_s = _summ(rel_errors(model, fit, target)), _summ(
            rel_errors(model, val, target)
        )
        ok = v_s["median"] <= MEDIAN_BOUND and v_s["max"] <= MAX_BOUND
        rows.append(
            {
                "family": "area",
                "target": target,
                "coeffs": json.dumps(model.coeffs),
                "fit_n": f_s["n"],
                "fit_median": f_s["median"],
                "fit_max": f_s["max"],
                "val_n": v_s["n"],
                "val_median": v_s["median"],
                "val_max": v_s["max"],
                "val_pass": ok,
            }
        )
        log(
            f"area {target:9s} fit med {f_s['median']:.4f} max {f_s['max']:.4f} | "
            f"val med {v_s['median']:.4f} max {v_s['max']:.4f} {'ok' if ok else 'MISS'}"
        )
    report = pd.DataFrame(rows)
    report.to_csv(cpu_dir / "area" / "validation.csv", index=False)
    return report


def evaluate_area_test(
    platform_dir: str | Path, *, commit: str = "", log=print
) -> pd.DataFrame:
    """Evaluate the area and leakage models on the test configurations, once."""
    cpu_dir = Path(platform_dir) / "cpu"
    out = cpu_dir / "area" / "accuracy.csv"
    if out.exists():
        raise TestAlreadyEvaluated(
            f"{out} exists: the area test set was already evaluated"
        )
    df = pd.read_csv(cpu_dir / "area" / "corpus.csv")
    test = df[df["role"] == "test"]
    rows = []
    for target in AREA_TARGETS:
        model = area_model(target)
        if model.load_model(cpu_dir / "models" / "area" / f"{target}.json") is None:
            raise RuntimeError(
                f"no fitted area model for {target}: run 'area-fit' first"
            )
        for rec, err in zip(test.to_dict("records"), rel_errors(model, test, target)):
            rows.append(
                {
                    "family": "area",
                    "target": target,
                    "point": json.dumps({k: rec[k] for k in AREA_RAW}),
                    "measured": rec[target],
                    "predicted": model.predict_feat(rec),
                    "rel_err": err,
                    "level": model.confidence_feat(rec).level.value,
                    "informational": False,
                    "evaluated_at": commit,
                }
            )
    acc = pd.DataFrame(rows)
    acc.to_csv(out, index=False)
    for target, g in acc.groupby("target", sort=False):
        s = _summ(g["rel_err"].to_numpy())
        ok = s["median"] <= MEDIAN_BOUND and s["max"] <= MAX_BOUND
        log(
            f"area {target:9s} test n {s['n']:2d} med {s['median']:.4f} max {s['max']:.4f} {'PASS' if ok else 'FAIL'}"
        )
    return acc
