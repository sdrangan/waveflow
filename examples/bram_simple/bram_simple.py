"""bram_simple.py — shared memory between two tasks, **domain-free** and command-driven.

``plans/bram_simple.md``.  Two free-running ``hls::task`` bodies share one true-dual-port memory that
lives *beside* the kernel as hand-written Verilog; a generated wrapper joins them.  The mechanism is
:mod:`waveflow.hw.bram` and is documented in ``docs/guide/interface/bram.md`` — what is here is the
worked example a reader who wants "shared memory between two modules" should be able to read without
knowing anything about RF::

    cmd_w  ─▶ ┌────────────┐ ──buf_w──▶ ┌──────────┐
    data_w ─▶ │ BramWriteCmd│           │  T2pBram │   hand-written Verilog,
    resp_w ◀─ └────────────┘            │          │   BESIDE the kernel
    cmd_r  ─▶ ┌────────────┐ ──buf_r──▶ │          │
    data_r ◀─ │ BramReadCmd │ ◀─────────└──────────┘
    resp_r ◀─ └────────────┘

**The duplication with** :mod:`waveflow.hw.rf_shot_buf` **is deliberate.**  Seeing the same primitive
carry two unrelated designs is the point of having a primitive; this one is the domain-free half.

Scenario zero is the witness, and its numbers are not negotiable
---------------------------------------------------------------
``plans/witness/t2p_bram/`` is four hand-written files that were csynthed and simulated **before any
of this infrastructure existed**: write ``buf[i] = i + 100`` for 256 words, then read addresses
``0, 1, 7, 255, 128`` and get back ``100, 101, 107, 355, 228``.  That is the only gate in this repo
checking Waveflow against something built independently of Waveflow.  The command-driven design
*subsumes* it: the witness is ``write(wp=0, nwords=256)`` followed by five one-word reads.

A **ramp rather than a constant**, deliberately: the likeliest failure is a read-latency mismatch
between the kernel's ``latency=`` pragma and the memory's published ``READ_LATENCY``, which shifts
every value by one and would sail through a constant check.

The geometry wraps, and that is what the retired ``bram_toy`` could not do
--------------------------------------------------------------
The gated configuration is **64-bit words**.  Vitis byte-addresses a ``mode=bram`` port, so the
wrapper has to undo a ``>> 3`` at 64 bits; a design that never addresses past ``depth / (W/8)``
round-trips perfectly whether or not the wrapper undoes anything.  The retired ``bram_toy`` filled 256 of
1024 words at 16 bits — byte addresses 0…510, no wrap — and stayed green through the defect that had
every BRAM design in the repo mis-addressed (``fix(build): the BRAM wrapper fed a BYTE address to a
word-addressed memory``, 2026-08-24).  At 64 bits the same 256 words reach byte address 2040 in a
1024-word memory: **word 128 onward aliases immediately** if the convention is wrong.

Both commands answer, and ``ReadResp`` is not there for symmetry
----------------------------------------------------------------
A ``WriteResp`` is obvious — a write has no return path, so a write that does not fully land
completes silently and leaves the memory half-written.  A ``ReadResp`` needs its own argument and has
one: **a refused read returns zero words, and zero words is indistinguishable from "not yet" on a
stream.**  A consumer waiting for ``nwords`` that will never arrive does not see an error; it sees a
stream that has gone quiet.  The only channel that can say "no" is one that answers whether or not
there is data.

**Status carries the range check**, in **word** units, and it is *refusal* rather than wrap: a
command whose range leaves the memory is rejected whole, because a silent wrap would hand back
plausible data from the wrong place.  (Contrast :class:`~waveflow.hw.rf_shot_buf.RfShotBuf` and the
RF buffers, where a *circular* pointer is the whole point.)

> **The bounds check would NOT have caught the addressing bug.**  The check is in words — the
> design's units — while the byte/word scaling defect lived *below* it, in the wrapper.  A command
> reading words 0…255 of 1024 passes the range check and still aliased.  Two different failures, two
> different guards: the range check is the caller's, and
> ``test_the_wrapper_undoes_the_shift_vitis_actually_emits`` is the convention's.

**Only two statuses exist**, and that is a scope decision rather than an oversight: a *legal* range
whose payload arrives short is a third status this design has no scenario for, and inventing it
before a scenario needs it would put an unexercised branch in a teaching example.  What
:attr:`BramStatus.OUT_OF_RANGE` covers is the range refusal, and the refused write's payload is **consumed
and discarded** so the payload stream does not desynchronize behind it.

Overlap is the point, and it is conventional rather than structural
------------------------------------------------------------------
The scenario runs in two phases and the second is where the teaching is.  Phase 1 is the witness:
load, then read, nothing live at the same time.  Phase 2 writes 64…127 **while** a read of 0…63 is
outstanding — which is what a true-dual-port memory is *for*, and also where "no hazard" stops being
structural: the design permits overlap, so keeping the ranges disjoint is the caller's job.
``bram_t2p.v``'s ``$error`` is what catches a mistake, and
``tests/examples/test_bram_simple_xsi.py`` makes it fire on purpose rather than assuming it would.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.bram import BramIF, BramIFMaster, T2pBram, word_element
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL, SEQUENTIAL_XSI_TB
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.stream_tb import StreamDriver, StreamSink

__all__ = ["ADDRS", "BASE", "DEPTH", "EXPECTED", "FILL", "SCHEMA_CLASSES", "SENTINEL_BASE",
           "WORD_BW", "XSI_N_CYCLES", "BramReadCmd", "BramSimple", "BramSimpleTB", "BramStatus",
           "BramStatusField", "BramWriteCmd", "ReadCmd", "ReadResp", "Scenario", "Word64",
           "WriteCmd", "WriteResp", "check_outputs", "check_xsi_outputs", "collision_scenario",
           "resp_words", "run_pysim", "scenario_zero", "write_scenario"]

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

#: **The gated word width, and it is 64 for one reason**: Vitis's byte-address scaling is ``>> 3``
#: there, and 256 words of a 1024-word memory reach byte address 2040 — past the wrap.  A wrapper
#: that does not undo the scaling aliases at word 128, immediately and visibly.  At 16 bits the same
#: scenario is green either way, which is the whole lesson of ``bram_toy``'s failure as a witness.
WORD_BW = 64

#: Words in the memory.  A power of two: the Verilog indexes ``mem[addr[AW-1:0]]``, so anything else
#: aliases high addresses onto low ones silently.
DEPTH = 1024

#: Words the witness writes.  Deliberately **less than the depth** so an off-by-one in the address
#: arithmetic has somewhere to show, and deliberately **more than** ``DEPTH / (WORD_BW / 8) = 128``
#: so the byte/word convention is exercised rather than assumed.
FILL = 256

#: The witness's ramp base and its five addresses.  ``buf[i] = i + 100``; address 255 reads the last
#: word written and 128 the middle — both still look right under a one-cycle shift if the data is
#: constant, which is why it is not.
BASE = 100
ADDRS = (0, 1, 7, 255, 128)
EXPECTED = tuple(a + BASE for a in ADDRS)          # 100, 101, 107, 355, 228

#: First word of the **sentinel** a legal write puts at the top of the memory before the refused one
#: aims at it.  Reading never-written memory is not a check: pysim returns 0 from a zeroed numpy
#: array and the RTL returns ``X``, because ``bram_t2p.v``'s ``mem`` has no initial value.  Distinct
#: from :data:`BASE` so a sentinel word can never be mistaken for a ramp word.
SENTINEL_BASE = 500


# ---------------------------------------------------------------------------
# The four messages, and they are DataLists
# ---------------------------------------------------------------------------
#
# Every field layout on this design's boundary is declared ONCE, here, and both backends read it
# through the schema: pysim with ``get(WriteCmd)`` / ``write(resp)``, the kernel with
# ``c.read_stream<W>(s)`` / ``r.write_stream<W>(s)`` out of the header ``include_filename``
# generates.  Neither side takes a message apart a word at a time.
#
# That is not a style preference.  A command read as N separate ``s.read()`` calls authors the field
# layout a second time, in the one place nothing checks it against the generated header -- the same
# defect as hand-rolled element packing, one level up.  Stage 1 of this example did exactly that, and
# this is the correction.  See docs/guide/interface/stream.md#the-four-ways-to-move-data.


class BramStatus(IntEnum):
    """What a command's response says happened.

    An ``IntEnum`` rather than two module constants, because the point of the change is that no
    number in a response is one nothing can name.  ``0`` in a capture is meaningless; ``BramStatus.OK``
    is the same word spelled the same way by the schema, the generated C++ ``enum class``, the model
    and the test.
    """

    OK = 0
    OUT_OF_RANGE = 1


#: The status field, and the reason it is listed in :data:`SCHEMA_CLASSES` in its own right: the
#: opcode has to reach C++ as a real ``enum``, which is what lets a body compare against
#: ``BramStatus::OUT_OF_RANGE`` rather than against a bare integer nothing checks.  ``FirOpField`` in
#: ``examples/fir_block`` is the precedent.
BramStatusField = EnumField.specialize(enum_type=BramStatus, bitwidth=WORD_BW)

#: One field per **stream word**, at the design's own width.
#:
#: The choice is deliberate and it is what fixes the wire shapes: a command is three fields and
#: therefore exactly three words, a response two and therefore two.  A narrower field would pack
#: several per word and make the message shorter than its field count, which is harder to read off a
#: waveform for no benefit here -- ``waddr`` has to span the memory's address range and ``tid`` is a
#: host's to choose, so neither wants to be squeezed.
#:
#: It also **pins the design to a 64-bit boundary**: an ``EnumField`` may not straddle a word, so
#: these messages cannot be carried on a narrower stream.  See
#: ``test_the_messages_pin_the_stream_width``.
Word64 = IntField.specialize(bitwidth=WORD_BW, signed=False)


class WriteCmd(DataList):
    """Write ``nsamp`` payload words starting at ``waddr``.

    ``tid`` is what makes the response usable from a second thread: a host correlates a reply to the
    command it issued instead of inferring it from ordering.  Same reason
    :class:`~waveflow.hw.rf_tx_stream.TxResp` carries one.
    """

    include_filename: ClassVar[str | None] = "bram_write_cmd.h"
    elements: ClassVar[dict] = {
        "tid":   {"schema": Word64, "description": "transaction id, echoed on the response"},
        "nsamp": {"schema": Word64, "description": "payload words this command carries"},
        "waddr": {"schema": Word64, "description": "first word address written"},
    }


class WriteResp(DataList):
    """One per :class:`WriteCmd`, and the whole reason the write side has an output at all.

    A write has no return path: a command that does not fully land completes **silently** and leaves
    the memory half-written.  This is the only channel that can say otherwise.
    """

    include_filename: ClassVar[str | None] = "bram_write_resp.h"
    elements: ClassVar[dict] = {
        "tid":    {"schema": Word64, "description": "the command's transaction id"},
        "status": {"schema": BramStatusField, "description": "OK or OUT_OF_RANGE"},
    }


class ReadCmd(DataList):
    """Return ``nsamp`` words starting at ``raddr``, on the data stream."""

    include_filename: ClassVar[str | None] = "bram_read_cmd.h"
    elements: ClassVar[dict] = {
        "tid":   {"schema": Word64, "description": "transaction id, echoed on the response"},
        "nsamp": {"schema": Word64, "description": "words to return"},
        "raddr": {"schema": Word64, "description": "first word address read"},
    }


class ReadResp(DataList):
    """One per :class:`ReadCmd`, and it is not there for symmetry.

    A refused read returns **zero words**, and zero words is indistinguishable from *"not yet"* on a
    stream: a consumer waiting for ``nsamp`` that will never arrive sees silence, not an error.  The
    only channel that can report the refusal is one that answers whether or not there is data.
    """

    include_filename: ClassVar[str | None] = "bram_read_resp.h"
    elements: ClassVar[dict] = {
        "tid":    {"schema": Word64, "description": "the command's transaction id"},
        "status": {"schema": BramStatusField, "description": "OK or OUT_OF_RANGE"},
    }


#: What the build emits C++ headers for.  ``BramStatusField`` is listed on its own so the status
#: reaches the kernel as a real ``enum class`` rather than an integer literal.
SCHEMA_CLASSES = [BramStatusField, WriteCmd, WriteResp, ReadCmd, ReadResp]

#: A fixed run bound for the generated XSI main — a testbench constant, not a latency.  The sink
#: timestamps the real completion, and ``WANT_CYCLES`` in the XSI test is that measurement.
XSI_N_CYCLES = 4000


def _word(ep) -> ProcessGen[int]:
    """Take exactly one word off a stream — the **token** reader.

    What is left after the payload paths were vectorized: ``go`` really is one word, once, and
    ``nwords_max=1`` says so.  It is not a general stream idiom — a data path that reads one word at
    a time in Python has opted out of the LT model (``plans/typed_transfer_codec.md``, Case 2).
    """
    words = yield from ep.get(nwords_max=1)
    return int(np.asarray(words).ravel()[0])


# ---------------------------------------------------------------------------
# The two tasks
# ---------------------------------------------------------------------------

@dataclass
class BramWriteCmd(FreeRunMod):
    """Take ``(wp, nwords)`` plus ``nwords`` payload words, write them, and **answer**.

    The answer is the whole reason this task is not just a stream-to-memory relay: a write has no
    return path, so a command that does not fully land completes silently and leaves the memory
    half-written.  :attr:`BramStatus.OUT_OF_RANGE` is the one refusal this design defines, and it is a **range**
    check in words — ``wp + nwords > depth`` — refused whole rather than clipped or wrapped.

    **A refused command still consumes its payload.**  The payload belongs to the command; leaving
    it in the stream would shift every later command's data by ``nwords`` and turn one caller error
    into a corrupted run.  Discarding it costs the same cycles as writing it and keeps the two
    streams in step, which is what makes the refusal *recoverable* rather than merely reported.

    The task body is **hand-written** (``src/bram_write_cmd_task.h``) for the reason
    ``MemRStream``'s is: it owns a ``bram`` array parameter, which the extractor
    has no vocabulary for.  :meth:`run_iter` is the pysim golden, not the source of the C++.
    """

    cpp_kernel_name: ClassVar[str | None] = "bram_write_cmd"

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = DEPTH
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        if d & (d - 1):
            raise ValueError(f"buffer depth must be a power of two (got {d}): the wrap is a mask")
        self.cmd_w = StreamIFSlave(sim=self.sim, name=f"{self.name}_cmd", bitwidth=w)
        self.data_w = StreamIFSlave(sim=self.sim, name=f"{self.name}_data", bitwidth=w)
        self.buf_w = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_w",
                                  element_type=word_element(w), nelem=d, access="write")
        self.resp_w = StreamIFMaster(sim=self.sim, name=f"{self.name}_resp", bitwidth=w)
        self.go_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_go", bitwidth=w)
        for ep in (self.cmd_w, self.data_w, self.buf_w, self.resp_w, self.go_out):
            self.add_endpoint(ep)
        #: pysim twin of the C++ body's ``static bool announced``.  One token, once — see
        #: :class:`BramReadCmd` for what it is for.
        self.announced = False

    def kernel_task(self) -> KernelTask:
        return KernelTask(task_fn="bram_write_cmd_task", header="bram_write_cmd_task.h",
                          signature=("buf_w", "cmd_w", "data_w", "resp_w", "go_out"),
                          template_args=(int(self.bitwidth), int(self.depth)))

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one command — which is one iteration of the C++ body.

        **Nothing here iterates elements**, and that is the point.  The command is read in one call
        and the response written in one (``get(WriteCmd)`` derives the word count from the schema and
        deserializes; ``write(resp)`` serializes) — and now the payload is too: one
        ``get_pipelined`` for the whole vector, one ``write_pipelined`` into the memory.  A
        per-element ``for`` in a pysim body opts out of the LT model that is the tool's reason to
        exist; the C++ keeps its ``#pragma HLS PIPELINE II=1`` loop, exactly as
        ``poly_evaluate_impl.tpp`` keeps its lane loop.

        **The two phases overlap because the write is anchored.**  ``get_pipelined`` hands back the
        cycle the payload's *first* word arrived, and passing that as the memory write's ``t_start``
        says the writing began then — so the pair costs ``max(stream, memory)`` rather than their
        sum, which is what a task that writes a word as it receives it actually does.

        A refused command **still consumes its payload**, which is why the get is outside the ``if``:
        leaving it in the stream would shift every later command's data.
        """
        cmd = yield from self.cmd_w.get(WriteCmd)
        wp, n = int(cmd.waddr), int(cmd.nsamp)
        ok = n <= int(self.depth) and wp <= int(self.depth) - n
        if n:
            x, tstart = yield from self.data_w.get_pipelined(self.buf_w.element_type, count=n)
            if ok:                                   # refused: consumed, then dropped on the floor
                yield from self.buf_w.write_pipelined(x, wp, tstart)
        resp = WriteResp()
        resp.tid = cmd.tid
        resp.status = BramStatus.OK if ok else BramStatus.OUT_OF_RANGE
        yield from self.resp_w.write(resp)
        if not self.announced:
            yield from self.go_out.write(np.array([1], dtype=np.uint64))
            self.announced = True


