"""bram.py — on-chip memory shared **between** modules, realized as hand-written Verilog.

The storage category that has no expression inside a Vitis kernel.  Vitis has no model of memory
shared between processes: an array crossing two ``hls::task`` bodies becomes a synchronizing PIPO
channel — silently, with a handshake that **stalls the writer** — and one ``bram`` port used both
ways is refused outright::

    INFO:  [HLS 200-741] Implementing PIPO rx_buf_r_RAM_T2P_BRAM_1R1W using a single memory
    ERROR: [HLS 200-976] Argument 'buf_r' failed dataflow checking:
                         Cannot read as well as write over function parameter.

That is not an oversight.  DATAFLOW's promise is that the parallel result equals the sequential C
result, and a shared buffer with independent pointers has no sequential-C meaning at all — whether
``buf[rd]`` sees the old value or the new one depends on *when*, which C does not express.  So the
division is about **who owns the correctness argument**: for a channel the tool owns it and enforces
it with handshakes; for a memory like this one the designer owns it, and the tool does not interfere.

The consequence is this module.  The memory lives *beside* the kernel as pre-written Verilog
(:meth:`~waveflow.hw.hw_module.HwModule.rtl_module`), the kernel reaches it through sized ``bram``
ports, and a wrapper joins the two.  See ``plans/rtl_module.md`` and its witness in
``plans/witness/t2p_bram/`` — four hand-written files that ran, ramp-verified, before any of this
infrastructure was designed against them.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from waveflow.hw.hw_module import HwModule, HwParam
from waveflow.hw.interface import InterfaceEndpoint


@dataclass
class BramIFSlave(InterfaceEndpoint):
    """The **memory** side of a BRAM port pair — one accessor's window onto storage it does not own.

    Slave because the memory never initiates: the accessor drives the address, the enable and the
    write data, and the memory answers ``dout`` a fixed number of cycles later.  The direction that
    *is* declared here is :attr:`access` — what the accessor does through this port — because a
    true-dual-port memory's whole safety argument is that one side writes and the other reads.  A
    port used both ways is exactly what Vitis refuses inside a kernel, and it is no safer outside
    one; saying which is which makes the invariant checkable rather than remembered.

    One endpoint is one *port* of the memory, not the memory: :class:`T2pBram` carries two.
    """

    #: Bits per word.  Named ``bitwidth`` like every other endpoint, so structural machinery that
    #: reads a port's width (``boundary_signature``, the resource path) needs no special case.
    bitwidth: int = 16
    #: Words addressable through this port.  Part of the endpoint rather than only the module,
    #: because the kernel-side C++ parameter is a **sized** array and its size comes from here — an
    #: unsized pointer with ``mode=bram`` silently degrades to an ``ap_vld`` scalar port.
    depth: int = 1024
    #: What the **accessor** does through this port: ``"read"`` or ``"write"``.
    access: str = "read"
    type_name = 'bram_if_slave'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.access not in ("read", "write"):
            raise ValueError(
                f"BramIFSlave '{self.name}': access must be 'read' or 'write', got "
                f"{self.access!r}. A port used BOTH ways is the structure Vitis refuses "
                f"(HLS 200-976) and the one a true-dual-port memory's correctness argument rules "
                f"out — the direction is declared, never inferred."
            )


#: RAMB18 aspect ratios: ``(data width, depth at that width)``.  18 Kb of storage, addressable as
#: 16K x 1 through 1K x 18 (the 2 parity bits per 16 are what make the widths 9 and 18 rather than 8
#: and 16).  This is device geometry, not a fit.
_RAMB18_ASPECTS = ((1, 16384), (2, 8192), (4, 4096), (9, 2048), (18, 1024))


def ramb18_count(depth: int, dwidth: int) -> int:
    """How many 18 Kb block RAMs a *depth* x *dwidth* memory takes — **by geometry**.

    Structural, so it is derived rather than measured: a 1024x16 true-dual-port buffer is one RAMB18
    and no tool run is needed to know it.  The count is the best tiling over the aspect ratios — wide
    words split across blocks, deep ones stack — which is the same arithmetic the tools do.

    What is *not* structural is the rounding at the edges (when a tool promotes a pair of narrow
    blocks to a RAMB36, when it decides an array is small enough for LUTRAM instead).  So this number
    should eventually be **gated against a real synthesis** rather than trusted forever — the same
    two-tier shape the calibration work already uses: a cheap derived value, an authoritative measured
    one, and a regression guard between them (``plans/rtl_module.md``, "Resource accounting").
    """
    return min(ceil(dwidth / w) * ceil(depth / d) for w, d in _RAMB18_ASPECTS)


@dataclass
class T2pBram(HwModule):
    """A true-dual-port on-chip memory: one port written, one port read, **realized as Verilog**.

    The first consumer of :meth:`~waveflow.hw.hw_module.HwModule.rtl_module`, and the module the
    witness proves: two free-running ``hls::task`` bodies, one writing at a running pointer and one
    reading at an address it is told, sharing this memory through two sized ``bram`` ports.  In xsim
    the reader returned ``100, 101, 107, 355, 228`` for addresses ``0, 1, 7, 255, 128`` against a
    ramp written by the writer — **values, not plumbing**, because the likeliest failure (a read
    latency that disagrees with the pragma) shifts the data by one and passes a constant check.

    **The design invariant lives in the Verilog.**  ``bram_t2p.v`` ``$error``s when the read port
    touches the address the write port is writing that cycle — for a circular buffer, *"rd trails
    wr"*.  Nothing else would check it: if it fails, the data is whatever the BRAM's
    read-during-write mode happens to be and no tool says a word.  A hand-written memory is *more*
    verifiable than an emulated one, which is worth stating out loud.

    **No pysim behaviour yet.**  S1 declares the artifact and its ports; there is no ``BramIF`` to
    drive this module through and therefore nothing to model.  Whether the pysim side is a plain
    numpy array on this class, and which endpoint carries the access latency, is open
    (``plans/rtl_module.md``, open questions) and answered in S2 where the wiring lands — not guessed
    at here.
    """

    #: Bits per word.  16 in the witness.
    dwidth: HwParam[int] = 16
    #: Words.  1024 in the witness — exactly one RAMB18 at 16 bits wide.
    depth: HwParam[int] = 1024

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.dwidth), int(self.depth)
        #: Port A — the accessor writes through it.
        self.wr_port = BramIFSlave(sim=self.sim, name=f"{self.name}_wr", bitwidth=w, depth=d,
                                   access="write")
        #: Port B — the accessor reads through it.
        self.rd_port = BramIFSlave(sim=self.sim, name=f"{self.name}_rd", bitwidth=w, depth=d,
                                   access="read")
        self.add_endpoint(self.wr_port)
        self.add_endpoint(self.rd_port)

    @property
    def addr_bits(self) -> int:
        """The memory's address width, ``AW`` — ``log2(depth)``, refusing a non-power-of-two depth.

        The Verilog indexes ``mem[a_addr[AW-1:0]]``, so a depth that is not a power of two would
        alias silently: address 1024 in a 1000-word memory would wrap to 24 and the write would land
        on live data.  Refused rather than rounded up, because rounding up would quietly buy storage
        the caller did not ask for and still not make the wrap go away.
        """
        d = int(self.depth)
        aw = d.bit_length() - 1
        if d <= 0 or (1 << aw) != d:
            raise ValueError(
                f"T2pBram depth must be a power of two (got {d}): the Verilog addresses "
                f"mem[addr[AW-1:0]], so any other depth aliases high addresses onto low ones "
                f"silently."
            )
        return aw

    @property
    def read_latency(self) -> int:
        """Cycles from address to data — **read from the Verilog**, never declared here.

        The one number the C++ ``latency=`` pragma is also emitted from.  Python holding a second
        copy is precisely the arrangement in which the two desynchronize, so this is a property that
        reads the artifact rather than a field anybody can set.  See
        :func:`~waveflow.build.rtl_gen.rtl_read_latency`.
        """
        from waveflow.build.rtl_gen import rtl_read_latency

        lat = rtl_read_latency(self.rtl_module())
        if lat is None:                                          # pragma: no cover - gated by check
            raise ValueError(
                f"{type(self).__name__}'s Verilog publishes no READ_LATENCY, so there is no number "
                f"to emit the kernel's latency= pragma from."
            )
        return lat

    def rtl_module(self):
        """The hand-written true-dual-port memory in ``waveflow/build/rtl/bram_t2p.v``.

        Copied byte-for-byte from the witness that ran, plus the ``localparam READ_LATENCY`` line
        that makes the pragma derivable from it.  ``DW``/``AW`` ride on the *instantiation* — a
        Verilog parameter is not a code generator, and the file is never rewritten.
        """
        from waveflow.build.rtl_gen import RtlModule

        return RtlModule(
            module="bram_t2p",
            files=("bram_t2p.v",),
            ports={
                # Port A is the write side, port B the read side -- which is what the kernel's two
                # unidirectional bram interfaces expect, and the assignment the $error assertion in
                # the Verilog is written against.
                "wr_port": {"addr": "a_addr", "en": "a_en", "din": "a_din",
                            "dout": "a_dout", "we": "a_we"},
                "rd_port": {"addr": "b_addr", "en": "b_en", "din": "b_din",
                            "dout": "b_dout", "we": "b_we"},
            },
            params=(("DW", int(self.dwidth)), ("AW", self.addr_bits)),
            clock="clk",
        )

    @classmethod
    def get_rm(cls, platform):
        """This memory's footprint, **declared from geometry** rather than looked up.

        The general rule the resource taxonomy implies: *structural* blocks (memories, FIFOs) can
        declare their footprint; *logic* blocks cannot and need a run.  A memory is the clearest case
        — depth x width maps to a primitive count by construction — and the declaration is needed
        here because the alternative is nothing at all: ``csynth`` of the kernel reports **no BRAM**,
        since the memory is outside it.  A wrapper you cannot count is half the point of having one.

        ``uram`` is declared 0 rather than left unpredicted: Vivado does not infer URAM without an
        attribute, so zero is a structural fact about this Verilog, and an unpredicted counter would
        make the module read as ``UNCALIBRATED`` for a resource it genuinely does not use.
        """
        from waveflow.calib.resource_model import PriorResourceModel

        return PriorResourceModel(
            name="T2pBram:geometry",
            platform=platform,
            params_fn=lambda comp: {"depth": int(comp.depth), "dwidth": int(comp.dwidth)},
            formulas={"bram": lambda f: ramb18_count(f["depth"], f["dwidth"]),
                      "uram": lambda f: 0},
        )
