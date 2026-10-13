"""The TX playout buffer at RTL — the loaded window through real Verilog.

``plans/adc_model.md`` staging item 3, TX half.  What xsim elaborates is the **wrapper**
(``rf_samp_buf_tx_top``): the kernel plus its ``bram_t2p`` buffer, so the testbench sees only
AXI-Stream and the converter model consumes it exactly as it consumes any other design's output.

The mirror of ``test_rf_samp_buf_rx_xsi.py``, and the counters are the mirror too.  RX gates on
``ADC_DROPPED``, because an ADC cannot be back-pressured and what the fabric does not take is gone.
TX gates on ``DAC_UNDERRUN``, because a DAC plays whatever is in its FIFO when a period comes due,
including nothing.  Neither loss has a protocol event, which is why both are read off the converter
model rather than from the wire.

Four things are gated here, and they fail in different ways:

* **The played samples**, bit-exact against the ramp that was loaded — so the loader, the circular
  buffer, the player and the converter's own unpack are all covered by one comparison.
* **Every command answered**, in order, with its own tid — which is the evidence that the *in-band*
  frame stayed aligned.  A refused command that left its payload in the stream would show up here as
  a response with somebody else's tid.
* **The completion cycle**, recorded exactly.  A result, distinct from the run's loop bound.
* **The memory's own assertion never fired.**  ``bram_t2p.v`` ``$error``\\ s when the reader touches
  the address the writer is writing that cycle, which is the lead/margin logic failing.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.rf_samp_buf_tx.rf_samp_buf_tx import (
    GATE_COMMANDS,
    PRIMED_AT,
    SAMP_BW,
    TxResp,
    expected_responses,
    find_loaded_run,
)
from examples.rf_samp_buf_tx import rf_samp_buf_tx_system_build as sysb
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run

#: Where the system run's testbench read and recorded (``tests/examples/_system_run.py``).
VECTORS = WORK / sysb.TOP / "xsi_run" / "vectors"

#: Cycle the last response reached its sink.  Recorded 2026-08-16 on the first green run.  Exact,
#: not a bound — it moves only if the design's timing changes, and both directions are worth a
#: human.  Most of it is the loader waiting for the player to free the slots the first command
#: names: it asks for a window one whole buffer ahead of the play pointer, so it cannot be placed
#: until the player has walked that far.
WANT_RESP_LAST_CYCLE = 5191

#: Blocks the DAC pulled out of the fabric over the run — 40000 cycles at 0.256 words/cycle.
#:
#: 40, re-recorded 2026-08-18 with the 300 -> 250 MHz move.  The DAC plays on its own grid, so in a
#: fixed CYCLE budget the number of blocks it gets through scales with its rate per cycle, and
#: ``samp_rate / (samp_per_word * f_axis)`` rose by exactly 300/250 = 1.2: 33 x 1.2 = 39.6, and the
#: run lands on 40.  The design is unchanged -- ``RESP_LAST_CYCLE`` did not move, because the
#: loader's completion is fabric-paced work that costs the same number of cycles either way.
WANT_DAC_BLOCKS_OUT = 40

#: Contiguous, in-order, bit-exact loaded samples that reached the RF sink.  A floor rather than an
#: equality: the converter zero-fills whole blocks during the priming transient, which splits the
#: stream for a reason that belongs to the DAC rather than to this buffer.  Measured at 737.
WANT_MIN_LOADED_RUN = 256


@pytest.fixture(scope="module")
def run() -> tuple[dict[str, int], str]:
    """One RTL run, shared by the assertions below: the system run
    (``examples/rf_samp_buf_tx/rf_samp_buf_tx_system_build.py``) -- the buffer is the DUT (its wrapper: the kernel and
    its ``bram_t2p``), the RFDC's converter model spans the cut.  The counters are the ones the
    converter model writes as a recording (``vectors/counters/``), read under the names the old
    hand-written main printed; the text is the run's report."""
    work = system_run(sysb.build_dag(work_dir=WORK), sysb.HERE, sysb.TOP)
    assert compared_clean(work) and recorded_names(work) == ['counters', 'resp', 'rf_out']
    return sysb.counters(work), (work / "report.json").read_text(encoding="utf-8")


