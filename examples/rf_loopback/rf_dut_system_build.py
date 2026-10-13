"""The pass-through's DUT gate through the **system** flow: the kernel alone between AXIS models.

``plans/system_model.md`` S3e.  The design the kernel flow's gate runs (``rf_dut_build.py``,
``test_xsi_bfm.py``'s ``rf_pass_through`` row), taken through ``add_system_steps``: ``csynth`` ->
``system_rtl`` (no crossbar, so no IP) -> ``scenario`` / ``pysim`` -> ``system_xsi`` -> ``compare``.
The sink's recording is compared RTL against pysim; the golden -- every word relayed bit-exact, the
last landing at cycle **1066** -- is the kernel flow's own check, run by :func:`check_xsi`.

    python -m examples.rf_loopback.rf_dut_system_build --synth check     # the whole flow
"""
from __future__ import annotations

from pathlib import Path

from examples.rf_loopback.rf_dut_build import RfPassThroughSystem, check_xsi_outputs
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
TOP = "rf_pass_through_system"
WORK_DIR = "xsi_work"
#: The kernel flow's gate: the cycle the last relayed word lands.
EXPECTED_CYCLES = 1066


def system() -> RfPassThroughSystem:
    """The kernel gate's scenario: eight one-block bursts, back to back."""
    return RfPassThroughSystem()


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`.  The kernel's csynth project is the kernel
    flow's (``rf_pass_through.tcl`` at the root)."""
    dag = BuildDag()
    add_system_steps(dag, system(), work_dir=work_dir, top=TOP,
                     tcls={"rf_pass_through": Path("rf_pass_through.tcl")})
    return dag


def check_xsi(work: Path, want_cycles: int = EXPECTED_CYCLES) -> None:
    """The kernel flow's golden check, on the RTL run's recording."""
    check_xsi_outputs(Path(work) / "xsi_run", want_cycles)


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
