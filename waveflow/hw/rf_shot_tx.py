"""rf_shot_tx.py — ``plans/rf_shot_buf.md`` Stage B: **TX — play a stored waveform**.

Stage A built the primitive (:mod:`waveflow.hw.rf_shot_buf`): a memory, a loader, a reader, and a
``rdy`` token between them.  It had no command, no payload source and no verdict, because it had
nothing to answer.  This module adds those three and nothing else::

    DMA (MM2S) --AXIS: [ShotTxHdr][samples ... TLAST]--> RfShotBufLoad --> BRAM
    DMA (S2MM) <---------------- ShotTxResp -----------                     | rdy
                                                                            v
                                     RfShotBufRead --> RfRelayoutToSlots --> Rfdc.tx_streams[0]

The payload is **in-band on the stream**, the shape ``examples/stream_inband`` teaches: a header
ahead of the samples, ``TLAST`` marking the end, and a persistent loop that halts on ``END``.

Why in-band, and not the ``m_axi`` arena the plan first chose
-------------------------------------------------------------

``plans/rf_shot_buf.md`` § *Where the payload comes from* chose ``m_axi`` with an in-band address on
2026-08-24, and § *Decisions a session must not re-open* listed it.  **Re-opened and reversed
2026-08-31**, for a constraint that was not on the table when it was written: this design is handed
to someone wiring it in Vivado IPI by hand and driving it from PYNQ.  The plan's own case for
``m_axi`` is RX-driven and concedes *"TX is the weaker case"*; TX-before-RX is a separate settled
decision, so Stage C stays free to choose its own transport with capture evidence in hand.

Three things decided it, and none is development time:

* **The port was already a stream.**  :class:`~waveflow.hw.rf_shot_buf.RfShotBufLoad`'s ``s_in`` is a
  ``StreamIFSlave`` with ``has_tlast=True``.  The arena route inserts
  :class:`~waveflow.hw.mem_stream.MemRStream` — a burst engine — to feed a port that was streaming
  anyway.
* **The short-load verdict becomes structural.**  § *The response is not optional* exists because a
  short transfer completes cleanly at the DMA while the buffer sits half loaded.  On a stream that is
  ``TLAST`` before ``nword`` words: the defect is visible **on the data path**, not inferred from a
  completion echo.
* **In Vivado it is one IP.**  MM2S carries header and payload, S2MM carries the verdict, both
  channels of the same AXI DMA.  The arena route needs the kernel's own master wired to a PS HP port
  and ``pynq.allocate().physical_address`` plumbed into the command, whose failure mode is a
  plausible-looking wrong address.

**What was given up, stated so it is not rediscovered as a surprise:** the kernel can no longer
*fetch*.  A resident library of waveforms in DDR, switched by command with no host transfer, is an
``m_axi`` capability, and pulse-to-pulse agility is where it would matter.  Nor can several modules
share one arena without a DMA each.  The escape hatch is scatter-gather DMA with pre-built
descriptors, which is **more** Vivado than the ``m_axi`` it replaces, not less.

**What was NOT given up: the ceiling.**  ``m_axi`` does not let a design exceed the buffer.  Playing
past the shot means a producer refilling while a consumer drains — a live reader and a live writer,
which is the concurrency problem :mod:`waveflow.hw.rf_tx_stream` and ``plans/rf_samp_new.md``'s
credit/ack/margin machinery exist for.  Even the BRAM-less version (burst from DDR straight at the
converter) does not escape it: variable DDR latency against a hard grid deadline forces a prefetch
FIFO, and managing that FIFO's occupancy *is* a streaming buffer.  **The transport choice does not
move the boundary in** ``docs/guide/rf/choosing.md`` **— that boundary is concurrency.**

The in-band *address* argument survives untouched, and still forbids a control register: per
``plans/design_cut.md`` ``BFM_DUALS["axilite_slave"].model is None``, so a design taking its command
from a host-written register could not be XSI-lowered at all.

**Nothing here modifies Stage A.**  ``RfShotBufLoad`` and ``RfShotBufRead`` are used exactly as they
were built and gated; the command layer sits *in front of* the loader.
"""
from __future__ import annotations

from typing import ClassVar

from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.rf_samp_buf import IDX_BW

# ---------------------------------------------------------------------------
# The opcode
# ---------------------------------------------------------------------------

#: Load the samples that follow this header, then play them.
SHOT_LOAD = 0
#: Break the persistent loop and return cleanly.  ``examples/stream_inband``'s ``END``: a free-running
#: kernel with no way to stop is one whose testbench can only end by timing out, and a timeout is
#: indistinguishable from a deadlock.
SHOT_END = 1

# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

#: The shot is in the memory and is playable.
SHOT_LOADED = 0
#: ``TLAST`` arrived before the shot was full — **the status this response exists for**.  A short
#: transfer completes cleanly at the DMA, so the host sees success while the buffer holds a block of
#: the right shape carrying half a signal.  Nothing on the host side can see it.
SHOT_SHORT = 1
#: ``nsamp`` disagrees with the shot the buffer was built for.  Build-time structure
#: (:attr:`~waveflow.hw.rf_shot_buf.RfShotBuf.nword`) is the single source for the length; a command
#: that disagreed is refused rather than truncated, because a truncated waveform is data of the wrong
#: duration and plays as a quieter, shorter signal.
SHOT_WRONG_LEN = 2
#: The header arrived while a shot was playing.  Refused **at the command**, before a word is taken,
#: rather than asserted after the fact by :class:`~waveflow.hw.rf_shot_buf.ShotPhase`.
SHOT_BUSY = 3
#: ``nsamp == 0``.  Refused for the reason :data:`~waveflow.hw.rf_tx_stream.TX_ZERO_LEN` gives next
#: door: a zero-length load has nothing to complete on, so it can never resolve.
SHOT_ZERO_LEN = 4

#: Human-readable names, so an assertion says what happened rather than a number.
SHOT_STATUS_NAMES = {
    SHOT_LOADED: "SHOT_LOADED",
    SHOT_SHORT: "SHOT_SHORT",
    SHOT_WRONG_LEN: "SHOT_WRONG_LEN",
    SHOT_BUSY: "SHOT_BUSY",
    SHOT_ZERO_LEN: "SHOT_ZERO_LEN",
}

IdxField = IntField.specialize(bitwidth=IDX_BW, signed=False)
OpField = IntField.specialize(bitwidth=8, signed=False)


# ---------------------------------------------------------------------------
# The in-band header, and the verdict that answers it
# ---------------------------------------------------------------------------

class ShotTxHdr(DataList):
    r"""The header that rides ahead of the samples on the same stream.

    Named so it cannot be confused with the two ``TxCmd``\ s that already exist, which
    ``plans/rf_shot_buf.md`` § *The commands* asks for explicitly:
    :class:`waveflow.hw.rf_tx_stream.TxCmd` names a **schedule**,
    :class:`waveflow.hw.rf_samp_buf_tx.TxCmd` names a **buffer window**, and this one names a
    **stream transaction**.  The three designs are alternatives, never layers.

    **There is no length-of-shot field.**  How many words a shot is, is build-time structure declared
    once on ``RfShotBuf.nword``; a command that restated it would be a second source that could
    disagree — the discipline :class:`~waveflow.hw.rfdc.Rfdc` follows by reading ``samp_rate`` off the
    clock rather than declaring its own.  ``nsamp`` is here because it is what the *host* believes it
    is sending, and catching that belief disagreeing with what arrived is the verdict's whole job.
    """

    include_filename: ClassVar[str | None] = "rf_shot_tx_hdr.h"
    elements = {
        "opcode":  {"schema": OpField, "description": "SHOT_LOAD or SHOT_END"},
        "tid":     {"schema": IdxField, "description": "transaction id, echoed on the response"},
        "nsamp":   {"schema": IdxField, "description": "samples the host is sending (0 for END)"},
        "nrepeat": {"schema": IdxField,
                    "description": "times to play the shot once loaded (>= 1)"},
    }


class ShotTxResp(DataList):
    """One response per header, and exactly one — see ``plans/rf_shot_buf.md`` § *Why no
    ``has_response`` flag*: there is no configuration in which a command is issued and nobody wants to
    know whether it worked.

    ``nsamp_loaded`` is **what actually landed**, not what was asked for.  On :data:`SHOT_LOADED` the
    two agree; on :data:`SHOT_SHORT` the difference *is* the diagnosis, and it is the number a DMA
    cannot produce — ``sendchannel.transfer()`` knows it pushed bytes, not whether they were a whole
    waveform.
    """

    include_filename: ClassVar[str | None] = "rf_shot_tx_resp.h"
    elements = {
        "tid":          {"schema": IdxField, "description": "the header's transaction id"},
        "status":       {"schema": IdxField,
                         "description": "SHOT_LOADED / SHORT / WRONG_LEN / BUSY / ZERO_LEN"},
        "nsamp_loaded": {"schema": IdxField,
                         "description": "samples actually written to the buffer"},
    }
