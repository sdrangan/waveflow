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

The geometry wraps, and that is what ``bram_toy`` could not do
--------------------------------------------------------------
The gated configuration is **64-bit words**.  Vitis byte-addresses a ``mode=bram`` port, so the
wrapper has to undo a ``>> 3`` at 64 bits; a design that never addresses past ``depth / (W/8)``
round-trips perfectly whether or not the wrapper undoes anything.  ``examples/bram_toy`` fills 256 of
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
:data:`ST_OUT_OF_RANGE` covers is the range refusal, and the refused write's payload is **consumed
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
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.bram import BramIF, BramIFMaster, T2pBram
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL, SEQUENTIAL_XSI_TB
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.stream_tb import StreamDriver, StreamSink

__all__ = ["ADDRS", "BASE", "DEPTH", "EXPECTED", "FILL", "ST_OK", "ST_OUT_OF_RANGE", "WORD_BW",
           "XSI_N_CYCLES", "BramReadCmd", "BramSimple", "BramSimpleTB", "BramWriteCmd",
           "Scenario", "check_outputs", "check_xsi_outputs", "collision_scenario", "run_pysim",
           "scenario_zero", "write_scenario"]

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

#: **The gated word width, and it is 64 for one reason**: Vitis's byte-address scaling is ``>> 3``
#: there, and 256 words of a 1024-word memory reach byte address 2040 — past the wrap.  A wrapper
#: that does not undo the scaling aliases at word 128, immediately and visibly.  At 16 bits the same
#: scenario is green either way, which is the whole lesson of ``bram_toy``'s failure to be a witness.
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

#: The two response statuses, and there are only two — see the module docstring.  Mirrored in
#: ``src/bram_cmd_status.h``; ``test_bram_simple.py`` checks the two spellings against each other,
#: because a status code that means one thing in Python and another in C++ is a divergence no run
#: would report.
ST_OK = 0
ST_OUT_OF_RANGE = 1

#: A fixed run bound for the generated XSI main — a testbench constant, not a latency.  The sink
#: timestamps the real completion, and ``WANT_CYCLES`` in the XSI test is that measurement.
XSI_N_CYCLES = 4000


