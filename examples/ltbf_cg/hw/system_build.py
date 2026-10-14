"""system_build.py -- the engine through the **system** flow: csynth -> RTL -> pysim -> XSI -> compare.

``plans/mimo_cg/ltbf_cg_dse.md`` steps 4.3 and 4.7 (AC5, AC5b), on ``examples/state_toy``'s pattern:
the :class:`~examples.ltbf_cg.hw.engine.LtbfSystem` graph (driver -> engine -> sink), the cut the
engine alone.  The kernel's csynth project is ``hw/build.py``'s (``ltbf_engine.tcl`` in the
configuration's build tree, which is this flow's root).  What the sink records is compared RTL
against pysim; the golden -- every X word of every job equal to the fixed-point model -- is
:func:`check_xsi`, on the RTL run's recording.

    python -m examples.ltbf_cg.hw.system_build --form factored --synth build
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from examples.ltbf_cg.hw import build as hb
from examples.ltbf_cg.hw import engine as eg
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps
from waveflow.utils.burst_io import read_burst_bundle

WORK_DIR = "xsi_work"
#: The AC5 configurations: N_rx = 64, 64 columns (8 connected UEs), 16 right-hand sides (2 own
#: UEs jointly, then each alone with H kept), a 2 x 2 tile, 16-bit words, 4 iterations.
GATE = {
    "factored": {"nmax": 64, "cmax": 64, "mmax": 16, "R": 2, "C": 2, "explicit": False, "W": 16},
    "explicit": {"nmax": 64, "cmax": 64, "mmax": 16, "R": 2, "C": 2, "explicit": True, "W": 16},
}
NIT = 4


def system(form: str) -> eg.LtbfSystem:
    return eg.LtbfSystem(**GATE[form], nit=NIT)


def root(form: str) -> Path:
    """The configuration's build tree: the kernel flow's csynth project lives here."""
    return hb.BUILD / hb.name(GATE[form])


def top(form: str) -> str:
    return f"ltbf_{form}_system"


def build_dag(form: str, work_dir=WORK_DIR) -> BuildDag:
    dag = BuildDag()
    add_system_steps(
        dag, system(form), work_dir=work_dir, top=top(form),
        tcls={"ltbf_engine": Path("ltbf_engine.tcl")},
    )  # fmt: skip
    return dag


def check_xsi(work: Path, form: str) -> np.ndarray:
    """The golden on the RTL run's recording: every X word of every job."""
    bursts = read_burst_bundle(Path(work) / "xsi_run" / "vectors" / "m_out")
    got = np.concatenate([np.asarray(b).ravel() for b in bursts]).astype(np.uint64)
    np.testing.assert_array_equal(got, np.concatenate(system(form).expected()))
    return got


if __name__ == "__main__":
    import argparse

    from waveflow.build.cli import run_dag_cli

    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--form", choices=tuple(GATE), default="factored")
    known, rest = ap.parse_known_args()
    import sys

    sys.argv = [sys.argv[0], *rest]
    run_dag_cli(
        lambda a: build_dag(known.form), description=__doc__.splitlines()[0],
        default_through="compare", root_dir=root(known.form),
        extra_args=[(("--synth",), {"choices": ("build", "check"), "default": "check",
                     "help": "build: csynth a stale top; check: fail on one instead"})],
        params_from_args=lambda a: {"synth": a.synth},
    )  # fmt: skip
