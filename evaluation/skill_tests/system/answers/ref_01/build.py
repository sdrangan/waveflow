"""The system flow's DAG: each device alone in its harness, then the pair on the bus.

    python build.py --through pair_pysim          # needs no toolchain
    python build.py --status

Three systems on one DAG, each with a prefix (``scale_``, ``chk_``, ``pair_``): ``scenario`` and
``pysim`` for each, a ``golden`` step for the two harnesses (each device states ``golden(kind)``), and
the RTL steps (``csynth``, ``system_rtl``, ``system_xsi``, ``compare``) after them.  **The RTL steps
are not run here**: they need Vitis HLS and Vivado 2025.1, and each kernel's HLS body behind
``kernel_task()`` (``ScaleCore``, ``Checksum``), and the host's C++ twin -- none of them written yet.
"""
from __future__ import annotations

from pathlib import Path

from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.harness import harness

from devices import Checksum, ScaleDev
from system import make_system

HERE = Path(__file__).resolve().parent
WORK = "xsi_work"

#: The steps that need no toolchain, one per system: what CI and this checkout can run.
NO_TOOLCHAIN = ("scale_golden", "chk_golden", "pair_pysim")


def build_dag() -> BuildDag:
    dag = BuildDag()
    add_system_steps(dag, harness(ScaleDev, workload="test"), work_dir=WORK, top="scale_dev_top",
                     prefix="scale_")
    add_system_steps(dag, harness(Checksum, workload="test"), work_dir=WORK, top="checksum_top",
                     prefix="chk_")
    add_system_steps(dag, make_system("test"), work_dir=WORK, top="pair_top", prefix="pair_")
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    raise SystemExit(run_dag_cli(build_dag, description=__doc__.splitlines()[0],
                                 default_through="pair_pysim", root_dir=HERE))
