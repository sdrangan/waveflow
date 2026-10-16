"""rf_shot_tx through the **system** flow: the shot transmitter as the DUT, the RFDC in the testbench.

``plans/system_model.md`` S3e.  The RTL rung of both builds -- the gates are
``tests/examples/test_rf_shot_tx_xsi.py`` and ``..._abs_xsi.py``: two builds, two frame streams
each -- through ``add_system_steps``: ``csynth`` -> ``system_rtl`` (the transmitter instantiated as
its wrapper: the kernel and its ``bram_t2p``) -> ``scenario`` / ``pysim`` -> ``system_xsi`` ->
``compare``.  Each frame stream is a system of its own.  The responses are compared RTL against
pysim; the playout and the DAC model's counter recording are the example's to check.

The run is **traced**: the wrapper's scope is dumped (``<work>/<top>_trace.vcd``) and its hazard
manifest written beside it (``<work>/hazards.json``), which is what the gates' waveform claims (the
memory's own pins: port overlap, write range, the speculative read at a handover) read.

    python -m examples.rf_shot_tx.rf_shot_tx_system_build --synth check [--abs] [--loop]
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from examples.rf_shot_tx.rf_shot_tx import (BLKSIZE, RESP, RfShotTx, RfShotTxAbs, RfShotTxSystem,
                                            blocks_to_codes)
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
WORK_DIR = "xsi_work"
BUILDS = {"default": RfShotTx, "abs": RfShotTxAbs}
SCENARIO_NAMES = ("cmd", "cmd_loop")


def top(build: str, scenario: str) -> str:
    """The system top of one run: ``rf_shot_tx[_abs][_loop]_system``."""
    return (f"{BUILDS[build].cpp_kernel_name}{'_loop' if scenario == 'cmd_loop' else ''}"
            f"_system")


def system(build: str = "default", scenario: str = "cmd") -> RfShotTxSystem:
    """One of the gates' four runs: a build and a frame stream, 1400 cycles."""
    return RfShotTxSystem(dut_cls=BUILDS[build], scenario=scenario)


def build_dag(build: str = "default", scenario: str = "cmd", work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`, traced.  The kernel's csynth project is the
    one ``rf_shot_tx_build.py`` generates (``<cpp_kernel_name>.tcl`` at the root)."""
    name = BUILDS[build].cpp_kernel_name
    dag = BuildDag()
    add_system_steps(dag, system(build, scenario), work_dir=work_dir, top=top(build, scenario),
                     tcls={name: Path(f"{name}.tcl")}, trace=True)
    return dag


def played(work: Path) -> np.ndarray:
    """The samples the RTL run's RF sink captured, as signed converter codes."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    return blocks_to_codes(read_rf_bundle(Path(work) / "xsi_run" / "vectors" / "rf_out", 1, BLKSIZE))


def responses(work: Path) -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` off the RTL run's response recording, in arrival order."""
    from waveflow.utils.burst_io import read_burst_bundle

    words = np.concatenate(read_burst_bundle(Path(work) / "xsi_run" / "vectors" / "resp")).ravel()
    n = RESP.nwords_per_inst(64)
    out = []
    for i in range(0, words.size - n + 1, n):
        r = RESP().deserialize(words[i:i + n], word_bw=64)
        out.append((int(r.tid), int(r.status), int(r.nsamp_loaded)))
    return out


def counters(work: Path) -> dict:
    """The gates' counter names, from the RTL run's recordings: the DAC model's counters and
    the last response's arrival cycle."""
    run = Path(work) / "xsi_run" / "vectors"
    dac = json.loads((run / "counters" / "xsi_tb_dac_axis.json").read_text(encoding="utf-8"))
    return {"DAC_WORDS_RECV": dac["words_recv"], "DAC_UNDERRUN": dac["underrun"],
            "DAC_LAST_UNDERRUN_CYCLE": dac["last_underrun_cycle"],
            "DAC_BLOCKS_ZERO_FILLED": dac["blocks_zero_filled"],
            "RESP_LAST_CYCLE": int(np.fromfile(run / "resp" / "cycles.bin", dtype="<u8")[-1])}


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag("abs" if a.abs else "default", "cmd_loop" if a.loop else "cmd"),
                description=__doc__.splitlines()[0], default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead")),
                            (("--abs",), dict(action="store_true", help="the absolute_index=1 build")),
                            (("--loop",), dict(action="store_true", help="the looping frame stream"))],
                params_from_args=lambda a: {"synth": a.synth})