@dataclass
class BramReadCmd(FreeRunMod):
    """Take ``(rp, nwords)``, stream the words back, and **answer**.

    The response is what a refused read has instead of data.  Zero words on a stream is
    indistinguishable from "not yet": a consumer waiting for ``nwords`` that will never arrive sees a
    quiet stream, not an error.  So the status channel answers whether or not there is data, which is
    exactly what the data stream cannot do.

    **The one-time arm on ``go`` is the sequencing, and it belongs in the design.**  The witness got
    its ordering from a testbench that drove all 256 samples and only then the addresses; a
    concurrent BFM harness cannot do that, because every driver pushes from cycle 0.  So the reader
    waits **once** for the writer's first completed command and is command-driven from then on —
    which is also what leaves phase 2 free to overlap.
    """

    cpp_kernel_name: ClassVar[str | None] = "bram_read_cmd"

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = DEPTH
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        if d & (d - 1):
            raise ValueError(f"buffer depth must be a power of two (got {d}): the wrap is a mask")
        self.go_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_go", bitwidth=w)
        self.cmd_r = StreamIFSlave(sim=self.sim, name=f"{self.name}_cmd", bitwidth=w)
        self.buf_r = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_r",
                                  element_type=word_element(w), nelem=d, access="read")
        self.data_r = StreamIFMaster(sim=self.sim, name=f"{self.name}_data", bitwidth=w)
        self.resp_r = StreamIFMaster(sim=self.sim, name=f"{self.name}_resp", bitwidth=w)
        for ep in (self.go_in, self.cmd_r, self.buf_r, self.data_r, self.resp_r):
            self.add_endpoint(ep)
        #: pysim twin of the C++ body's ``static bool armed``.
        self.armed = False

    def kernel_task(self) -> KernelTask:
        return KernelTask(task_fn="bram_read_cmd_task", header="bram_read_cmd_task.h",
                          signature=("buf_r", "go_in", "cmd_r", "data_r", "resp_r"),
                          template_args=(int(self.bitwidth), int(self.depth)))

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one command, read in one call with ``get(ReadCmd)`` — and **no element loop**.

        The read path is objective 4, and the whole of it is now on the interface.  ``read_pipelined``
        publishes the model: ``READ_LATENCY`` cycles of **fill**, then one element per cycle, with the
        latency reached through the bound :class:`~waveflow.hw.bram.BramIF` from the memory's Verilog
        ``localparam``.  A body that wrote ``yield self.timeout(1)`` would be inventing the number the
        framework exists to derive — which is exactly what this body used to do, one line of it,
        behind a flag.  The fill is paid once per command because it *is* a pipeline fill; a
        pipelined reader still answers one word per cycle whatever the memory's latency.

        ``tstart`` is when the memory's **first** word came back, and handing it to the stream's
        ``write_pipelined`` overlaps the two phases instead of queuing one behind the other.
        """
        if not self.armed:
            yield from _word(self.go_in)
            self.armed = True
        cmd = yield from self.cmd_r.get(ReadCmd)
        rp, n = int(cmd.raddr), int(cmd.nsamp)
        ok = n <= int(self.depth) and rp <= int(self.depth) - n
        if ok and n:
            y, tstart = yield from self.buf_r.read_pipelined(self.buf_r.element_type, n, rp)
            yield from self.data_r.write_pipelined(y, tstart)
        resp = ReadResp()
        resp.tid = cmd.tid
        resp.status = BramStatus.OK if ok else BramStatus.OUT_OF_RANGE
        yield from self.resp_r.write(resp)


# ---------------------------------------------------------------------------
# The composite: two tasks, one token channel, and a memory beside the kernel
# ---------------------------------------------------------------------------

@dataclass
class BramSimple(FreeRunMod):
    """The design scope, and the registrations **are** the design.

    ============================  ==============================================================
    ``add_comp(wr) / (rd)``       children realized as ``hls::task``\\ s **inside** the top
    ``add_if(go_if)``             an internal channel -> an ``hls::stream`` inside the top
    ``add_rtl_mod(mem)``          a module realized as hand-written Verilog **beside** the top
    ``add_rtl_if(w_if) / (r_if)`` wrapper wires -> the tasks' memory ports stay BOUNDARY ports
    ============================  ==============================================================

    The last row is the mechanism.  Because a :class:`~waveflow.hw.bram.BramIF` is *not* in the
    ``add_if`` registry, ``derive_boundary`` never sees it, so ``buf_w`` and ``buf_r`` come out as
    boundary ports of the kernel and the join happens one level up, in the wrapper.  A ``BramIF``
    placed in ``add_if`` instead would make the memory ports vanish into a FIFO that does not exist.
    """

    cpp_kernel_name: ClassVar[str | None] = "bram_simple"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = DEPTH
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        self.wr = BramWriteCmd(sim=self.sim, name=f"{self.name}_wr", bitwidth=w, depth=d,
                               clk=self.clk)
        self.rd = BramReadCmd(sim=self.sim, name=f"{self.name}_rd", bitwidth=w, depth=d,
                              clk=self.clk)
        self.add_comp(self.wr)
        self.add_comp(self.rd)

        #: The "the memory has something in it" token: one word, once, on an ordinary internal
        #: channel, so it lowers to an ``hls::stream`` and both endpoints leave the boundary.  Depth
        #: 1 because exactly one is ever sent.
        go_if = StreamIF(name=f"{self.name}_go_if", sim=self.sim, clk=self.clk, bitwidth=w, depth=1)
        go_if.bind(ep_name="master", endpoint=self.wr.go_out)
        go_if.bind(ep_name="slave", endpoint=self.rd.go_in)
        self.add_if(go_if)

        # `mem`, not `buf`: an attribute name becomes the Verilog INSTANCE name and `buf` is a
        # primitive gate, which the wrapper emitter refuses by name rather than letting xvlog fail on
        # a syntax error that mentions no Python.
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem",
                           element_type=word_element(w), nelem=d)
        self.add_rtl_mod(self.mem)
        # `clk` is what makes the ports' pipelined ops (Case 2) measurable in cycles; the scalar
        # mem_read / mem_write need none, which is why the field is optional on BramIF.
        w_if = BramIF(name=f"{self.name}_bufw_if", sim=self.sim, clk=self.clk)
        w_if.bind(ep_name="master", endpoint=self.wr.buf_w)
        w_if.bind(ep_name="slave", endpoint=self.mem.wr_port)
        self.add_rtl_if(w_if)
        r_if = BramIF(name=f"{self.name}_bufr_if", sim=self.sim, clk=self.clk)
        r_if.bind(ep_name="master", endpoint=self.rd.buf_r)
        r_if.bind(ep_name="slave", endpoint=self.mem.rd_port)
        self.add_rtl_if(r_if)

        #: ``add_comp`` x ``add_endpoint`` order with the ``go`` endpoints removed.  The two ``buf_*``
        #: entries are ports of the KERNEL, joined to the memory inside the wrapper.
        self.boundary = ["cmd_w", "data_w", "buf_w", "resp_w",
                         "cmd_r", "buf_r", "data_r", "resp_r"]


@dataclass
class BramSimpleTB(FreeRunMod):
    """The DUT between generic AXI-Stream BFMs — and **nothing else**.

    The memory is not here.  It is inside the DUT's wrapper, which is what makes the RTL harness
    small: the elaborated design's only pins are AXI-Stream, so the BFM library needs no memory
    model.  **There is no BRAM XSI object anywhere in this repo**, and that is the stronger story: in
    XSI the memory is ``bram_t2p.v`` itself, compiled into the simulation beside the synthesized
    kernel and named in ``rtl_bram_simple_top.f``.  There is no second implementation that could
    disagree with the first — which is ``docs/guide/interface/bram.md``'s point that a hand-written
    memory is *more* verifiable than an emulated one.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    bitwidth: int = WORD_BW
    depth: int = DEPTH
    n_cycles: int = XSI_N_CYCLES
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.dut = BramSimple(sim=self.sim, name=f"{self.name}_dut", bitwidth=w,
                              depth=int(self.depth),
                              clk=self.clk)
        # has_tlast=True on the participants because the DUT's stream endpoints declare it (it is
        # StreamIFSlave/Master's default) and StreamIF refuses a mismatch.  It is pysim framing
        # only: the generated top carries plain `hls::stream<ap_uint<W> >` ports and the generic
        # BFMs drive no TLAST pin, so the RTL sees one word after another either way.
        self.cmd_w_drv = StreamDriver(sim=self.sim, name=f"{self.name}_cmd_w_drv", bitwidth=w,
                                      in_bundle="vectors/cmd_w", has_tlast=True)
        self.data_w_drv = StreamDriver(sim=self.sim, name=f"{self.name}_data_w_drv", bitwidth=w,
                                       in_bundle="vectors/data_w", has_tlast=True)
        self.cmd_r_drv = StreamDriver(sim=self.sim, name=f"{self.name}_cmd_r_drv", bitwidth=w,
                                      in_bundle="vectors/cmd_r", has_tlast=True)
        self.resp_w_snk = TimedStreamSink(sim=self.sim, name=f"{self.name}_resp_w_snk", bitwidth=w,
                                          out_bundle="vectors/resp_w", has_tlast=True)
        self.data_r_snk = TimedStreamSink(sim=self.sim, name=f"{self.name}_data_r_snk", bitwidth=w,
                                          out_bundle="vectors/data_r", has_tlast=True)
        self.resp_r_snk = TimedStreamSink(sim=self.sim, name=f"{self.name}_resp_r_snk", bitwidth=w,
                                          out_bundle="vectors/resp_r", has_tlast=True)
        for c in (self.dut, self.cmd_w_drv, self.data_w_drv, self.cmd_r_drv,
                  self.resp_w_snk, self.data_r_snk, self.resp_r_snk):
            self.add_comp(c)

        self._join(f"{self.name}_cmd_w_if", self.cmd_w_drv.stream_ep, self.dut.wr.cmd_w, w)
        self._join(f"{self.name}_data_w_if", self.data_w_drv.stream_ep, self.dut.wr.data_w, w)
        self._join(f"{self.name}_resp_w_if", self.dut.wr.resp_w, self.resp_w_snk.stream_ep, w)
        self._join(f"{self.name}_cmd_r_if", self.cmd_r_drv.stream_ep, self.dut.rd.cmd_r, w)
        self._join(f"{self.name}_data_r_if", self.dut.rd.data_r, self.data_r_snk.stream_ep, w)
        self._join(f"{self.name}_resp_r_if", self.dut.rd.resp_r, self.resp_r_snk.stream_ep, w)

    def _join(self, name: str, master, slave, w: int) -> None:
        iface = StreamIF(name=name, sim=self.sim, clk=self.clk, bitwidth=w)
        iface.bind(ep_name="master", endpoint=master)
        iface.bind(ep_name="slave", endpoint=slave)
        self.add_if(iface)


