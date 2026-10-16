"""rf_repeat_play through the **system** flow: the transmitter as the DUT, the RFDC in the testbench.

``plans/system_model.md`` S3e.  The design the kernel flow's RTL gate runs
(``tests/examples/test_rf_circ_play_xsi.py``), taken through ``add_system_steps``: ``csynth`` ->
``system_rtl`` (no crossbar, so no IP: ``wave_in`` and ``samp_out`` are top ports) -> ``scenario`` /
``pysim`` -> ``system_xsi`` -> ``compare``.  The DAC model spans the cut, as in ``rf_loopback``; the RF
sink's recording is the example's to check -- the kernel gate's claims (the ``start_now`` window
bit-exact, two zero runs, then 15143 slots fed without a gap) on the RTL's capture.

    python -m examples.rf_repeat_play.rf_repeat_play_system_build --synth check     # the whole flow
"""
from __future__ import annotations

from pathlib import Path

from examples.rf_repeat_play.rf_repeat_play import RfCircPlaySystem
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
TOP = "rf_repeat_play_system"
WORK_DIR = "xsi_work"


def system() -> RfCircPlaySystem:
    """The kernel gate's run: one waveform pushed once, 60000 fabric cycles."""
    return RfCircPlaySystem()


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`.  The kernel's csynth project is the kernel
    flow's (``rf_repeat_play.tcl`` at the root)."""
    dag = BuildDag()
    add_system_steps(dag, system(), work_dir=work_dir, top=TOP,
                     tcls={"rf_repeat_play": Path("rf_repeat_play.tcl")})
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
