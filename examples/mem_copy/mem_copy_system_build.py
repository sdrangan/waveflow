"""mem_copy through the **system** flow: the kernel as the DUT of an ``HwSystem`` with no crossbar.

``plans/system_model.md`` S3c.  The same design the single-kernel flow builds (``mem_copy_build.py``),
taken through ``add_system_steps`` instead: ``csynth`` -> ``system_rtl`` (no crossbar, so no IP: the
kernel's two streams and two ``m_axi`` bundles are top ports) -> ``scenario`` / ``pysim`` ->
``system_xsi`` -> ``compare``.  The testbench -- the command driver, the completion sink and the
memory behind both bundles -- is answered by its participants' own models, and what the sink and
the memory record is compared RTL against pysim.  The golden check (the destination equals the
source, one completion per job, the last completion at 2908 cycles) runs on the RTL's recordings
afterwards: :func:`check_xsi`.

    python -m examples.mem_copy.mem_copy_system_build --synth check     # the whole flow
"""
from __future__ import annotations

from pathlib import Path

from examples.mem_copy.mem_copy import XSI_JOBS, check_mem_copy_xsi_outputs
from examples.mem_copy.mem_copy_sim import MemCopySystem
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
TOP = "mem_copy_system"
WORK_DIR = "xsi_work"
#: The single-kernel flow's gate, which this flow must reproduce.
EXPECTED_CYCLES = 2908


def system() -> MemCopySystem:
    """The 16-job scenario the XSI gate runs."""
    return MemCopySystem(jobs=tuple(XSI_JOBS))


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`.  The kernel's csynth project is the
    single-kernel flow's (``mem_copy.tcl`` at the root); ``include/`` and ``gen/`` are its sources."""
    dag = BuildDag()
    add_system_steps(dag, system(), work_dir=work_dir, top=TOP,
                     tcls={"mem_copy": Path("mem_copy.tcl")})
    return dag


def check_xsi(work: Path, want_cycles: int | None = EXPECTED_CYCLES) -> int:
    """The golden check on the RTL run's recordings: every destination region equals the source,
    one completion per job, and the cycle the last one landed.  Returns that cycle."""
    return check_mem_copy_xsi_outputs(Path(work) / "xsi_run", want_cycles, jobs=XSI_JOBS)


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