@dataclass
class TimedStreamSink(StreamSink):
    """A :class:`~waveflow.simulation.stream_tb.StreamSink` that also records **arrival cycles**.

    The XSI ``AxisSlave`` already timestamps every word into ``cycles.bin``; pysim's sink keeps only
    the words.  Objective 4 is a claim about *when* a word appears, so the two backends have to be
    comparable in the same units — which means recording the cycle in pysim too.

    It is a sink subclass rather than a framework change because the timestamp is a *measurement of
    this example*, not a property of the participant: nothing about a stream sink needs it, and a
    field on the framework class would be a second thing every graph carries for one example's sake.
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        #: Arrival cycle of each word, in the same units as the XSI ``cycles.bin``.
        self.cycles: list[int] = []

    def rx_proc(self, words):
        """Stamp every word in the burst with **its own** arrival cycle.

        A burst is n beats at II=1, not one event: it completes at ``now``, so word k of n arrived
        ``(n-1-k)`` cycles earlier.  That is ``StreamIFSlave.get_pipelined``'s back-calculation,
        applied at the sink — the same convention, so the two ends of a channel agree about when a
        word moved.

        Stamping the whole burst with the completion cycle was invisible while every burst here was
        one word; vectorizing the design (``plans/typed_transfer_codec.md``, Case 2) made it visible,
        and it would have reported a 64-word read as sixty-four simultaneous arrivals — a cadence of
        0 where the XSI ``AxisSlave``, which timestamps each beat as it takes it, measures 1.
        """
        clk = self.stream_ep.interface.clk
        end = int(round(self.now * float(clk.freq)))
        n = int(np.asarray(words).size)
        self.cycles.extend(end - (n - 1 - k) for k in range(n))
        return (yield from super().rx_proc(words))


# ---------------------------------------------------------------------------
# The scenario — one on-disk source, both backends
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    """What both backends play, plus what both are checked against.

    A dataclass rather than six loose arrays because the *expectations* travel with the stimulus: a
    scenario whose commands and whose expected answers can be edited independently is a scenario that
    will eventually be checked against itself.
    """

    #: The commands, as **message objects** rather than words.  Serializing them is
    #: :func:`write_scenario`'s job and deserializing them is the design's; nothing in between counts
    #: words, which is the whole point of declaring the schema.
    cmd_w: tuple[WriteCmd, ...]
    #: The payload, in **words**, because it is a data stream rather than a structured message.
    data_w: tuple[int, ...]
    cmd_r: tuple[ReadCmd, ...]
    #: Expected responses, again as objects: a captured response is deserialized and compared field
    #: by field, so a test failure names ``status`` rather than a word that changed.
    want_resp_w: tuple[WriteResp, ...]
    want_data_r: tuple[int, ...]
    want_resp_r: tuple[ReadResp, ...]
    #: ``(start, stop)`` into ``data_r`` for the **overlapping** read, and the index into ``resp_w``
    #: of the write that must be live inside it.  Phase 2 is a claim about *when*, so it is checked
    #: with the two backends' arrival cycles rather than with their words — a read and a write whose
    #: address ranges are disjoint produce identical data whether they overlapped in time or ran one
    #: after the other, which is exactly why "it passed" is not evidence that anything overlapped.
    overlap_read: tuple[int, int] = (0, 0)
    #: Index of the **response**, not of a word.  A sink timestamps every word and a response is
    #: :func:`resp_words` of them, so a caller indexing an arrival-cycle array has to convert —
    #: ``resp_words(WriteResp, i + 1) - 1`` is that response's last word.  Stated rather than stored
    #: as a word index because the conversion is what changed when the response grew a ``tid``, and a
    #: stored index would have silently pointed into the middle of a message.
    overlap_write_resp: int = -1
    #: ``(start, stop)`` into ``data_r`` for the read whose cadence is the throughput claim, and the
    #: index of the first data word overall (objective 4's first-word offset).
    cadence_read: tuple[int, int] = (0, 0)
    label: str = ""


def ramp(n: int, base: int = BASE) -> list[int]:
    """``base + i`` — the witness's ramp, and the reason a shift by one is visible."""
    return [base + i for i in range(int(n))]


