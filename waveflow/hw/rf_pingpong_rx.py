r"""rf_pingpong_rx.py — **continuous capture**: fill one half while a reader drains the other.

``plans/t2p_lock_chan.md`` S2, and the second consumer of
:class:`~waveflow.hw.locked_mem.LockedT2pMemIF`.  The RX counterpart of
:mod:`waveflow.hw.rf_shot_loop`, and the direction where the region parameter stops being an
optimisation and starts being correctness::

    Rfdc.rx_streams[0] --slots--> RfRelayoutToDense --dense--> PingPongCapture --[lock]--> [ BRAM ]
                                                                     |                        |
                                                                    rdy                       |
                                                                     v                        |
                                                            PingPongWindow --[lock]-----------+
                                                                     |
                                                                  w_out (one frame per window)

**Why two regions here and one on TX.**  On TX a handover is a *gap* — the converter plays filler for
as long as the swap takes, and you had already accepted discontinuity when you asked to change
waveform.  On RX there is no such option: **you cannot back-pressure an ADC**, so a reader holding
the region the capture needs is not a gap, it is *lost samples*.  Two disjoint regions are what make
"nothing is dropped" reachable at all, and this module is where ``[start, end)`` is finally exercised
as a range rather than as the whole memory.

The lock arbitrates; it does not synchronise
--------------------------------------------
**There is a ``rdy`` channel, and its existence is a finding rather than an oversight.**  The lock
answers *may I touch these addresses* — it has no way to say *there is something there worth
touching*.  A reader that alternated blindly would acquire a half the capture had not filled yet and
drain zeros, which is the plausible-samples failure this whole arc keeps meeting.  So the capture
announces each region as it completes it, exactly as
:class:`~waveflow.hw.rf_shot_buf.RfShotBufLoad` announces a shot, and the reader blocks on that
announcement before it asks for anything.

One channel, one word, and the word is the region's **base address** — not an index, because an index
would be a second encoding of a geometry the lock already speaks in addresses.

What the capture will not do
----------------------------
**It will not stall, and it will not overwrite an unread region.**  Those two together are what
produce a drop: every firing it takes a block off its input whatever happens (a task that
back-pressured an ADC would be modelling something that cannot exist), and it writes that block only
into a region that is *free* — not yielded to the reader, and not still full of samples nobody has
read.  When there is no such region the block is **discarded and counted**.

That counter is the design's, not the interface's.  ``plans/t2p_lock_chan.md`` is explicit: *the count
is the design's to produce and the gate's to assert; the interface does not supply it.*  See
:attr:`PingPongCapture.n_dropped`, and :meth:`RfPingPongRx.assert_no_loss` for the verdict that makes
it loud — because a dropped block is otherwise perfectly silent, in exactly the way sub-block loss
already was.

**The strongest statement of "nothing was dropped" is not the counter.**  It is that the windows,
concatenated, are *contiguous*: the source is a ramp, so a gap in the numbers is a gap in the capture
and no counter has to be believed.  :meth:`RfPingPongRx.assert_windows_contiguous`.

The count is on the wire, and so is a verdict
---------------------------------------------
A Python counter is invisible to a host and invisible to the RTL.  So every window goes out as a
**frame** — one :class:`CaptureWindowHdr` and then the samples — and the header carries both halves of
what a host needs:

* ``n_dropped``, the words lost **since reset**.  Cumulative, never incremental, for
  :mod:`waveflow.hw.reverse_stream`'s rule 1: a lost cumulative value is harmless because the next
  one carries the whole truth, and a lost *increment* is wrong forever.
* ``status``, :data:`CAP_OK` or :data:`CAP_LOST` — **was anything lost immediately before this
  window?**  That is the actionable question, and it is not derivable from one cumulative reading; a
  host would have to remember the last one and subtract.  The design already knows, so it says so.

The two are different questions and both are asked, which is the same split
:class:`~waveflow.hw.rf_shot_tx.ShotTxResp` makes between a status and an ``nsamp_loaded``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.bram import T2pBram, word_element
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.hw.locked_mem import (
    LOCK_ACQUIRE,
    LOCK_GRANTED,
    LOCK_STATUS_NAMES,
    LockedMemMasterIF,
    LockedMemSlaveIF,
    LockedT2pMemIF,
)
from waveflow.hw.mem_stream import KernelTask
from waveflow.hw.rf_relayout import RfRelayoutToDense
from waveflow.hw.rf_shot_buf import WORD_BW
from waveflow.simulation.simobj import ProcessGen

#: Nothing was lost immediately before this window: it is contiguous with the one before it.
CAP_OK = 0
#: Samples were lost between the previous window and this one.  **A verdict, not a count** — a host
#: that only had the cumulative number would have to remember the last one and subtract, and the
#: design already knows the answer.
CAP_LOST = 1

#: Human-readable names, so an assertion says what happened rather than a number.
CAP_STATUS_NAMES = {CAP_OK: "CAP_OK", CAP_LOST: "CAP_LOST"}

#: Width of the cumulative drop counter on the wire.  **It wraps**, and that is stated rather than
#: pretended away: 28 bits is 268 million words, which at any converter rate this repo models is
#: hours — and the field is cumulative, so a wrap is a step a host can see rather than a value it
#: silently mis-reads.  ``8 + 28 + 28`` is exactly 64, the width everything in this arc speaks.
DROP_BW = 28

_StatusField = IntField.specialize(bitwidth=8, signed=False)
_AddrField = IntField.specialize(bitwidth=DROP_BW, signed=False)


class CaptureWindowHdr(DataList):
    """The word that rides ahead of every window, and travels twice.

    It is written **once**, by the capture, onto the ``rdy`` channel — the announcement that a region
    is complete — and the window reader forwards it verbatim as the header of the frame it hands the
    host.  One schema rather than two because it is one statement: *here is a region, here is what was
    lost before it*.  A second schema would be a second place for the two to disagree.

    **``base_addr`` is an address, not a region index.**  The lock speaks in addresses and the reader
    turns this straight into an ``acquire``; an index would be a second encoding of a geometry that
    already has one.
    """

    include_filename: ClassVar[str | None] = "capture_window_hdr.h"
    elements = {
        "status":    {"schema": _StatusField, "description": "CAP_OK or CAP_LOST"},
        "base_addr": {"schema": _AddrField,
                      "description": "first element of the region this window came from"},
        "n_dropped": {"schema": _AddrField,
                      "description": "words lost since reset, CUMULATIVE (wraps at 2**28)"},
    }


#: Schema classes a build emits C++ headers for.  One: the whole vocabulary of this design's status
#: is a header on a window.  Compare :data:`~waveflow.hw.rf_shot_tx.SHOT_TX_SCHEMA_CLASSES`, which is
#: two because TX has a command to answer; a capture is asked nothing.
CAPTURE_SCHEMA_CLASSES = [CaptureWindowHdr]


def split_windows(frames, bitwidth: int = WORD_BW):
    """``[frame, ...]`` -> ``[(hdr, samples), ...]`` — the frame layout, read by the design that
    defines it.

    A gate that sliced the header off by hand would be a second author of this layout, and the two
    would be free to disagree about a field width.  The schema's own deserializer is the one place
    it lives.
    """
    hn = CaptureWindowHdr.nwords_per_inst(int(bitwidth))
    out = []
    for f in frames:
        raw = np.asarray(f, dtype=np.uint64).ravel()
        out.append((CaptureWindowHdr().deserialize(raw[:hn], word_bw=int(bitwidth)), raw[hn:]))
    return out


#: Regions the memory is split into.  **Two, and fixed at S2.**  Three would need an allocator and a
#: policy for which one to hand out, which is S3's — and two is what the plan's *"the writer fills
#: ``[256, 512)`` while the reader drains ``[0, 256)``"* asks for.  It is a module constant rather
#: than a parameter because a design with a different number is a different design, not a
#: configuration of this one.
N_REGION = 2


# ---------------------------------------------------------------------------
# The capture — the OWNER, and the side that cannot stop
# ---------------------------------------------------------------------------

@dataclass
class PingPongCapture(FreeRunMod):
    r"""Take a block every firing; put it in a free region, or drop it and say so.

    The owner side of the lock: it holds the whole memory by default, yields a region on request, and
    polls the command channel exactly once per :attr:`blk_words` elements of its own work — which is
    what makes the reader's wait for a grant a stated number.

    **The ordering rule is the same one TX turns on, and here it is free.**  A grant must not go out
    while this task is touching the region.  On TX that needed a state change before the grant; here
    it needs *nothing*, because the reader only ever asks for a region this task has already
    announced as full and moved off.  The guard is still enforced —
    :meth:`~waveflow.hw.locked_mem.LockedMemSlaveIF.grant` takes the region out of the owner's hands
    before the answer goes on the wire — so a design that drifted into granting the region it is
    filling raises on its very next write rather than corrupting a window.

    **Two flags decide everything**: which region is being filled, and which regions still hold
    samples nobody has read.  A region becomes free when the reader *releases* it, and the release is
    what clears the flag — so a reader that never comes back stops the capture using that half
    forever, which is the correct behaviour and is exactly what :attr:`n_dropped` counts.
    """

    cpp_kernel_name: ClassVar[str | None] = "pingpong_capture"

    #: Word width in bits — the converter's, the memory's, one number.
    bitwidth: HwParam[int] = WORD_BW
    #: Memory depth in **elements**.  Split into :data:`N_REGION` equal regions, so it must divide.
    depth: HwParam[int] = 512
    #: Words per block: the chunk this task moves per firing **and** its poll period.  One number,
    #: because they are one boundary — a converter block is the quantum on the input edge, and a poll
    #: per block is the natural cadence for a grant.
    blk_words: HwParam[int] = 16
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, bw = int(self.bitwidth), int(self.depth), int(self.blk_words)
        if d % N_REGION:
            raise ValueError(
                f"a {d}-element memory does not split into {N_REGION} equal regions. The halves are "
                f"the design, not a rounding: an odd split would make one window shorter and the "
                f"contiguity check would have to know which.")
        if bw < 1 or (d // N_REGION) % bw:
            raise ValueError(
                f"blk_words={bw} does not divide a {d // N_REGION}-element region. A block that "
                f"straddled a region boundary would have to be split across two locks, and the half "
                f"on the far side would land in memory the capture may not hold.")
        #: Densely-packed samples from the converter's side of the re-layout.
        self.samp_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_samp_in", bitwidth=w,
                                     has_tlast=True)
        #: One endpoint, three channels: the memory port (**write** — this is the RX inversion), the
        #: command in, the response out.
        self.lock = LockedMemSlaveIF(sim=self.sim, name=f"{self.name}_lock",
                                     element_type=word_element(w), nelem=d, access="write",
                                     check_period=bw)
        #: *Region ready*: one :class:`CaptureWindowHdr` per region this task finishes filling.  See
        #: the module docstring — the lock arbitrates, it does not synchronise, so the announcement is
        #: a channel of its own.
        self.rdy_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_rdy", bitwidth=w,
                                      has_tlast=True)
        for ep in (self.samp_in, self.lock, self.rdy_out):
            self.add_endpoint(ep)

        #: Which region is being filled, and how far into it.  ``static``\ s in the C++ twin.
        self.cur = 0
        self.wp = 0
        #: Per region: does it still hold samples nobody has read?  A region is free when the reader
        #: releases it, never merely when the reader takes it.
        self.full = [False] * N_REGION
        #: **The count the interface does not supply.**  Blocks taken off the input that had nowhere
        #: to go, in words.  Zero is the design's claim; non-zero is the reader not keeping up.
        self.n_dropped = 0
        #: Words written, blocks taken, regions announced.  All three, because a run that dropped
        #: nothing *and moved nothing* looks identical to a run that worked.
        self.n_written = 0
        self.n_blocks = 0
        self.n_ready = 0
        #: :attr:`n_dropped` as it stood when the *previous* region was announced.  The difference is
        #: what turns a cumulative count into the per-window verdict a host can act on.
        self._announced_dropped = 0

    # -- geometry ----------------------------------------------------------------------------

    @property
    def region_words(self) -> int:
        """Elements in one region — ``depth // N_REGION``."""
        return int(self.depth) // N_REGION

    def region(self, i: int) -> tuple[int, int]:
        """Region *i* as ``[lo, hi)``.  Half-open, so the two are adjacent with no ±1 anywhere."""
        n = self.region_words
        return int(i) * n, (int(i) + 1) * n

    def kernel_task(self) -> KernelTask:
        # `lock` appears ONCE and becomes THREE arguments, spliced in adjacent in
        # physical_endpoints() order -- which is why the C++ takes (buf, cmd, resp) together.
        return KernelTask("pingpong_capture_task", "pingpong_capture_task.h",
                          ("samp_in", "lock", "rdy_out"),
                          template_args=(int(self.bitwidth), int(self.depth), N_REGION,
                                         int(self.blk_words)))

    # -- the pysim twin ----------------------------------------------------------------------

    def _free_region(self) -> int | None:
        """A region this task may fill: not yielded, and not still holding an unread window."""
        for i in range(N_REGION):
            lo, hi = self.region(i)
            if not self.full[i] and self.lock.may_touch(lo) and self.lock.may_touch(hi - 1):
                return i
        return None

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one block in, one block placed **or dropped**, and exactly one poll.

        ``get(nwords_max=blk_words)`` because one C++ firing consumes one whole block — the rule is
        to match the pysim read granularity to the **task firing**, never to the word.  The input is
        taken *first and unconditionally*: a body that read only when it had room would be
        back-pressuring an ADC, which is not a thing that can happen.
        """
        bw = int(self.blk_words)
        words = np.asarray((yield from self.samp_in.get(nwords_max=bw)),
                           dtype=np.uint64).ravel()[:bw]
        self.n_blocks += 1

        lo, hi = self.region(self.cur)
        if self.full[self.cur] or self.wp + bw > hi:
            nxt = self._free_region()
            if nxt is None:
                # NOWHERE TO PUT IT.  The block is gone -- and it is gone for the one reason RX has
                # and TX does not: the reader is holding, or has not drained, the region this task
                # needs.  Counted rather than raised, because on a real ADC this is a fact about the
                # run and not a bug in the design.
                self.n_dropped += bw
                yield self.timeout(bw / float(self.clk.freq))
                yield from self._poll()
                return
            self.cur = nxt
            lo, hi = self.region(nxt)
            self.wp = lo

        yield from self.lock.write_pipelined(words, addr=self.wp)
        self.wp += bw
        self.n_written += bw
        if self.wp >= hi:
            # The region is complete.  Mark it, announce it, and leave `cur` where it is -- the next
            # firing will look for a free region and find the other one.
            self.full[self.cur] = True
            self.n_ready += 1
            # THE VERDICT IS DECIDED HERE, WHERE THE ANSWER IS KNOWN.  Anything lost since the last
            # announcement fell immediately before this window, so this window is not contiguous with
            # the one before it -- which is the question a host actually has, and the one a single
            # cumulative reading cannot answer.
            hdr = CaptureWindowHdr()
            hdr.status = CAP_LOST if self.n_dropped > self._announced_dropped else CAP_OK
            hdr.base_addr = int(lo)
            hdr.n_dropped = int(self.n_dropped) & ((1 << DROP_BW) - 1)
            self._announced_dropped = int(self.n_dropped)
            # A BLOCKING write, and it cannot block: at most N_REGION regions can be full at once, so
            # at most N_REGION announcements can be outstanding, and the channel is that deep.
            yield from self.rdy_out.write(hdr)
        yield from self._poll()

    def _poll(self) -> ProcessGen[None]:
        """Exactly one look at the lock channel, outside everything above.

        That *is* what ``check_period`` means, and it is what keeps the datapath's II untouched: the
        chunk is a counted loop with nothing data-dependent in its trip count, and the lock traffic
        is the outer loop's.

        A ``RELEASE`` is applied by ``handle_nb`` and clears the region's *full* flag here — the
        release is what makes a region reusable, never the acquire.  An ``ACQUIRE`` comes back
        untouched, and is granted with no state change: this task only ever yields a region it has
        already announced and moved off.
        """
        cmd = yield from self.lock.handle_nb()
        if cmd is None:
            return
        idx = int(cmd.start_addr) // self.region_words
        if int(cmd.opcode) == LOCK_ACQUIRE:
            yield from self.lock.grant(int(cmd.start_addr), int(cmd.end_addr))
        elif 0 <= idx < N_REGION:
            self.full[idx] = False


# ---------------------------------------------------------------------------
# The window reader — the REQUESTER
# ---------------------------------------------------------------------------

@dataclass
class PingPongWindow(FreeRunMod):
    r"""Wait to be told a region is ready, take it, drain it, give it back.

    The requester side of the lock: it holds nothing, arrives with a transaction, and is the bursty
    half.  One firing is one window, which is one frame on :attr:`w_out`.

    **It blocks on ``rdy`` before it asks for anything**, and that ordering is the whole reason the
    channel exists: acquiring a region the capture has not filled would drain zeros, and zeros out of
    a capture buffer look exactly like a quiet signal.

    :attr:`stall_blocks` is the knob that makes this design **fail on purpose**.  A reader that holds
    its window longer than the capture takes to fill the other region is the one thing that loses
    samples on RX, and a gate that could not produce that condition could not tell a design that
    keeps up from one that merely was not pushed.  It is sim-only and reaches no template argument.
    """

    cpp_kernel_name: ClassVar[str | None] = "pingpong_window"

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = 512
    #: Words handed to the host per burst.  The window is emitted as **one frame**, and this is the
    #: burst inside it — a modelling shape on the host edge, the same accommodation the converter
    #: edge gets on TX.
    blk_words: HwParam[int] = 16

    #: Blocks' worth of time to sit on the window before releasing it — **sim-only, and there to
    #: break things**.  ``0`` is the design; anything else is the dirty run.
    stall_blocks: int = 0
    #: Seconds one block takes at the source's rate, for :attr:`stall_blocks` to be measured in.
    #: ``None`` means the fabric's own rate.
    blk_period: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, bw = int(self.bitwidth), int(self.depth), int(self.blk_words)
        if d % N_REGION or (d // N_REGION) % bw:
            raise ValueError(
                f"a {d}-element memory does not split into {N_REGION} regions of whole "
                f"{bw}-word blocks")
        #: One :class:`CaptureWindowHdr` per region the capture has finished filling.
        self.rdy_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_rdy", bitwidth=w,
                                    has_tlast=True)
        #: One endpoint, three channels: the memory port (**read** — the RX inversion), the command
        #: out, the response in.
        self.lock = LockedMemMasterIF(sim=self.sim, name=f"{self.name}_lock",
                                      element_type=word_element(w), nelem=d, access="read")
        #: The window, out to the host.  A boundary port with its own ``TLAST`` so a host reading it
        #: through a DMA S2MM channel learns where one window ends without being told how long it is.
        self.w_out = FramedStreamIFMaster(sim=self.sim, name=f"{self.name}_w_out", bitwidth=w,
                                          has_tlast=True)
        for ep in (self.rdy_in, self.lock, self.w_out):
            self.add_endpoint(ep)

        #: Windows drained, and the base address of each in order.  The order is the evidence the
        #: ping-pong is a ping-pong rather than one half being read twice.
        self.n_windows = 0
        self.bases: list[int] = []
        #: ``(status, n_dropped)`` for every window, as it went out on the wire.  Read from here only
        #: for convenience; the gate reads the **stream**, because the wire is what a host sees.
        self.hdrs: list[tuple[int, int]] = []

    @property
    def region_words(self) -> int:
        return int(self.depth) // N_REGION

    def kernel_task(self) -> KernelTask:
        return KernelTask("pingpong_window_task", "pingpong_window_task.h",
                          ("rdy_in", "lock", "w_out"),
                          template_args=(int(self.bitwidth), int(self.depth), N_REGION,
                                         int(self.blk_words)))

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one window: wait, acquire, drain, release.

        The drain is one anchored ``read_pipelined`` over the whole region and one framed write, for
        the reason the TX loader's is: one C++ firing consumes the whole window inside its counted
        loop, so one pysim call must too.
        """
        w, n = int(self.bitwidth), self.region_words
        hdr = yield from self.rdy_in.get_schema(CaptureWindowHdr)
        base = int(hdr.base_addr)
        status = yield from self.lock.acquire(base, base + n)
        if status != LOCK_GRANTED:
            # Unreachable while the capture only announces regions inside the memory it declared --
            # which is build-time structure, checked at construction on both ends.  Raised rather
            # than counted, because a region the design named and the memory refuses is a wiring
            # fault and not a fact about the run.
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': ACQUIRE [{base}, {base + n}) came back "
                f"{LOCK_STATUS_NAMES.get(status, status)}. The capture announced a region the "
                f"memory does not have, so the two ends disagree about the geometry.")
        data, t0 = yield from self.lock.read_pipelined(word_element(w), n, addr=base)
        # THE HEADER GOES OUT AHEAD OF THE SAMPLES, IN **ONE** FRAME -- one burst, one TLAST, at the
        # end.  Two writes would be two bursts and therefore two frames, and a host reading through a
        # DMA would see the header arrive as a transfer of its own; the C++ twin writes the header
        # beat and the payload beats into a single axi4s frame, so a split here would make the two
        # backends disagree about the boundary rather than about a value.
        #
        # Forwarded verbatim: this task is not the author of the verdict and must not become a second
        # one -- it did not see the drops and has no way to.
        frame = np.concatenate([
            np.asarray(hdr.serialize(word_bw=w), dtype=np.uint64).ravel(),
            np.asarray(data.val, dtype=np.uint64).reshape(-1)])
        yield from self.w_out.write_pipelined(frame, t_out_start=t0)
        if self.stall_blocks:
            # THE DIRTY KNOB.  Holding the region past the time the capture needs it is the only way
            # RX loses samples, and a gate that cannot produce the condition cannot tell a design
            # that keeps up from one that was never pushed.
            per = (self.blk_period if self.blk_period
                   else int(self.blk_words) / float(self.clk.freq))
            yield self.timeout(int(self.stall_blocks) * float(per))
        yield from self.lock.release()
        self.n_windows += 1
        self.bases.append(base)
        self.hdrs.append((int(hdr.status), int(hdr.n_dropped)))


