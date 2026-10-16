"""sweep.py — the pre-registered calibration sweep for a processor platform.

The points a calibration measures, and the role each plays, are chosen **here, before any of them is
measured** (``plans/cpu_model.md`` rule 11, step 10):

* **fit** points span each family's whole feature range, corners included, so every later point
  lies inside the region the model was fitted over;
* **validation** points are strictly interior, and are what model-structure decisions may look at
  (a regime feature, a piecewise fit);
* **test** points are strictly interior too, on different values and seeds, and are evaluated once:
  that number is the acceptance number.

Both held-out sets are stratified: every family's range, and both sides of the L1D and L2 boundaries
for the kernels whose working set crosses them (``cdot_q15``: 8 bytes per sample, so the boundaries
sit at 4,096 and 131,072 samples; ``gather_hist``: 4 bytes per bin, at 8,192 and 262,144 bins).

The area plan is a grid of core configurations, fit on its edges and held out in its interior.

    python -m waveflow.cpu.calib.sweep --platform-dir waveflow/calib/platforms/a53_hpi_1200mhz_gem5v25_1
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]  # no pandas-stubs in the dev deps

from waveflow.cpu.calib.prereg import SweepPlan

Row = tuple[str, dict[str, Any], str]

#: Seeds per role, disjoint so a held-out point never repeats a fitted input.
FIT_SEEDS = (1, 2, 3)
VAL_SEEDS = (101, 102)
TEST_SEEDS = (201, 202)


def _grid(kernel: str, role: str, points: list[dict[str, Any]]) -> list[Row]:
    return [(kernel, p, role) for p in points]


def sched_rows() -> list[Row]:
    rows: list[Row] = []
    for op in ("add", "delete", "reprio", "sort"):
        top = 512 if op == "sort" else 1024  # insertion sort is quadratic
        fit_n = [n for n in (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024) if n <= top]
        val_n = [n for n in (3, 12, 48, 192, 768) if n < top]
        test_n = [n for n in (6, 24, 96, 384) if n < top]
        rows += _grid(
            "sched_ops",
            "fit",
            [{"op": op, "n": n, "seed": s} for n in fit_n for s in FIT_SEEDS],
        )
        rows += _grid(
            "sched_ops",
            "validation",
            [{"op": op, "n": n, "seed": s} for n in val_n for s in VAL_SEEDS],
        )
        rows += _grid(
            "sched_ops",
            "test",
            [{"op": op, "n": n, "seed": s} for n in test_n for s in TEST_SEEDS],
        )
    return rows


def cdot_rows() -> list[Row]:
    fit_n = (16, 64, 256, 1024, 4096, 16384, 65536, 131072, 262144, 524288)
    # Interior, and on both sides of each boundary (L1D at 4096 samples, L2 at 131072).
    val_n = (128, 3500, 5000, 100_000, 160_000)
    test_n = (512, 3800, 4500, 120_000, 150_000, 400_000)
    rows = _grid(
        "cdot_q15", "fit", [{"n": n, "seed": s} for n in fit_n for s in FIT_SEEDS[:2]]
    )
    rows += _grid(
        "cdot_q15",
        "validation",
        [{"n": n, "seed": s} for n in val_n for s in VAL_SEEDS[:1]],
    )
    rows += _grid(
        "cdot_q15",
        "test",
        [{"n": n, "seed": s} for n in test_n for s in TEST_SEEDS[:1]],
    )
    return rows


def gather_rows() -> list[Row]:
    fit_m = (64, 1024, 8192, 65536, 262144, 1048576, 4194304)
    fit_n = (1024, 16384, 131072)
    # Interior bins on both sides of each boundary (L1D at 8192 bins, L2 at 262144).
    val = [
        (4096, 4096),
        (7000, 50_000),
        (10_000, 4096),
        (200_000, 50_000),
        (300_000, 4096),
    ]
    test = [
        (512, 50_000),
        (7500, 4096),
        (9000, 50_000),
        (240_000, 4096),
        (290_000, 50_000),
        (2_000_000, 50_000),
    ]
    rows = _grid(
        "gather_hist",
        "fit",
        [{"n": n, "m": m, "seed": 1} for m, n in itertools.product(fit_m, fit_n)],
    )
    rows += _grid(
        "gather_hist",
        "validation",
        [{"n": n, "m": m, "seed": VAL_SEEDS[0]} for m, n in val],
    )
    rows += _grid(
        "gather_hist",
        "test",
        [{"n": n, "m": m, "seed": TEST_SEEDS[0]} for m, n in test],
    )
    return rows


def dispatch_rows() -> list[Row]:
    rows = _grid(
        "dispatch",
        "fit",
        [
            {"n": n, "seed": s}
            for n in (1, 4, 16, 64, 256, 1024, 4096)
            for s in FIT_SEEDS[:2]
        ],
    )
    rows += _grid(
        "dispatch",
        "validation",
        [{"n": n, "seed": s} for n in (8, 128, 2048) for s in VAL_SEEDS],
    )
    rows += _grid(
        "dispatch",
        "test",
        [{"n": n, "seed": s} for n in (32, 512, 3000) for s in TEST_SEEDS],
    )
    return rows


def switch_rows() -> list[Row]:
    rows = _grid("ctx_switch", "fit", [{"k": k} for k in (1, 4, 16, 64, 256)])
    rows += _grid("ctx_switch", "validation", [{"k": k} for k in (8, 32, 128)])
    rows += _grid("ctx_switch", "test", [{"k": k} for k in (2, 12, 48, 200)])
    rows += _grid("swapcontext", "fit", [{"k": k} for k in (1, 16, 64)])
    rows += _grid("swapcontext", "test", [{"k": k} for k in (8, 32)])
    return rows


def sweep_rows() -> list[Row]:
    """Every registered kernel point, in a fixed order."""
    return sched_rows() + cdot_rows() + gather_rows() + dispatch_rows() + switch_rows()


#: The area grid: cores, L1 (KiB, I and D alike), L2 (KiB).  Edges are fit; the interior is held out.
AREA_CORES = (1, 2, 3, 4)
AREA_L1_KB = (16, 32, 48, 64)
AREA_L2_KB = (256, 512, 768, 1024, 1536, 2048)


def area_rows() -> list[dict[str, Any]]:
    rows = []
    interior = []
    for c, l1, l2 in itertools.product(AREA_CORES, AREA_L1_KB, AREA_L2_KB):
        edge = (
            c in (AREA_CORES[0], AREA_CORES[-1])
            or l1 in (AREA_L1_KB[0], AREA_L1_KB[-1])
            or l2
            in (
                AREA_L2_KB[0],
                AREA_L2_KB[-1],
            )
        )
        row = {"n_cores": c, "l1_kb": l1, "l2_kb": l2}
        if edge:
            rows.append({**row, "role": "fit"})
        else:
            interior.append(row)
    # 16 interior configurations, alternated between validation and test in a fixed order.
    for i, row in enumerate(interior):
        rows.append({**row, "role": "validation" if i % 2 == 0 else "test"})
    return rows


def write_plans(cpu_dir: str | Path) -> tuple[Path, Path]:
    """Write ``sweep_plan.csv`` and ``area_plan.csv`` into *cpu_dir* (they still need committing)."""
    cpu_dir = Path(cpu_dir)
    sweep = SweepPlan.write(cpu_dir / "sweep_plan.csv", sweep_rows())
    area = cpu_dir / "area_plan.csv"
    pd.DataFrame(area_rows()).to_csv(area, index=False)
    return sweep, area


def summary(rows: list[Row]) -> pd.DataFrame:
    """Counts per kernel (and op) and role."""
    df = pd.DataFrame(
        [
            {"kernel": k + (f":{p['op']}" if "op" in p else ""), "role": r}
            for k, p, r in rows
        ]
    )
    return df.groupby(["kernel", "role"]).size().unstack(fill_value=0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Write the pre-registered sweep and area plans."
    )
    ap.add_argument("--platform-dir", required=True, type=Path)
    args = ap.parse_args(argv)
    sweep, area = write_plans(args.platform_dir / "cpu")
    print(f"wrote {sweep} and {area}")
    print(summary(sweep_rows()).to_string())
    print(json.dumps(pd.DataFrame(area_rows())["role"].value_counts().to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
