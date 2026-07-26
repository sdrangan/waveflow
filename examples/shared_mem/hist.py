"""
hist.py — histogram accelerator as a synthesizable ``HwModule`` (the codegen
source for the ``shared_mem`` example).

It is the reference design for AXI-MM (``m_axi``) codegen, exercising the full
multi-buffer surface:

* **three distinct buffers** at independent ``MemAddr`` fields — float input
  ``data``, float ``bin_edges``, uint32 ``counts``;
* **two element types** over one ``m_axi`` bundle — ``Float32`` reads, ``Uint32``
  writes;
* **validation → status** — ``ndata``/``nbins``/address checks select a
  :class:`HistError` into the response before any memory access.

The schemas, the numpy golden (:class:`HistogramAccel`), the synthesizable
component, and a SimPy harness all live in this module; the cosim/burst harness
(``HistTest``) and the build DAG live in :mod:`hist_build`. ``HistAccel`` is the
codegen source; ``HistogramAccel`` is the numpy golden it is validated against.

The **command** rides in-band on ``s_in`` and the response on ``m_out``; the data
lives in memory over ``m_mem``. The **control plane** is separate: ``HistAccel`` is
a :class:`~waveflow.hw.hw_hostactivated.HostActivated`, so a control-only
``VitisRegMap({})`` puts ``ap_start``/``ap_done`` in the AXI-Lite slave that
``m_axi ... offset=slave`` already forces into existence (it carries ``m_mem``'s
base at ``0x10`` and previously left ``0x00`` *reserved*). The codegen root is
``on_start``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

import numpy as np
import numpy.typing as npt

from waveflow.hw.arrayutils import get_nwords, read_array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, DataList, EnumField, FloatField, IntField, MemAddr
from waveflow.hw.hw_module import HwParam
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.hw_testbench import SeqTB
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import DirectMMIF, MMIFMaster
from waveflow.hw.memory import AddrUnit, MemModel, Memory
from waveflow.hw.regmap import VitisRegMap, VitisRegMapMMIFSlave
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation

# ---------------------------------------------------------------------------
# Data model — schemas, type fields, constants, and the numpy golden
# (the data-model home for the example, matching simp_fun.py; hist_build.py
# imports these from here).
# ---------------------------------------------------------------------------
INCLUDE_DIR = "include"

TxIdField = IntField.specialize(bitwidth=16, signed=False)
NdataField = IntField.specialize(bitwidth=32, signed=False)
NbinField = IntField.specialize(bitwidth=32, signed=False)
Uint32Field = IntField.specialize(bitwidth=32, signed=False, include_dir=INCLUDE_DIR)

"""
Parameters
"""
MAX_NDATA = 1024        # number of float32 data elements
MAX_NBINS = 32          # number of histogram bins
STREAM_DWIDTH = 32      # stream data width
MEM_DWIDTH = 32         # memory data width
MEM_AWIDTH = 64         # memory address width
MEM_AUNIT = AddrUnit.byte  # memory address unit

AddrField = MemAddr.specialize(bitwidth=MEM_AWIDTH)
Float32 = FloatField.specialize(bitwidth=32, include_dir=INCLUDE_DIR)

"""
Define the data schemas for the histogram accelerator
"""


class HistError(IntEnum):
    NO_ERROR = 0
    INVALID_NDATA = 1
    INVALID_NBINS = 2
    ADDRESS_ERROR = 3


HistErrorField = EnumField.specialize(enum_type=HistError)


class HistCmd(DataList):
    """Command descriptor for the histogram accelerator."""

    elements = {
        "tx_id": {
            "schema": TxIdField,
            "description": "Transaction ID for correlating command and response",
        },
        "data_addr": {
            "schema": AddrField,
            "description": "Base address of the input data buffer",
        },
        "bin_edges_addr": {
            "schema": AddrField,
            "description":("Base address of the output histogram bin edges.  " 
                  "There should be nbins-1 edge values.  "
                  "bin 0 will have values x < bin_edges[0], bin i will have values "
                  "bin_edges[i-1] <= x < bin_edges[i], and the last bin will have values "
                  "x >= bin_edges[nbins-2]"),
        },
        "ndata": {
            "schema": NdataField,
            "description": "Number of input data elements to histogram",
        },
        "nbins": {
            "schema": NbinField,
            "description": "Number of histogram bins to produce",
        },
        "cnt_addr": {
            "schema": AddrField,
            "description": "Base address of the output histogram counts buffer",
        },
    }


class HistResp(DataList):
    """Response descriptor returned by the histogram accelerator."""

    elements = {
        "tx_id": {
            "schema": TxIdField,
            "description": "Echo of the transaction ID from the command",
        },
        "status": {
            "schema": HistErrorField,
            "description": "Histogram execution status code",
        },
    }


SCHEMA_CLASSES = [
    HistErrorField,
    HistCmd,
    HistResp,
]


@dataclass(slots=True)
class HistSimResult:
    """Result bundle returned by HistTest.simulate."""

    cmd: HistCmd
    resp: HistResp
    counts: np.ndarray
    expected: np.ndarray

    @property
    def passed(self) -> bool:
        return np.array_equal(self.counts, self.expected)



class HistogramAccel(object):
    """
    Python model for the histogram accelerator.  This is where the core logic of the histogram computation lives, and it operates on the Memory model to read inputs and write outputs.  In a real implementation, this logic would be synthesized into hardware, but here we can use the full expressiveness of Python and numpy to implement it in a straightforward way.
    """

    def __init__(
            self,
            mem : Memory,
            max_ndata: int = MAX_NDATA,
            max_nbins: int = MAX_NBINS
            ):
        """
        Constructor with the dimensioning parameters and reference to the shared memory.

        Parameters:
        -----------
        mem: Memory
            Reference to the shared memory instance for reading inputs and writing outputs.
        max_ndata: int
            Maximum number of data elements that the accelerator can process in one command.
        max_nbins: int
            Maximum number of histogram bins that the accelerator can produce in one command.
        """
        self.max_ndata = max_ndata
        self.max_nbins = max_nbins
        self.mem = mem

    def compute_hist(
        self,
        cmd : HistCmd,
    ) -> HistResp:
        """
        Compute the histogram based on the input command descriptor.  This method reads the input data and bin edges from memory, computes the histogram counts, and writes the counts back to memory.  It also constructs a response descriptor with the appropriate status code.

        Parameters:
        -----------
        cmd: HistCmd
            The command descriptor containing the transaction ID, input data address, bin edges address, number of data elements, number of bins, and output counts address.

        Returns:
        --------
        HistResp
            The response descriptor containing the transaction ID and status code.
        """
        resp = HistResp()
        resp.tx_id = cmd.tx_id

        ndata = int(cmd.ndata)
        nbins = int(cmd.nbins)

        if ndata <= 0 or ndata > self.max_ndata:
            resp.status = HistError.INVALID_NDATA
            return resp

        if nbins <= 0 or nbins > self.max_nbins:
            resp.status = HistError.INVALID_NBINS
            return resp

        try:
            data_nwords = get_nwords(Float32, word_bw=self.mem.word_size, shape=ndata)
            data_words = self.mem.read(int(cmd.data_addr), nwords=data_nwords)
            data = np.asarray(read_array(data_words, elem_type=Float32, word_bw=self.mem.word_size, shape=ndata), dtype=np.float32)

            if nbins > 1:
                edge_shape = nbins - 1
                edge_nwords = get_nwords(Float32, word_bw=self.mem.word_size, shape=edge_shape)
                edge_words = self.mem.read(int(cmd.bin_edges_addr), nwords=edge_nwords)
                bin_edges = np.asarray(
                    read_array(edge_words, elem_type=Float32, word_bw=self.mem.word_size, shape=edge_shape),
                    dtype=np.float32,
                )
            else:
                bin_edges = np.array([], dtype=np.float32)

            bin_index = np.searchsorted(bin_edges, data, side="right")
            counts = np.bincount(bin_index, minlength=nbins).astype(np.uint32, copy=False)
            packed_counts = write_array(counts, elem_type=Uint32Field, 
                                        word_bw=self.mem.word_size)
            self.mem.write(int(cmd.cnt_addr), packed_counts)
        except ValueError:
            resp.status = HistError.ADDRESS_ERROR
            return resp

        resp.status = HistError.NO_ERROR
        return resp


# ---------------------------------------------------------------------------
# Local-buffer types — used only to type the compute hook's array params/return
# in the generated C++ (``float data[]`` / ``ap_uint<32> counts[]``).  They do
# not change the SimPy runtime (the hook works on numpy arrays) and are not
# schema headers; they carry only (element type, compile-time max) for codegen.
# ---------------------------------------------------------------------------

#: Total m_axi region the testbench's flat memory array spans: data (max_ndata)
#: + edges (max_nbins) + counts (max_nbins).  Matches the generated header's
#: ``max_mem_words``.
MAX_MEM_WORDS = MAX_NDATA + 2 * MAX_NBINS


class HistDataBuf(DataArray):
    cpp_typing_only = True
    element_type = Float32
    static = True
    max_shape = (MAX_NDATA,)
    cpp_storage = "raw"


class HistEdgeBuf(DataArray):
    cpp_typing_only = True
    element_type = Float32
    static = True
    max_shape = (MAX_NBINS,)
    cpp_storage = "raw"


class HistCountBuf(DataArray):
    cpp_typing_only = True
    element_type = Uint32Field
    static = True
    max_shape = (MAX_NBINS,)
    cpp_storage = "raw"


# ---------------------------------------------------------------------------
# Golden model (numpy) — the binning semantics, shared by the SimPy hook below
# ---------------------------------------------------------------------------

def golden_counts(
    data: npt.NDArray[np.float32],
    bin_edges: npt.NDArray[np.float32],
    nbins: int,
) -> npt.NDArray[np.uint32]:
    """Reference histogram: ``bin = #{edges <= sample}`` then count per bin.

    Identical to :meth:`HistogramAccel.compute_hist`'s core and to the
    ``compute`` hook's inner loop (``searchsorted(..., side="right")``).
    """
    d = np.asarray(data, dtype=np.float32)
    e = np.asarray(bin_edges, dtype=np.float32)
    bin_index = np.searchsorted(e, d, side="right")
    return np.bincount(bin_index, minlength=int(nbins)).astype(np.uint32, copy=False)


# ---------------------------------------------------------------------------
# Accelerator (SimPy model + codegen source)
# ---------------------------------------------------------------------------

@dataclass
class HistAccel(HostActivated):
    """Synthesizable histogram kernel — the codegen source for the generated kernel (``gen/hist.cpp``).

    ``on_start`` is the kernel body (one ``ap_start`` invocation): read one
    :class:`HistCmd`, validate it into a status, read the data + bin edges from
    memory over ``m_mem``, bin them in the ``compute`` hook, write the counts
    back, and emit one :class:`HistResp`. It is written to read cleanly as the
    source the generated kernel is lowered from (decision 4).

    **The command stays in-band on** ``s_in`` — that is the ``shared_mem`` lesson
    and the point of the example; only the *control* plane is host-activated. The
    ``VitisRegMap({})`` below declares **no application registers**: its whole job
    is ``ap_start``/``ap_done``. The AXI-Lite slave is not new — ``m_axi ...
    offset=slave`` already forced one into existence to carry ``m_mem``'s base at
    ``0x10``, and it declared ``0x00 : reserved`` with zero ``ap_start`` logic. So
    the kernel needed *two* masters: a CPU for the base address and something else
    to pulse an ``ap_start`` wire. Deriving from
    :class:`~waveflow.hw.hw_hostactivated.HostActivated` fills the reserved slot in
    the slave that was already there, and makes the component's kind explicit
    (``check(HistAccel)`` can now answer).
    """

    cpp_kernel_name: ClassVar[str | None] = "hist"
    cpp_namespace:   ClassVar[str | None] = "hist_impl"

    in_bw:     HwParam[int] = STREAM_DWIDTH
    out_bw:    HwParam[int] = STREAM_DWIDTH
    mem_bw:    HwParam[int] = MEM_DWIDTH
    mem_awidth: HwParam[int] = MEM_AWIDTH   # m_axi address width (TB-side typing)
    max_ndata: HwParam[int] = MAX_NDATA
    max_nbins: HwParam[int] = MAX_NBINS
    clk:       Clock = field(default_factory=lambda: Clock(freq=1e9))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in  = StreamIFSlave( name=f'{self.name}_s_in',  sim=self.sim, bitwidth=self.in_bw)
        self.m_out = StreamIFMaster(name=f'{self.name}_m_out', sim=self.sim, bitwidth=self.out_bw)
        self.m_mem = MMIFMaster(    name=f'{self.name}_m_mem', sim=self.sim, bitwidth=self.mem_bw)
        # Control-only regmap: no application registers — the command rides in-band on
        # ``s_in``.  This exists purely so ap_start/ap_done land at 0x00 of the AXI-Lite
        # slave that ``m_axi ... offset=slave`` already creates for ``m_mem`` (0x10).
        self.regmap = VitisRegMap({})
        self.s_lite = VitisRegMapMMIFSlave(
            name=f'{self.name}_s_lite',
            sim=self.sim,
            bitwidth=32,
            regmap=self.regmap,
            on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.m_mem, self.s_lite):
            self.add_endpoint(ep)

    def on_start(self) -> ProcessGen[None]:
        """Kernel body (single ``ap_start``/``ap_done`` invocation).

        ``validate`` returns a status; a non-NO_ERROR status takes the early-
        return path (an ``!=`` against a constant, which the extractor lowers
        directly, decision 4). The three array ops then read/write against the
        one ``m_mem`` bundle at the three command addresses, with per-buffer
        compile-time bounds via ``max_count`` (decisions 1–3). The edges read is
        unconditional — when ``nbins == 1`` its runtime count is ``0`` (a no-op
        burst), avoiding a ``>`` branch the extractor can't lower.
        """
        cmd: HistCmd = yield from self.s_in.get(HistCmd)

        status = yield from self.validate(cmd)
        if status != HistError.NO_ERROR:
            yield from self.respond(self.m_out, cmd.tx_id, status)
            return

        data = yield from self.m_mem.read_array(
            Float32, cmd.ndata, cmd.data_addr, max_count=self.max_ndata)
        edges = yield from self.m_mem.read_array(
            Float32, cmd.nbins - 1, cmd.bin_edges_addr, max_count=self.max_nbins)

        counts = yield from self.compute(data, edges, cmd.ndata, cmd.nbins)
        yield from self.m_mem.write_array(
            counts, Uint32Field, cmd.cnt_addr, cmd.nbins, max_count=self.max_nbins)

        # status is NO_ERROR on the success path — reuse it for the response.
        yield from self.respond(self.m_out, cmd.tx_id, status)

    @synthesizable
    def validate(self, cmd: HistCmd) -> ProcessGen[HistError]:
        """Bounds + alignment checks (hand-written as ``hist_validate_impl.cpp``).

        Returns the :class:`HistError` status; ``NO_ERROR`` means proceed. As a
        ``@synthesizable`` hook its body is *not* extracted (the C++ is
        hand-written and references the ``max_ndata``/``max_nbins`` constants),
        so it may freely read the HwParams here for the SimPy model."""
        ndata = int(cmd.ndata)
        nbins = int(cmd.nbins)
        word_bytes = self.mem_bw // 8
        if ndata <= 0 or ndata > self.max_ndata:
            return HistError.INVALID_NDATA
        if nbins <= 0 or nbins > self.max_nbins:
            return HistError.INVALID_NBINS
        if (int(cmd.data_addr) % word_bytes
                or int(cmd.bin_edges_addr) % word_bytes
                or int(cmd.cnt_addr) % word_bytes):
            return HistError.ADDRESS_ERROR
        return HistError.NO_ERROR
        yield  # unreachable — makes this a generator

    @synthesizable
    def respond(self, m_out: StreamIFMaster, tx_id: int, status: HistError) -> ProcessGen[None]:
        """Build the HistResp and emit it (hand-written as ``hist_respond_impl``).

        A hook: codegen emits the call, the
        hand-written C++ constructs the response and writes the AXI4-Stream."""
        resp = HistResp()
        resp.tx_id = tx_id
        resp.status = status
        yield from m_out.write(resp)

    @synthesizable
    def compute(self, data: HistDataBuf, edges: HistEdgeBuf, ndata: int, nbins: int) -> ProcessGen[HistCountBuf]:
        """The binning hook (the datapath; hand-written as ``hist_compute_impl.cpp``).

        Returns the ``nbins`` counts (the kernel will fill a ``static
        ap_uint<32> count_buf[max_nbins]`` in place — HLS can't return an array
        by value — but the SimPy model returns it, per the build's chosen
        buffer convention)."""
        counts = golden_counts(np.asarray(data)[:int(ndata)], edges, int(nbins))
        return counts
        yield  # unreachable — makes this a generator


# ---------------------------------------------------------------------------
# SimPy controller (timing-accurate testbench driver)
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class HistController(SimObj):
    """Drives one histogram transaction against the accelerator.

    Plays the **CPU**: allocates three regions in the shared memory (data, edges,
    counts) in order, writes the inputs, pulses ``ap_start`` over AXI-Lite, pushes
    the command, waits for the response, and reads the kernel-produced counts back.

    The ``ap_start`` write is what makes the control plane coherent (mirrors
    :class:`~examples.stream_inband.poly.PolyTB`): the same master that writes the
    ``m_mem`` base address into the AXI-Lite slave at ``0x10`` now also launches the
    kernel from ``0x00`` of that same slave, instead of a second, unmodelled agent
    pulsing a bare ``ap_start`` wire.  The command itself still rides in-band on
    ``s_in`` — only control moved.
    """

    mem: MemModel
    data: npt.NDArray[np.float32]
    bin_edges: npt.NDArray[np.float32]
    nbins: int
    tx_id: int = 7
    word_bw: int = 32
    addr_misalign: int = 0   # test hook: byte offset added to cmd.data_addr only

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_cmd  = StreamIFMaster(name=f'{self.name}_m_cmd',  sim=self.sim, bitwidth=self.word_bw)
        self.s_resp = StreamIFSlave( name=f'{self.name}_s_resp', sim=self.sim, bitwidth=self.word_bw)
        self.m_lite = MMIFMaster(    name=f'{self.name}_m_lite', sim=self.sim, bitwidth=32)
        self.data_addr:  int | None = None
        self.edge_addr:  int | None = None
        self.count_addr: int | None = None
        self.resp: HistResp | None = None
        self.counts: npt.NDArray[np.uint32] | None = None
        self._regmap_ref: VitisRegMap | None = None

    def run_proc(self) -> ProcessGen[None]:
        bw = self.word_bw
        ndata = len(self.data)
        nbins = int(self.nbins)
        nedges = max(nbins - 1, 0)

        # Allocate the three regions, in order (data, edges, counts).
        data_nwords  = get_nwords(Float32, word_bw=self.mem.word_size, shape=ndata)
        edge_nwords  = get_nwords(Float32, word_bw=self.mem.word_size, shape=max(nedges, 1))
        count_nwords = get_nwords(Uint32Field, word_bw=self.mem.word_size, shape=nbins)
        self.data_addr  = self.mem.alloc(data_nwords)
        self.edge_addr  = self.mem.alloc(edge_nwords)
        self.count_addr = self.mem.alloc(count_nwords)

        # Populate the input buffers (TB-side memory access).
        yield from self.mem.m_mm.write_array(
            np.asarray(self.data, dtype=np.float32), Float32, self.data_addr, word_bw=bw)
        if nedges > 0:
            yield from self.mem.m_mm.write_array(
                np.asarray(self.bin_edges, dtype=np.float32), Float32, self.edge_addr, word_bw=bw)

        # Launch the kernel (ap_start at 0x00 of the AXI-Lite slave).  on_start then
        # blocks on s_in.get until the in-band command below arrives.
        yield from self._regmap().bind_master(self.m_lite).start()

        # Issue the command and await the response.
        cmd = HistCmd(
            tx_id=self.tx_id,
            data_addr=self.data_addr + self.addr_misalign,
            bin_edges_addr=self.edge_addr,
            ndata=ndata,
            nbins=nbins,
            cnt_addr=self.count_addr,
        )
        yield from self.m_cmd.write(cmd)

        resp_words = yield from self.s_resp.get()
        self.resp = HistResp().deserialize(resp_words, word_bw=bw)

        # Read the kernel-produced counts back.
        out = yield from self.mem.m_mm.read_array(Uint32Field, nbins, self.count_addr, word_bw=bw)
        self.counts = np.asarray(out, dtype=np.uint32)

    def _regmap(self) -> VitisRegMap:
        if self._regmap_ref is None:
            raise RuntimeError(
                "HistController._regmap_ref is unset; call connect() before run_sim()."
            )
        return self._regmap_ref


def connect(sim: Simulation, ctrl: HistController, accel: HistAccel,
            mem: MemModel, clk: Clock) -> None:
    """Wire controller ↔ accelerator (two StreamIFs + AXI-Lite) and accelerator → memory."""
    in_stream  = StreamIF(sim=sim, clk=clk)
    out_stream = StreamIF(sim=sim, clk=clk)
    mem_link   = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)
    lite_link  = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)
    in_stream.bind( "master", ctrl.m_cmd)
    in_stream.bind( "slave",  accel.s_in)
    out_stream.bind("master", accel.m_out)
    out_stream.bind("slave",  ctrl.s_resp)
    mem_link.bind(  "master", accel.m_mem)
    mem_link.bind(  "slave",  mem.s_mm)
    lite_link.bind( "master", ctrl.m_lite)
    lite_link.bind( "slave",  accel.s_lite)
    ctrl._regmap_ref = accel.regmap


# ---------------------------------------------------------------------------
# Codegen-source testbench (lowered to gen/hist_tb.cpp; mirrors IncrTBHls)
# ---------------------------------------------------------------------------

@dataclass
class HistTBHls(SeqTB):
    """Sequential codegen-source testbench for the histogram kernel.

    ``main()`` lowers to ``gen/hist_tb.cpp``: read the command + input buffers
    from disk, allocate the three regions (data, edges, counts) preserving
    allocation order, populate the inputs, run the DUT with the ``mem`` pointer,
    drain the response, and write the kernel-produced counts back out for the
    functional-verify step.  The three allocations carry runtime counts that can
    be 0 (``nbins-1`` edges when ``nbins==1``; an invalid ``ndata``/``nbins`` on a
    validation-failure case); the alloc codegen clamps each region to >= 1 word
    so those cases still get a valid address (the kernel reads/writes the runtime
    count, a no-op when 0).
    """

    cpp_kernel_name: ClassVar[str | None] = "hist"

    def main(self) -> None:
        dut = HistAccel()
        mem = MemModel(name="mem", sim=None, inline=False,
                           nwords_tot=MAX_MEM_WORDS)

        cmd = HistCmd()
        cmd.read_uint32_file(self.data_dir + "/cmd.bin")

        data = HistDataBuf()
        data.read_uint32_file_array(self.data_dir + "/data_array.bin", count=cmd.ndata)
        edges = HistEdgeBuf()
        edges.read_uint32_file_array(self.data_dir + "/edges_array.bin", count=cmd.nbins - 1)
        counts = HistCountBuf()

        # Allocate the three regions in order (data, edges, counts) — addresses
        # come from alloc, not the file (so the cmd stays parametric in n).
        cmd.data_addr = mem.alloc_array(data, Float32, count=cmd.ndata)
        cmd.bin_edges_addr = mem.alloc_array(edges, Float32, count=cmd.nbins - 1)
        cmd.cnt_addr = mem.alloc_array(counts, Uint32Field, count=cmd.nbins)

        dut.s_in.push(cmd)
        dut.run(mem=mem)

        resp = HistResp()
        dut.m_out.pop(resp)

        out = mem.read_array(cmd.cnt_addr, Uint32Field, count=cmd.nbins)

        # Outputs for FunctionalVerifyStep (actual side, in the data dir).
        resp.write_uint32_file(self.data_dir + "/resp_data.bin")
        out.write_uint32_file_array(self.data_dir + "/counts_array.bin", count=cmd.nbins)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@dataclass
class HistResult:
    """Result bundle from a histogram simulation run."""

    counts: npt.NDArray[np.uint32]
    expected: npt.NDArray[np.uint32]
    status: HistError

    @property
    def passed(self) -> bool:
        return (self.status == HistError.NO_ERROR
                and np.array_equal(self.counts, self.expected))


def run_sim(
    data: npt.NDArray[np.float32],
    bin_edges: npt.NDArray[np.float32],
    nbins: int,
    *,
    clk_freq: float = 1e9,
    tx_id: int = 7,
    addr_misalign: int = 0,
) -> HistResult:
    """Run the SimPy histogram sim and return observed vs golden counts."""
    sim = Simulation()
    clk = Clock(freq=clk_freq)
    mem = MemModel(name="mem", sim=sim, inline=False, clk=clk)
    accel = HistAccel(name="hist_accel", sim=sim, clk=clk)
    ctrl = HistController(name="hist_ctrl", sim=sim, mem=mem,
                          data=data, bin_edges=bin_edges, nbins=nbins, tx_id=tx_id,
                          addr_misalign=addr_misalign)
    connect(sim, ctrl, accel, mem, clk)
    sim.run_sim()

    expected = golden_counts(data, bin_edges, nbins)
    return HistResult(
        counts=ctrl.counts if ctrl.counts is not None else np.array([], dtype=np.uint32),
        expected=expected,
        status=ctrl.resp.status if ctrl.resp is not None else HistError.INVALID_NDATA,
    )


def _gen_test_data(seed: int = 7, ndata: int = 37, nbins: int = 6):
    rng = np.random.default_rng(seed)
    data = rng.normal(loc=0.0, scale=1.25, size=ndata).astype(np.float32)
    bin_edges = np.sort(rng.uniform(-2.5, 2.5, size=max(nbins - 1, 0)).astype(np.float32))
    return data, bin_edges


def main() -> None:
    data, bin_edges = _gen_test_data()
    res = run_sim(data, bin_edges, nbins=6)
    print(f"histogram sim: ndata={len(data)}, nbins=6, "
          f"status={res.status.name}, passed={res.passed}")
    if not res.passed:
        print(f"  expected={res.expected}")
        print(f"  got     ={res.counts}")


if __name__ == "__main__":
    main()
