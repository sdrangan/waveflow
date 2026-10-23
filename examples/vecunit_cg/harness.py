"""The CG-configured vector unit in its own harness: pysim + golden, csynth, XSI
(``plans/vecunit_new.md``, Stages 4-5).

    python -m examples.vecunit_cg.harness --root /tmp/vu_cg --through golden     # pysim only
    python -m examples.vecunit_cg.harness --root /tmp/vu_cg --through compare    # + csynth, XSI
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from examples.vecunit_cg.cg_unit import CgVecUnit
from waveflow.simulation.simulation import Simulation
from waveflow.vecunit.build import build_config, build_dag


def unit(**kw) -> CgVecUnit:
    return CgVecUnit(name="vecunit", sim=Simulation(), **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--through", default="compare")
    ap.add_argument("--workload", default="test")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    res = build_dag(unit(), workload=a.workload, trace=a.trace).run(build_config(root),
                                                                     through=a.through)
    ok = True
    for name, r in res.items():
        print(f"{name:24} {'ok' if r.success else 'FAIL'}  {'' if r.success else r.message[:3000]}")
        ok &= r.success
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
