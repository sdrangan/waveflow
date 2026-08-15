"""bram_toy.py — the witness's design, expressed in Waveflow.

``plans/rtl_module.md`` S2/S3.  Two free-running tasks share a true-dual-port memory: one writes at a
running pointer, the other answers addresses it is told.  That structure has **no expression inside a
Vitis kernel** — a local array shared between two ``hls::task`` bodies becomes a synchronizing PIPO
channel (silently, with a handshake that stalls the writer), and one ``bram`` port used both ways is
refused outright — so the memory lives *beside* the kernel as hand-written Verilog and a generated
wrapper joins the two.

**This example exists to be gated against something that already ran.**
``plans/witness/t2p_bram/`` is four hand-written files that were csynthed and simulated before any of
this infrastructure was designed: write ``buf[i] = i + 100`` for 256 samples, then read addresses
``0, 1, 7, 255, 128`` and get back ``100, 101, 107, 355, 228``.  Those are the numbers
``tests/examples/test_bram_toy_xsi.py`` demands of the Waveflow-generated design.  A **ramp, not a
constant, on purpose**: the likeliest failure is a read-latency mismatch between the kernel's pragma
and the memory, which shifts every value by one and would sail through a constant check.

Three structural facts, each of which is the point of a piece of machinery:

* ``T2pBram`` is registered with :meth:`~waveflow.hw.hw_module.HwModule.add_rtl_mod`, **not**
  ``add_comp`` — it is not a task, so no walk that emits tasks should ever see it.
* the two ``BramIF``\\ s are registered with ``add_rtl_if``, **not** ``add_if`` — they are wrapper
  wires, not internal channels, so the tasks' memory ports stay *boundary ports* of the kernel and
  are joined one level up.
* the ``go`` stream **is** an ``add_if`` channel, and it is what makes the answers deterministic:
  the reader waits once for the writer's "buffer ready" token.  The witness got that ordering from
  its testbench (drive all the samples, *then* the addresses); a concurrent BFM harness cannot, so
  the ordering moves into the design where it belongs.
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
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.stream_tb import StreamDriver, StreamSink

#: The witness's geometry, kept exactly: a 1024 x 16 true-dual-port buffer (one RAMB18), of which the
#: scenario fills the first 256 words.
WORD_BW = 16
DEPTH = 1024
FILL = 256

#: The scenario, from the witness's ``tb.v``: a RAMP (``buf[i] = i + 100``), then five addresses.
#: Address 255 reads the LAST word written and 128 reads the middle — both would still look right
#: under a one-cycle shift if the data were constant, which is why it is not.
BASE = 100
ADDRS = (0, 1, 7, 255, 128)
EXPECTED = tuple(a + BASE for a in ADDRS)          # 100, 101, 107, 355, 228

#: A generous ``h.run(N)`` for the XSI main: 256 writes + 5 reads, plus room for the arming handshake.
#: A testbench constant, not the design's latency — the sink timestamps that.
XSI_N_CYCLES = 2000


@dataclass
class BramWrite(FreeRunMod):
    """Writes one word per firing at a running pointer, and emits one token when the buffer is full.

    The task body is **hand-written** (``src/bram_write_task.h``), for the reason
    ``MemRStream``'s is: it owns a resource the extractor has no vocabulary for — there an ``m_axi``
    pointer, here a ``bram`` array parameter and a static write pointer.  ``run_iter`` below is the
    pysim golden, not the source of the C++.
    """

    cpp_kernel_name: ClassVar[str | None] = "bram_write"

    bitwidth: int = WORD_BW
    depth: int = DEPTH
    fill: int = FILL
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.rx_str = StreamIFSlave(sim=self.sim, name=f"{self.name}_rx", bitwidth=w)
        self.buf_w = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_w", bitwidth=w,
                                  depth=int(self.depth), access="write")
        self.go_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_go", bitwidth=w)
        for ep in (self.rx_str, self.buf_w, self.go_out):
            self.add_endpoint(ep)
        self.wr = 0                       # pysim write pointer (the C++ body's `static ap_uint<32>`)

    def kernel_task(self) -> KernelTask:
        return KernelTask(task_fn="bram_write_task", header="bram_write_task.h",
                          signature=("buf_w", "rx_str", "go_out"),
                          template_args=(int(self.bitwidth), int(self.depth), int(self.fill)))

    def run_iter(self) -> ProcessGen[None]:
        words = yield from self.rx_str.get(nwords_max=1)
        self.buf_w.mem_write(self.wr, int(words[0]))
        if self.wr == int(self.fill) - 1:
            yield from self.go_out.write(np.array([1], dtype=np.uint64))
        self.wr = 0 if self.wr == int(self.depth) - 1 else self.wr + 1


@dataclass
class BramRead(FreeRunMod):
    """Answers one address per firing, after waiting once for the writer's "buffer ready" token."""

    cpp_kernel_name: ClassVar[str | None] = "bram_read"

    bitwidth: int = WORD_BW
    depth: int = DEPTH
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.go_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_go", bitwidth=w)
        self.addr_str = StreamIFSlave(sim=self.sim, name=f"{self.name}_addr", bitwidth=w)
        self.out_str = StreamIFMaster(sim=self.sim, name=f"{self.name}_out", bitwidth=w)
        self.buf_r = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_r", bitwidth=w,
                                  depth=int(self.depth), access="read")
        for ep in (self.go_in, self.addr_str, self.out_str, self.buf_r):
            self.add_endpoint(ep)
        self.armed = False                # pysim twin of the C++ body's `static bool armed`

    def kernel_task(self) -> KernelTask:
        return KernelTask(task_fn="bram_read_task", header="bram_read_task.h",
                          signature=("buf_r", "go_in", "addr_str", "out_str"),
                          template_args=(int(self.bitwidth), int(self.depth)))

    def run_iter(self) -> ProcessGen[None]:
        if not self.armed:
            yield from self.go_in.get(nwords_max=1)
            self.armed = True
        addr = yield from self.addr_str.get(nwords_max=1)
        val = self.buf_r.mem_read(int(addr[0]))
        yield from self.out_str.write(np.array([val], dtype=np.uint64))