def write_cmd(tid: int, nsamp: int, waddr: int) -> WriteCmd:
    """A :class:`WriteCmd`, built by name so a scenario never states a field ORDER."""
    c = WriteCmd()
    c.tid, c.nsamp, c.waddr = int(tid), int(nsamp), int(waddr)
    return c


def read_cmd(tid: int, nsamp: int, raddr: int) -> ReadCmd:
    c = ReadCmd()
    c.tid, c.nsamp, c.raddr = int(tid), int(nsamp), int(raddr)
    return c


def write_resp(tid: int, status: BramStatus) -> WriteResp:
    r = WriteResp()
    r.tid, r.status = int(tid), status
    return r


def read_resp(tid: int, status: BramStatus) -> ReadResp:
    r = ReadResp()
    r.tid, r.status = int(tid), status
    return r


def resp_words(schema, n: int = 1, bitwidth: int = WORD_BW) -> int:
    """Words ``n`` responses of *schema* occupy — never a literal.

    A sink timestamps every **word**, so anything indexing an arrival-cycle array by *response* has
    to go through this.  Writing the number down instead is the same re-authoring of the layout that
    hand-unpacking is, just on the measurement side: the response grew from one word to two the day
    it gained a ``tid``, and a literal would not have noticed.
    """
    return int(schema.nwords_per_inst(int(bitwidth))) * int(n)


