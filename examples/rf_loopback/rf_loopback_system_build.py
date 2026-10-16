"""rf_loopback through the **system** flow: the RFDC in the testbench, the pass-through as the DUT.

``plans/system_model.md`` S3d.  The same design the single-kernel flow gates
(``tests/examples/test_rf_loopback_xsi.py``), taken through ``add_system_steps``: the DUT is the
pass-through kernel alone, and the converter is a testbench participant whose two models each span
the cut -- a stream port of the top on the fabric side, an RF behavioral edge to the data source or
sink on the other.  The system harness is the kernel flow's, model for model.

The sink's recording is checked by the example (:func:`check_xsi`), not compared against pysim:
the two backends legitimately disagree about sub-block loss.

    python -m examples.rf_loopback.rf_loopback_system_build --synth check
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from examples.rf_loopback.rf_loopback import RfLoopbackSystem
from examples.rf_loopback.rf_loopback_xsi import XSI_BLKSIZE, XSI_N_CYCLES, XSI_NBLK
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

HERE = Path(__file__).resolve().parent
TOP = "rf_loopback_system"
WORK_DIR = "xsi_work"
#: The single-kernel gate's numbers (``test_rf_loopback_xsi.py``): grid periods that carry data, and
#: blocks the sink collects in the run's cycle budget.
WANT_DATA_BLOCKS_OUT = 7
WANT_SINK_BLOCKS = 63


def system(variant: str = "real") -> RfLoopbackSystem:
    """The gate scenario of *variant* (``"real"``, ``"2ch"``, ``"iq"``): eight 256-sample blocks,
    the kernel flow's cycle budget."""
    return RfLoopbackSystem(variant=variant, n_src_blk=XSI_NBLK, blksize=XSI_BLKSIZE,
                            n_cycles=XSI_N_CYCLES)


def build_dag(work_dir=WORK_DIR, variant: str = "real") -> BuildDag:
    """The framework's system steps; the kernels' csynth projects are ``rf_pass_through.tcl``'s
    and, for the two-channel tile, ``rf_pass_through_2ch.tcl``'s."""
    dag = BuildDag()
    add_system_steps(dag, system(variant), work_dir=work_dir, top=TOPS[variant],
                     tcls={"rf_pass_through": Path("rf_pass_through.tcl"),
                           "rf_pass_through_2ch": Path("rf_pass_through_2ch.tcl")})
    return dag


#: Grid periods of zero-fill before the first data block at RTL (``test_rf_loopback_xsi.py``).
RTL_STARTUP_BLOCKS = 1


