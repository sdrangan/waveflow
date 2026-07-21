"""publish.py — promote calibration from an untracked work dir into the tracked platform library.

Calibration storage is **two-tier**, on purpose:

* **work dir** (untracked, e.g. ``calib/work/<name>/``) — where sweeps, tests, and the DAG calibration
  steps (:mod:`waveflow.build.calib_steps`) write *freely*.  It churns; it is git-ignored; nothing here
  is shared.
* **tracked library** (``calib/platforms/<name>/``) — the committed, infra-wide parameters every
  project reuses.  **Only this command writes here.**

That split buys two things.  First, a stray test or an experimental sweep can never touch the shared
parameters — the tracked dir has exactly one writer.  Second, because a re-fit on the same corpus is
deterministic, a byte-identical result publishes as a **no-op**: unchanged files are never rewritten,
so a re-run produces *no* git diff.

Only the **stable** artifacts are promoted (params + the distilled corpus), never the churny raw
per-run firing trees::

    mm_bus.json                    # the bus-transfer law (BusCalib)
    points/*.json                  # bus corpus — distilled {num_trans, nwords, span}
    components/<c>/params.json      # a component's residual params (StreamTimingModel)
    components/<c>/corpus.csv       # a component's distilled corpus — re-fittable offline

  excluded: ``components/<c>/rtl/`` and ``components/<c>/pysim/`` (the raw per-run firings — large,
  reproducible from a re-sweep, and the source of the churn this split removes).

The command is **dry-run by default** — it prints the plan and writes nothing; ``--apply`` writes only
the *changed* files.  A **coverage-regression guard** refuses to replace a fit with one built from
*fewer* datapoints (a thinner re-sweep silently clobbering a richer library) unless ``--force``.
"""
from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass, field
from pathlib import Path


class RegressionError(RuntimeError):
    """Raised by :func:`apply_plan` when publishing would replace a fit with a thinner one (fewer
    datapoints) and ``force`` is not set."""


@dataclass
class FileAction:
    """One publishable artifact and what publishing it would do."""

    rel: str                 #: path relative to the platform dir
    status: str              #: "created" | "updated" | "unchanged"


@dataclass
class Guard:
    """A coverage-regression check for one calibration *unit* (the bus, or one component): how many
    datapoints back the incoming fit vs the tracked one."""

    unit: str                #: "bus" or "components/<c>"
    tracked: int             #: datapoints behind the currently-tracked fit
    incoming: int            #: datapoints behind the work-dir fit

    @property
    def regressed(self) -> bool:
        return self.incoming < self.tracked


@dataclass
class PublishPlan:
    work_dir: Path
    tracked_dir: Path
    actions: list[FileAction] = field(default_factory=list)
    guards: list[Guard] = field(default_factory=list)

    @property
    def changed(self) -> list[FileAction]:
        return [a for a in self.actions if a.status != "unchanged"]

    @property
    def regressions(self) -> list[Guard]:
        return [g for g in self.guards if g.regressed]


# -- discovery ---------------------------------------------------------------------------------------
def _publishable(work_dir: Path) -> list[str]:
    """The relative paths of the stable artifacts present in *work_dir*, in a stable order."""
    rels: list[str] = []
    if (work_dir / "mm_bus.json").is_file():
        rels.append("mm_bus.json")
    for p in sorted((work_dir / "points").glob("*.json")):
        rels.append(p.relative_to(work_dir).as_posix())
    comp_root = work_dir / "components"
    for comp in sorted(d for d in comp_root.glob("*") if d.is_dir()):
        for fname in ("params.json", "corpus.csv"):
            if (comp / fname).is_file():
                rels.append((comp / fname).relative_to(work_dir).as_posix())
    return rels


def _same_bytes(a: Path, b: Path) -> bool:
    return b.is_file() and a.read_bytes() == b.read_bytes()


def _csv_rows(p: Path) -> int:
    """Datapoint count of a distilled corpus.csv (data rows = lines minus the header). 0 if absent."""
    if not p.is_file():
        return 0
    n = sum(1 for _ in p.read_text(encoding="utf-8").splitlines())
    return max(0, n - 1)


def _bus_points(platform_dir: Path) -> int:
    return len(list((platform_dir / "points").glob("*.json")))


