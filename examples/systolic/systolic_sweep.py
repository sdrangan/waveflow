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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, help="a short build root (Windows path length)")
    ap.add_argument("--points", nargs="+", default=list(POINTS), choices=list(POINTS))
    ap.add_argument("--force", action="store_true", help="re-synthesize points already recorded")
    a = ap.parse_args()
    root = Path(a.root).resolve()
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
