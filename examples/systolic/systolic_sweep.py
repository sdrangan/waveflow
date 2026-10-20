"""systolic_sweep.py -- csynth the systolic engine over a spread of configurations -> resources.json.

    python -m examples.systolic.systolic_sweep --root C:/wfsc/sw                  # every point
    python -m examples.systolic.systolic_sweep --root C:/wfsc/sw --points small centre

Each point is one ``SystolicEngine`` configuration, synthesized alone (csynth only, no simulation):
its resources and estimated clock from csynth's own XML.  The points vary one parameter at a time
from the centre -- the array (``R``, ``C``), the lanes (``L``), the maxima, the word width -- plus a
few corners; they are the builds a resource model over the engine's parameters is fitted and scored
on (docs/examples/systolic/resource.md).  A point already in ``resources.json`` is skipped unless
``--force``.  Build from a short root on Windows: past 260 characters a csynth path fails silently.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from examples.systolic import engine_harness as EH
from examples.systolic.systolic_measure import _resources

HERE = Path(__file__).resolve().parent
RESOURCES = HERE / "resources.json"

CENTRE = dict(Mmax=8, Kmax=8, Nmax=32, Pmax=4, R=4, C=8, L=4, word_bits=64)

#: name -> the parameters that differ from the centre.
POINTS = {
    "centre": {},
    "w128": {"word_bits": 128},
    "small": {"R": 2, "C": 4, "L": 2, "Mmax": 4, "Nmax": 16},      # a quarter of the array
    "r2": {"R": 2, "L": 2},
    "r8": {"R": 8, "L": 8, "Mmax": 16},
    "c4": {"C": 4},
    "c16": {"C": 16, "Nmax": 64},
    "l8": {"L": 8},
    "k4": {"Kmax": 4},
    "k16": {"Kmax": 16},
    "p1": {"Pmax": 1},
    "p8": {"Pmax": 8},
    "m16": {"Mmax": 16},
    "n64": {"Nmax": 64},
    "big": {"R": 8, "C": 16, "L": 8, "Mmax": 16, "Nmax": 64, "word_bits": 128},
}


def synth(name: str, root: Path) -> dict:
    cfg = {**CENTRE, **POINTS[name]}
    run_root = root / name
    run_root.mkdir(parents=True, exist_ok=True)
    res = EH.build_dag(cfg).run(EH.build_config(run_root), through="csynth")
    bad = {k: r.message[-500:] for k, r in res.items() if not r.success}
    out = {"params": cfg, "tool": EH.build_config(run_root).vitis_version, "ok": not bad,
           "failed": bad or None, "date": date.today().isoformat()}
    if not bad:
        out["resources"] = _resources(run_root / f"{EH.KERNEL}_proj")
        out["meets_clock"] = out["resources"]["est_clock_ns"] <= EH.PERIOD_NS
    return out


#: The points held out of the fit, to score it: a smaller array and a larger one.
HOLDOUT = ("c4", "r8")
#: Each resource's features: the elements (R·C) and, for the logic, the drain groups (R·C/L).
FEATURES = {"dsp": ("rc",), "lut": ("rc", "groups"), "ff": ("rc", "groups")}


def _features(params: dict) -> dict:
    rc = params["R"] * params["C"]
    return {"rc": rc, "groups": rc / params["L"]}


def fit(points: dict) -> dict:
    """Least squares of each resource on its features over every point but :data:`HOLDOUT`; the
    held-out points' errors score it."""
    import numpy as np

    out = {}
    train = {n: r for n, r in points.items() if r.get("ok") and n not in HOLDOUT}
    held = {n: r for n, r in points.items() if r.get("ok") and n in HOLDOUT}
    for res, feats in FEATURES.items():
        X = np.asarray([[1.0] + [_features(r["params"])[f] for f in feats] for r in train.values()])
        y = np.asarray([r["resources"][res] for r in train.values()], float)
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)

        def pred(r):
            f = _features(r["params"])
            return float(coef[0] + sum(c * f[k] for c, k in zip(coef[1:], feats)))

        err = {n: round(100 * (pred(r) - r["resources"][res]) / r["resources"][res], 1)
               for n, r in held.items()}
        train_err = max(abs(pred(r) - r["resources"][res]) / r["resources"][res]
                        for r in train.values())
        out[res] = {"intercept": round(float(coef[0]), 2),
                    **{f: round(float(c), 2) for f, c in zip(feats, coef[1:])},
                    "train_max_err_pct": round(100 * train_err, 1), "holdout_err_pct": err}
    return {"features": {k: list(v) for k, v in FEATURES.items()}, "holdout": list(HOLDOUT),
            "params": out}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, help="a short build root (Windows path length)")
    ap.add_argument("--points", nargs="+", default=list(POINTS), choices=list(POINTS))
    ap.add_argument("--force", action="store_true", help="re-synthesize points already recorded")
    ap.add_argument("--fit", action="store_true", help="fit the resource model on what is recorded")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    if a.fit:
        data = json.loads(RESOURCES.read_text(encoding="utf-8"))
        data["fit"] = fit(data["points"])
        print(json.dumps(data["fit"], indent=1))
        RESOURCES.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        return
    data = json.loads(RESOURCES.read_text(encoding="utf-8")) if RESOURCES.exists() else {
        "target": {"part": EH.PART, "period_ns": EH.PERIOD_NS}, "points": {}}
    for name in a.points:
        if name in data["points"] and not a.force:
            continue
        r = synth(name, root)
        data["points"][name] = r
        print(json.dumps({"point": name, **{k: r.get(k) for k in ("ok", "resources")}}), flush=True)
        RESOURCES.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
