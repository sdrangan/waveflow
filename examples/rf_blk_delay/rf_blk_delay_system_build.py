"""rf_blk_delay through the **system** flow: the pattern-B loop as the DUT, the RFDC in the testbench.

``plans/system_model.md`` S3e.  The design the kernel flow's RTL gate runs
(``tests/examples/test_rf_blk_delay_xsi.py``), taken through ``add_system_steps``: ``csynth`` ->
``system_rtl`` (the loop instantiated as its wrapper: the kernel and both buffers' ``bram_t2p``) ->
``scenario`` / ``pysim`` -> ``system_xsi`` -> ``compare``.  The kernel gate read its counters from a
hand-written main; here the converter models write them as a recording (``vectors/counters/``), and
:func:`check_xsi` reads them with the sinks' captures.

    python -m examples.rf_blk_delay.rf_blk_delay_system_build --synth check     # the whole flow
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from examples.rf_blk_delay.rf_blk_delay import (BLKSIZE, SAMP_BW, RfBlkDelaySystem,
                                                ramp_samples)
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
TOP = "rf_blk_delay_system"
WORK_DIR = "xsi_work"
WORD_BW = 64


def system() -> RfBlkDelaySystem:
    """The kernel gate's run: SRC_NBLK ramp blocks through both converters, 60000 cycles."""
    return RfBlkDelaySystem()


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """The framework's system steps for :func:`system`.  The kernel's csynth project is the kernel
    flow's (``rf_blk_delay.tcl`` at the root)."""
    dag = BuildDag()
    add_system_steps(dag, system(), work_dir=work_dir, top=TOP,
                     tcls={"rf_blk_delay": Path("rf_blk_delay.tcl")})
    return dag


def played(work: Path) -> np.ndarray:
    """The samples the RF sink captured in the RTL run, as unsigned words -- the far side of the
    DAC."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    blocks = read_rf_bundle(Path(work) / "xsi_run" / "vectors" / "rf_out", 1, BLKSIZE)
    flat = np.concatenate([np.asarray(b, dtype=np.float64).ravel() for b in blocks])
    ints = np.rint(flat * float(1 << (SAMP_BW - 1))).astype(np.int64)
    return (ints % (1 << SAMP_BW)).astype(np.uint64)


def responses(work: Path, name: str, cls) -> tuple[list[tuple[int, ...]], int, int]:
    """``(responses, words, last arrival cycle)`` of response bundle *name*, deserialized through
    the schema."""
    from waveflow.utils.burst_io import read_burst_bundle

    d = Path(work) / "xsi_run" / "vectors" / name
    flat = np.concatenate(read_burst_bundle(d)).astype(np.uint64)
    n = cls.nwords_per_inst(WORD_BW)
    out = [tuple(int(getattr(cls().deserialize(flat[i:i + n], word_bw=WORD_BW), f))
                 for f in cls.elements) for i in range(0, flat.size, n)]
    cycles = np.fromfile(d / "cycles.bin", dtype="<u8")
    return out, int(flat.size), int(cycles[-1])


def counters(work: Path) -> dict:
    """The kernel gate's counter names, read from the recordings the RTL run's models wrote."""
    def read(name: str) -> dict:
        return json.loads((Path(work) / "xsi_run" / "vectors" / "counters" / f"{name}.json")
                          .read_text(encoding="utf-8"))

    from waveflow.hw.rf_samp_buf import RxResp
    from waveflow.hw.rf_samp_buf_tx import TxResp

    adc, dac = read("xsi_tb_adc_axis"), read("xsi_tb_dac_axis")
    _rx, rx_words, rx_last = responses(work, "rxresp", RxResp)
    _tx, tx_words, tx_last = responses(work, "txresp", TxResp)
    return {"ADC_WORDS_SENT": adc["words_sent"], "ADC_DROPPED": adc["dropped"],
            "ADC_LAST_DROP_CYCLE": adc["last_drop_cycle"],
            "DAC_WORDS_RECV": dac["words_recv"], "DAC_UNDERRUN": dac["underrun"],
            "DAC_LAST_UNDERRUN_CYCLE": dac["last_underrun_cycle"],
            "DAC_BLOCKS_ZERO_FILLED": dac["blocks_zero_filled"], "DAC_BLOCKS_OUT": dac["blocks_out"],
            "RX_RESP_WORDS": rx_words, "TX_RESP_WORDS": tx_words,
            "RX_RESP_LAST_CYCLE": rx_last, "TX_RESP_LAST_CYCLE": tx_last}


def measured_delay(p: np.ndarray) -> int | None:
    """Where input sample 0 lands in what the DAC played."""
    want = ramp_samples()[:16]
    for i in range(max(0, p.size - 16)):
        if np.array_equal(p[i:i + 16], want):
            return i
    return None


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
