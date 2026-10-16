"""fir_block through the **system** flow: the filter kernel as the DUT of an ``HwSystem``.

``plans/system_model.md`` S3e.  The same design the kernel flow builds (``fir_block_build.py``), taken
through ``add_system_steps``: ``csynth`` -> ``system_rtl`` (no crossbar, so no IP: the kernel's two
streams and two ``m_axi`` bundles are top ports) -> ``scenario`` / ``pysim`` -> ``system_xsi`` ->
``compare``.  What the sink and the memory record is compared RTL against pysim; the golden -- every
output block bit-exact across a tap reload and the carry, one completion per command -- is the kernel
flow's own check (``check_xsi_outputs``), run on the RTL's recordings by :func:`check_xsi`.

    python -m examples.fir_block.fir_block_system_build --synth check     # the whole flow
"""
from __future__ import annotations

from pathlib import Path

from examples.fir_block.fir_block_build import check_xsi_outputs
from examples.fir_block.fir_block_sim import FirBlockSystem
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
TOP = "fir_block_system"
WORK_DIR = "xsi_work"


def system() -> FirBlockSystem:
    """The kernel gate's program: LOAD_TAPS -> FILTER x2 -> LOAD_TAPS -> FILTER."""
    return FirBlockSystem()


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`.  The kernel's csynth project is the kernel
    flow's (``fir_block.tcl`` at the root)."""
    dag = BuildDag()
    add_system_steps(dag, system(), work_dir=work_dir, top=TOP,
                     tcls={"fir_block": Path("fir_block.tcl")})
    return dag


def check_xsi(work: Path) -> None:
    """The kernel flow's golden check, on the RTL run's recordings."""
    check_xsi_outputs(Path(work) / "xsi_run")


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