def scenario_zero(depth: int = DEPTH, fill: int = FILL) -> Scenario:
    """**The witness, plus the two refusals, plus the overlap phase.**

    Phase 1 — the witness, unchanged in substance: ``write(0, 256)`` of the ramp, then the five
    one-word reads ``0, 1, 7, 255, 128`` answering ``100, 101, 107, 355, 228``.

    The refusals, which are the responses earning their keep:

    * a **write** whose range leaves the memory (``wp=1020, nwords=8`` in a 1024-word memory) is
      refused whole — reported rather than half-applied.  A read of ``1020…1023`` afterwards must
      still find the **sentinel** a legal write put there, not the refused command's payload.
    * a **read** whose range leaves the memory is refused — reported rather than leaving the consumer
      waiting on a stream that has gone quiet.  It returns **zero data words**, so the only evidence
      it happened at all is on the response channel.

    **The sentinel is not decoration.**  Reading memory that was never written is not a check: pysim
    returns a zero from a zeroed numpy array and the RTL returns ``X``, because ``bram_t2p.v``'s
    ``reg [DW-1:0] mem [...]`` has no initial value.  The two backends genuinely disagree there, and
    they should — so the words the refusal must not have touched are given a value first.

    Phase 2 — the deliberate overlap: ``write(64, 64)`` runs **while** ``read(0, 64)`` is
    outstanding.  Disjoint ranges, so it is legal; ``bram_t2p.v``'s ``$error`` is what would say
    otherwise, and :func:`collision_scenario` is the same design driven into it on purpose.

    **The order of the commands is the only ordering this design has**, and that is a property worth
    meeting head-on.  There is exactly one token, spent once, arming the reader after the writer's
    first command; every later dependency is the *caller's* to arrange.  This scenario arranges the
    one it has — the sentinel read must not overtake the refused write — by making it the reader's
    last command, behind a 64-word read, while the refused write is the writer's third of four.  Both
    backends then confirm the arrangement held, because a sentinel read that *did* overtake would
    return the ramp's tail instead of the sentinel and fail loudly in whichever backend it happened
    in.
    """
    d, f = int(depth), int(fill)
    bad_wp, bad_n = d - 4, 8                    # 1020 + 8 > 1024 -- refused
    sentinel = ramp(4, base=SENTINEL_BASE)      # a KNOWN value at 1020..1023, so the refusal is
    phase2 = ramp(64, base=7000)                # checkable against something other than "unwritten"

    # `tid` is 1-based and simply counts the commands on each stream.  Any host scheme would do --
    # what matters is that the response echoes it, so a reply can be matched to its command without
    # relying on ordering.
    writes = [write_cmd(1, f, 0),                          # the witness's ramp
              write_cmd(2, len(sentinel), bad_wp),         # the sentinel
              write_cmd(3, bad_n, bad_wp),                 # OUT OF RANGE: 1020 + 8 > 1024
              write_cmd(4, len(phase2), 64)]               # phase 2, overlapping the read below
    data_w = ramp(f) + sentinel + ramp(bad_n, base=900) + phase2
    reads = ([read_cmd(i + 1, 1, a) for i, a in enumerate(ADDRS)]   # the witness's five addresses
             + [read_cmd(6, bad_n, bad_wp)]                # OUT OF RANGE: no data, only a status
             + [read_cmd(7, 64, 0)]                        # phase 2: overlaps the write of 64..127
             + [read_cmd(8, len(sentinel), bad_wp)])       # the refused write left these alone
    want_data_r = list(EXPECTED) + ramp(64) + sentinel
    ok, bad = BramStatus.OK, BramStatus.OUT_OF_RANGE
    return Scenario(cmd_w=tuple(writes), data_w=tuple(data_w), cmd_r=tuple(reads),
                    want_resp_w=tuple(write_resp(c.tid, bad if i == 2 else ok)
                                      for i, c in enumerate(writes)),
                    want_data_r=tuple(want_data_r),
                    want_resp_r=tuple(read_resp(c.tid, bad if i == 5 else ok)
                                      for i, c in enumerate(reads)),
                    overlap_read=(len(EXPECTED), len(EXPECTED) + 64),
                    overlap_write_resp=3,
                    cadence_read=(len(EXPECTED), len(EXPECTED) + 64),
                    label="scenario zero")