@dataclass
class BramToy(FreeRunMod):
    """The design scope: two tasks, one internal channel, and a memory beside the kernel.

    What this class demonstrates is the *registration*, and it is three lines that each mean
    something different:

    ==========================  ==============================================================
    ``add_comp(wr) / (rd)``     children realized as ``hls::task``\\ s **inside** the generated top
    ``add_if(go_if)``           an internal channel -> an ``hls::stream`` inside the top
    ``add_rtl_mod(buf)``        a module realized as hand-written Verilog **beside** the top
    ``add_rtl_if(w_if) / (r_if)``  wrapper wires -> the tasks' memory ports stay BOUNDARY ports
    ==========================  ==============================================================

    The last row is the whole of S2's mechanism.  Because a ``BramIF`` is *not* in the ``add_if``
    registry, ``derive_boundary`` never sees it, so ``buf_w`` and ``buf_r`` come out as boundary ports
    of the kernel with no change to that walk at all — which is what ``plans/rtl_module.md`` predicted
    ("plumbed out by machinery that already runs").
    """

    cpp_kernel_name: ClassVar[str | None] = "bram_toy"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: int = WORD_BW
    depth: int = DEPTH
    fill: int = FILL
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        self.wr = BramWrite(sim=self.sim, name=f"{self.name}_wr", bitwidth=w, depth=d,
                            fill=int(self.fill), clk=self.clk)
        self.rd = BramRead(sim=self.sim, name=f"{self.name}_rd", bitwidth=w, depth=d, clk=self.clk)
        self.add_comp(self.wr)
        self.add_comp(self.rd)

        # The "buffer ready" token: a normal internal channel, so it lowers to an hls::stream and
        # both its endpoints leave the boundary.
        go_if = StreamIF(name=f"{self.name}_go_if", sim=self.sim, clk=self.clk, bitwidth=w)
        go_if.bind(ep_name="master", endpoint=self.wr.go_out)
        go_if.bind(ep_name="slave", endpoint=self.rd.go_in)
        self.add_if(go_if)

        # The memory, and the two wires to it.  Neither registry is the one derive_boundary reads.
        # `mem`, not `buf`: an attribute name becomes the Verilog INSTANCE name, and `buf` is a
        # primitive gate — the wrapper emitter refuses it by name rather than letting xvlog fail
        # on a syntax error that mentions no Python.
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

        #: Boundary names, in ``add_comp`` x ``add_endpoint`` order with the ``go`` endpoints removed.
        #: The two ``buf_*`` entries are the ones this whole plan is about: they are ports of the
        #: KERNEL, joined to the memory in the wrapper.
        self.boundary = ["rx_str", "buf_w", "addr_str", "out_str", "buf_r"]


