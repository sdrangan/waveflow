"""rf_shot_rx through the **system** flow: the ping-pong receiver as the DUT, the RFDC in the testbench.

``plans/system_model.md`` S3e.  The RTL rung of both builds -- the gates are
``tests/examples/test_rf_shot_rx_xsi.py`` and ``..._abs_xsi.py``, one per build -- through
``add_system_steps``: ``csynth`` -> ``system_rtl`` (the receiver instantiated as its wrapper: the
kernel and its ``bram_t2p``) -> ``scenario`` / ``pysim`` -> ``system_xsi`` -> ``compare``.  The window
frames are compared RTL against pysim; the gates' counters come from the ADC model's own counter
recording.

The run is **traced**: the wrapper's scope is dumped (``<work>/<top>_trace.vcd``) and its hazard
manifest written beside it (``<work>/hazards.json``), which is what the gates' waveform claims (both
memory ports live together, never in one region) read.

    python -m examples.rf_shot_rx.rf_shot_rx_system_build --synth check            # the default build
    python -m examples.rf_shot_rx.rf_shot_rx_system_build --synth check --abs      # absolute_index=1
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from examples.rf_shot_rx.rf_shot_rx import RfShotRxAbs, RfShotRxSystem
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.rf_shot_rx import RfShotRx

HERE = Path(__file__).resolve().parent
WORK_DIR = "xsi_work"
#: ``{build: (DUT class, system top)}``.
BUILDS = {"default": (RfShotRx, "rf_shot_rx_system"), "abs": (RfShotRxAbs, "rf_shot_rx_abs_system")}


def system(build: str = "default") -> RfShotRxSystem:
    """The gates' run: forty ramp blocks into the receiver, 2800 cycles."""
    return RfShotRxSystem(dut_cls=BUILDS[build][0])


def build_dag(build: str = "default", work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`, traced.  The kernel's csynth project is the
    one ``rf_shot_rx_build.py`` generates (``<cpp_kernel_name>.tcl`` at the root)."""
    cls, top = BUILDS[build]
    name = cls.cpp_kernel_name
    dag = BuildDag()
    add_system_steps(dag, system(build), work_dir=work_dir, top=top,
                     tcls={name: Path(f"{name}.tcl")}, trace=True)
    return dag


def frames(work: Path) -> list[np.ndarray]:
    """The window frames the RTL run's sink captured -- header and samples."""
    from waveflow.utils.burst_io import read_burst_bundle

    return [np.asarray(b, dtype=np.uint64).ravel()
            for b in read_burst_bundle(Path(work) / "xsi_run" / "vectors" / "win")]


def counters(work: Path) -> dict:
    """The gates' counter names, from the RTL run's recordings: the ADC model's counters,
    the RF edge's, and the window sink's word count and last arrival cycle."""
    run = Path(work) / "xsi_run" / "vectors"

    def read(name: str) -> dict:
        return json.loads((run / "counters" / f"{name}.json").read_text(encoding="utf-8"))

    adc, edge = read("xsi_tb_adc_axis"), read("xsi_tb_adc_if")
    cycles = np.fromfile(run / "win" / "cycles.bin", dtype="<u8")
    return {"ADC_WORDS": adc["words_sent"], "ADC_DROPPED": adc["dropped"],
            "ADC_BLOCKS_IN": edge["transferred"], "WIN_WORDS": int(sum(f.size for f in frames(work))),
            "WIN_LAST_CYCLE": int(cycles[-1])}


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag("abs" if a.abs else "default"),
                description=__doc__.splitlines()[0], default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead")),
                            (("--abs",), dict(action="store_true",
                             help="the absolute_index=1 build"))],
                params_from_args=lambda a: {"synth": a.synth})