def write_scenario(root, sc: Scenario | None = None, bitwidth: int = WORD_BW) -> Scenario:
    """Materialize ``<root>/vectors/{cmd_w,data_w,cmd_r}`` — what both backends play.

    **One burst is one message**, on all three streams, and the burst length always comes from the
    thing being sent rather than from a convention stated here: a command's from its schema
    (``serialize`` decides the length), a payload's from the ``nsamp`` of the command it belongs to.

    *A command is ONE burst.*  ``get(WriteCmd)`` asks for the schema's whole word count in a single
    call, and a pysim slave dequeues a whole burst per call — so a command split across bursts would
    be read a fragment at a time and the design would have to count words again.

    *A payload is ONE BURST OF ITS COMMAND'S ``nsamp`` WORDS.*  This replaces a one-word-per-burst
    framing whose stated reason was "one pysim firing equals one RTL firing".  That rationale is
    retired with the per-element loops it justified: a pysim body that reads a word at a time is not
    a faithful twin of an ``II=1`` C++ loop, it is a design that has opted out of the LT model — the
    same relationship ``PolyAccel`` has to ``poly_evaluate_impl.tpp``, where vectorized Python stands
    against a looped ``.tpp`` and the timing lives in the model.  ``get_pipelined(count=nsamp)``
    needs the payload as one burst, because a pysim slave dequeues a whole burst per call and
    truncation *discards* the remainder.

    **This is a change to the vectors, so it was re-gated rather than assumed.**  ``words.bin`` is
    byte-identical — the same words in the same order — but ``bounds.bin`` is not, and both backends
    read it: the XSI ``AxisMaster`` asserts ``TLAST`` once per command now instead of once per word.
    The DUT does not care (``bram_write_cmd_task`` reads a raw ``hls::stream`` ``nsamp`` times and
    never inspects ``TLAST`` on the payload), which is why the measured cycle count is unmoved — see
    ``tests/examples/test_bram_simple_xsi.py``.
    """
    from waveflow.utils.burst_io import write_burst_bundle

    sc = sc or scenario_zero()
    root = Path(root)
    bw = int(bitwidth)
    for name, msgs in (("cmd_w", sc.cmd_w), ("cmd_r", sc.cmd_r)):
        write_burst_bundle([np.asarray(m.serialize(word_bw=bw), dtype=np.uint64) for m in msgs],
                           root / "vectors" / name)
    words = np.asarray(sc.data_w, dtype=np.uint64)
    ends = np.cumsum([int(c.nsamp) for c in sc.cmd_w])
    if ends[-1] != words.size:
        raise ValueError(
            f"the scenario's write commands ask for {int(ends[-1])} payload words but data_w holds "
            f"{words.size}. The payload is framed BY COMMAND now, so the two cannot drift: a "
            f"mismatch would silently re-align every later command's data.")
    write_burst_bundle([words[a:b] for a, b in zip([0, *ends[:-1]], ends)],
                       root / "vectors" / "data_w")
    return sc


