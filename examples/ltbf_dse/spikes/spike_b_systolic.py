"""Spike 1.2 b (plans/ltbf_dse/ltbf_dse.md): ``SystolicEngineMem`` at LTBF shapes.

csynth (Vitis HLS 2024.1) of the memory-backed systolic engine sized for an inner dimension of
K = 1024 (``Pmax · Kmax = 1024``), the centre otherwise, 16-bit operands; its resources, estimated
clock, csynth wall time and peak memory go to ``hw/build/spike_b/<name>/result.json`` (local).

    python -m examples.ltbf_dse.spikes.spike_b_systolic --name k1024
"""

from __future__ import annotations

import argparse
import json
import resource
import time
from pathlib import Path

from examples.systolic import engine_mem_harness as EM
from examples.systolic.engine_harness import build_config, reg
from examples.systolic.systolic_measure import _resources

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "hw" / "build" / "spike_b"

#: name -> the parameters that differ from the harness's centre.
POINTS = {
    "centre": {},
    "centre_w16_p128": {"Pmax": 128, "a": reg(16, 4), "b": reg(16, 4), "c": reg(16, 4)},
    "p16": {"Pmax": 16},
    "k1024": {
        "Pmax": 128,
        "word_bits": 128,
        "a": reg(16, 4),
        "b": reg(16, 4),
        "c": reg(16, 4),
    },
    "k1024_k16": {
        "Kmax": 16,
        "Pmax": 64,
        "word_bits": 128,
        "a": reg(16, 4),
        "b": reg(16, 4),
        "c": reg(16, 4),
    },
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", choices=sorted(POINTS), required=True)
    a = ap.parse_args()
    root = OUT / a.name
    root.mkdir(parents=True, exist_ok=True)
    cfg = POINTS[a.name]
    t0 = time.time()
    res = EM.build_dag(cfg).run(build_config(root), through="csynth")
    wall = time.time() - t0
    ok = all(r.success for r in res.values())
    out = {
        "name": a.name,
        "cfg": {k: (str(v) if not isinstance(v, int) else v) for k, v in cfg.items()},
        "ok": ok,
        "wall_s": round(wall, 1),
        "peak_rss_children_mb": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        // 1024,
        "steps": {
            k: (r.success, "" if r.success else r.message[:2000])
            for k, r in res.items()
        },
    }
    if ok:
        xmls = sorted(root.rglob("solution1/syn/report/csynth.xml"))
        out["resources"] = {x.parents[3].name: _resources(x.parents[3]) for x in xmls}
    (root / "result.json").write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str)[:3000])


if __name__ == "__main__":
    main()
