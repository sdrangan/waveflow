"""accuracy_points.py — the 30 accuracy points the CG model's move must leave byte-identical.

Gate 8.0 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (§10, Phase 8) fixed them before the model moved
into ``waveflow/linalg/cg.py``:

* :data:`GRID_POINTS`, 26 points of the Phase 3 sweep, each regenerated at its recorded budget
  (1,000 errors for a refined point, 100 otherwise) and compared, line for line, with its slice of
  the committed ``paper_data/accuracy_grid.csv``.  Both residual forms on the same samples
  (``16qam_64x8_explicit`` and ``16qam_64x8``), every (M, K), every modulation at each K, every
  iteration count up to 16, refined and capped budgets.
* :data:`STRESS_POINTS`, 4 points of the saturation stress set, which the sweep never ran:
  ``simulate_point_fixed`` at a fixed budget (:data:`STRESS_MAX_BITS`), in both residual forms,
  written once to ``paper_data/accuracy_stress_points.csv`` (before the move) and compared with it.

The values are pure Python (numpy 2.5.3); no tool version applies.

    python -m examples.mimo_cg.tools.accuracy_points            # write the stress table
    python -m examples.mimo_cg.tools.accuracy_points --check    # regenerate all 30 and compare
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import time
from pathlib import Path

from examples.mimo_cg import mimo_cg_accuracy_sweep as S
from examples.mimo_cg.mimo_cg import MAX_BITS, MIN_ERRORS, _fmt, provenance, write_table

HERE = Path(__file__).resolve().parents[1]
GRID = HERE / "paper_data" / "accuracy_grid.csv"
STRESS_TABLE = HERE / "paper_data" / "accuracy_stress_points.csv"

#: (case, SNR offset) of the sweep grid.
GRID_POINTS: tuple[tuple[str, int], ...] = (
    ("16qam_64x8_explicit", -6),
    ("16qam_64x8_explicit", 0),
    ("16qam_64x8_explicit", 1),
    ("16qam_64x8_explicit", 4),
    ("16qam_64x8", -6),
    ("16qam_64x8", 0),
    ("16qam_64x8", 1),
    ("16qam_64x8", 4),
    ("qpsk_32x4", 1),
    ("qpsk_32x4", 6),
    ("qpsk_64x8", 2),
    ("qpsk_64x8", -6),
    ("qpsk_128x16", 0),
    ("qpsk_128x16", -6),
    ("16qam_64x4", -1),
    ("16qam_64x4", 5),
    ("16qam_128x8", 1),
    ("16qam_128x8", -6),
    ("16qam_32x16", 1),
    ("16qam_32x16", 6),
    ("64qam_128x4", 1),
    ("64qam_128x4", -6),
    ("64qam_32x8", 0),
    ("64qam_32x8", 4),
    ("64qam_64x16", -1),
    ("64qam_64x16", 6),
)
#: (case, SNR offset, explicit residual) of the stress set.
STRESS_POINTS: tuple[tuple[str, int, bool], ...] = (
    ("16qam_32x16", 0, False),
    ("16qam_32x16", 0, True),
    ("16qam_64x8", 0, False),
    ("16qam_64x8", 0, True),
)
STRESS_MAX_BITS = 2_000_000
STRESS_MIN_ERRORS = 1000


def _lines(rows: list[dict]) -> list[str]:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for row in rows:
        writer.writerow([_fmt(v) for v in row.values()])
    return buf.getvalue().rstrip("\n").split("\n")


def grid_budget(case: str, offset: int) -> int:
    """The ``min_errors`` a grid point was run at."""
    return S.REFINE_MIN_ERRORS if offset in S.refine_offsets(case) else MIN_ERRORS


def grid_lines(case: str, offset: int, workers: int = 2) -> list[str]:
    """A grid point regenerated, as the grid writes it."""
    rows = S.simulate_sweep_point(
        case,
        offset,
        workers=workers,
        max_bits=MAX_BITS,
        min_errors=grid_budget(case, offset),
    )
    return _lines(rows)


def committed_grid_lines(case: str, offset: int) -> list[str]:
    """The point's slice of the committed grid."""
    c = S.CONFIGS[case]
    form = "explicit" if c.explicit else "recurrence"
    key = f"{c.modulation},{c.M},{c.K},{form},{_fmt(S.point_snr(case, offset))},"
    with GRID.open(encoding="utf-8") as fh:
        return [ln.rstrip("\n") for ln in fh if ln.startswith(key)]


def stress_rows(case: str, offset: int, explicit: bool) -> list[dict]:
    """A stress point: float CG and the stress set's fixed-point CG on identical samples."""
    from examples.mimo_cg.mimo_cg_conformance import STRESS_FORMATS
    from examples.mimo_cg.mimo_cg_fixed import simulate_point_fixed

    c = S.CONFIGS[case]
    rows = simulate_point_fixed(
        c.config,
        S.point_snr(case, offset),
        {"stress": STRESS_FORMATS},
        explicit_residual=explicit,
        max_bits=STRESS_MAX_BITS,
        min_errors=STRESS_MIN_ERRORS,
    )
    form = "explicit" if explicit else "recurrence"
    return [{"case": case, "offset": offset, "residual": form, **row} for row in rows]


def write_stress() -> None:
    rows = [r for p in STRESS_POINTS for r in stress_rows(*p)]
    comment = provenance(
        "accuracy_stress_points (gate 8.0, before the CG model moved)",
        max_bits=STRESS_MAX_BITS,
        min_errors=STRESS_MIN_ERRORS,
        points=len(STRESS_POINTS),
    )
    write_table(STRESS_TABLE, rows, comment)


def committed_stress_lines(case: str, offset: int, explicit: bool) -> list[str]:
    form = "explicit" if explicit else "recurrence"
    with STRESS_TABLE.open(encoding="utf-8") as fh:
        return [
            ln.rstrip("\n") for ln in fh if ln.startswith(f"{case},{offset},{form},")
        ]


def check(workers: int = 2) -> int:
    """Regenerate all 30 points and compare; returns the number that differ."""
    bad = 0
    t_all = time.time()
    for case, offset in GRID_POINTS:
        t0 = time.time()
        got, want = grid_lines(case, offset, workers), committed_grid_lines(
            case, offset
        )
        same = bool(want) and got == want
        bad += not same
        print(
            f"grid   {case:22s} {offset:+d}  {len(got):3d} rows  "
            f"{'identical' if same else 'DIFFERENT'}  {time.time() - t0:5.1f} s",
            flush=True,
        )
    for case, offset, explicit in STRESS_POINTS:
        t0 = time.time()
        got = _lines(stress_rows(case, offset, explicit))
        want = committed_stress_lines(case, offset, explicit)
        same = bool(want) and got == want
        bad += not same
        form = "explicit" if explicit else "recurrence"
        print(
            f"stress {case:12s} {form:10s} {offset:+d}  {len(got):3d} rows  "
            f"{'identical' if same else 'DIFFERENT'}  {time.time() - t0:5.1f} s",
            flush=True,
        )
    n = len(GRID_POINTS) + len(STRESS_POINTS)
    print(f"{n - bad} of {n} points identical, {time.time() - t_all:.0f} s")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--check", action="store_true", help="regenerate all 30 and compare"
    )
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args(argv)
    if args.check:
        return 1 if check(args.workers) else 0
    write_stress()
    print(f"wrote {STRESS_TABLE.relative_to(HERE.parents[1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