def check_outputs(resp_w, data_r, resp_r, sc: Scenario | None = None, where: str = "",
                  bitwidth: int = WORD_BW) -> None:
    """The acceptance check, in one place because both backends make the same claim.

    Responses are **deserialized and compared field by field**, so a failure names ``tid`` or
    ``status`` rather than a word that changed — and a status is reported as ``BramStatus.OK``, which
    is the point of the field being an enum.  The payload is compared as words, because that is what
    it is.

    A shift by one is called out **by name**: it is what a read-latency mismatch between the kernel's
    ``latency=`` pragma and the memory's published ``READ_LATENCY`` produces, and the reason the
    payload is a ramp.
    """
    sc = sc or scenario_zero()
    bw = int(bitwidth)

    for got, want, schema, name in ((resp_w, sc.want_resp_w, WriteResp, "resp_w"),
                                    (resp_r, sc.want_resp_r, ReadResp, "resp_r")):
        per = schema.nwords_per_inst(bw)
        raw = np.asarray(got, dtype=np.uint64).ravel()
        if raw.size != per * len(want):
            raise AssertionError(
                f"{where}{name}: {raw.size} words = {raw.size / per:g} responses, expected "
                f"{len(want)} ({per} words each). A missing response is a command that never "
                f"answered; a partial one means the two ends disagree about the message layout, "
                f"which is what reading through the schema exists to make impossible.")
        for i, exp in enumerate(want):
            obj = schema().deserialize(raw[i * per:(i + 1) * per], word_bw=bw)
            if int(obj.tid) != int(exp.tid) or int(obj.status) != int(exp.status):
                raise AssertionError(
                    f"{where}{name}[{i}]: tid={int(obj.tid)} status={BramStatus(int(obj.status))!r}"
                    f", expected tid={int(exp.tid)} status={BramStatus(int(exp.status))!r}")

    g = np.asarray(data_r, dtype=np.uint64).ravel()
    w = np.asarray(sc.want_data_r, dtype=np.uint64).ravel()
    if g.size != w.size:
        raise AssertionError(
            f"{where}data_r: {g.size} words, expected {w.size}. A short or long read is the failure "
            f"a quiet stream cannot report and the response exists for.\n"
            f"  got  {g.tolist()}\n  want {w.tolist()}")
    if np.array_equal(g, w):
        return
    extra = ""
    if np.array_equal(g, w + 1) or np.array_equal(g, w - 1):
        extra = (" — every value is off by one, which is a READ-LATENCY MISMATCH between the "
                 "kernel's latency= pragma and the memory's published READ_LATENCY, not a data "
                 "error.")
    bad = int(np.argmax(g != w))
    raise AssertionError(
        f"{where}data_r word {bad}: {int(g[bad])} != {int(w[bad])} "
        f"({int((g != w).sum())} of {w.size} words differ){extra}\n"
        f"  got  {g.tolist()}\n  want {w.tolist()}")