def _word(ep) -> ProcessGen[int]:
    """Take exactly one word off a stream — the pysim unit of every one of these bodies.

    ``nwords_max=1`` against a scenario written **one word per burst**: a pysim slave dequeues a
    whole burst per ``get`` and truncation *discards* the remainder, so any other framing would make
    one pysim firing stand for several RTL firings.
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
    half-written.  :data:`ST_OUT_OF_RANGE` is the one refusal Stage 1 defines, and it is a **range**
    check in words — ``wp + nwords > depth`` — refused whole rather than clipped or wrapped.

    **A refused command still consumes its payload.**  The payload belongs to the command; leaving
    it in the stream would shift every later command's data by ``nwords`` and turn one caller error
    into a corrupted run.  Discarding it costs the same cycles as writing it and keeps the two
    streams in step, which is what makes the refusal *recoverable* rather than merely reported.

    The task body is **hand-written** (``src/bram_write_cmd_task.h``) for the reason
    ``bram_toy``'s and ``MemRStream``'s are: it owns a ``bram`` array parameter, which the extractor
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
        self.buf_w = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_w", bitwidth=w, depth=d,
                                  access="write")
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

        ``get(nwords_max=1)`` per word, so the scenario must write **one word per burst**: a pysim
        slave dequeues a whole burst per ``get`` and truncation *discards* the remainder, so a
        multi-word burst would be one pysim firing against several RTL firings and the two backends
        would be running different designs.
        """
        wp = yield from _word(self.cmd_w)
        n = yield from _word(self.cmd_w)
        ok = n <= int(self.depth) and wp <= int(self.depth) - n
        for i in range(n):
            x = yield from _word(self.data_w)
            if ok:                                   # refused: consumed, then dropped on the floor
                self.buf_w.mem_write(wp + i, x)
        yield from self.resp_w.write(
            np.array([ST_OK if ok else ST_OUT_OF_RANGE], dtype=np.uint64))
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
    #: Model the memory's read latency in pysim (Stage 3 / learning objective 4).  The number itself
    #: is never written down here — it is read from ``self.buf_r.read_latency``, which resolves
    #: through the bound :class:`~waveflow.hw.bram.BramIF` to the memory's published
    #: ``READ_LATENCY``.  ``False`` is the *un*-modelled backend, kept so the difference between the
    #: two can be **measured** rather than asserted from a docstring.
    model_read_latency: bool = True
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        if d & (d - 1):
            raise ValueError(f"buffer depth must be a power of two (got {d}): the wrap is a mask")
        self.go_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_go", bitwidth=w)
        self.cmd_r = StreamIFSlave(sim=self.sim, name=f"{self.name}_cmd", bitwidth=w)
        self.buf_r = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_r", bitwidth=w, depth=d,
                                  access="read")
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
        """One firing is one command.

        The read path is where the two backends differ, and :attr:`model_read_latency` is objective
        4: ``mem_read`` is a **plain method**, and the absence of the ``yield`` is the interface
        stating that no simulated time passes — deliberately, because a BRAM answer is deterministic,
        unarbitrated and one cycle, so a discrete-event model of it would add a timestep and no
        fidelity.  What that leaves out is not throughput (a pipelined reader still answers one word
        per cycle) but **when the first answer appears**, which at RTL is ``READ_LATENCY`` cycles
        after the address.  Paying it once per command, outside the per-word loop, is exactly that:
        a pipeline fill, not a per-word cost.
        """
        if not self.armed:
            yield from _word(self.go_in)
            self.armed = True
        rp = yield from _word(self.cmd_r)
        n = yield from _word(self.cmd_r)
        ok = n <= int(self.depth) and rp <= int(self.depth) - n
        if ok and n:
            if self.model_read_latency:
                # The number is NEVER written down here.  `BramIFMaster.read_latency` raises when
                # unbound, precisely so a latency that cannot be traced to a memory's published value
                # never reaches a model -- a student writing `yield self.timeout(1)` is doing the
                # thing the framework refuses to do.
                yield self.timeout(int(self.buf_r.read_latency) / float(self.clk.freq))
            for i in range(n):
                val = self.buf_r.mem_read(rp + i)
                yield from self.data_r.write(np.array([val], dtype=np.uint64))
        yield from self.resp_r.write(
            np.array([ST_OK if ok else ST_OUT_OF_RANGE], dtype=np.uint64))


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
    model_read_latency: bool = True
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        self.wr = BramWriteCmd(sim=self.sim, name=f"{self.name}_wr", bitwidth=w, depth=d,
                               clk=self.clk)
        self.rd = BramReadCmd(sim=self.sim, name=f"{self.name}_rd", bitwidth=w, depth=d,
                              model_read_latency=bool(self.model_read_latency), clk=self.clk)
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
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem", dwidth=w, depth=d)
        self.add_rtl_mod(self.mem)
        w_if = BramIF(name=f"{self.name}_bufw_if", sim=self.sim)
        w_if.bind(ep_name="master", endpoint=self.wr.buf_w)
        w_if.bind(ep_name="slave", endpoint=self.mem.wr_port)
        self.add_rtl_if(w_if)
        r_if = BramIF(name=f"{self.name}_bufr_if", sim=self.sim)
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
    model_read_latency: bool = True
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.dut = BramSimple(sim=self.sim, name=f"{self.name}_dut", bitwidth=w,
                              depth=int(self.depth),
                              model_read_latency=bool(self.model_read_latency), clk=self.clk)
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
        clk = self.stream_ep.interface.clk
        cyc = int(round(self.now * float(clk.freq)))
        self.cycles.extend([cyc] * int(np.asarray(words).size))
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

    cmd_w: tuple[int, ...]
    data_w: tuple[int, ...]
    cmd_r: tuple[int, ...]
    want_resp_w: tuple[int, ...]
    want_data_r: tuple[int, ...]
    want_resp_r: tuple[int, ...]
    #: ``(start, stop)`` into ``data_r`` for the **overlapping** read, and the index into ``resp_w``
    #: of the write that must be live inside it.  Phase 2 is a claim about *when*, so it is checked
    #: with the two backends' arrival cycles rather than with their words — a read and a write whose
    #: address ranges are disjoint produce identical data whether they overlapped in time or ran one
    #: after the other, which is exactly why "it passed" is not evidence that anything overlapped.
    overlap_read: tuple[int, int] = (0, 0)
    overlap_write_resp: int = -1
    #: ``(start, stop)`` into ``data_r`` for the read whose cadence is the throughput claim, and the
    #: index of the first data word overall (objective 4's first-word offset).
    cadence_read: tuple[int, int] = (0, 0)
    label: str = ""