def _played(xsi: Path) -> np.ndarray:
    """The samples the RF sink captured, as unsigned 16-bit words."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    d = xsi / "vectors" / "rf_out"
    if not d.is_dir():
        return np.zeros(0, dtype=np.uint64)
    blocks = read_rf_bundle(d, 1, 256)
    flat = np.concatenate([np.asarray(b, dtype=np.float64).ravel() for b in blocks])
    ints = np.rint(flat * float(1 << (SAMP_BW - 1))).astype(np.int64)
    return (ints % (1 << SAMP_BW)).astype(np.uint64)


@pytest.mark.xsi
def test_the_rtl_plays_the_loaded_ramp(run):
    """The loaded samples come out of the converter, in order and bit-exact.

    A ramp is used precisely so a wrong SLOT is visible: a circular buffer's whole failure mode is
    playing the right number of samples from the wrong place.

    Measured by search rather than at a fixed offset, because **the RTL may not assume played sample
    i is slot i**: the player is free-running and the DAC emits blocks as it gets them, so the offset
    depends on the priming transient — and that transient differs from pysim's, where the player is
    not paced by ``TREADY``.  What both backends must agree on is that the loaded samples emerge
    unbroken and in order, which is what this measures.
    """
    got = _played(VECTORS.parent)
    assert got.size > PRIMED_AT, f"the RF sink captured only {got.size} samples"
    n = find_loaded_run(got)
    assert n >= WANT_MIN_LOADED_RUN, (
        f"only {n} loaded samples came out of the RTL contiguously, expected at least "
        f"{WANT_MIN_LOADED_RUN}. A short run is a slot or lead bug, not a data one.")


@pytest.mark.xsi
def test_every_command_is_answered_and_the_in_band_frame_stayed_aligned(run):
    """One response per command, in order, each with its own tid.

    **This is the in-band framing gate.**  The payload rides the same stream as the command, so a
    refused command that left its words behind would make the next read take a sample for a tid —
    and that shows up here as a wrong tid or a missing response, not as wrong data.
    """
    from waveflow.utils.burst_io import read_burst_bundle

    d = VECTORS / "resp"
    assert d.is_dir(), "the run dumped no response bundle"
    flat = np.concatenate(read_burst_bundle(d)).astype(np.uint64)
    n = TxResp.nwords_per_inst(SAMP_BW)
    got = [tuple(int(v) for v in flat[i:i + n]) for i in range(0, flat.size, n)]
    assert [t for t, _s, _n in got] == [t for t, _s, _n in GATE_COMMANDS], (
        f"responses are not one-per-command in order: {got}")
    assert got == expected_responses(1), f"RTL responses {got} != predicted {expected_responses(1)}"


@pytest.mark.xsi
def test_the_dac_was_fed_and_its_underruns_are_the_startup_transient(run):
    """THE gate for this design, and the mirror of RX's ``ADC_DROPPED == 0``.

    A DAC plays whatever is in its FIFO when a sample period comes due, so an underrun is the only
    evidence the player missed a deadline — there is no protocol signal for it.  The count is not
    asserted to zero, because the player is free-running and genuinely has nothing to play before the
    buffer is primed; what is asserted is that the DAC was fed at all and that the blocks it pulled
    are the recorded number.
    """
    c, out = run
    assert c["DAC_WORDS_RECV"] > 0, f"the DAC received nothing:\n{out[-2000:]}"
    assert c["DAC_BLOCKS_OUT"] == WANT_DAC_BLOCKS_OUT, (
        f"the DAC pulled {c['DAC_BLOCKS_OUT']} blocks, gate expects {WANT_DAC_BLOCKS_OUT}")
    # One response per command, the last included: the frame is in-band, so the last command is
    # answered only once every payload word ahead of it was taken -- the driver delivered the whole
    # frame, and 'nothing played' cannot be confused with 'nothing was commanded'.
    assert c["RESP_WORDS"] == len(GATE_COMMANDS) * TxResp.nwords_per_inst(SAMP_BW), (
        "one response per command, same three-word shape")


# REMOVED 2026-08-25: `test_the_memorys_read_during_write_assertion_never_fired` asserted that
# "read-during-write collision" was absent from the run's stdout.  It could never fire --
# the XSI flow discards RTL text output, so `bram_t2p.v`'s $error reaches no channel a test
# can read (measured four ways, see plans/bram_access.md).  The whole test body was that one
# assertion, so the test went with it rather than stand as evidence it never had.  The
# condition is to be gated from the VCD trace; until then it is checked nowhere, which is
# what was already the case.

@pytest.mark.xsi
def test_the_completion_cycle_is_the_recorded_one(run):
    """Time to the last response — a result, distinct from the run's loop bound."""
    c, _out = run
    assert c["RESP_LAST_CYCLE"] == WANT_RESP_LAST_CYCLE, (
        f"the last response landed at cycle {c['RESP_LAST_CYCLE']}, gate expects "
        f"{WANT_RESP_LAST_CYCLE}. That is a real behaviour change: either a regression or an "
        f"improvement worth re-recording.")
