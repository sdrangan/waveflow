"""sched_release.py -- the scheduler's ``release`` operation as an A53 kernel, for its cost model.

``plans/ltbf_dse/ltbf_dse.md`` step 4.6 (AC10; spec §5.6).  When a job finishes, the host decrements
each successor's count of unfinished dependencies and collects the ones that reach zero
(:func:`examples.ltbf_dse.hw.sched.release`).  ``cpu/release.c`` is that loop on a drawn successor
list; :func:`release_kernel` its twin, built on ``sched.release`` itself (the same counters,
``n_succ`` and ``n_ready``); :data:`FAMILY` the cost model's form: cycles linear in both.  Calibrated
like ``ltbf_proj`` (``a53cal``; points in ``waveflow/cpu/calib/sweep.py``'s
:func:`~waveflow.cpu.calib.sweep.release_rows`, committed as ``cpu/sweep_plan_release.csv``).

    python -m examples.ltbf_dse.sched_release plan | smoke | campaign | fit
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from examples.ltbf_dse import a53cal
from examples.ltbf_dse.hw.sched import release
from waveflow.cpu.calib.calibrate import Family
from waveflow.cpu.calib.kernels import Kernel
from waveflow.cpu.calib.kernels.common import Rng

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "cpu" / "release.c"
PLAN = HERE / "cpu" / "sweep_plan_release.csv"
DATA = HERE / "data" / "a53_ltbf"


def release_kernel(succ: int, ready: int = 1, seed: int = 1) -> dict[str, Any]:
    """The twin of ``cpu/release.c``: its successor permutation, ``sched.release`` on it, its JSON."""
    n, n_ready = int(succ), int(ready)
    rng = Rng(int(seed))
    ids = list(range(n))
    for i in range(n - 1, 0, -1):  # Fisher-Yates, as the C
        j = rng.next() % (i + 1)
        ids[i], ids[j] = ids[j], ids[i]
    pending = [1 if i < n_ready else 2 for i in range(n)]
    ready_ids, counters = release(ids, pending)
    h = 0
    for s in ready_ids:
        h = (h * 31 + s) & 0xFFFFFFFF
    return {"kernel": "release", "n_succ": int(counters["n_succ"]),
            "n_ready": int(counters["n_ready"]), "checksum": h}  # fmt: skip


KERNEL = Kernel(
    name="release",
    twin=release_kernel,
    args=("succ", "ready", "seed"),
    counters=("n_succ", "n_ready"),
    working_set=lambda o: 12.0
    * o["n_succ"],  # the successor list, the counts, the ready list
    smoke=({"succ": 1, "ready": 1, "seed": 1}, {"succ": 40, "ready": 13, "seed": 7}),
    symbols=("release",),
    source_path=SOURCE,
)

FAMILY = Family("release", "release", ("n_succ", "n_ready"), ("n_succ", "n_ready"))

CAL = a53cal.Calibration(KERNEL, FAMILY, SOURCE, PLAN, DATA, "release_rows")

if __name__ == "__main__":
    sys.exit(CAL.main())
