"""rf_shot_buf.py — ``RfShotBuf``: the **finite** sample buffer, where nothing reads while something writes.

``plans/rf_shot_buf.md``, Stage A.  One question decides between this and the streaming buffer, and
``docs/guide/rf/choosing.md`` already states it: *does anything read the buffer while something else
is writing it?*  A **no** is this class, and everything else follows from that one answer — no credit
channel, no ack, no progress channel, no ``MARGIN``, 100% of the memory is payload, and a window that
starts **before** a trigger is reachable because nothing has thrown the past away.

**Framework, not an example**, the discipline :mod:`waveflow.hw.rf_samp_buf` already follows: the
hand-written ``hls::task`` bodies ship from ``waveflow/build/`` and an example gets a copy beside the
top Vitis compiles.  :meth:`RfShotBufLoad.run_iter` and :meth:`RfShotBufRead.run_iter` are the pysim
**twins** of those bodies, never the source of them.

::

    s_in --> [load] --BramIF(write)--> T2pBram --BramIF(read)--> [read] --> s_out
                |                                                  ^
                +---------------- rdy (one token per shot) --------+

**Why a BRAM here when a BRAM failed for the streaming buffer.**  ``plans/rf_samp_new.md`` refutes
the BRAM, and every clause of that refutation is about **concurrency** — a reader that has to learn a
live writer's position, out of band and stale, needing a margin to bound the staleness, paid for with
a data-dependent spin that cost 2 cycles/word.  This design's defining property is that the reader
and the writer are **never live at the same time**.  There is no position to communicate and
therefore no wait to schedule around.  The earlier reversal is evidence about concurrency, not about
memories.

**The phase separation is asserted, not assumed.**  A read while the writer is live is the one thing
this design is not allowed to do, and the failure mode is *plausible samples* rather than an error —
so :class:`ShotPhase` refuses it in pysim, and ``bram_t2p.v``'s ``$error`` refuses the same thing at
RTL.  The ``rdy`` token is what establishes the ordering; the assertion is what notices when it does
not.

Two deviations from the plan's sketch, both stated rather than absorbed
----------------------------------------------------------------------
* **:attr:`RfShotBuf.depth` is in WORDS, not samples.**  ``plans/rf_shot_buf.md`` § *Stage A* says
  "depth in samples".  A word is the memory's unit — :class:`~waveflow.hw.bram.T2pBram` is
  ``depth`` x ``dwidth`` and its address wrap is a mask, so the power-of-two constraint is a
  constraint on *words* — and :class:`~waveflow.hw.rf_samp_buf.RfSampBufRx` already spells ``depth``
  in words.  Two classes in one repo whose ``depth`` meant different units would be exactly the
  ``nbits`` defect :mod:`waveflow.hw.rfdc_samp_word` exists to correct.  Samples are available as
  :attr:`RfShotBuf.nsamp_held`.
* **The word type is a constructor, not a field.**  ``plans/rf_shot_buf.md`` § *Open questions* asks
  whether ``RfShotBuf`` carries the word type or reads it off the converter, and answers "read,
  almost certainly".  It reads it — through :meth:`RfShotBuf.for_word` — but it cannot *hold* it: a
  type-valued parameter cannot be an ``HwParam``, because ``HwModule.__post_init__`` wraps every one
  in ``HwParamValue(int(value))``.  So the geometry the buffer keeps is integers, and the classmethod
  is the single place the converter's word decides them.

**Not in scope at Stage A**: the converter, the RF grid, and any command format.  The load length is
build-time structure (:attr:`RfShotBuf.nword`), which is the same discipline the commands will follow
— ``plans/rf_shot_buf.md`` § *The commands* keeps ``n_tx`` off the command for this reason.

The **re-layout** the logic-side port needs — dense-packed effective-width samples, not
``RfdcSampWord`` — lives next door in :mod:`waveflow.hw.rf_relayout`, and Stage A measures it rather
than predicting it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.bram import BramIF, BramIFMaster, T2pBram, word_element
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.hw.synth import sim_only
from waveflow.simulation.simobj import ProcessGen

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

#: Default word width in bits — **64**, and that number is a measurement rather than a preference.
#: ``plans/adc_model.md`` § *Take 64 bits, not 56*: the integer serializer never straddles a word
#: boundary, so four 14-bit samples occupy the same word count at 64 bits as at a tight 56, byte
#: aligned, with 8 bits idle — and 64 is exactly the RFDC word width at four 16-bit slots, which
#: makes the logic-side re-layout a **pure re-layout inside one width**.
WORD_BW = 64

#: Default buffer depth in **words** (a power of two — the memory's address wrap is a mask).
BUF_DEPTH = 1024

#: Default words in one shot.  Not the depth: a shot shorter than the memory is the ordinary case,
#: and the two being separate is what lets a gate exercise a partial buffer.
SHOT_WORDS = 256


class ShotPhase:
    """Which phase the buffer is in — **pysim only**, and the thing that refuses the illegal overlap.

    Not hardware.  At RTL the ordering is established by the ``rdy`` token and checked by
    ``bram_t2p.v``'s read-during-write ``$error``; this is the same statement on the Python side,
    where a read of a word the writer has not reached yet returns a **zero from a zeroed numpy
    array** — a plausible sample, indistinguishable from a quiet one, which is precisely the failure
    a shot buffer must not be able to have silently.

    It is deliberately a shared mutable object rather than a message: the two tasks are separate
    :class:`~waveflow.hw.hw_freerun.FreeRunMod`\\ s and nothing about their *hardware* connects them
    beyond the token, so an assertion that spans them can only live outside both.
    """

    def __init__(self) -> None:
        #: ``True`` between the first word of a shot and the token that ends it.
        self.writing = False
        #: ``True`` while the reader is draining a shot.
        self.reading = False
        #: Shots completed — the writer has emitted this many tokens.
        self.n_shots = 0
        #: Words written and words read, over the whole run.  Counted rather than inferred, so a run
        #: that never exercised the overlap is visibly a run that never exercised it.
        self.n_written = 0
        self.n_read = 0

    def begin_write(self) -> None:
        """The writer is about to touch the memory."""
        if self.reading:
            raise AssertionError(
                "RfShotBuf: the loader wrote while the reader was draining a shot. A shot buffer's "
                "whole safety argument is that the two are never live at the same time — there is "
                "no credit, no ack and no progress channel to arbitrate between them, so the reader "
                "would return words from two different shots and nothing downstream could tell. "
                "Either the `rdy` handshake is not wired, or a second load was started before the "
                "read finished (which is Stage B's repeat-play question, not Stage A's).")
        self.writing = True
        self.n_written += 1

    def end_write(self) -> None:
        """The shot is complete; the token goes out now."""
        self.writing = False
        self.n_shots += 1

    def begin_read(self) -> None:
        """The reader is about to touch the memory."""
        if self.writing:
            raise AssertionError(
                "RfShotBuf: the reader read while the loader was still filling the shot. It would "
                "have got a word that was never written — a zero from a zeroed array in pysim, and "
                "whatever the BRAM's read-during-write mode happens to be at RTL. The `rdy` token "
                "exists to make this unreachable; if it fired, the token is not in the path.")
        if self.n_shots == 0:
            raise AssertionError(
                "RfShotBuf: the reader read before any shot had been loaded. Nothing is in the "
                "memory yet, so every word it returns is a zero that looks like a sample.")
        self.reading = True
        self.n_read += 1

    def end_read(self) -> None:
        """The shot has been drained."""
        self.reading = False


# ---------------------------------------------------------------------------
# The two tasks
# ---------------------------------------------------------------------------

@dataclass
class RfShotBufLoad(FreeRunMod):
    """Fill the buffer with one shot, then say so — ``nword`` words in, one token out.

    The hardware body is hand-written (``waveflow/build/rf_shot_buf_load_task.h``) and is a
    ``while (1)`` around a **counted** inner loop::

        while (1) { for (i = 0; i < NW; i++) buf_w[i] = s_in.read(); rdy_out.write(1); }

    Two properties come out of that shape and neither is decoration.  The inner loop is
    ``PIPELINE II=1`` over a counted trip, so there is no data-dependent spin for Vitis to refuse to
    flatten — the defect that pins
    :attr:`~waveflow.hw.rf_samp_buf.RfSampBufCapture.cycles_per_word` at 2 next door.  And the shot
    boundary is the *outer* loop, so ``plans/witness/task_loop/``'s measured 3-cycle boundary gap is
    paid **once per shot**, not once per word.

    It also needs **no ``static``**, which is worth stating because the alternative shape does: a
    running write pointer across firings would be a static, and a static in an ``hls::task`` is the
    reset trap ``examples/rf_blk_delay`` lost a day to (see
    ``docs/guide/rf/…`` / ``reference-hls-task-reset-trap``).  Here the address *is* the loop index.

    **The never-stall law does not apply to this task**, and that is not an oversight.  Its input is
    a stream from an ``m_axi`` arena or a DMA — something that *can* be back-pressured — not a
    converter.  Copying :class:`~waveflow.hw.rf_samp_buf.RfSampBufIngress`'s law onto it would be
    inheriting an obligation instead of measuring one.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_shot_buf_load"

    #: Word width in bits.
    bitwidth: HwParam[int] = WORD_BW
    #: Buffer depth in **words** (power of two — the memory's wrap is a mask).
    depth: HwParam[int] = BUF_DEPTH
    #: Words in one shot.  ``<= depth``.
    nword: HwParam[int] = SHOT_WORDS
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, nw = int(self.bitwidth), int(self.depth), int(self.nword)
        if d & (d - 1):
            raise ValueError(f"buffer depth must be a power of two (got {d}): the wrap is a mask")
        if not 1 <= nw <= d:
            raise ValueError(
                f"a shot is {nw} words but the buffer holds {d}: a shot longer than the memory is "
                f"not a shot, it is a stream, and streaming is what waveflow.hw.rf_tx_stream is for")
        self.s_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w, has_tlast=True)
        self.buf_w = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_w",
                                  element_type=word_element(w), nelem=d, access="write")
        self.rdy_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_rdy", bitwidth=w,
                                      has_tlast=True)
        for ep in (self.s_in, self.buf_w, self.rdy_out):
            self.add_endpoint(ep)
        #: The phase guard.  Replaced by the composite with one shared with the reader — a private
        #: one here means a task instantiated on its own still runs rather than raising on `None`.
        self.phase = ShotPhase()

    def kernel_task(self) -> KernelTask:
        return KernelTask("rf_shot_buf_load_task", "rf_shot_buf_load_task.h",
                          ("buf_w", "s_in", "rdy_out"),
                          template_args=(int(self.bitwidth), int(self.depth), int(self.nword)))

    def run_iter(self) -> ProcessGen[None]:
        """The pysim twin: one firing is one shot, which is one iteration of the C++ outer loop.

        ``get(nwords_max=1)`` per word, and the scenario must therefore write **one word per burst**
        — the trap ``examples/bram_access`` spells out: a pysim slave dequeues a whole burst per ``get``
        and truncation *discards* the rest, so a single 256-word burst would be one pysim firing
        against 256 RTL firings and the two backends would be running different designs.
        """
        nw = int(self.nword)
        for i in range(nw):
            words = yield from self.s_in.get(nwords_max=1)
            self.phase.begin_write()
            self.buf_w.mem_write(i, int(np.asarray(words).ravel()[0]))
        self.phase.end_write()
        yield from self.rdy_out.write(np.array([1], dtype=np.uint64))


