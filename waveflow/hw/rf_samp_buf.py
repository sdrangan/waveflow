"""rf_samp_buf.py — ``RfSampBufRx``: the sample buffer that stands between a converter and a design.

**Framework, not an example.**  ``plans/adc_model.md`` § *Two design patterns* makes this the
**default** way a user's logic reaches an RF converter: the topology is
``Rfdc -> RfSampBuf -> your logic -> RfSampBuf -> Rfdc`` rather than ``Rfdc -> your logic``, and the
reason is that somebody has to own the never-stall obligation.  Under the direct pattern that
somebody is *every user*, each of whom must hand-write a pipelined ``@synthesizable`` body because
pipelined ops cannot be extracted (``plans/pipelined_ops.md``).  Here it is written **once**, and no
user's DUT has the conversation at all.

That is why this module lives beside :mod:`waveflow.hw.mem_stream` — the same shape and the same
argument.  Both are reusable :class:`~waveflow.hw.hw_module.HwModule`\\ s whose **kernel body is
fixed** and hand-written, shipped as a width-templated header in ``waveflow/build/`` and copied into
an example's ``include/`` by a build step.  ``run_iter`` here is the **pysim twin**, not the source
of the RTL.

::

    s_in --> [ingress] --BramIF(write)--> T2pBram --BramIF(read)--> [capture] --> s_out
                 |                                                      ^            s_resp
                 +------------- progress channel (wr) ------------------+
    s_cmd --> [capture]

**Why two tasks and a memory rather than one task and an array.**  The two accessors are concurrent
by nature — the ADC never pauses and a capture may run for a long time — and Vitis has no way to
express a memory shared between two ``hls::task`` bodies: a local array becomes a synchronizing PIPO
whose handshake *stalls the writer*, which is the one thing a converter-facing stage may never do.
So the buffer is hand-written Verilog beside the kernel (:class:`~waveflow.hw.bram.T2pBram`), joined
by a generated wrapper.  See ``docs/guide/interface/primitive/bram.md``.

**The never-stall law applies to the ingress only.**  It is written on :class:`RfSampBufIngress` and
deliberately *not* on :class:`RfSampBufCapture`: the capture may block for as long as it likes,
because nothing upstream of it loses data while it waits.  Copying the law onto the capture would
make it wrong — and the four command cases exist precisely because it may wait.

**A word carries ``samp_per_word`` samples, and the buffer stores words.**  The ingress body moves one
*word per cycle*, so widening the word is now the **only** throughput lever: it absorbs
``samp_per_word`` samples per cycle — 1.0 at one sample per word, 4.0 at four — which is exactly what
the AXIS port carries, so this stage no longer binds and the port does.  (It absorbed
``samp_per_word / 2`` until 2026-08-18, when the body became an infinite pipelined loop; see
:attr:`RfSampBufIngress.cycles_per_word`.)  The buffer is ``depth`` **words** deep and holds
``depth * samp_per_word`` samples.

**Windows are word-aligned.**  ``RxCmd`` names a window in *sample* index, but both ``start`` and
``nsamp`` must be multiples of ``samp_per_word``, and the capture emits whole words.  A sub-word
window would mean unpacking, selecting and re-packing inside a loop that must stay cheap; that is
real work and it is deliberately not done here, so a misaligned window is **refused** with
:data:`RF_SAMP_BUF_MISALIGNED` rather than silently rounded.  At ``samp_per_word == 1`` the
constraint is vacuous and the check folds away to a constant in both backends.

**The sample counter wraps at ``2**IDX_BW``**, independently of the word width — widening the word
must not move where a sample index wraps, or a command naming a sample would mean different things at
different widths.  Every comparison against the counter is therefore a circular
(signed-difference) one; see :func:`sdiff`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.bram import BramIF, BramIFMaster, T2pBram, word_element
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.hw.synth import sim_only
from waveflow.simulation.simobj import ProcessGen

# ---------------------------------------------------------------------------
# Geometry.  16-bit samples, one per word; a 1024-sample buffer is one RAMB18.
# ---------------------------------------------------------------------------

#: Width of a sample **index** — the counter the hardware keeps and the width of every ``RxCmd`` /
#: ``RxResp`` field.  Deliberately *not* the AXIS word width: widening the word must not silently
#: change where the sample counter wraps.  16 bits addresses 65536 samples of history, far more than
#: any buffer that fits in on-chip RAM.
IDX_BW = 16

#: Default AXIS word width in bits — the configuration the RTL gate runs (one 16-bit sample/word).
WORD_BW = 16

#: Default buffer depth in **words**.  1024 x 16 bits is one RAMB18.
BUF_DEPTH = 1024

#: Default samples of horizon given up to bound the progress channel's staleness — see
#: :attr:`RfSampBufCapture.horizon_margin`.  The usable horizon is ``depth - horizon_margin``.
HORIZON_MARGIN = 16

#: Response status codes, mirrored as literals in ``rf_samp_buf_capture_task.h``.  Two places, one
#: encoding: the C++ side spells them as literals so the generated schema header cannot disagree
#: about the value.
#: One vocabulary for **both** directions, not two parallel ones: a host that drives an RX buffer and
#: a TX buffer reads one status table, and a code means the same thing wherever it appears.
RF_SAMP_BUF_OK = 0
#: RX only: the window had already been overwritten by the ingress — the data asked for is gone.
RF_SAMP_BUF_TOO_OLD = 1
#: The window was not a whole number of words — see the module docstring.  Refused rather than
#: rounded: a rounded window is data from the wrong time, which nothing downstream can detect.
#: Shared by both directions, because word alignment is a property of the geometry, not of a path.
RF_SAMP_BUF_MISALIGNED = 2
#: TX only: the slot named by the command had **already played**.  A distinct code from
#: :data:`RF_SAMP_BUF_TOO_OLD` rather than a reuse of it, because the two are opposite failures and
#: conflating them would make a TX diagnostic read as an RX one — "the data you wanted was
#: overwritten" versus "the data you sent arrived after its slot went out of the DAC".  See
#: :mod:`waveflow.hw.rf_samp_buf_tx`.
RF_SAMP_BUF_TOO_LATE = 3

#: The element type of every ``RxCmd`` / ``RxResp`` field.  ``Word16`` is kept as an alias because it
#: is the name the schemas were written against; the width it means is now :data:`IDX_BW`.
IdxField = IntField.specialize(bitwidth=IDX_BW, signed=False)
Word16 = IdxField


class RxCmd(DataList):
    """One capture command: return the samples at indices ``[start, start + nsamp)``.

    The window is in **sample index** — the converter's own running count, not a buffer address —
    which is what lets a host ask for samples *around an event* it timestamped, and what makes three
    of the four cases (buffer / future / straddling) one question rather than three.
    """

    include_filename: ClassVar[str | None] = "rx_cmd.h"
    elements = {
        "tid":   {"schema": Word16, "description": "transaction id, echoed on the response"},
        "start": {"schema": Word16, "description": "first sample index of the window"},
        "nsamp": {"schema": Word16, "description": "samples to capture"},
    }


class RxResp(DataList):
    """One response per command — the **counted contract**.

    A capture that returned fewer samples than asked, or none at all, must say so in band: the
    alternative is a host that cannot tell "your window fell off the end of the buffer" from "the
    samples have not arrived yet", which is the difference between a bug and a wait.
    """

    include_filename: ClassVar[str | None] = "rx_resp.h"
    elements = {
        "tid":    {"schema": Word16, "description": "the command's transaction id"},
        "status": {"schema": Word16, "description": "0 = OK, 1 = too old (fell off the horizon)"},
        "nsent":  {"schema": Word16, "description": "samples actually emitted"},
    }


#: The schema classes an example must run ``DataSchemaStep`` over to get ``rx_cmd.h`` / ``rx_resp.h``.
SCHEMA_CLASSES = [RxCmd, RxResp]


def sdiff(a: int, b: int, bits: int = IDX_BW) -> int:
    """``a - b`` as a **signed circular difference** on a *bits*-wide counter.

    The pysim twin of the C++ ``(ap_int<IDX_W>)(a - b)``.  A plain ``a < b`` on a wrapping counter is
    wrong the first time it wraps, and wrong *silently*; this is exact as long as the two positions
    are within ``2**(bits-1)`` of each other, which a 1024-deep buffer guarantees.
    """
    half = 1 << (bits - 1)
    return ((int(a) - int(b) + half) % (1 << bits)) - half


def samp_type(word_bw: int, samp_per_word: int) -> type[IntField]:
    """The element type one buffered **sample** is, given the word geometry.

    Unsigned: the buffer moves raw converter words and never interprets them arithmetically.  This is
    the type handed to :func:`~waveflow.hw.arrayutils.read_array` /
    :func:`~waveflow.hw.arrayutils.write_array`, which is how a sample index becomes a slot in a word
    — never a hand-rolled shift.  Slot order is unobservable at one sample per word, so a mistake
    there passes every test written at that width; the generated ``<stem>_array_utils.h`` is the C++
    half of the same statement.
    """
    if int(word_bw) % int(samp_per_word):
        raise ValueError(
            f"word width {int(word_bw)} is not a multiple of samp_per_word={int(samp_per_word)}: a "
            f"sample cannot straddle a slot")
    return IntField.specialize(bitwidth=int(word_bw) // int(samp_per_word), signed=False)


def pack_samples(samples, word_bw: int, samp_per_word: int) -> np.ndarray:
    """``samp_per_word`` samples per word, packed the way the converter packs them.

    Through :func:`~waveflow.hw.arrayutils.write_array` — one source of slot order for both backends.
    """
    from waveflow.hw.arrayutils import write_array

    st = samp_type(word_bw, samp_per_word)
    vals = np.asarray(samples, dtype=np.uint64).ravel()
    if vals.size % int(samp_per_word):
        raise ValueError(
            f"{vals.size} samples is not a whole number of {int(samp_per_word)}-sample words")
    return np.asarray(write_array(vals, elem_type=st, word_bw=int(word_bw)), dtype=np.uint64)


def unpack_samples(words, word_bw: int, samp_per_word: int) -> np.ndarray:
    """The inverse of :func:`pack_samples`, through the same serializer."""
    from waveflow.hw.arrayutils import read_array

    st = samp_type(word_bw, samp_per_word)
    raw = np.asarray(words, dtype=np.uint64).ravel()
    out = read_array(raw, elem_type=st, word_bw=int(word_bw),
                     shape=int(raw.size) * int(samp_per_word))
    return np.asarray(getattr(out, "val", out), dtype=np.uint64).ravel()


# ---------------------------------------------------------------------------
# The two tasks
# ---------------------------------------------------------------------------

@dataclass
class RfSampBufIngress(FreeRunMod):
    """One sample off the converter port, one sample into the buffer, one progress update.

    **This task may never stall its input**, and it satisfies that *structurally* rather than by a
    sizing argument: it writes a BRAM port, and a BRAM port has no handshake to refuse it.  The
    ``rf_loopback`` ingress had to argue about FIFO depth; this one has nothing to size.

    The progress write is non-blocking on purpose.  A blocking write would stall the converter in
    order to deliver a number that is stale by the time it lands — see :class:`RfSampBufCapture` for
    what the resulting lag costs and how it is paid for.

    **A word carries ``samp_per_word`` samples**, and that is the throughput lever: the body moves one
    *word per cycle*, so the sample rate this stage absorbs is
    ``samp_per_word / cycles_per_word`` = ``samp_per_word`` per fabric cycle.  The buffer behind it is
    ``depth`` **words** deep.

    The hardware body is hand-written (``waveflow/build/rf_samp_buf_ingress_task.h``);
    :meth:`run_iter` is the **pysim twin**, and it is *paced* — see there for why that is the whole
    point of this class existing.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_samp_buf_ingress"

    #: **Fabric cycles per WORD** — one word in, one word stored, per this many cycles.
    #:
    #: MEASURED, not assumed: the body is an infinite pipelined loop and this is the loop's
    #: **achieved** ``PipelineII`` from ``rf_samp_buf_ingress_task_*_csynth.xml``.  Achieved, not the
    #: target: Vitis reports both and they differ whenever it misses, and a per-word cost taken from
    #: the target is a wish.
    #:
    #: **It replaced ``fire_cycles = 2`` on 2026-08-18**, and the rename is not cosmetic.
    #: ``fire_cycles`` meant *cycles per firing*, and a ``while (1)`` body has no firing — csynth
    #: reports ``TripCount = inf`` and no overall latency for this module, so there is no such
    #: quantity to declare.  What survives the change of shape is cycles per word.  Anyone reaching
    #: for ``latency + 1`` on a looped body should not: it is measured to be **pessimistic by 2**
    #: (``plans/witness/task_loop/``), which would silently understate this design's throughput.
    #:
    #: It is declared because it is a *rate contract* — the maximum sample rate this design can absorb
    #: is ``samp_per_word * f_axis / cycles_per_word``, and a converter faster than that loses samples
    #: with no protocol event to mark it.  At 1 that ceiling is ``samp_per_word * f_axis``, which is
    #: exactly the port's own capacity: this stage no longer binds, the port does.
    #:
    #: Getting this wrong is not theoretical: at 256 MSPS on a 300 MHz fabric with one sample per
    #: word, the first RTL run of this design dropped **1695 of 4096 samples** (58.6% accepted =
    #: 0.5/0.853, exactly the ratio the old 2 predicts) while pysim reported none — the documented
    #: block-granularity blind spot (``docs/guide/rf/rfdc/fidelity.md``), because a pysim ingress
    #: consumes a whole burst per firing and never meets the per-word rate at all.
    #: :meth:`RfSampBufRx.check_rate` turns that into a refusal at build time.
    cycles_per_word: ClassVar[int] = 1

    #: AXIS word width in bits.  Read off the converter's ``axis_bitwidth``.
    bitwidth: HwParam[int] = WORD_BW
    #: Samples carried by one word; ``bitwidth // samp_per_word`` is the sample width.
    samp_per_word: HwParam[int] = 1
    #: Buffer depth in **words** (power of two — the wrap is a bit mask).
    depth: HwParam[int] = BUF_DEPTH
    clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, spw = int(self.bitwidth), int(self.depth), int(self.samp_per_word)
        if d & (d - 1):
            raise ValueError(f"buffer depth must be a power of two (got {d}): the wrap is a mask")
        if spw & (spw - 1):
            raise ValueError(
                f"samp_per_word must be a power of two (got {spw}): the sample->word conversion is a "
                f"shift in the never-stall path, and a divide there would cost cycles the converter "
                f"does not give back")
        samp_type(w, spw)                       # refuses a sample that would straddle a slot
        self.s_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w, has_tlast=True)
        self.buf_w = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_w",
                                  element_type=word_element(w), nelem=d, access="write")
        self.wr_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_wr_out", bitwidth=w,
                                     has_tlast=True)
        for ep in (self.s_in, self.buf_w, self.wr_out):
            self.add_endpoint(ep)
        #: The write pointer in **sample index**, wrapping at ``2**IDX_BW`` exactly as the RTL's does.
        self.wr = 0

    @property
    def capacity_samp_per_cycle(self) -> float:
        """Samples this ingress absorbs per fabric cycle — ``samp_per_word / cycles_per_word``."""
        return int(self.samp_per_word) / float(self.cycles_per_word)

    def kernel_task(self) -> KernelTask:
        return KernelTask("rf_samp_buf_ingress_task", "rf_samp_buf_ingress_task.h",
                          ("buf_w", "s_in", "wr_out"),
                          template_args=(int(self.bitwidth), int(self.samp_per_word),
                                         int(self.depth), IDX_BW))

    def run_iter(self) -> ProcessGen[None]:
        """The pysim twin — burst-granular in *data*, word-granular in **rate**.

        A burst is pysim's quantum: :meth:`StreamIFSlave.get` dequeues a whole burst whatever width
        is asked for, so a word-at-a-time body would silently discard the rest of it.  What the
        hardware does is one word per :attr:`cycles_per_word` cycles, and **that** is the part the model
        must get right, because it is the only thing that decides whether the converter outruns this
        stage.

        The predecessor did not charge that time at all: it drained a burst instantly, so the
        boundary port never backed up and ``dropped`` was **zero for a design that lost 41% of its
        samples at RTL**.  Charging ``nwords * cycles_per_word`` fabric cycles per firing puts pysim's
        drop threshold exactly at :meth:`RfSampBufRx.max_samp_rate` — the same number
        :meth:`RfSampBufRx.check_rate` refuses against — so the static check and the simulation now
        agree instead of one of them being decorative.  Both terms scale with the burst length, so
        the threshold is the *design's* capacity and does not depend on the block size.

        **This is deliberately not a ``get_pipelined`` / ``write_pipelined`` body**, and both halves
        are worth stating because they look like the right constructs and are not:

        *``get_pipelined`` cannot express an unknown-length read.*  With ``count=N`` it unpacks
        through ``read_array(shape=N)``, which **zero-pads to N** rather than returning what arrived,
        so it needs a length known in advance.  This ingress has none — the burst is the converter's
        block, which the module does not and should not know.

        *Its ``tstart`` is the wrong anchor anyway.*  It is back-calculated as
        ``now - (nwords-1) * fabric_period``, which assumes an II=1 **fabric-paced** producer.  A
        converter delivers at ``samp_rate / samp_per_word`` words per second, far slower, so pacing
        from that anchor would discount ``(nwords-1)`` fabric cycles from every firing and move the
        drop threshold from the *design's* capacity to the *port's* — precisely the confusion that
        cost 1695 samples in the first place.

        *``write_pipelined`` on the progress channel would stall the converter.*  It blocks, the
        channel is one deep, and the capture only drains it while it is waiting.  The RTL writes it
        non-blockingly (``write_nb``) exactly so a stale position never costs a sample, so the twin
        keeps :meth:`~waveflow.hw.interface.StreamIFMaster.offer`.  A dropped progress update is
        correct rather than tolerated: only the newest position means anything.
        """
        words = yield from self.s_in.get()
        raw = np.asarray(words).ravel()

        mask = int(self.depth) - 1
        spw = int(self.samp_per_word)
        wrap = 1 << IDX_BW
        for x in raw:
            self.buf_w.mem_write((self.wr // spw) & mask, int(x))
            self.wr = (self.wr + spw) % wrap

        # THE RATE CONTRACT, made of time rather than of a comment.  One word per cycles_per_word
        # fabric cycles; the next `get` therefore happens that much later, and a converter offering
        # faster than this stage drains fills the 2-deep boundary port and drops -- which pysim can
        # now see, and could not before.
        yield self.timeout(raw.size * self.cycles_per_word * self.clk.period)

        # `offer`, not `write`: the same non-blocking semantics as the RTL's write_nb, and for the
        # same reason.  What it drops is counted on the interface, and a nonzero count there is
        # normal rather than a fault -- only the newest position has meaning.
        yield from self.wr_out.offer(np.array([self.wr], dtype=np.uint64))


@dataclass
class RfSampBufCapture(FreeRunMod):
    """One ``RxCmd`` in, the named window out, one ``RxResp`` per command.

    **This task is allowed to block**, which is the whole reason the four cases collapse into one
    loop.  Nothing upstream of it loses data while it waits: the ingress keeps filling the buffer
    whatever this task is doing.

    ==============  =====================================  =========================================
    case            condition                              what happens
    ==============  =====================================  =========================================
    in the buffer   ``wr-N <= start, start+nsamp <= wr``    served straight out of the buffer
    in the future   ``start >= wr``                         waits per sample, then serves
    straddling      ``start < wr < start+nsamp``            pre-trigger from the buffer, then streams
    too old         ``start < wr - N``                      refused, counted, never a silent read
    ==============  =====================================  =========================================

    **The horizon is checked per sample, not per command.**  A long capture whose output is
    back-pressured can start legal and go stale mid-stream — valid when it was asked for, overwritten
    by the time it is read — so both bounds live inside the loop.

    **What the margin is for.**  :attr:`last_wr` is a *lower bound* on the true write pointer, because
    the progress channel drops rather than stalls.  Staleness makes the "already written?" test
    harder to pass (safe: this task merely waits longer) and the "not yet overwritten?" test *easier*
    to pass (unsafe: an overwritten sample could slip through).  So the usable horizon is declared as
    ``depth - horizon_margin``, and the margin is what makes the unsafe direction bounded rather than
    hoped-for.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_samp_buf_capture"

    #: **Fabric cycles per WORD.**  Declared because an undeclared cost is not a cost of zero — it is
    #: an unmeasured one, and this stage carries a throughput obligation like every other: *every*
    #: sample in a pattern-B loop passes through all four stages, so the loop's ceiling is the
    #: **minimum** over them, not the minimum over the two converter-facing ones.
    #:
    #: **2, and this body cannot take the fix that got the others to 1.**  Its per-word loop contains
    #: an inner spin that waits for the ingress to have written the word being asked for, and Vitis
    #: reports the same pair of diagnostics the loader used to get::
    #:
    #:     [HLS 200-960] Cannot flatten loop 'VITIS_LOOP_97_1' ... sub loop is do-while
    #:     [HLS 200-1470] Target II = NA, Final II = 1, Depth = 1, loop 'VITIS_LOOP_106_2'
    #:
    #: The loader's equivalent wait could be hoisted out of the loop because it asks a question about
    #: the *whole frame* ("is there room for all of it?").  **This one cannot**: it asks a question
    #: about *this word* ("has it been written yet?"), and hoisting it to the frame would mean
    #: refusing to emit anything until the entire window exists — which deletes the straddling case
    #: that is the reason this loop has four cases instead of three.  See the class docstring.
    #:
    #: Corroborated end to end rather than read off one report: ``examples/rf_blk_delay`` runs clean
    #: to 400 MSa/s at ``samp_per_word = 4`` and breaks at exactly 500, which is
    #: ``samp_per_word * f_axis / 2`` — this stage's ceiling, and the loop's.
    cycles_per_word: ClassVar[int] = 2

    bitwidth: HwParam[int] = WORD_BW
    samp_per_word: HwParam[int] = 1
    depth: HwParam[int] = BUF_DEPTH
    #: **Samples** of horizon surrendered to bound the progress channel's lag.  It must exceed the number
    #: of samples the ingress can write between this task polling the channel and using the value —
    #: one ingress firing plus whatever the channel dropped while a sample was being written out.  At
    #: one sample per converter period and a capture loop of a few fabric cycles, that is a handful;
    #: 16 is a generous round number, and 1.6% of the buffer is a cheap price for a stated bound.
    horizon_margin: HwParam[int] = HORIZON_MARGIN
    clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        samp_type(w, int(self.samp_per_word))
        self.wr_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_wr_in", bitwidth=w,
                                   has_tlast=True)
        self.s_cmd = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_cmd", bitwidth=w,
                                   has_tlast=True)
        self.s_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_out", bitwidth=w,
                                    has_tlast=True)
        self.s_resp = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_resp", bitwidth=w,
                                     has_tlast=True)
        self.buf_r = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_r",
                                  element_type=word_element(w), nelem=d, access="read")
        for ep in (self.wr_in, self.s_cmd, self.s_out, self.s_resp, self.buf_r):
            self.add_endpoint(ep)
        #: What this task last heard about the ingress's position — a LOWER bound, never an upper one.
        self.last_wr = 0
        #: Commands refused because their window had fallen off the horizon.  Not a diagnostic: it is
        #: the counted half of the contract, and a run in which it stays zero has not tested it.
        self.n_too_old = 0
        #: Commands that had to wait for at least one word (the future / straddling cases).
        self.n_waited = 0
        #: Commands refused because the window was not a whole number of words.
        self.n_misaligned = 0

    @property
    def usable_horizon(self) -> int:
        """Samples of history a command may reach back over — ``depth * spw - horizon_margin``."""
        return int(self.depth) * int(self.samp_per_word) - int(self.horizon_margin)

    def kernel_task(self) -> KernelTask:
        return KernelTask("rf_samp_buf_capture_task", "rf_samp_buf_capture_task.h",
                          ("buf_r", "wr_in", "s_cmd", "s_out", "s_resp"),
                          template_args=(int(self.bitwidth), int(self.samp_per_word),
                                         int(self.depth), int(self.horizon_margin), IDX_BW))

    @sim_only
    def count_too_old(self) -> None:
        """Tally a refused command — instrumentation, and marked as such."""
        self.n_too_old += 1

    @sim_only
    def count_waited(self) -> None:
        """Tally a command that had to wait for the converter."""
        self.n_waited += 1

    @sim_only
    def count_misaligned(self) -> None:
        """Tally a command whose window was not a whole number of words."""
        self.n_misaligned += 1

    def run_iter(self) -> ProcessGen[None]:
        mask = int(self.depth) - 1
        spw = int(self.samp_per_word)
        wrap = 1 << IDX_BW
        usable = self.usable_horizon

        cmd = yield from self.s_cmd.get(RxCmd)
        idx = int(cmd.start)
        sent = 0
        status = RF_SAMP_BUF_OK
        waited = False

        # Word alignment, decided before anything is emitted.  Refused rather than rounded: a
        # rounded window is data from the wrong time, and nothing downstream could detect it.  At
        # spw == 1 both operands are 0 and the branch folds away in both backends.
        nword = int(cmd.nsamp) // spw
        if (int(cmd.start) % spw) or (int(cmd.nsamp) % spw):
            status = RF_SAMP_BUF_MISALIGNED
            self.count_misaligned()
            nword = 0

        for _ in range(nword):
            # 1. Wait until the word holding sample `idx` has been written.  Poll first (take the
            #    newest position the channel is holding), and only then block -- there is nothing to
            #    do until the ingress advances, and blocking costs one event instead of one per
            #    fabric cycle.
            while True:
                got = yield from self.wr_in.get_nb()
                if got is not None:
                    self.last_wr = int(np.asarray(got).ravel()[-1])
                if sdiff(idx, self.last_wr) < 0:
                    break
                waited = True
                got = yield from self.wr_in.get()
                self.last_wr = int(np.asarray(got).ravel()[-1])

            # 2. Horizon, per word.
            if sdiff(self.last_wr, idx) > usable:
                status = RF_SAMP_BUF_TOO_OLD
                self.count_too_old()
                break

            val = self.buf_r.mem_read((idx // spw) & mask)
            yield from self.s_out.write(np.array([val], dtype=np.uint64))
            sent += spw
            idx = (idx + spw) % wrap

        if waited:
            self.count_waited()
        resp = RxResp(tid=int(cmd.tid), status=int(status), nsent=int(sent))
        yield from self.s_resp.write(resp)


# ---------------------------------------------------------------------------
# The composite: two tasks, one channel, one memory beside the kernel
# ---------------------------------------------------------------------------

@dataclass
class RfSampBufRx(FreeRunMod):
    """The RX sample buffer as one design scope: ingress + capture + the memory between them.

    The registrations are the design:

    ===========================   =============================================================
    ``add_comp(ingress/capture)`` the two ``hls::task``\\ s inside the generated kernel
    ``add_if(wr_if)``             the progress channel -> an ``hls::stream`` **depth 1**
    ``add_rtl_mod(mem)``          the buffer, realized as hand-written Verilog beside the kernel
    ``add_rtl_if(...)``           wrapper wires -> the tasks' memory ports stay boundary ports
    ===========================   =============================================================

    **The progress channel is depth 1 on purpose.**  It carries a running position, so only the
    newest value means anything and a deeper queue would only serve older ones.  Combined with the
    non-blocking write on one end and a non-blocking poll on the other, "the channel is full" simply
    means "the capture already knows roughly where we are".
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_samp_buf_rx"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: HwParam[int] = WORD_BW
    samp_per_word: HwParam[int] = 1
    depth: HwParam[int] = BUF_DEPTH
    horizon_margin: HwParam[int] = HORIZON_MARGIN
    clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, spw = int(self.bitwidth), int(self.depth), int(self.samp_per_word)
        self.ingress = RfSampBufIngress(sim=self.sim, name=f"{self.name}_ingress", bitwidth=w,
                                        samp_per_word=spw, depth=d, clk=self.clk)
        self.capture = RfSampBufCapture(sim=self.sim, name=f"{self.name}_capture", bitwidth=w,
                                        samp_per_word=spw, depth=d,
                                        horizon_margin=int(self.horizon_margin), clk=self.clk)
        self.add_comp(self.ingress)
        self.add_comp(self.capture)

        wr_if = StreamIF(name=f"{self.name}_wr_if", sim=self.sim, clk=self.clk, bitwidth=w, depth=1)
        wr_if.bind(ep_name="master", endpoint=self.ingress.wr_out)
        wr_if.bind(ep_name="slave", endpoint=self.capture.wr_in)
        self.add_if(wr_if)

        # `mem`, not `buf`: the attribute name becomes the Verilog instance name and `buf` is a
        # primitive gate (the wrapper emitter refuses it by name).
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem",
                           element_type=word_element(w), nelem=d)
        self.add_rtl_mod(self.mem)
        w_if = BramIF(name=f"{self.name}_bufw_if", sim=self.sim)
        w_if.bind(ep_name="master", endpoint=self.ingress.buf_w)
        w_if.bind(ep_name="slave", endpoint=self.mem.wr_port)
        self.add_rtl_if(w_if)
        r_if = BramIF(name=f"{self.name}_bufr_if", sim=self.sim)
        r_if.bind(ep_name="master", endpoint=self.capture.buf_r)
        r_if.bind(ep_name="slave", endpoint=self.mem.rd_port)
        self.add_rtl_if(r_if)

        #: ``add_comp`` x ``add_endpoint`` order, with the progress endpoints removed.  The two
        #: ``buf_*`` entries are ports of the KERNEL, joined to the memory inside the wrapper.
        self.boundary = ["s_in", "buf_w", "s_cmd", "s_out", "s_resp", "buf_r"]

        # Convenience refs for the testbenches — the boundary endpoints live on the children.
        self.s_in = self.ingress.s_in
        self.s_cmd = self.capture.s_cmd
        self.s_out = self.capture.s_out
        self.s_resp = self.capture.s_resp

    @property
    def nsamp_held(self) -> int:
        """Samples the buffer holds — ``depth`` words of ``samp_per_word`` samples each."""
        return int(self.depth) * int(self.samp_per_word)

    @property
    def capacity_samp_per_cycle(self) -> float:
        """Samples this buffer absorbs per fabric cycle — the ingress's rate contract."""
        return self.ingress.capacity_samp_per_cycle

    def max_samp_rate(self, f_axis: float, samp_per_word: int | None = None) -> float:
        """The fastest converter this buffer can be fed by, in samples/s, at *f_axis*.

        ``samp_per_word * f_axis / cycles_per_word``.  **Not** the port's capacity, which is
        ``samp_per_word * f_axis`` and which :class:`~examples.rf_loopback.rfdc.Rfdc` already checks;
        the difference between the two is where the first RTL run of this design lost 1695 of 4096
        samples.

        *samp_per_word* defaults to this buffer's own, and is an argument only so a caller can ask
        the counterfactual — "what would widening the word buy me?" — without building a second one.
        """
        spw = int(self.samp_per_word) if samp_per_word is None else int(samp_per_word)
        return float(f_axis) * spw / self.cycles_per_word

    @property
    def cycles_per_word(self) -> int:
        """Cycles per word for the buffer as a whole — the **max over its stages**, hence the slowest.

        **Not the ingress's alone**, and the difference is the whole point.  "May block freely" is a
        *safety* property: the capture is allowed to stall because nothing upstream loses data while
        it waits.  It is not a throughput exemption — every sample that crosses this buffer passes
        through *both* stages, so the buffer sustains what its slowest one does.

        Reading only the converter-facing stage is how a design ends up with a ceiling nothing can
        reach: the ingress reached II=1 on 2026-08-18 and this buffer's ceiling did **not** become
        ``samp_per_word * f_axis``, because :attr:`RfSampBufCapture.cycles_per_word` is 2.
        """
        return max(RfSampBufIngress.cycles_per_word, RfSampBufCapture.cycles_per_word)

    def check_rate(self, samp_rate: float, f_axis: float, samp_per_word: int | None = None) -> float:
        """Refuse a converter this buffer cannot absorb, and return the utilisation.

        **The converter's own rate check is not enough.**  ``Rfdc.pre_sim`` asserts
        ``samp_rate <= samp_per_word * f_axis``, which is the *port*'s capacity — one word per cycle.
        The design behind the port moves a word every :attr:`cycles_per_word` cycles -- the **slowest**
        of its stages, not the ingress's alone -- so its real capacity is that divided by the
        per-word cost, and the difference is not academic: the
        first RTL run of this design lost 41% of its samples in the gap between the two numbers,
        silently, while pysim reported a clean run.

        Owned here rather than by a testbench because a module's throughput is part of its interface
        contract — but it needs the *pairing*, so the converter's rate is an argument rather than a
        lookup.
        """
        spw = int(self.samp_per_word) if samp_per_word is None else int(samp_per_word)
        cap = self.max_samp_rate(f_axis, spw)
        if float(samp_rate) > cap:
            raise ValueError(
                f"{type(self).__name__} '{self.name}': {float(samp_rate):g} samples/s exceeds what "
                f"the ingress can absorb — samp_per_word * f_axis / cycles_per_word = "
                f"{spw} * {float(f_axis):g} / {self.cycles_per_word} = "
                f"{cap:g}. The converter cannot be back-pressured, so the excess is not delayed, it "
                f"is LOST. pysim WILL now show it as a nonzero `dropped` on the boundary "
                f"interface — RfSampBufIngress.run_iter charges cycles_per_word per word — so this "
                f"refusal and the simulation agree. Lower the rate, widen the word "
                f"(samp_per_word), or make the ingress body cheaper.")
        return float(samp_rate) / cap

    @property
    def n_too_old(self) -> int:
        """Commands refused because their window had fallen off the horizon."""
        return int(self.capture.n_too_old)

    @property
    def n_waited(self) -> int:
        """Commands that had to wait for the converter at least once."""
        return int(self.capture.n_waited)

    @property
    def n_misaligned(self) -> int:
        """Commands refused because their window was not a whole number of words."""
        return int(self.capture.n_misaligned)
