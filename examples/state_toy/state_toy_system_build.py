"""state_toy through the **system** flow: the accumulator as the DUT of an ``HwSystem``.

``plans/system_model.md`` S3e.  The same design the kernel flow builds (``state_toy.py``), taken through
``add_system_steps``: ``csynth`` -> ``system_rtl`` (no crossbar, so no IP: the kernel's two streams
are top ports) -> ``scenario`` / ``pysim`` -> ``system_xsi`` -> ``compare``.  The sink's recording is
compared RTL against pysim; the golden -- per-lane totals climbing 1..5 across five firings, the
kernel flow's gate (``tests/examples/test_state_toy.py``) -- is :func:`check_xsi`.

    python -m examples.state_toy.state_toy_system_build --synth check     # the whole flow
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from examples.state_toy.state_toy import StateAccumSystem, expected_words
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps
from waveflow.utils.burst_io import read_burst_bundle

HERE = Path(__file__).resolve().parent
TOP = "state_toy_system"
WORK_DIR = "xsi_work"
NVEC = 5


def system() -> StateAccumSystem:
    """The five-vector scenario the kernel gate runs."""
    return StateAccumSystem(nvec=NVEC)


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`.  The kernel's csynth project is the kernel
    flow's (``state_accum.tcl`` at the root, with the hand-written hook as an extra source)."""
    dag = BuildDag()
    add_system_steps(dag, system(), work_dir=work_dir, top=TOP,
                     tcls={"state_accum": Path("state_accum.tcl")})
    return dag


def check_xsi(work: Path) -> np.ndarray:
    """The golden on the RTL run's recording: the per-lane running totals.  Returns the words."""
    bursts = read_burst_bundle(Path(work) / "xsi_run" / "vectors" / "m_out")
    got = np.concatenate([np.asarray(b).ravel() for b in bursts]).astype(np.uint64)
    np.testing.assert_array_equal(got, expected_words(NVEC))
    return got


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