@dataclass
class BramToyTB(FreeRunMod):
    """The DUT between generic AXI-Stream BFMs — and **nothing else**.

    The memory is not here.  It is inside the DUT's wrapper, which is exactly the property that makes
    ``plans/rtl_module.md`` S3 small: the elaborated design's only pins are AXI-Stream, so the BFM
    library needs no memory model and is untouched.  If a memory ever needed a BFM, the wrapper would
    be the thing that is wrong.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    bitwidth: int = WORD_BW
    depth: int = DEPTH
    fill: int = FILL
    n_cycles: int = XSI_N_CYCLES
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.dut = BramToy(sim=self.sim, name=f"{self.name}_dut", bitwidth=w, depth=int(self.depth),
                           fill=int(self.fill), clk=self.clk)
        # has_tlast=True on the participants because the DUT's stream endpoints declare it and
        # StreamIF refuses a mismatch.  It is pysim framing only: the generated top carries plain
        # `hls::stream<ap_uint<W>>` ports and the generic BFMs drive no TLAST pin.
        self.rx_drv = StreamDriver(sim=self.sim, name=f"{self.name}_rx_drv", bitwidth=w,
                                   in_bundle="vectors/rx", has_tlast=True)
        self.addr_drv = StreamDriver(sim=self.sim, name=f"{self.name}_addr_drv", bitwidth=w,
                                     in_bundle="vectors/addr", has_tlast=True)
        self.sink = StreamSink(sim=self.sim, name=f"{self.name}_snk", bitwidth=w,
                               out_bundle="vectors/out", has_tlast=True)
        for c in (self.dut, self.rx_drv, self.addr_drv, self.sink):
            self.add_comp(c)

        rx_if = StreamIF(name=f"{self.name}_rx_if", sim=self.sim, clk=self.clk, bitwidth=w)
        rx_if.bind(ep_name="master", endpoint=self.rx_drv.stream_ep)
        rx_if.bind(ep_name="slave", endpoint=self.dut.wr.rx_str)
        self.add_if(rx_if)

        addr_if = StreamIF(name=f"{self.name}_addr_if", sim=self.sim, clk=self.clk, bitwidth=w)
        addr_if.bind(ep_name="master", endpoint=self.addr_drv.stream_ep)
        addr_if.bind(ep_name="slave", endpoint=self.dut.rd.addr_str)
        self.add_if(addr_if)

        out_if = StreamIF(name=f"{self.name}_out_if", sim=self.sim, clk=self.clk, bitwidth=w)
        out_if.bind(ep_name="master", endpoint=self.dut.rd.out_str)
        out_if.bind(ep_name="slave", endpoint=self.sink.stream_ep)
        self.add_if(out_if)


# ---------------------------------------------------------------------------
# The scenario — one on-disk source, both backends
# ---------------------------------------------------------------------------

def ramp_words(fill: int = FILL, base: int = BASE) -> np.ndarray:
    """``buf[i] = i + base`` — the witness's ramp."""
    return (np.arange(int(fill), dtype=np.uint64) + int(base))


def write_scenario(root, fill: int = FILL) -> None:
    """Materialize ``<root>/vectors/rx`` and ``.../addr`` — what both backends play."""
    from waveflow.utils.burst_io import write_burst_bundle

    # ONE WORD PER BURST, and that is not a detail.  The RTL task fires once per word, and a pysim
    # slave takes a whole burst per `get`; a single 256-word burst would therefore be one pysim
    # firing against 256 RTL firings — the two backends would be running different designs.  The XSI
    # `AxisMaster` reads the flat `words.bin` and never sees the burst bounds, so the stimulus it
    # plays is byte-identical either way.
    root = Path(root)
    write_burst_bundle([np.array([x], dtype=np.uint64) for x in ramp_words(fill)],
                       root / "vectors" / "rx")
    write_burst_bundle([np.array([a], dtype=np.uint64) for a in ADDRS],
                       root / "vectors" / "addr")


def check_outputs(got: np.ndarray, where: str = "") -> None:
    """The acceptance check, in one place because both backends make the same claim.

    A shift by one is called out by name: it is the specific failure a read-latency mismatch between
    the kernel's ``latency=`` pragma and the memory's ``READ_LATENCY`` produces, and the reason the
    scenario is a ramp.
    """
    want = np.array(EXPECTED, dtype=np.uint64)
    got = np.asarray(got, dtype=np.uint64).ravel()
    assert got.size == want.size, (
        f"{where}bram_toy returned {got.size} words for {want.size} addresses {ADDRS}: {got}")
    if not np.array_equal(got, want):
        shifted = np.array_equal(got, want + 1) or np.array_equal(got, want - 1)
        raise AssertionError(
            f"{where}bram_toy read {got.tolist()}, expected {list(want)} for addresses {ADDRS}"
            + (" — every value is off by one, which is a READ-LATENCY MISMATCH between the kernel's "
               "latency= pragma and the memory's published READ_LATENCY, not a data error."
               if shifted else ""))


def check_xsi_outputs(xsi_dir, want_cycles: int | None = None) -> None:
    """Check an XSI run from the bundle it dumped — the golden, in Python."""
    from waveflow.utils.burst_io import read_burst_bundle

    vdir = Path(xsi_dir) / "vectors"
    assert (vdir / "out").is_dir(), f"no capture bundle at {vdir / 'out'} — the run did not dump one"
    got = np.concatenate(read_burst_bundle(vdir / "out"))
    check_outputs(got, where="XSI: ")
    if want_cycles is not None:
        cycles = np.fromfile(vdir / "out" / "cycles.bin", dtype="<u8")
        last = int(cycles[-1])
        assert last == want_cycles, (
            f"bram_toy completed at cycle {last}, gate expects {want_cycles}. That is a real "
            f"behaviour change: either a regression or an improvement worth re-recording.")


def run_pysim(root=None) -> np.ndarray:
    """Run the graph in SimPy and return the captured words — the toolchain-free golden."""
    import tempfile

    from waveflow.simulation.simulation import Simulation

    tb = BramToyTB(name="tb", sim=Simulation())
    with tempfile.TemporaryDirectory() as tmp:
        write_scenario(root or tmp)
        tb.rx_drv.root = Path(root or tmp)
        tb.addr_drv.root = Path(root or tmp)
        tb.sim.run_sim()
    return np.concatenate(tb.sink.words) if tb.sink.words else np.zeros(0, dtype=np.uint64)
