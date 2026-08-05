"""P4 of ``plans/sweep_runner.md`` — the collapsed sweep files what the hand-written one did.

`test_sweep_runner.py` covers the control flow against a stub, which is where the runner's value
lives and what can be checked on every run.  What a stub cannot check is that a **real** point, driven
through the **real** DAG, still lands the same records in the same store.

That is the surviving half of the plan's dropped P0 gate: not "the summary is byte-identical" (the
summary deliberately changed shape) but *the same measurements get filed*.

Marked ``vitis`` because it needs a toolchain and ~12 minutes.  The comparison itself is cheap and
runs unmarked: it reads a store the sweep already wrote, so a machine with no Vitis still checks the
last sweep's output rather than skipping silently.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.calib.record_store import INTEGRATION_TARGET, ModuleStore

REPO = Path(__file__).resolve().parents[2]
TRACKED = REPO / "examples" / "vecmult" / "calib" / "platforms" / "zynq7020_vecmult"
WORK = REPO / "examples" / "vecmult" / "calib" / "work" / "zynq7020_vecmult_sweep"

COUNTERS = ("lut", "ff", "dsp", "bram")


def _measurements(store: ModuleStore) -> dict:
    """``{key: {params, resource, integration}}`` — what a store says, ignoring provenance."""
    out: dict = {}
    for key in store.keys():
        ident = store.get_identity(key)
        best = store.best(key, "resource")
        integ = store.read(key, INTEGRATION_TARGET, verify=False)
        out[key] = {
            "params": dict(ident.params) if ident else None,
            "resource": ({c: int(best.payload[c]) for c in COUNTERS if c in best.payload}
                         if best else None),
            "integration": ({c: int(integ[-1].payload[c]) for c in COUNTERS
                             if c in integ[-1].payload} if integ else None),
        }
    return out


@pytest.fixture(scope="module")
def both_stores():
    if not TRACKED.is_dir():
        pytest.skip("no tracked vecmult platform to compare against")
    if not WORK.is_dir():
        pytest.skip("no work-tier sweep output — run `python -m examples.vecmult.vecmult_sweep`")
    return _measurements(ModuleStore(TRACKED)), _measurements(ModuleStore(WORK))


def test_the_same_modules_are_addressed(both_stores):
    """Same keys means same structures measured — a re-sweep that moved a key changed the design."""
    tracked, work = both_stores
    assert set(work) == set(tracked), (
        f"keys differ: only in work {sorted(set(work) - set(tracked))}, "
        f"only in tracked {sorted(set(tracked) - set(work))}")


def test_the_same_counters_are_filed(both_stores):
    """The measurements themselves, module by module.

    Synthesis is deterministic for a fixed design, part and period, so any difference here is a change
    in what was *built* or in how it was attributed — not noise to tolerate.
    """
    tracked, work = both_stores
    differ = {k: (tracked[k]["resource"], work[k]["resource"])
              for k in sorted(set(tracked) & set(work))
              if tracked[k]["resource"] != work[k]["resource"]}
    assert not differ, f"per-module counters changed: {differ}"


def test_the_integration_term_is_filed_the_same(both_stores):
    """The third additive term, which used to have nowhere durable to live at all."""
    tracked, work = both_stores
    differ = {k: (tracked[k]["integration"], work[k]["integration"])
              for k in sorted(set(tracked) & set(work))
              if tracked[k]["integration"] != work[k]["integration"]}
    assert not differ, f"integration records changed: {differ}"


def test_every_point_filed_something(both_stores):
    """A sweep that ran clean and filed nothing is the failure the store tier exists to prevent."""
    _tracked, work = both_stores
    empty = [k for k, v in work.items() if not v["resource"]]
    assert not empty, f"{len(empty)} key(s) carry no resource record: {empty[:5]}"
