"""The in-band interleaver through the **system** flow: the kernel as the DUT of an ``HwSystem``.

``plans/system_model.md`` S3e.  The design the kernel flow's twelve-job gate runs
(``tests/examples/test_interleaver_inband_xsi.py``, the m_axi pointer-FIFO regression), taken through
``add_system_steps``: ``csynth`` -> ``system_rtl`` (no crossbar, so no IP) -> ``scenario`` / ``pysim``
-> ``system_xsi`` -> ``compare``.  What the sink and the memory record is compared RTL against pysim;
the golden -- every Y region bit-exact, one completion per job, the last at **1300** cycles -- runs on
the RTL's recordings: :func:`check_xsi`.

    python -m examples.interleaver.interleaver_inband_system_build --synth check     # the whole flow
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from examples.interleaver.interleaver_inband import (XSI_GATE_N_CYCLES, XSI_GATE_SIZES,
                                                     check_xsi_outputs)
from examples.interleaver.interleaver_inband_sim import InterleaverInbandSystem
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
TOP = "interleaver_inband_system"
WORK_DIR = "xsi_work"
#: The kernel flow's gate, which this flow must reproduce: the cycle of the last completion.
WANT_LAST_DONE_CYCLE = 1300


def system() -> InterleaverInbandSystem:
    """The twelve-job scenario the kernel gate runs -- past the old pointer-FIFO wall at six."""
    return InterleaverInbandSystem(sizes=XSI_GATE_SIZES, n_cycles=XSI_GATE_N_CYCLES)


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`.  The kernel's csynth project is the kernel
    flow's (``interleaver_inband.tcl`` at the root)."""
    dag = BuildDag()
    add_system_steps(dag, system(), work_dir=work_dir, top=TOP,
                     tcls={"interleaver_inband": Path("interleaver_inband.tcl")})
    return dag


def check_xsi(work: Path, want_cycle: int | None = WANT_LAST_DONE_CYCLE) -> int:
    """The kernel gate on the RTL run's recordings: every Y region equals the golden, one completion
    per job, and the cycle the last one landed.  Returns that cycle."""
    run = Path(work) / "xsi_run"
    cycles = np.fromfile(run / "vectors" / "s_done" / "cycles.bin", dtype=np.uint64)
    assert len(cycles) == len(XSI_GATE_SIZES), (
        f"{len(cycles)} of {len(XSI_GATE_SIZES)} jobs completed -- 6 is the m_axi pointer-FIFO "
        f"deadlock (plans/maxi_pointer_fifo.md)")
    check_xsi_outputs(run, sizes=XSI_GATE_SIZES)
    last = int(cycles[-1])
    if want_cycle is not None:
        assert last == want_cycle, f"last completion at cycle {last}, want {want_cycle}"
    return last


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
