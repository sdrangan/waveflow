"""rf_relayout through the **system** flow: the two conversions as the DUT of an ``HwSystem``.

``plans/system_model.md`` S3e.  The same design the kernel flow builds (``rf_relayout_build.py``), taken
through ``add_system_steps``: ``csynth`` -> ``system_rtl`` (no crossbar, so no IP: the two streams
are top ports) -> ``scenario`` / ``pysim`` -> ``system_xsi`` -> ``compare``.  The sink's recording is
compared RTL against pysim; the golden -- every converter word back unchanged, the 64th landing at
cycle **68** -- is the kernel flow's own check (``check_xsi_outputs``), run by :func:`check_xsi`.

    python -m examples.rf_relayout.rf_relayout_system_build --synth check     # the whole flow
"""
from __future__ import annotations

from pathlib import Path

from examples.rf_relayout.rf_relayout import NWORD, RfRelayoutSystem, check_xsi_outputs
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
TOP = "rf_relayout_system"
WORK_DIR = "xsi_work"
#: The kernel flow's gate (``test_rf_relayout_xsi.py``): the cycle the 64th word lands.
WANT_CYCLES = 68


def system() -> RfRelayoutSystem:
    """The 64-word loopback the kernel gate runs."""
    return RfRelayoutSystem()


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`.  The kernel's csynth project is the kernel
    flow's (``rf_relayout.tcl`` at the root)."""
    dag = BuildDag()
    add_system_steps(dag, system(), work_dir=work_dir, top=TOP,
                     tcls={"rf_relayout": Path("rf_relayout.tcl")})
    return dag


def check_xsi(work: Path, want_cycles: int | None = WANT_CYCLES) -> None:
    """The kernel flow's golden check, on the RTL run's recording."""
    check_xsi_outputs(Path(work) / "xsi_run", NWORD, want_cycles)


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
