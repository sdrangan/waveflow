"""mf_measure.py -- the matched-filter unit at RTL, across workloads and builds -> measured.json.

    python -m examples.vecunit_mf.mf_measure --root /tmp/vu_mf_m                  # every run
    python -m examples.vecunit_mf.mf_measure --root /tmp/vu_mf_m --builds mf_v256 # one build's

The catalogue of ``plans/vecunit_new.md`` Stage 9c, as ``examples/systolic/systolic_measure.py`` is the
systolic engine's.  Each run (:func:`waveflow.vecunit.measure.measure_run`) is the unit's harness in
pysim and through csynth and XSI -- every output checked -- recording each job's status cycle, pysim's,
csynth's resources and whether the 4 ns clock is met, the execute task's loop schedule and each job's
bounds (``report.job_bounds``: the loader's, the execute task's, the store's).  The builds
(:data:`waveflow.vecunit.calib.BUILDS`): as built (``VLMAX 64``), 128-bit words, ``VLMAX`` 16 and
256, and ``L 8``.  The workloads: ``steady`` (four frames of four chunks) at 8, 4 and 1 antennas
(at one, ``X`` and ``R`` both land a value a cycle), plus the calibration's pre-registered jobs
(Stage 9d).  A build's runs share its root, so csynth and the XSI build run once a build.  A run
already in ``measured.json`` is skipped unless ``--force``.  ``docs/examples/vecunit_mf/timing.md``
is written from what this records.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from waveflow.vecunit.calib import BUILDS, build_unit
from waveflow.vecunit.measure import measure_run

HERE = Path(__file__).resolve().parent
MEASURED = HERE / "measured.json"

#: build -> its workloads, in run order.
RUNS = {
    "mf": ("steady", "steady_nrx4", "steady_nrx1", "calib", "holdout2"),
    "mf_w128": ("steady", "steady_nrx1"),
    "mf_v16": ("steady",),
    "mf_v256": ("steady", "calib"),
    "mf_l8": ("steady",),
}
#: Runs also traced, for the trace report (which task binds).
TRACED = {("mf", "steady"), ("mf_v256", "steady")}


def _load() -> dict:
    if MEASURED.exists():
        return json.loads(MEASURED.read_text(encoding="utf-8"))
    return {"target": {"part": "xczu48dr-ffvg1517-2-e", "period_ns": 4.0}, "runs": {}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, type=Path, help="a build root per build under it")
    ap.add_argument("--builds", nargs="+", default=list(RUNS), choices=list(RUNS))
    ap.add_argument("--force", action="store_true", help="re-run runs already recorded")
    a = ap.parse_args(argv)
    data = _load()
    ok = True
    for build in a.builds:
        for kind in RUNS[build]:
            name = f"{build}_{kind}"
            if name in data["runs"] and data["runs"][name].get("exact") and not a.force:
                continue
            r = measure_run(build_unit(build), kind, a.root.resolve() / build,
                            trace=(build, kind) in TRACED)
            r.update({"build": build, "params": BUILDS[build][1],
                      "date": datetime.now().astimezone().date().isoformat()})
            data = _load()  # another process may have filed runs meanwhile (one per build)
            data["runs"][name] = r
            data["runs"] = dict(sorted(data["runs"].items()))
            MEASURED.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
            ok &= bool(r["exact"])
            print(name, "exact" if r["exact"] else f"FAILED {r['failed']}",
                  r.get("intervals"), r.get("meets_clock"), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
