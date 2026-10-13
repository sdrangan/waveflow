"""campaign.py — measure every registered point of a platform's sweep on gem5.

Reads the platform's committed ``cpu/sweep_plan.csv`` through :class:`SweepPlan` (which refuses an
uncommitted plan), measures the points not yet in the corpus on a pool of workers, and appends each
row to ``cpu/<kernel>/corpus.csv`` as it lands — so an interrupted campaign resumes where it stopped.
A point whose program output disagrees with its Python twin is still recorded (with
``output_matches_twin = False``) and reported: it is evidence, not something to drop.

    python -m waveflow.cpu.calib.campaign --platform-dir waveflow/calib/platforms/a53_hpi_1200mhz_gem5v25_1
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]  # no pandas-stubs in the dev deps

from waveflow.cpu.calib.gem5 import Gem5Runner
from waveflow.cpu.calib.kernels import KERNELS
from waveflow.cpu.calib.prereg import SweepPlan, point_key


def corpus_path(cpu_dir: Path, kernel: str) -> Path:
    return cpu_dir / kernel / "corpus.csv"


def measured_keys(cpu_dir: Path) -> set[str]:
    """Points already in a corpus (by :func:`point_key`)."""
    keys: set[str] = set()
    for kernel in KERNELS:
        path = corpus_path(cpu_dir, kernel)
        if path.is_file():
            df = pd.read_csv(path)
            keys |= {point_key(kernel, json.loads(p)) for p in df["point"]}
    return keys


def append_row(cpu_dir: Path, row: dict[str, Any]) -> None:
    path = corpus_path(cpu_dir, row["kernel"])
    path.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame([row])
    if path.is_file():
        new = pd.concat([pd.read_csv(path), new], ignore_index=True)
    new.to_csv(path, index=False)


def run_campaign(
    platform_dir: str | Path,
    *,
    runner: Gem5Runner | None = None,
    workers: int = 4,
    kernels: tuple[str, ...] | None = None,
    log=print,
) -> dict[str, Any]:
    """Measure every registered, not-yet-measured point; return a summary."""
    cpu_dir = Path(platform_dir) / "cpu"
    plan = SweepPlan.load(cpu_dir / "sweep_plan.csv")
    runner = runner or Gem5Runner()
    why = runner.unavailable()
    if why:
        raise RuntimeError(f"cannot measure: {why}")

    done = measured_keys(cpu_dir)
    df = pd.read_csv(plan.path)
    todo = [
        (r.kernel, json.loads(r.point))
        for r in df.itertuples()
        if (kernels is None or r.kernel in kernels)
        and point_key(r.kernel, json.loads(r.point)) not in done
    ]
    log(
        f"{len(todo)} points to measure ({len(done)} already in the corpus), {workers} workers"
    )
    for name in sorted(
        {k for k, _ in todo}
    ):  # build once, before the workers race to it
        runner.build(KERNELS[name])
    empty = runner.empty_region_cycles()
    log(f"empty region: {empty:g} cycles")

    t0 = time.monotonic()
    mismatches, failures = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(runner.measure, KERNELS[k], p, empty_cycles=empty, plan=plan): (
                k,
                p,
            )
            for k, p in todo
        }
        for i, fut in enumerate(as_completed(futs), 1):
            k, p = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001 - record and keep going
                failures.append((k, p, repr(exc)))
                log(f"[{i}/{len(todo)}] FAILED {k} {p}: {exc}")
                continue
            append_row(cpu_dir, row)
            if not row["output_matches_twin"]:
                mismatches.append((k, p))
            log(f"[{i}/{len(todo)}] {k} {p} {row['role']}: {row['cycles']:g} cycles")
    return {
        "measured": len(todo) - len(failures),
        "failures": failures,
        "mismatches": mismatches,
        "empty_region_cycles": empty,
        "wall_s": time.monotonic() - t0,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Measure a platform's registered sweep on gem5."
    )
    ap.add_argument("--platform-dir", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--kernel", action="append", help="limit to these kernels (repeatable)"
    )
    args = ap.parse_args(argv)
    summary = run_campaign(
        args.platform_dir,
        workers=args.workers,
        kernels=tuple(args.kernel) if args.kernel else None,
        log=lambda s: print(s, flush=True),
    )
    print(
        json.dumps({k: v for k, v in summary.items() if k != "failures"}, default=str)
    )
    for f in summary["failures"]:
        print("FAILED", f)
    return 1 if summary["failures"] or summary["mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
