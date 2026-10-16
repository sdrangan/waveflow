"""rf_samp_buf_rx through the **system** flow: the capture buffer as the DUT, the RFDC in the testbench.

``plans/system_model.md`` S3e.  The design the kernel flow's RTL gate runs
(``tests/examples/test_rf_samp_buf_rx_xsi.py``), taken through ``add_system_steps``: ``csynth`` ->
``system_rtl`` (the buffer instantiated as its wrapper: the kernel and its ``bram_t2p``) ->
``scenario`` / ``pysim`` -> ``system_xsi`` -> ``compare``.  The two sinks' recordings are compared RTL
against pysim; the kernel gate's counters come from the ADC model's own counter recording.

    python -m examples.rf_samp_buf_rx.rf_samp_buf_rx_system_build --synth check     # the whole flow
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from examples.rf_samp_buf_rx.rf_samp_buf_rx import RfSampBufRxSystem
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
TOP = "rf_samp_buf_rx_system"
WORK_DIR = "xsi_work"


def system() -> RfSampBufRxSystem:
    """The kernel gate's run: sixteen ramp blocks, four capture commands, 40000 cycles."""
    return RfSampBufRxSystem()


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`.  The kernel's csynth project is the kernel
    flow's (``rf_samp_buf_rx.tcl`` at the root)."""
    dag = BuildDag()
    add_system_steps(dag, system(), work_dir=work_dir, top=TOP,
                     tcls={"rf_samp_buf_rx": Path("rf_samp_buf_rx.tcl")})
    return dag


def recorded(work: Path, name: str) -> np.ndarray:
    """Recording *name* of the RTL run, flat."""
    from waveflow.utils.burst_io import read_burst_bundle

    return np.concatenate(read_burst_bundle(Path(work) / "xsi_run" / "vectors" / name)
                          ).astype(np.uint64)


def counters(work: Path) -> dict:
    """The kernel gate's counter names, from the RTL run's recordings: the ADC model's counters, the
    capture's last arrival cycle, and the command / response word counts."""
    run = Path(work) / "xsi_run" / "vectors"
    adc = json.loads((run / "counters" / "xsi_tb_adc_axis.json").read_text(encoding="utf-8"))
    return {"ADC_WORDS_SENT": adc["words_sent"], "ADC_DROPPED": adc["dropped"],
            "OUT_LAST_CYCLE": int(np.fromfile(run / "out" / "cycles.bin", dtype="<u8")[-1]),
            "CMD_TOTAL": int(recorded(work, "cmd").size), "RESP_WORDS": int(recorded(work, "resp").size)}


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
