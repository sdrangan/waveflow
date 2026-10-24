"""Incremental, end to end (``plans/unit_harness.md`` Stage 6): change one unit's build parameter and
only that unit, and the systems holding it, do any work.

The unit is markov's generator, the parameter its draw interval (``MarkovGen.gen_ii``: the draw loop's
pipeline II -- its RTL and pace, no contract with the chain).  Built warm at ``gen_ii=1``, then at 2:

* the generator: re-synthesized (alone of the five tops), its harness re-run, and -- a new identity --
  calibrated afresh (or found in the store, once it has been); the parent system re-run, its pysim
  estimate composed from the new record, and still within 5% of its RTL;
* the chain core and the memory: nothing at all -- their harnesses, calibrations and checks skip.

Then back to 1: the generator re-synthesized again, but its calibration is a cache hit (its record for
``gen_ii=1`` is still in the store) -- no calibration run.  ``codegen`` regenerates every time (seconds,
Python only; csynth decides by content).

Run: ``pytest tests/examples/test_incremental_units.py -m vitis`` (Vitis + Vivado; several minutes --
two csynths, a calibration and the system's RTL run).
"""
from __future__ import annotations

import json

import pytest

from waveflow.toolchain.toolchain import find_vitis_path, find_vivado_path

pytestmark = pytest.mark.vitis

#: Everything a build of markov reaches: the system, and each unit's harness check.
TARGETS = ("compare", "ugen_compare", "ucore_compare", "umem_compare")
UNCHANGED = ("ucore_", "umem")


def _build(gen_ii: int) -> tuple[set[str], dict]:
    """Build every target at *gen_ii*: the steps that ran, and the csynth step's per-top outcome."""
    from examples.markov.markov_build import HERE, build_dag
    from waveflow.build.build import BuildConfig

    ran: set[str] = set()
    csynth: dict = {}
    for t in TARGETS:
        dag = build_dag(gen_ii=gen_ii)
        res = dag.run(BuildConfig(root_dir=HERE), through=t)
        bad = {n: r.message[:400] for n, r in res.items() if not r.success}
        assert not bad, f"{t}: {bad}"
        ran |= {n for n, r in res.items() if not r.skipped}
        step = next((s for s in dag.steps() if s.name == "csynth"), None)
        csynth.update(getattr(step, "last_run", {}) or {})
    return ran, csynth


def _composition_holds() -> float:
    from examples.markov.markov_build import HERE

    work = HERE / "xsi_work" / "markov"
    pysim = json.loads((work / "pysim.json").read_text())["cycles"]
    rtl = json.loads((work / "report.json").read_text())["cycles"]
    return (pysim - rtl) / rtl


def _assert_no_work_for_unchanged(ran: set[str]) -> None:
    others = sorted(n for n in ran if n.startswith(UNCHANGED))
    assert others == [], f"unchanged units did work: {others}"


@pytest.fixture(scope="module")
def toolchain():
    if not (find_vitis_path() and find_vivado_path()):
        pytest.skip("Stage 6 gate prerequisite missing: Vitis and Vivado")


def test_one_units_parameter_rebuilds_only_that_unit_and_its_parents(toolchain):
    _build(1)                                                     # warm
    ran, csynth = _build(2)
    _assert_no_work_for_unchanged(ran)
    assert [t for t, how in csynth.items() if how == "ran"] == ["csynth_markov_gen"]
    # The generator's harness and the parent re-run; its calibration is fitted afresh the first time
    # (a new identity) and a cache hit once its record is in the store.
    assert {"ugen_system_xsi", "pysim", "system_xsi"} <= ran
    assert abs(_composition_holds()) <= 0.05

    ran, csynth = _build(1)                                       # and back
    _assert_no_work_for_unchanged(ran)
    assert [t for t, how in csynth.items() if how == "ran"] == ["csynth_markov_gen"]
    assert "ugen_calib_system_xsi" not in ran                     # the record for gen_ii=1: a hit
    assert abs(_composition_holds()) <= 0.05
