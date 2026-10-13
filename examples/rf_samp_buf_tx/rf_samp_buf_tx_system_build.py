"""rf_samp_buf_tx through the **system** flow: the playout buffer as the DUT, the RFDC in the testbench.

``plans/system_model.md`` S3e.  The design the kernel flow's RTL gate runs
(``tests/examples/test_rf_samp_buf_tx_xsi.py``), taken through ``add_system_steps``: ``csynth`` ->
``system_rtl`` (the buffer instantiated as its wrapper: the kernel and its ``bram_t2p``) ->
``scenario`` / ``pysim`` -> ``system_xsi`` -> ``compare``.  The responses are compared RTL against
pysim; the RF capture and the DAC model's counter recording are the example's to check.

    python -m examples.rf_samp_buf_tx.rf_samp_buf_tx_system_build --synth check     # the whole flow
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from examples.rf_samp_buf_tx.rf_samp_buf_tx import SAMP_BW, RfSampBufTxSystem
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
TOP = "rf_samp_buf_tx_system"
WORK_DIR = "xsi_work"


def system() -> RfSampBufTxSystem:
    """The kernel gate's run: the in-band load frame, a DAC-only tile, 40000 cycles."""
    return RfSampBufTxSystem()


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`.  The kernel's csynth project is the kernel
    flow's (``rf_samp_buf_tx.tcl`` at the root)."""
    dag = BuildDag()
    add_system_steps(dag, system(), work_dir=work_dir, top=TOP,
                     tcls={"rf_samp_buf_tx": Path("rf_samp_buf_tx.tcl")})
    return dag


def played(work: Path) -> np.ndarray:
    """The samples the RF sink captured in the RTL run, as unsigned 16-bit words."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    blocks = read_rf_bundle(Path(work) / "xsi_run" / "vectors" / "rf_out", 1, 256)
    flat = np.concatenate([np.asarray(b, dtype=np.float64).ravel() for b in blocks])
    ints = np.rint(flat * float(1 << (SAMP_BW - 1))).astype(np.int64)
    return (ints % (1 << SAMP_BW)).astype(np.uint64)


def recorded(work: Path, name: str) -> np.ndarray:
    """Recording *name* of the RTL run, flat."""
    from waveflow.utils.burst_io import read_burst_bundle

    return np.concatenate(read_burst_bundle(Path(work) / "xsi_run" / "vectors" / name)
                          ).astype(np.uint64)


def counters(work: Path) -> dict:
    """The kernel gate's counter names, from the RTL run's recordings: the DAC model's counters, the
    last response's arrival cycle, and the command / response word counts."""
    run = Path(work) / "xsi_run" / "vectors"
    dac = json.loads((run / "counters" / "xsi_tb_dac_axis.json").read_text(encoding="utf-8"))
    return {"DAC_WORDS_RECV": dac["words_recv"], "DAC_BLOCKS_OUT": dac["blocks_out"],
            "RESP_LAST_CYCLE": int(np.fromfile(run / "resp" / "cycles.bin", dtype="<u8")[-1]),
            "RESP_WORDS": int(recorded(work, "resp").size)}


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