@dataclass
class RfShotBufRead(FreeRunMod):
    """Wait for a shot, then emit it — one token in, ``nword`` words out, in order.

    The mirror of :class:`RfShotBufLoad` and the same shape::

        while (1) { rdy_in.read(); for (i = 0; i < NW; i++) s_out.write(buf_r[i]); }

    **There is no command stream.**  ``examples/bram_access``'s reader answers a ``(rp, nwords)`` command,
    which makes it a witness for the memory rather than a buffer; a shot buffer plays a *contiguous*
    shot, so the address is the loop index and the only thing that crosses the boundary is the
    payload.  That is the whole of the simplification the shot design claims over the streaming one,
    made structural: there is nothing to arbitrate, so there is nothing on the wire to arbitrate it.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_shot_buf_read"

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = BUF_DEPTH
    nword: HwParam[int] = SHOT_WORDS
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, nw = int(self.bitwidth), int(self.depth), int(self.nword)
        if d & (d - 1):
            raise ValueError(f"buffer depth must be a power of two (got {d}): the wrap is a mask")
        if not 1 <= nw <= d:
            raise ValueError(f"a shot is {nw} words but the buffer holds {d}")
        self.rdy_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_rdy", bitwidth=w,
                                    has_tlast=True)
        self.s_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_out", bitwidth=w,
                                    has_tlast=True)
        self.buf_r = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_r",
                                  element_type=word_element(w), nelem=d, access="read")
        for ep in (self.rdy_in, self.s_out, self.buf_r):
            self.add_endpoint(ep)
        self.phase = ShotPhase()

    def kernel_task(self) -> KernelTask:
        return KernelTask("rf_shot_buf_read_task", "rf_shot_buf_read_task.h",
                          ("buf_r", "rdy_in", "s_out"),
                          template_args=(int(self.bitwidth), int(self.depth), int(self.nword)))

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one shot: arm on the token, then emit ``nword`` words, one per burst."""
        yield from self.rdy_in.get(nwords_max=1)
        for i in range(int(self.nword)):
            self.phase.begin_read()
            val = self.buf_r.mem_read(i)
            yield from self.s_out.write(np.array([val], dtype=np.uint64))
        self.phase.end_read()


