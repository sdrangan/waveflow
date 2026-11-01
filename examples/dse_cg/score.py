"""score.py -- turn run records into the paper's numbers (plan ``plans/dse_cg.md``, *Measurements*).

Reads only ledgers, reports and the ground-truth table; never re-runs anything.

Skeleton (plan phases P3, P4).
"""

from __future__ import annotations

from pathlib import Path


def optimum(truth_dir: Path, objective: dict) -> dict:
    """The ground-truth best feasible design for ``objective``, from the replay table."""
    raise NotImplementedError


def anytime_curve(ledger: Path, truth_dir: Path, objective: dict) -> list[tuple[float, float]]:
    """``(tool-seconds charged, best verified objective so far)`` after every call.

    Verified means synthesized and RTL-simulated in this run, and feasible by the ground truth.
    """
    raise NotImplementedError


def time_to_target(ledger: Path, truth_dir: Path, objective: dict, epsilon: float) -> float | None:
    """Tool-seconds to the first verified design within ``epsilon`` of the optimum, or ``None``."""
    raise NotImplementedError


def check_report(report: dict, ledger: Path) -> list[str]:
    """Every claim in the report the ledger does not back: a number never returned, or one whose
    ``source`` is wrong (for example a pysim prediction reported as ``synth``)."""
    raise NotImplementedError


def summarize(record_dirs: list[Path], truth_dir: Path) -> dict:
    """Per arm and objective: medians and spreads of time-to-target, quality at budget, syntheses
    and RTL runs spent, tokens, and report errors; with calibration charged up front and amortized."""
    raise NotImplementedError
