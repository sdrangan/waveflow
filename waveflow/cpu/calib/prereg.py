"""prereg.py — :class:`SweepPlan`, the pre-registered points a calibration may measure.

A cost model's accuracy is only worth reporting if the points it is judged on were chosen before any
of them was measured.  So the plan of a sweep — every point, and whether it is for **fit**,
**validation** or **test** — is a committed CSV, and the gem5 runner refuses any point not in it.
Model-structure decisions may look at the validation set; the test set is evaluated once, and that
number is the acceptance number (``plans/cpu_model.md``, rule 11).

"Committed" is checked, not trusted: the file must be tracked by git and unmodified, and the commit
that added it is stamped into every measured row, so a test can show that every measurement came
after the registration it claims.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]  # no pandas-stubs in the dev deps

ROLES = ("fit", "validation", "test")


def point_key(kernel: str, point: Mapping[str, Any]) -> str:
    """A canonical, order-independent key for one point of one kernel."""
    return f"{kernel}:{json.dumps(dict(point), sort_keys=True)}"


class PreregistrationError(RuntimeError):
    """A point is not registered, or the registration is not committed."""


def _git(repo: Path, *args: str) -> str:
    run = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    if run.returncode != 0:
        raise PreregistrationError(f"git {' '.join(args)} failed: {run.stderr.strip()}")
    return run.stdout.strip()


def require_committed(path: str | Path) -> str:
    """The commit that added *path*; a :class:`PreregistrationError` unless git tracks it unmodified."""
    path = Path(path).resolve()
    repo = Path(_git(path.parent, "rev-parse", "--show-toplevel"))
    rel = str(path.relative_to(repo))
    if not _git(repo, "ls-files", "--", rel):
        raise PreregistrationError(
            f"{rel} is not tracked: commit the plan before measuring"
        )
    if _git(repo, "status", "--porcelain", "--", rel):
        raise PreregistrationError(f"{rel} has uncommitted changes: commit them first")
    added = _git(repo, "log", "--diff-filter=A", "--format=%H", "--", rel).splitlines()
    return added[-1] if added else ""


@dataclass
class SweepPlan:
    """A committed ``sweep_plan.csv``: columns ``kernel``, ``point`` (JSON) and ``role``."""

    path: Path
    #: The commit that added the file; stamped into every row measured under this plan.
    commit: str = ""
    roles: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> SweepPlan:
        """Read *path*, refusing it unless git tracks it and it is unmodified."""
        path = Path(path).resolve()
        commit = require_committed(path)
        rel = path.name
        df = pd.read_csv(path)
        roles: dict[str, str] = {}
        for row in df.itertuples(index=False):
            if row.role not in ROLES:
                raise PreregistrationError(f"unknown role {row.role!r} in {rel}")
            key = point_key(row.kernel, json.loads(row.point))
            if key in roles:
                raise PreregistrationError(f"{key} is registered twice in {rel}")
            roles[key] = row.role
        return cls(path=path, commit=commit, roles=roles)

    def role(self, kernel: str, point: Mapping[str, Any]) -> str:
        """The registered role of *point*, or a :class:`PreregistrationError`."""
        key = point_key(kernel, point)
        try:
            return self.roles[key]
        except KeyError:
            raise PreregistrationError(f"{key} is not in {self.path.name}") from None

    @staticmethod
    def write(
        path: str | Path, rows: Sequence[tuple[str, Mapping[str, Any], str]]
    ) -> Path:
        """Write a plan file from ``(kernel, point, role)`` rows (it still has to be committed)."""
        df = pd.DataFrame(
            [
                {"kernel": k, "point": json.dumps(dict(p), sort_keys=True), "role": r}
                for k, p, r in rows
            ]
        )
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
        return path