def ramp(n: int, base: int = BASE) -> list[int]:
    """``base + i`` — the witness's ramp, and the reason a shift by one is visible."""
    return [base + i for i in range(int(n))]


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
    sentinel = ramp(4, base=500)                # a KNOWN value at 1020..1023, so the refusal is
    phase2 = ramp(64, base=7000)                # checkable against something other than "unwritten"

    cmd_w = [0, f, bad_wp, len(sentinel), bad_wp, bad_n, 64, len(phase2)]
    data_w = ramp(f) + sentinel + ramp(bad_n, base=900) + phase2
    cmd_r = ([v for a in ADDRS for v in (a, 1)]     # the witness's five one-word reads
             + [bad_wp, bad_n]                      # refused read: no data, only a status
             + [0, 64]                              # phase 2: overlaps the write of 64..127
             + [bad_wp, len(sentinel)])             # the refused write left these words alone
    want_data_r = list(EXPECTED) + ramp(64) + sentinel
    return Scenario(cmd_w=tuple(cmd_w), data_w=tuple(data_w), cmd_r=tuple(cmd_r),
                    want_resp_w=(ST_OK, ST_OK, ST_OUT_OF_RANGE, ST_OK),
                    want_data_r=tuple(want_data_r),
                    want_resp_r=(ST_OK,) * 5 + (ST_OUT_OF_RANGE,) + (ST_OK,) * 2,
                    overlap_read=(len(EXPECTED), len(EXPECTED) + 64),
                    overlap_write_resp=3,
                    cadence_read=(len(EXPECTED), len(EXPECTED) + 64),
                    label="scenario zero")


def write_scenario(root, sc: Scenario | None = None) -> Scenario:
    """Materialize ``<root>/vectors/{cmd_w,data_w,cmd_r}`` — what both backends play.

    **One word per burst**, and that is not a detail.  Each task consumes one word per ``get``, so a
    multi-word burst would be one pysim firing against several RTL firings and the two backends would
    be running different designs.  The XSI ``AxisMaster`` reads the flat ``words.bin`` and never sees
    the burst bounds, so the stimulus it plays is byte-identical either way.
    """
    from waveflow.utils.burst_io import write_burst_bundle

    sc = sc or scenario_zero()
    root = Path(root)
    for name, words in (("cmd_w", sc.cmd_w), ("data_w", sc.data_w), ("cmd_r", sc.cmd_r)):
        write_burst_bundle([np.array([x], dtype=np.uint64) for x in words],
                           root / "vectors" / name)
    return sc


def check_outputs(resp_w, data_r, resp_r, sc: Scenario | None = None, where: str = "") -> None:
    """The acceptance check, in one place because both backends make the same claim.

    A shift by one is called out **by name**: it is what a read-latency mismatch between the kernel's
    ``latency=`` pragma and the memory's published ``READ_LATENCY`` produces, and the reason the
    payload is a ramp.  An aliasing wrapper is called out too, because that is what this example's
    geometry exists to expose.
    """
    sc = sc or scenario_zero()
    for got, want, name in ((resp_w, sc.want_resp_w, "resp_w"),
                            (data_r, sc.want_data_r, "data_r"),
                            (resp_r, sc.want_resp_r, "resp_r")):
        g = np.asarray(got, dtype=np.uint64).ravel()
        w = np.asarray(want, dtype=np.uint64).ravel()
        if g.size != w.size:
            raise AssertionError(
                f"{where}{name}: {g.size} words, expected {w.size}. On a RESPONSE channel that is a "
                f"command that never answered; on the data channel it is a short or long read, "
                f"which is the failure a quiet stream cannot report and the response exists for.\n"
                f"  got  {g.tolist()}\n  want {w.tolist()}")
        if np.array_equal(g, w):
            continue
        extra = ""
        if name == "data_r" and (np.array_equal(g, w + 1) or np.array_equal(g, w - 1)):
            extra = (" — every value is off by one, which is a READ-LATENCY MISMATCH between the "
                     "kernel's latency= pragma and the memory's published READ_LATENCY, not a data "
                     "error.")
        bad = int(np.argmax(g != w))
        raise AssertionError(
            f"{where}{name} word {bad}: {int(g[bad])} != {int(w[bad])} "
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
    cmd_w = [0, f]
    data_w = ramp(f)
    for _ in range(int(rounds)):
        cmd_w += [base, int(lw)]
        data_w += ramp(int(lw), base=7000)
    cmd_r = []
    for _ in range(int(rounds)):
        cmd_r += [base, int(lr)]
    return Scenario(cmd_w=tuple(cmd_w), data_w=tuple(data_w), cmd_r=tuple(cmd_r),
                    want_resp_w=(), want_data_r=(), want_resp_r=(),
                    label="collision (deliberate hazard)")


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def run_pysim(root=None, sc: Scenario | None = None, *, bitwidth: int = WORD_BW,
              depth: int = DEPTH, model_read_latency: bool = True) -> BramSimpleTB:
    """Run the graph in SimPy and return the testbench — the toolchain-free golden.

    Returns the TB rather than the words so a caller can also read the sinks' **arrival cycles**,
    which is the half objective 4 is about and a byte comparison cannot see.
    """
    import tempfile

    from waveflow.simulation.simulation import Simulation

    tb = BramSimpleTB(name="tb", sim=Simulation(), bitwidth=int(bitwidth), depth=int(depth),
                      model_read_latency=bool(model_read_latency))
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
