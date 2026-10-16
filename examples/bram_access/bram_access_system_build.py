"""bram_access through the **system** flow: the kernel and its hand-written memory as one DUT.

``plans/system_model.md`` S3e/S3f.  ``add_system_steps``: ``csynth`` -> ``system_rtl`` ->
``scenario`` / ``pysim`` -> ``system_xsi`` (traced) -> ``compare``.  The memory is ``rtl_module()``
RTL beside the kernel, so the system top instantiates the DUT as its wrapper (``wrapper_gen``),
rendered into the top's file, with ``bram_t2p.v`` on its compile list.  The run is **traced**: the
wrapper's scope is dumped (``<work>/<top>_trace.vcd``) and its hazard manifest written beside it
(``<work>/hazards.json``), which is what the read-during-write scan and the latency measurement
read.  Two scenarios, each a system of its own: ``zero`` (the gate) and ``collision`` (the
deliberate hazard).  The gate is ``tests/examples/test_bram_access_xsi.py``.

    python -m examples.bram_access.bram_access_system_build --synth check [--collision]
"""
from __future__ import annotations

import json
from pathlib import Path

from examples.bram_access.bram_access import BramAccessSystem
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
WORK_DIR = "xsi_work"
SCENARIOS = ("zero", "collision")


def top(scenario: str = "zero") -> str:
    """The system top of one scenario's run."""
    return "bram_access_system" if scenario == "zero" else f"bram_access_{scenario}_system"


#: Scenario zero's top, the gate's.
TOP = top()


def system(scenario: str = "zero") -> BramAccessSystem:
    return BramAccessSystem(scenario_name=scenario)


def build_dag(scenario: str = "zero", work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`, traced.  The kernel's csynth project is the
    one ``bram_access_build.py`` generates (``bram_access.tcl`` at the root)."""
    dag = BuildDag()
    add_system_steps(dag, system(scenario), work_dir=work_dir, top=top(scenario),
                     tcls={"bram_access": Path("bram_access.tcl")}, trace=True)
    return dag


def trace_of(work: Path) -> tuple[Path, dict]:
    """A traced run's ``(VCD, the DUT wrapper's hazard manifest)``."""
    work = Path(work)
    (manifest,) = json.loads((work / "hazards.json").read_text(encoding="utf-8"))
    return work / f"{work.name}_trace.vcd", manifest


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag("collision" if a.collision else "zero"),
                description=__doc__.splitlines()[0], default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead")),
                            (("--collision",), dict(action="store_true",
                             help="the deliberate read-during-write scenario"))],
                params_from_args=lambda a: {"synth": a.synth})