# ---------------------------------------------------------------------------
# The composite
# ---------------------------------------------------------------------------

@dataclass
class RfPingPongRx(FreeRunMod):
    r"""The whole continuous-capture receiver as one design scope.

    Three ``hls::task``\ s and one memory beside them::

        samp_in --> RfRelayoutToDense --> PingPongCapture --[lock]--> [ BRAM ]
                                               |                         |
                                              rdy                        |
                                               v                         |
                                      PingPongWindow --[lock]------------+
                                               |
                                            w_out

    The registrations are the design, and the interesting one is a single line: ``add_if(lock)``
    files the two lock streams as internal FIFOs **and** sweeps the two ``BramIF``\ s into the RTL
    registry, so the tasks' memory ports stay boundary ports.

    **The re-layout is FIRST here and LAST on TX**, which is not a symmetry that had to be arranged:
    the memory holds *dense* words on both sides, because dense is the logic-side format a host can
    write and read without knowing anything about justification.  On TX the conversion to converter
    slots therefore happens after the player; on RX the conversion from them happens before the
    capture.  The stage the converter's block quantum lands on is whichever one is adjacent to it,
    and that is the stage that carries ``blk_words``.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_pingpong_rx"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: HwParam[int] = WORD_BW
    #: Samples one word carries.
    samp_per_word: HwParam[int] = 4
    #: Memory depth in **WORDS** (a power of two: the address wrap is a mask).  Split into
    #: :data:`N_REGION` regions.
    depth: HwParam[int] = 512
    #: Bits the effective sample sits above the bottom of its converter slot.  **0 makes the first
    #: stage the identity**, so a build that leaves it there is measuring a pair of wires.
    shift: HwParam[int] = 2
    #: Words per converter block: the re-layout's pysim burst, the capture's chunk, its poll period,
    #: and the reader's output burst.  One number for all of them, because they are one quantum.
    blk_words: HwParam[int] = 16
    #: Sim-only, and there to break things — see :attr:`PingPongWindow.stall_blocks`.
    stall_blocks: int = 0
    #: Seconds per source block, for the stall to be measured in.  Not hardware.
    blk_period: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, spw = int(self.bitwidth), int(self.depth), int(self.samp_per_word)
        sh, bw = int(self.shift), int(self.blk_words)
        if d & (d - 1):
            raise ValueError(f"memory depth must be a power of two (got {d}): the wrap is a mask")

        # The re-layout is FIRST: the converter's slot words become the dense words the memory holds.
        self.relayout = RfRelayoutToDense(sim=self.sim, name=f"{self.name}_to_dense", bitwidth=w,
                                          n_slot=spw, shift=sh, blk_words=bw, clk=self.clk)
        self.capture = PingPongCapture(sim=self.sim, name=f"{self.name}_capture", bitwidth=w,
                                       depth=d, blk_words=bw, clk=self.clk)
        self.window = PingPongWindow(sim=self.sim, name=f"{self.name}_window", bitwidth=w,
                                     depth=d, blk_words=bw, stall_blocks=int(self.stall_blocks),
                                     blk_period=self.blk_period, clk=self.clk)
        # add_comp order is emit order and the DATA-FLOW order.
        for c in (self.relayout, self.capture, self.window):
            self.add_comp(c)

        for nm, master, slave, depth in (
                # Depth 2 -- the HLS default for a top argument and enough for a producer and a
                # consumer to overlap by one beat, which is all an II=1 chain needs.
                ("dense", self.relayout.s_out, self.capture.samp_in, 2),
                # Depth N_REGION, and that is the invariant rather than a guess: at most N_REGION
                # regions can be full at once, so at most that many announcements can be outstanding
                # -- which is what lets the capture write this channel BLOCKING without ever
                # stalling, and a task that stalled here would be back-pressuring an ADC.
                ("rdy", self.capture.rdy_out, self.window.rdy_in, N_REGION)):
            ifc = StreamIF(name=f"{self.name}_{nm}_if", sim=self.sim, clk=self.clk, bitwidth=w,
                           depth=depth)
            ifc.bind(ep_name="master", endpoint=master)
            ifc.bind(ep_name="slave", endpoint=slave)
            self.add_if(ifc)

        # `mem`, not `buf`: the attribute name becomes the Verilog INSTANCE name and `buf` is a
        # primitive gate, which the wrapper emitter refuses by name rather than letting xvlog fail.
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem",
                           element_type=word_element(w), nelem=d)
        self.add_rtl_mod(self.mem)

        #: The lock, and the whole of the memory wiring.  **The capture is the owner and it WRITES**;
        #: the window reader is the requester and it READS — the inversion of TX, and the reason
        #: :meth:`~waveflow.hw.locked_mem.LockedT2pMemIF._mem_if_for` routes by direction rather than
        #: by role.
        self.lock = LockedT2pMemIF(name=f"{self.name}_lock_if", sim=self.sim, clk=self.clk,
                                   element_type=word_element(w), nelem=d, memory=self.mem)
        self.lock.bind("master", self.window.lock)
        self.lock.bind("slave", self.capture.lock)
        self.add_if(self.lock)

        #: ``add_comp`` x ``add_endpoint`` order with every internally-bound endpoint removed.  The
        #: two ``buf_*`` entries are ports of the KERNEL, joined to the memory inside the wrapper.
        self.boundary = ["samp_in", "buf_w", "buf_r", "w_out"]

        # Convenience refs for testbenches — the boundary endpoints live on the children.
        self.samp_in = self.relayout.s_in
        self.w_out = self.window.w_out

    # -- geometry, read off the graph rather than restated -------------------------------------

    @property
    def region_words(self) -> int:
        """Elements in one region."""
        return self.capture.region_words

    @property
    def is_identity(self) -> bool:
        """``True`` when the re-layout does nothing.  A gate should assert it is ``False``, or it is
        measuring a pair of wires rather than the conversion."""
        return int(self.shift) == 0

    @classmethod
    def for_word(cls, word, *, depth: int = 512, **kwargs):
        """Build the receiver from the converter's **word type** — the single place the integers are
        derived.  A type cannot be an ``HwParam``, so what survives the call is integers."""
        from waveflow.hw.rf_relayout import check_geometry, slots_per_word
        from waveflow.hw.rfdc_samp_word import RfdcSampWord

        if not (isinstance(word, type) and issubclass(word, RfdcSampWord)):
            raise TypeError(
                f"RfPingPongRx.for_word() takes the converter's WORD TYPE — the packing convention, "
                f"not a width. Got {word!r}.")
        check_geometry(word)
        return cls(bitwidth=int(word.bitwidth), samp_per_word=slots_per_word(word),
                   depth=int(depth), shift=int(word.justify_shift()), **kwargs)

    # -- counters, and the verdicts that make them loud -----------------------------------------

    @property
    def n_dropped(self) -> int:
        """Words the capture had nowhere to put.  **The number TX did not need.**"""
        return int(self.capture.n_dropped)

    def assert_ran(self, min_windows: int = 2) -> None:
        """The run actually exercised the swap: enough windows, alternating halves, ending clean.

        A guard that never fired is not evidence.  *Alternating* is the part a window count cannot
        give: a design that handed the same half out twice would move the right number of words and
        prove nothing about two regions.
        """
        w = self.window
        if int(w.n_windows) < int(min_windows):
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': {int(w.n_windows)} window(s) drained, "
                f"expected at least {int(min_windows)}. A run with fewer has not swapped.")
        n = self.region_words
        want = [(i % N_REGION) * n for i in range(len(w.bases))]
        if w.bases != want:
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the windows came from bases {w.bases}, not "
                f"the alternating {want}. Handing the same half out twice moves the right number of "
                f"words and exercises no second region at all.")
        self.lock.assert_handover_happened(int(w.n_windows))

    def assert_published_loss(self, frames, *, where: str = "") -> None:
        """**The verdict, read off the wire.**  Every window says ``CAP_OK`` and reports zero lost.

        Off the stream rather than off the counter, for the reason
        ``examples/rf_shot_play`` reads its responses off the wire: a design that counted correctly
        and *serialized* wrongly passes every internal check there is, and the wire is the only thing
        a host can act on.

        This is what makes loss loud.  Without it, a dropped block is a Python attribute nobody
        looks at — which is exactly the shape sub-block loss had before ``offer()`` published it.
        """
        wins = split_windows(frames, int(self.bitwidth))
        if not wins:
            raise AssertionError(
                f"{where}no window reached the host, so its verdict says nothing about this run.")
        bad = [(i, int(h.status), int(h.n_dropped)) for i, (h, _s) in enumerate(wins)
               if int(h.status) != CAP_OK or int(h.n_dropped)]
        if bad:
            i, st, n = bad[0]
            raise AssertionError(
                f"{where}window {i} came out of the design carrying "
                f"{CAP_STATUS_NAMES.get(st, st)} with n_dropped={n} (and {len(bad)} window(s) in "
                f"all). The samples in it are perfectly good and there are exactly as many as there "
                f"should be — the header is the ONLY place this run says it lost anything.")
        last = int(wins[-1][0].n_dropped)
        if last != int(self.capture.n_dropped):
            raise AssertionError(
                f"{where}the last window published n_dropped={last} and the design counted "
                f"{int(self.capture.n_dropped)}. A design whose wire disagrees with its own counter "
                f"reports a number nobody should trust.")

    def assert_no_loss(self) -> None:
        """**Nothing was dropped.**  The verdict RX needs and TX does not.

        You cannot back-pressure an ADC, so a reader holding the region the capture needs is not a
        gap — it is samples that no longer exist.  They are invisible in the data (what comes out is
        a perfectly good ramp with a step in it) and invisible in a word count (the windows are all
        full), which is exactly the shape sub-block loss had.  So the count is asserted, and
        :meth:`assert_windows_contiguous` asserts the same thing again from the samples themselves.
        """
        if int(self.capture.n_dropped):
            blocks = int(self.capture.n_dropped) // int(self.blk_words)
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the capture dropped "
                f"{int(self.capture.n_dropped)} word(s) — {blocks} block(s) — because no region was "
                f"free to put them in. The reader is not keeping up: it holds a window for longer "
                f"than the capture takes to fill the other region. Nothing downstream can see this; "
                f"the windows are all full and the samples are all valid.")
        if int(self.capture.n_written) == 0:
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': nothing was dropped and nothing was written. "
                f"A run in which the capture never moved has not proved anything about loss.")

    def assert_windows_contiguous(self, windows, *, where: str = "") -> np.ndarray:
        """The strongest form of *nothing was dropped*: the windows join without a gap.

        *windows* is the list of drained windows in order, each an array of the source's ramp.  A
        counter can be wrong in either direction; a ramp cannot — a dropped block is a **step** in the
        numbers, and a step is visible whether or not anything counted it.

        Returns the concatenation, so a caller can go on to check its extent.
        """
        if not windows:
            raise AssertionError(f"{where}no windows were drained, so contiguity says nothing.")
        flat = np.concatenate([np.asarray(w).reshape(-1) for w in windows]).astype(np.int64)
        step = np.diff(flat)
        bad = np.flatnonzero(step != 1)
        if bad.size:
            i = int(bad[0])
            raise AssertionError(
                f"{where}the drained windows are not contiguous: sample {i} is {int(flat[i])} and "
                f"sample {i + 1} is {int(flat[i + 1])}, a jump of {int(step[i])}. That gap is "
                f"capture the design lost — and it is invisible in every other reading of this run, "
                f"because both windows either side of it are full of perfectly good samples.")
        return flat