def check_xsi(work: Path) -> dict:
    """The example's check on the RTL run's sink recording -- the bundle half of the kernel flow's
    gate, claim for claim: the startup transient is zero-fill, the **first** data block is bit-exact,
    and the counts of data-carrying periods and of collected blocks are the kernel gate's.  Later
    blocks are not compared whole: this read-then-write relay drops ADC words structurally (the kernel
    gate's ``WANT_ADC_DROPPED_IS_STRUCTURAL``), so they land with gaps.  Returns the counts."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    sysm = system()
    sysm.write_scenario(Path(work) / "_golden")              # the sent blocks, from the same writer
    got = read_rf_bundle(Path(work) / "xsi_run" / "vectors" / "rf_out", 1, XSI_BLKSIZE)
    data = [b for b in got if np.any(b)]
    sent = sysm.runner.sent
    assert len(got) == WANT_SINK_BLOCKS, f"the sink collected {len(got)} blocks"
    for k in range(RTL_STARTUP_BLOCKS):
        assert not np.any(got[k]), f"block {k} is inside the startup transient: zero-fill"
    assert len(data) == WANT_DATA_BLOCKS_OUT, f"{len(data)} grid periods carried data"
    assert np.array_equal(data[0], sent[0]), "the first data block is not the first sent block"
    counters = check_counters(work)
    return {"sink_blocks": len(got), "data_blocks": len(data), "adc_dropped": counters["adc_dropped"]}


#: Words the ADC produces over the scenario: 8 blocks of 64 words.
WANT_ADC_WORDS = XSI_NBLK * (XSI_BLKSIZE // 4)
#: Grid periods the DAC emits over the run, and how many of them carry zero-fill.
WANT_DAC_BLOCKS_OUT = 64
WANT_DAC_ZERO_FILLED = WANT_DAC_BLOCKS_OUT - WANT_DATA_BLOCKS_OUT


def check_counters(work: Path) -> dict:
    """The counter half of the kernel flow's gate, read from the counters the RTL run's models wrote
    (``vectors/counters/<model>.json``) instead of from a hand-written main:

    * the ADC accounts for every word it produced (sent + dropped), and **drops some** -- this
      read-then-write relay cannot keep up while the DAC paces its writes, and a converter cannot
      be told to wait (the kernel gate's ``WANT_ADC_DROPPED_IS_STRUCTURAL``);
    * the DAC emits on its grid for the whole run, and every period without a surviving data block
      is zero-fill;
    * what the fabric accepted but the DAC never took is under one block (still inside the DUT);
    * the RF edges themselves lose nothing: the loss is at the fabric boundary.
    """
    import json

    def read(name: str) -> dict:
        return json.loads((Path(work) / "xsi_run" / "vectors" / "counters" / f"{name}.json")
                          .read_text(encoding="utf-8"))

    adc, dac = read("xsi_tb_adc_axis"), read("xsi_tb_dac_axis")
    adc_ch, dac_ch = read("xsi_tb_adc_if"), read("xsi_tb_dac_if")
    src = read("xsi_tb_adc_if_tx")
    assert adc["words_sent"] + adc["dropped"] == WANT_ADC_WORDS, f"ADC accounting: {adc}"
    assert adc["dropped"] > 0, f"the relay is expected to drop (pattern A): {adc}"
    assert dac["blocks_out"] == WANT_DAC_BLOCKS_OUT, f"DAC: {dac}"
    assert dac["blocks_zero_filled"] == WANT_DAC_ZERO_FILLED, f"DAC zero-fill: {dac}"
    in_flight = (WANT_ADC_WORDS - adc["dropped"]) - dac["words_recv"]
    assert 0 <= in_flight < XSI_BLKSIZE // 4, f"{in_flight} words unaccounted between ADC and DAC"
    assert adc_ch["dropped"] == 0 and dac_ch["dropped"] == 0, f"an RF edge dropped: {adc_ch} {dac_ch}"
    assert src["blocks_out"] == XSI_NBLK and adc_ch["transferred"] == XSI_NBLK
    return {"adc_dropped": adc["dropped"], "in_flight": in_flight}


#: The system top of each variant's run.
TOPS = {"real": TOP, "2ch": "rf_loopback_2ch_system", "iq": "rf_loopback_iq_system"}
#: Its graph's name -- the prefix of every testbench model's name, and so of its counter files.
TB_NAMES = {"real": "xsi_tb", "2ch": "xsi_tb_2ch", "iq": "xsi_tb_iq"}


def counters(work: Path, variant: str = "real") -> dict:
    """The counters the kernel flow's hand-written mains printed, under their names, read from the
    recordings the RTL run's models wrote (``vectors/counters/<model>.json``): the converters'
    totals, per-port counts, bound port counts and format; the RF edges'; the source's and sink's."""
    import json

    tb = TB_NAMES[variant]

    def read(name: str) -> dict:
        d = Path(work) / "xsi_run" / "vectors" / "counters"
        # A converter model spanning several ports is named for the first (``..._axis_0``).
        f = d / f"{tb}_{name}.json"
        return json.loads((f if f.is_file() else d / f"{tb}_{name}_0.json").read_text(encoding="utf-8"))

    adc, dac = read("adc_axis"), read("dac_axis")
    adc_ch, dac_ch = read("adc_if"), read("dac_if")
    src, sink = read("adc_if_tx"), read("dac_if_rx")
    out = {"ADC_WORDS_SENT": adc["words_sent"], "ADC_DROPPED": adc["dropped"],
           "ADC_N_CH": adc["n_ch"],
           "DAC_WORDS_RECV": dac["words_recv"], "DAC_UNDERRUN": dac["underrun"],
           "DAC_BLOCKS_OUT": dac["blocks_out"], "DAC_BLOCKS_ZERO_FILLED": dac["blocks_zero_filled"],
           "DAC_LAST_ZERO_FILL_IDX": dac["last_zero_fill_idx"], "DAC_N_CH": dac["n_ch"],
           "ADC_CHAN_DROPPED": adc_ch["dropped"], "ADC_CHAN_TRANSFERRED": adc_ch["transferred"],
           "DAC_CHAN_DROPPED": dac_ch["dropped"], "DAC_CHAN_TRANSFERRED": dac_ch["transferred"],
           "SRC_BLOCKS_OUT": src["blocks_out"], "SINK_BLOCKS_IN": sink["blocks_in"],
           "SINK_LAST_BLOCK_CYCLE": sink["last_block_cycle"],
           "FMT_IQ_MODE": adc["fmt_iq_mode"], "FMT_IQ_ORDER": adc["fmt_iq_order"],
           "FMT_SLOTS_PER_WORD": adc["fmt_slots_per_word"], "FMT_WORD_BITS": adc["fmt_word_bits"]}
    for i in range(adc["n_ch"]):
        out[f"ADC_WORDS_SENT_{i}"] = adc[f"words_sent_{i}"]
        out[f"ADC_DROPPED_{i}"] = adc[f"dropped_{i}"]
    for i in range(dac["n_ch"]):
        out[f"DAC_WORDS_RECV_{i}"] = dac[f"words_recv_{i}"]
        out[f"DAC_UNDERRUN_{i}"] = dac[f"underrun_{i}"]
    return out


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0],
                default_through="compare", root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead"))],
                params_from_args=lambda a: {"synth": a.synth})
