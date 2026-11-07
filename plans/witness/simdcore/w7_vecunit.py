"""Witness 7 (plans/simdcore.md, Stage 1): csynth of the generic VecUnit at S = 1 against S = 3, at the
CG layout (L 4, NG 8), Vitis HLS 2025.1 -- separating the chaining cost from the format cost.

Uses ``waveflow.vecunit.calib.resource_config`` / ``measure_resources`` (the resource model's own
csynth path).  ``one`` replaces every vector group's format by the first and every scalar's by the
first: one vector format and one scalar format, so no union datapath.

    ../pysilicon-venv/Scripts/python.exe plans/witness/simdcore/w7_vecunit.py C:/wfsc/w7 s3 s1 s1one
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

from waveflow.simulation.simulation import Simulation
from waveflow.vecunit.build import build_config, build_dag
from waveflow.vecunit.calib import config_features, resource_config
from waveflow.vecunit.report import resources
from waveflow.vecunit.unit import VecUnit

SPECS = {
    "s3": ((4, 8, 3, "all", "cg"), False),
    "s1": ((4, 8, 1, "all", "cg"), False),
    "s1one": ((4, 8, 1, "all", "cg"), True),
    "s3one": ((4, 8, 3, "all", "cg"), True),
}


def cfg_of(name: str):
    spec, one = SPECS[name]
    cfg = resource_config(spec)
    if one:
        f = cfg.group_formats[0]
        cfg = dataclasses.replace(
            cfg,
            group_formats=(f,) * len(cfg.group_formats),
            in_queue_formats=(f,) * len(cfg.in_queue_formats),
            out_queue_formats=(f,) * len(cfg.out_queue_formats),
            scalar_formats=(cfg.scalar_formats[0],) * len(cfg.scalar_formats),
        )
    return cfg


def main() -> None:
    root = Path(sys.argv[1])
    for name in sys.argv[2:]:
        cfg = cfg_of(name)
        r = root / name
        unit = VecUnit(name="vecunit", sim=Simulation(), cfg=cfg)
        res = build_dag(unit).run(build_config(r), through="csynth")
        bad = [f"{n}: {x.message[:400]}" for n, x in res.items() if not x.success]
        row = {"name": name, "spec": list(SPECS[name][0]), "one_format": SPECS[name][1],
               "n_vfmt": len(cfg.vector_formats), "n_sfmt": len(cfg.scalar_format_set),
               "features": config_features(cfg), "bad": bad,
               "csynth": None if bad else resources(r)}
        (root / f"{name}.json").write_text(json.dumps(row, indent=1, default=str))
        print(json.dumps({k: row[k] for k in ("name", "n_vfmt", "n_sfmt", "bad")}), flush=True)
        if not bad:
            print(json.dumps(row["csynth"].get("unit", row["csynth"]), default=str), flush=True)


if __name__ == "__main__":
    main()
