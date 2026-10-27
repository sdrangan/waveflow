"""The simulated outcome of a processor-model run, pinned: ``plans/sim_speed_cpu_linalg.md`` steps 2 and 10.

An optimization of the simulation must leave what it simulates unchanged -- every task's submit,
start and end time, its core, its cycles and energy -- not only the results.  A run is reduced to a
*fingerprint*: each :class:`~waveflow.cpu.task.TaskRecord` written out field by field (floats as exact
hex), hashed, plus one digest over all of them.  The fingerprints of the unoptimized code are kept in
:data:`FIXTURE` (``tests/fixtures/cpu/sim_speed_outcomes.json``) with the commit they were taken at,
and the tests compare against them exactly.

The fixture has one section per test module (``cpu``, ``cpu_sched``); each module records its own with
``python -m <module> --record`` and leaves the others alone.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "cpu" / "sim_speed_outcomes.json"
TRACE = REPO / "tests" / "fixtures" / "cpu" / "cpu_sched_trace.jsonl"

#: Every field of a TaskRecord that is part of the simulated outcome (``feats``/``func`` are inputs).
RECORD_FIELDS = (
    "name",
    "prio",
    "t_submit",
    "t_start",
    "t_end",
    "core",
    "cycles",
    "switch_cycles",
    "busy_s",
    "energy_pj",
    "n_preempted",
    "is_irq",
)


def canon(value: Any) -> str:
    """*value* as text that differs whenever the value does: floats as exact hex."""
    return value.hex() if isinstance(value, float) else repr(value)


def record_key(obj: Any, fields: Iterable[str] = RECORD_FIELDS) -> str:
    return "|".join(canon(getattr(obj, f)) for f in fields)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def fingerprint(
    objs: Iterable[Any],
    fields: Iterable[str] = RECORD_FIELDS,
    *,
    per_record: bool = True,
) -> dict[str, Any]:
    """``n``, a ``digest`` over every object's key, and (with *per_record*) each key's short hash."""
    keys = [record_key(o, tuple(fields)) for o in objs]
    out: dict[str, Any] = {"n": len(keys), "digest": _h("\n".join(keys))}
    if per_record:
        out["records"] = [_h(k)[:16] for k in keys]
    return out


def assert_same(got: dict[str, Any], want: dict[str, Any], what: str) -> None:
    """*got* equals the pinned *want*; on a difference, name the first record that moved."""
    assert got["n"] == want["n"], f"{what}: {got['n']} records, pinned {want['n']}"
    if got["digest"] == want["digest"]:
        return
    if "records" in want and "records" in got:
        for i, (a, b) in enumerate(zip(got["records"], want["records"], strict=True)):
            if a != b:
                raise AssertionError(
                    f"{what}: record {i} differs from the pinned outcome "
                    f"(the first of the run's {got['n']} that does)"
                )
    raise AssertionError(f"{what}: the outcome differs from the pinned one")


def commit() -> str:
    run = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return run.stdout.strip() or "unknown"


def load(section: str) -> dict[str, Any]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.is_file() else {}
    if section not in data:
        raise LookupError(f"{FIXTURE.name} has no '{section}' section: record it first")
    return data[section]


def save(section: str, content: dict[str, Any]) -> None:
    """Write *section* (stamped with the commit), keeping the file's other sections."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.is_file() else {}
    data[section] = {"commit": commit(), **content}
    FIXTURE.write_text(
        json.dumps(dict(sorted(data.items())), indent=1) + "\n", encoding="utf-8"
    )


def load_trace() -> list[dict[str, Any]]:
    """The micro-scheduler's 410-operation trace (``examples/cpu_sched``), as committed."""
    return [json.loads(line) for line in TRACE.read_text().splitlines() if line.strip()]