# ---------------------------------------------------------------------------
# The composite: two tasks, one token channel, one memory beside the kernel
# ---------------------------------------------------------------------------

@dataclass
class RfShotBuf(FreeRunMod):
    """The shot buffer as one design scope: a loader, a reader, and the memory between them.

    The registrations *are* the design, and each line means something different:

    ============================  ==============================================================
    ``add_comp(load) / (read)``   the two ``hls::task``\\ s **inside** the generated kernel
    ``add_if(rdy_if)``            the "shot ready" token -> an ``hls::stream`` inside the top
    ``add_rtl_mod(mem)``          the memory, realized as hand-written Verilog **beside** the top
    ``add_rtl_if(...)``           wrapper wires -> the tasks' memory ports stay BOUNDARY ports
    ============================  ==============================================================

    The last row is the one that has to be right: a :class:`~waveflow.hw.bram.BramIF` goes in
    ``add_rtl_if`` and **never** ``add_if``, because the walks that derive channels and boundary
    ports read the ``add_if`` registry and a ``BramIF`` in it would make the kernel's memory ports
    disappear into a FIFO that does not exist.

    **The token channel is depth 1.**  There is exactly one token per shot and the reader consumes it
    before doing anything, so a deeper queue could only hold tokens for shots that have already been
    overwritten — which is the state :class:`ShotPhase` refuses.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_shot_buf"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: HwParam[int] = WORD_BW
    #: Samples carried by one word.  Structural only at Stage A — the buffer moves *words* and never
    #: reads one arithmetically — but it is what turns :attr:`depth` into :attr:`nsamp_held`, and a
    #: buffer that could not say how many samples it holds would be answering the wrong question.
    samp_per_word: HwParam[int] = 4
    #: Depth in **WORDS**; see the module docstring for why not samples.
    depth: HwParam[int] = BUF_DEPTH
    #: Words in one shot.
    nword: HwParam[int] = SHOT_WORDS
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, nw = int(self.bitwidth), int(self.depth), int(self.nword)
        spw = int(self.samp_per_word)
        if spw < 1 or w % spw:
            raise ValueError(
                f"a {w}-bit word cannot carry {spw} samples without one straddling a slot")
        self.load = RfShotBufLoad(sim=self.sim, name=f"{self.name}_load", bitwidth=w, depth=d,
                                  nword=nw, clk=self.clk)
        self.read = RfShotBufRead(sim=self.sim, name=f"{self.name}_read", bitwidth=w, depth=d,
                                  nword=nw, clk=self.clk)
        self.add_comp(self.load)
        self.add_comp(self.read)

        #: The one thing the two tasks say to each other, and it is one bit of information: *the shot
        #: is in the memory*.  Contrast ``rf_samp_new.md``'s credit + ack + progress machinery, all
        #: of which exists to arbitrate between a live reader and a live writer.
        rdy_if = StreamIF(name=f"{self.name}_rdy_if", sim=self.sim, clk=self.clk, bitwidth=w,
                          depth=1)
        rdy_if.bind(ep_name="master", endpoint=self.load.rdy_out)
        rdy_if.bind(ep_name="slave", endpoint=self.read.rdy_in)
        self.add_if(rdy_if)

        # `mem`, not `buf`: the attribute name becomes the Verilog INSTANCE name and `buf` is a
        # primitive gate, which the wrapper emitter refuses by name rather than letting xvlog fail on
        # a syntax error that mentions no Python.
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem",
                           element_type=word_element(w), nelem=d)
        self.add_rtl_mod(self.mem)
        w_if = BramIF(name=f"{self.name}_bufw_if", sim=self.sim)
        w_if.bind(ep_name="master", endpoint=self.load.buf_w)
        w_if.bind(ep_name="slave", endpoint=self.mem.wr_port)
        self.add_rtl_if(w_if)
        r_if = BramIF(name=f"{self.name}_bufr_if", sim=self.sim)
        r_if.bind(ep_name="master", endpoint=self.read.buf_r)
        r_if.bind(ep_name="slave", endpoint=self.mem.rd_port)
        self.add_rtl_if(r_if)

        #: One :class:`ShotPhase` for both tasks — the assertion has to span them, so it cannot live
        #: in either.  Assigned after construction rather than passed in, because it is sim-only
        #: state and must not look like a parameter of the hardware.
        self.phase = ShotPhase()
        self.load.phase = self.phase
        self.read.phase = self.phase

        #: ``add_comp`` x ``add_endpoint`` order with the token endpoints removed.  The two ``buf_*``
        #: entries are ports of the KERNEL, joined to the memory inside the wrapper.
        self.boundary = ["s_in", "buf_w", "s_out", "buf_r"]

        # Convenience refs for testbenches — the boundary endpoints live on the children.
        self.s_in = self.load.s_in
        self.s_out = self.read.s_out

    # -- geometry ------------------------------------------------------------------------------

    @property
    def nsamp_held(self) -> int:
        """Samples the memory holds — ``depth`` words of ``samp_per_word`` samples each.

        **100% payload**, which is the property the concurrency answer buys: there is no headroom
        reserved for data in flight, no ``MARGIN`` surrendered to bound a channel's staleness, and no
        horizon shorter than the memory.  Compare
        :attr:`~waveflow.hw.rf_samp_buf.RfSampBufCapture.usable_horizon`, which is ``depth * spw``
        *minus* a margin for exactly those reasons.
        """
        return int(self.depth) * int(self.samp_per_word)

    @property
    def nsamp_shot(self) -> int:
        """Samples in one shot."""
        return int(self.nword) * int(self.samp_per_word)

    def shot_seconds(self, samp_rate: float) -> float:
        """How long one shot lasts at *samp_rate* — the duration bound the memory imposes.

        The row ``plans/rf_shot_buf.md``'s table calls *duration: bounded by the memory*.  It is a
        function rather than a property because the rate belongs to the converter, not to the buffer,
        and the same single-source discipline that makes ``Rfdc`` read ``samp_rate`` off the clock
        says the buffer must not keep a copy of it.
        """
        return self.nsamp_shot / float(samp_rate)

    @classmethod
    def for_word(cls, word, *, depth: int = BUF_DEPTH, nword: int = SHOT_WORDS, **kwargs):
        """Build a buffer whose geometry is **read off the converter's word type**.

        ``plans/rf_shot_buf.md`` § *Open questions* asks whether the buffer carries the word type or
        reads it, and answers "read".  This is that reading, in one place: the word width is the
        word's own (:attr:`~waveflow.hw.rfdc_samp_word.RfdcSampWord.bitwidth`), so the logic-side
        re-layout stays a **pure re-layout inside one width** rather than a width conversion, and
        ``samp_per_word`` is the word's.

        The type is not stored.  A type-valued parameter cannot be an ``HwParam`` —
        ``HwModule.__post_init__`` wraps every one in ``HwParamValue(int(value))`` — so what survives
        the call is the two integers, and this classmethod is the only place they are derived.
        """
        from waveflow.hw.rfdc_samp_word import RfdcSampWord

        if not (isinstance(word, type) and issubclass(word, RfdcSampWord)):
            raise TypeError(
                f"RfShotBuf.for_word() takes the converter's WORD TYPE — the packing convention, "
                f"not a width. Got {word!r}. Build one with RfdcSampWord.specialize(...) or a board "
                f"preset such as Rfsoc4x2SampWord.specialize(samp_per_word=4).")
        return cls(bitwidth=int(word.bitwidth), samp_per_word=int(word.samp_per_word),
                   depth=int(depth), nword=int(nword), **kwargs)

    # -- counters ------------------------------------------------------------------------------

    @property
    def n_shots(self) -> int:
        """Shots the loader has completed — one per ``rdy`` token."""
        return int(self.phase.n_shots)

    @sim_only
    def assert_phases_separated(self) -> None:
        """Restate, after a run, what :class:`ShotPhase` refused during it.

        A guard that never fired is not evidence that the invariant held — it is evidence that
        *something* ran.  This asserts the run actually used both halves, so a scenario that
        accidentally loaded nothing (or read nothing) is a failure rather than a quiet pass.
        """
        if self.phase.n_written == 0 or self.phase.n_read == 0:
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the phase guard cannot have proved anything — "
                f"{self.phase.n_written} words were written and {self.phase.n_read} read. A run in "
                f"which one side never moved has not exercised the separation it claims to keep.")
        if self.phase.writing or self.phase.reading:
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the run ended mid-phase "
                f"(writing={self.phase.writing}, reading={self.phase.reading}). A shot that was "
                f"started and not finished leaves the memory holding half a signal, which is this "
                f"repo's recurring failure and is invisible from a word count.")