# -- plan --------------------------------------------------------------------------------------------
def build_plan(work_dir: str | Path, tracked_dir: str | Path) -> PublishPlan:
    """Compute what publishing *work_dir* into *tracked_dir* would do — a pure inspection, no writes.

    Classifies every stable artifact as created / updated / unchanged (by byte-compare against the
    tracked copy), and computes a coverage guard per unit (bus point files; each component's corpus
    rows)."""
    work_dir, tracked_dir = Path(work_dir), Path(tracked_dir)
    plan = PublishPlan(work_dir=work_dir, tracked_dir=tracked_dir)

    for rel in _publishable(work_dir):
        src, tgt = work_dir / rel, tracked_dir / rel
        if _same_bytes(src, tgt):
            status = "unchanged"
        else:
            status = "updated" if tgt.is_file() else "created"
        plan.actions.append(FileAction(rel=rel, status=status))

    # Guards: only for units the work dir actually carries (an absent unit is not a regression).
    if (work_dir / "mm_bus.json").is_file() or (work_dir / "points").is_dir():
        plan.guards.append(Guard(unit="bus", tracked=_bus_points(tracked_dir),
                                 incoming=_bus_points(work_dir)))
    for comp in sorted(d for d in (work_dir / "components").glob("*") if d.is_dir()):
        name = comp.name
        plan.guards.append(Guard(
            unit=f"components/{name}",
            tracked=_csv_rows(tracked_dir / "components" / name / "corpus.csv"),
            incoming=_csv_rows(comp / "corpus.csv")))
    return plan


def apply_plan(plan: PublishPlan, *, force: bool = False) -> list[str]:
    """Write the changed artifacts into the tracked dir; return the rel paths actually written.

    Refuses with :class:`RegressionError` if any unit would regress (fewer datapoints) unless *force*.
    Unchanged files are never touched — that is what keeps a re-publish churn-free."""
    if plan.regressions and not force:
        detail = ", ".join(f"{g.unit}: {g.incoming} < {g.tracked}" for g in plan.regressions)
        raise RegressionError(
            f"coverage regression ({detail}) — the work fit has fewer datapoints than the tracked "
            f"library. Re-sweep to cover it, or pass force=True to publish anyway.")
    written: list[str] = []
    for a in plan.changed:
        src, tgt = plan.work_dir / a.rel, plan.tracked_dir / a.rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, tgt)
        written.append(a.rel)
    return written


# -- CLI ---------------------------------------------------------------------------------------------
def _format_plan(plan: PublishPlan) -> str:
    lines = [f"publish  {plan.work_dir}  ->  {plan.tracked_dir}"]
    if not plan.actions:
        lines.append("  (no publishable artifacts found in the work dir)")
    for a in plan.actions:
        mark = {"created": "+", "updated": "~", "unchanged": "="}[a.status]
        lines.append(f"  {mark} {a.rel}  [{a.status}]")
    for g in plan.guards:
        flag = "  !! REGRESSION" if g.regressed else ""
        lines.append(f"  guard {g.unit}: incoming {g.incoming} vs tracked {g.tracked}{flag}")
    n = len(plan.changed)
    lines.append(f"  {n} file(s) to write, {len(plan.actions) - n} unchanged.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="publish_calib",
        description="Promote calibration from an untracked work dir into the tracked platform library.")
    ap.add_argument("work_dir", help="the untracked work platform dir (e.g. calib/work/<name>)")
    ap.add_argument("tracked_dir", help="the tracked platform library (e.g. calib/platforms/<name>)")
    ap.add_argument("--apply", action="store_true",
                    help="write the changed files (default is a dry-run that only prints the plan)")
    ap.add_argument("--force", action="store_true",
                    help="publish even if a unit's fit regressed (fewer datapoints than tracked)")
    args = ap.parse_args(argv)

    plan = build_plan(args.work_dir, args.tracked_dir)
    print(_format_plan(plan))

    if not args.apply:
        if plan.changed:
            print("dry-run: nothing written. Re-run with --apply to publish.")
        return 0
    try:
        written = apply_plan(plan, force=args.force)
    except RegressionError as exc:
        print(f"REFUSED: {exc}")
        return 1
    print(f"applied: wrote {len(written)} file(s).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