def collision_scenario(depth: int = DEPTH, fill: int = FILL, rounds: int = 48,
                       lw: int = 8, lr: int = 9) -> Scenario:
    """The **deliberate** hazard: a read and a write that are not disjoint, on purpose.

    The negative half of Stage 2's gate, and it exists because ``bram_t2p.v``'s ``$error`` is the
    only thing in the whole flow that checks the invariant the design leaves to its caller.  A guard
    nobody has seen fire is a guard nobody knows works.

    **Address overlap alone is not a collision**, which is the finding this scenario is built around.
    The memory's assertion is ``a_en && |a_we && b_en && a_addr == b_addr`` — same address *in the
    same cycle*.  Both tasks sweep their range at one word per cycle, so two commands over the
    identical range are two parallel lines in (cycle, address): they never meet unless they happen to
    start in the same cycle.  What makes them meet is a **relative phase that moves**, so the ranges
    are the same but the lengths differ by one word — each round shifts the writer and the reader by
    one cycle relative to each other, and within a few dozen rounds every offset in the window has
    been visited.

    ``want_*`` is deliberately empty: this scenario is checked by what the *memory* says, not by what
    comes back, and the data it returns is genuinely undefined — read-during-write is whatever the
    BRAM's mode happens to be.
    """
    d, f = int(depth), int(fill)
    base = f // 2                                   # inside the region the witness filled
    writes = [write_cmd(1, f, 0)]
    data_w = ramp(f)
    for k in range(int(rounds)):
        writes.append(write_cmd(k + 2, int(lw), base))
        data_w += ramp(int(lw), base=7000)
    reads = [read_cmd(k + 1, int(lr), base) for k in range(int(rounds))]
    return Scenario(cmd_w=tuple(writes), data_w=tuple(data_w), cmd_r=tuple(reads),
                    want_resp_w=(), want_data_r=(), want_resp_r=(),
                    label="collision (deliberate hazard)")


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def run_pysim(root=None, sc: Scenario | None = None, *, bitwidth: int = WORD_BW,
              depth: int = DEPTH) -> BramSimpleTB:
    """Run the graph in SimPy and return the testbench — the toolchain-free golden.

    Returns the TB rather than the words so a caller can also read the sinks' **arrival cycles**,
    which is the half objective 4 is about and a byte comparison cannot see.
    """
    import tempfile

    from waveflow.simulation.simulation import Simulation

    tb = BramSimpleTB(name="tb", sim=Simulation(), bitwidth=int(bitwidth), depth=int(depth))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(root or tmp)
        write_scenario(root, sc)
        for drv in (tb.cmd_w_drv, tb.data_w_drv, tb.cmd_r_drv):
            drv.root = root
        tb.sim.run_sim()
    return tb


def captured(tb: BramSimpleTB) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(resp_w, data_r, resp_r)`` as the three sinks collected them."""
    def words(sink) -> np.ndarray:
        return np.concatenate(sink.words) if sink.words else np.zeros(0, dtype=np.uint64)
    return words(tb.resp_w_snk), words(tb.data_r_snk), words(tb.resp_r_snk)


def check_xsi_outputs(xsi_dir, sc: Scenario | None = None, want_cycles: int | None = None) -> None:
    """Check an XSI run from the bundles it dumped — the same golden pysim is checked on."""
    from waveflow.utils.burst_io import read_burst_bundle

    vdir = Path(xsi_dir) / "vectors"
    got = []
    for name in ("resp_w", "data_r", "resp_r"):
        assert (vdir / name).is_dir(), f"no capture bundle at {vdir / name} — the run dumped none"
        bursts = read_burst_bundle(vdir / name)
        got.append(np.concatenate(bursts) if bursts else np.zeros(0, dtype=np.uint64))
    check_outputs(*got, sc=sc, where="XSI: ")
    if want_cycles is not None:
        last = int(np.fromfile(vdir / "data_r" / "cycles.bin", dtype="<u8")[-1])
        assert last == want_cycles, (
            f"bram_simple's last read word landed at cycle {last}, gate expects {want_cycles}. That "
            f"is a real behaviour change: either a regression or an improvement worth re-recording.")
