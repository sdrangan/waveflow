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

import numpy as np

from waveflow.hw.dataschema import DataSchema, IntField
from waveflow.hw.hw_module import HwModule, HwParam
from waveflow.hw.interface import Interface, InterfaceEndpoint


# ---------------------------------------------------------------------------
# The element — what one address holds
# ---------------------------------------------------------------------------
#
# A BRAM port in RTL is `addr` + `din`/`dout` + `we`/`en`: uniform width, address-indexed.  That is
# an ARRAY, definitionally, so an element type plus an element count describes exactly what the port
# can express — and every other number (the bus width, the byte-address shift, the pysim storage
# dtype) follows from it.  Declaring the width instead left three places free to disagree about what
# a word MEANS while agreeing about how wide it is.  See ``plans/typed_transfer_codec.md`` S3c.


def word_element(bitwidth: int) -> type[DataSchema]:
    """The element type of a memory that holds **raw words** — an unsigned integer *bitwidth* wide.

    Not ceremony, and not a placeholder for a "real" type: a sample buffer whose contents are packed
    converter words genuinely has an unsigned word as its element, and saying so is what makes
    :attr:`BramIFMaster.bitwidth` derivable instead of declared.  A design that knows more about its
    contents (``Float32``, a flat ``DataList``) passes that type instead and gets a memory backed by
    it.
    """
    return IntField.specialize(bitwidth=int(bitwidth), signed=False)


#: The default element: the witness's 16-bit word.  A default at all (rather than a required
#: argument) only because these are dataclasses whose other fields carry defaults; ``None`` is
#: refused, so no port ever reaches codegen without an element.
_DEFAULT_ELEMENT = word_element(16)


def check_bram_element(element_type: type[DataSchema] | None, owner: str) -> int:
    """Validate *element_type* as a BRAM port's element and return its bit width.

    **Refused here, where the type is DECLARED**, rather than in the wrapper emitter that discovers
    it.  The rule itself is not re-stated: this calls
    :func:`~waveflow.build.wrapper_gen._bram_addr_shift`, the emitter that owns it, so there is no
    second copy to drift.  What the declaration site adds is *when* — a 14-bit element (the RFdc
    dense-14 sample) is a fact about the type, knowable the moment it is named, and discovering it
    only at wrapper generation means a design that elaborates, simulates and csynths fine dies at the
    last rung with a message about an emitter.
    """
    if element_type is None:
        raise ValueError(
            f"{owner}: a BRAM port must declare element_type. Its width, its byte-address scaling "
            f"and its pysim storage all derive from the element, so there is no width to fall back "
            f"on -- pass word_element(N) for a memory of raw N-bit words.")
    from waveflow.build.hwcodegen import LoweringError
    from waveflow.build.wrapper_gen import _bram_addr_shift

    width = int(element_type().get_bitwidth())
    try:
        _bram_addr_shift(width)
    except LoweringError as exc:
        raise ValueError(
            f"{owner}: {element_type.__name__} is {width} bits, which cannot be a BRAM element "
            f"type. {exc} A dense 14-bit RFdc sample is the live case: pack it into a word "
            f"(word_element(16), or a whole 64-bit RfdcSampWord) and unpack inside the kernel."
        ) from exc
    return width


def _storage_dtype(element_type: type[DataSchema], owner: str) -> np.dtype:
    """The numpy dtype one element is **stored as** in pysim.

    The element's own dtype when it has one (``Float32`` -> ``float32``, so a float in the memory is
    a float, not a packing of one).  A composite element has none, and then the storage is the
    element's packed word — honest about what it is, and the reason a reference view over such a
    memory is refused rather than silently copied (S3b).
    """
    dtype = element_type._numpy_elem_dtype()
    if dtype is not None:
        return np.dtype(dtype)
    nbytes = int(element_type().get_bitwidth()) // 8
    if nbytes not in (1, 2, 4, 8):
        raise ValueError(
            f"{owner}: {element_type.__name__} has no numpy dtype and packs to {nbytes} bytes, "
            f"which numpy has no integer type for. pysim storage would have to truncate it "
            f"silently -- exactly what a memory model must not do.")
    return np.dtype(f"u{nbytes}")


@dataclass
class BramIFMaster(InterfaceEndpoint):
    """The **accessor** side of a BRAM port pair — a kernel task's window onto storage it does not own.

    Master because this end drives: the address, the enable, the write data and the write enable are
    all outputs of the kernel, and the memory answers ``Dout`` a fixed number of cycles later.  In
    C++ it is a **sized array parameter** (``ap_uint<W> buf_w[N]``) carrying
    ``#pragma HLS INTERFACE mode=bram``; in RTL it is fourteen ports, an A/B pair of seven signals
    (:func:`~waveflow.build.composite_gen.bram_port_signals`).

    **It carries no latency.**  How many cycles the memory takes to answer is a fact about the
    *memory*, published by its Verilog and reached through the bound :class:`BramIF` — see
    :meth:`BramIF.read_latency`.  A copy here is the second authorship site S1 exists to prevent: the
    two would be free to disagree, and a disagreement shifts every read by a cycle in silence.

    :attr:`access` says what this end *does*, and it is declared rather than inferred for the same
    reason it is on :class:`BramIFSlave`: a port used both ways is what Vitis refuses (HLS 200-976),
    and what a true-dual-port memory's correctness argument rules out.
    """

    #: What one address holds — the C++ array's **element type**.  Everything the port needs in
    #: bits is derived from it (see :attr:`bitwidth`); nothing restates it.
    element_type: type[DataSchema] = _DEFAULT_ELEMENT
    #: Elements — the C++ array's **size**.  Not decoration: ``mode=bram`` on an unsized pointer
    #: silently degrades to an ``ap_vld`` scalar port, so this number is what makes the pragma take
    #: effect.  One address holds one element, so it is also the memory's depth.
    nelem: int = 1024
    #: What this accessor does through the port: ``"read"`` or ``"write"``.
    access: str = "read"
    type_name = 'bram_if_master'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.access not in ("read", "write"):
            raise ValueError(
                f"BramIFMaster '{self.name}': access must be 'read' or 'write', got "
                f"{self.access!r}. A port used BOTH ways is the structure Vitis refuses "
                f"(HLS 200-976); the direction is declared, never inferred."
            )
        check_bram_element(self.element_type, f"BramIFMaster '{self.name}'")
        if int(self.nelem) <= 0:
            raise ValueError(
                f"BramIFMaster '{self.name}': nelem must be positive, got {self.nelem!r}.")

    @property
    def bitwidth(self) -> int:
        """Bits on the wire — **derived** from :attr:`element_type`, never declared.

        RTL is always bits, so the number does not disappear: ``bram_t2p.v`` stays parameterized by
        ``DW`` and a ``float`` is 32 bits there.  What changes is authorship — the width is now a
        consequence of what the memory holds, so a port and its memory cannot agree on 32 while
        disagreeing about whether those 32 bits are a float or a packed pair.  The same shape
        :class:`~waveflow.hw.interface.SobIFMaster` already uses.
        """
        return int(self.element_type().get_bitwidth())

    @property
    def read_latency(self) -> int:
        """Cycles from address to data, **from the memory this port is wired to**.

        Raises when unbound, and that refusal is the contract: a ``bram`` port whose pragma latency
        cannot be traced to a memory's published number has no business being emitted, because the
        number that would be invented here is exactly the one that shifts the ramp.
        """
        iface = self.interface
        if iface is None:
            raise ValueError(
                f"BramIFMaster '{self.name}' is not bound to a BramIF, so there is no memory to "
                f"take a read latency from. A bram port's latency=N pragma is emitted from the "
                f"MEMORY's published READ_LATENCY (plans/rtl_module.md S1); wire the port with a "
                f"BramIF before generating the top."
            )
        return iface.read_latency

    # -- pysim access ------------------------------------------------------------------------
    #
    # Untimed, and deliberately so.  A BRAM access is a *deterministic* one-cycle answer with no
    # arbitration and no queue, so there is nothing here for a discrete-event model to represent that
    # the cycle-accurate backend does not represent better.  Modelling it with a `yield` would put a
    # SimPy timestep between the address and the data and make pysim slower than the hardware for no
    # gain in fidelity.  (Contrast `MemoryMod`, where the bus, the arbitration and the burst are the
    # whole point of having a model.)
    #
    # These are plain methods rather than generators for the same reason: a caller writes
    # `self.buf_w.mem_write(i, x)`, not `yield from`, and the absence of the yield is the statement
    # that no simulated time passes.

    def mem_write(self, addr: int, value) -> None:
        """Write one **element** through this port (pysim).  Refused on a read port.

        *value* is not coerced to ``int``: the memory is typed, so a ``Float32`` memory takes a
        float and stores a float.  Over a word element type this is the same call it always was.
        """
        if self.access != "write":
            raise ValueError(
                f"BramIFMaster '{self.name}' declares access='read'; writing through it is the "
                f"read-during-write hazard the memory's $error exists to catch.")
        self._memory().store(int(addr), value)

    def mem_read(self, addr: int):
        """Read one **element** through this port (pysim).  Refused on a write port."""
        if self.access != "read":
            raise ValueError(
                f"BramIFMaster '{self.name}' declares access='write'; reading through it is not the "
                f"direction this port was wired for.")
        return self._memory().load(int(addr))

    def _memory(self):
        iface = self.interface
        if iface is None:
            raise ValueError(
                f"BramIFMaster '{self.name}' is not bound to a BramIF, so there is no memory to "
                f"access. Wire it with add_rtl_if before running the sim.")
        return iface.endpoints["slave"].comp


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

    #: What one address holds.  The memory's element, not the port's opinion of it —
    #: :meth:`BramIF.bind` refuses a bind where the two sides name different types.
    element_type: type[DataSchema] = _DEFAULT_ELEMENT
    #: Elements addressable through this port.  Part of the endpoint rather than only the module,
    #: because the kernel-side C++ parameter is a **sized** array and its size comes from here — an
    #: unsized pointer with ``mode=bram`` silently degrades to an ``ap_vld`` scalar port.
    nelem: int = 1024
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
        check_bram_element(self.element_type, f"BramIFSlave '{self.name}'")
        if int(self.nelem) <= 0:
            raise ValueError(
                f"BramIFSlave '{self.name}': nelem must be positive, got {self.nelem!r}.")

    @property
    def bitwidth(self) -> int:
        """Bits per element — **derived** from :attr:`element_type`.  Named ``bitwidth`` like every
        other endpoint, so structural machinery that reads a port's width (``boundary_signature``,
        the resource path) still needs no special case."""
        return int(self.element_type().get_bitwidth())


@dataclass
class BramIF(Interface):
    """The wire between an accessor and a memory port — **a wrapper wire, not a channel.**

    That distinction is the whole of S2.  A :class:`~waveflow.hw.interface.StreamIF` between two
    tasks is an *internal channel*: it lowers to an ``hls::stream`` inside the generated top, and
    both its endpoints vanish from the top's boundary.  A ``BramIF`` is not that.  One end is inside
    the kernel and the other is outside it, so the accessor's end **stays a boundary port** and the
    join happens one level up, in the wrapper.

    Which is why a ``BramIF`` is registered with :meth:`~waveflow.hw.hw_module.HwModule.add_rtl_if`
    and never with ``add_if``: the walks that derive channels and boundary ports read the ``add_if``
    registry, and a ``BramIF`` in it would make the kernel's memory ports disappear into a FIFO that
    does not exist.

    Binding is where the two halves are checked against each other.  The accessor's array size and
    the memory's geometry **must** agree — a kernel that thinks it has 1024 words of a 4096-word
    memory is not an error any tool reports, it is just wrong at address 1024 — so a mismatch is
    refused here rather than discovered in a waveform.
    """

    type_name = 'bram_if'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        self.endpoint_names = ('master', 'slave')
        super().__post_init__()

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if ep_name == "master" and not isinstance(endpoint, BramIFMaster):
            raise TypeError("master side of BramIF must bind to BramIFMaster (the accessor)")
        if ep_name == "slave" and not isinstance(endpoint, BramIFSlave):
            raise TypeError("slave side of BramIF must bind to BramIFSlave (the memory port)")
        super().bind(ep_name, endpoint)
        m, s = self.endpoints.get("master"), self.endpoints.get("slave")
        if m is None or s is None:
            return
        if m.element_type is not s.element_type:
            raise ValueError(
                f"BramIF '{self.name}': the accessor's element is "
                f"{m.element_type.__name__} but the memory's is {s.element_type.__name__}. Two "
                f"types of the same width are the same aliasing bug as two different sizes, and a "
                f"quieter one: nothing downstream would disagree, the addresses would line up, and "
                f"every read would return a correctly-shaped wrong number."
            )
        if int(m.nelem) != int(s.nelem):
            raise ValueError(
                f"BramIF '{self.name}': the accessor's array is {m.nelem}x{m.bitwidth} but the "
                f"memory port is {s.nelem}x{s.bitwidth}. The C++ array size IS the port's address "
                f"range, so a disagreement is a silent aliasing bug at the first address past the "
                f"smaller one."
            )
        if m.access != s.access:
            raise ValueError(
                f"BramIF '{self.name}': the accessor declares access={m.access!r} but the memory "
                f"port declares {s.access!r}. One side writing while the other believes it reads is "
                f"the read-during-write collision the memory's $error exists to catch."
            )

    @property
    def read_latency(self) -> int:
        """The memory's published read latency — the ONE number the kernel's pragma is emitted from.

        Reached through the memory *module* that owns the slave endpoint, which reads it from the
        Verilog (``localparam READ_LATENCY``).  So the chain from artifact to pragma has no place a
        second value could be introduced.
        """
        slave = self.endpoints.get("slave")
        if slave is None or slave.comp is None:
            raise ValueError(
                f"BramIF '{self.name}' has no memory bound on its slave side, so no read latency "
                f"can be resolved.")
        return int(slave.comp.read_latency)


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

    **The pysim side is a numpy array, and untimed** — the plan's open question, answered the way it
    guessed.  A BRAM access is a deterministic one-cycle answer with no arbitration and no queue, so
    a discrete-event model of it would add a timestep and no fidelity; the latency that matters is
    the one the RTL enforces, and it is published by the Verilog
    (:attr:`read_latency`).  Which endpoint "carries" the access latency turned out to be the wrong
    question: neither does, because the number belongs to the memory and both ports read it from
    there.
    """

    #: What one address holds.  A **plain field, not an** ``HwParam``: a type is not a build-time
    #: integer knob, and the param machinery bakes values into artifacts by name.  The width the
    #: Verilog is parameterized by is :attr:`dwidth`, derived from this.
    element_type: type[DataSchema] = _DEFAULT_ELEMENT
    #: Elements.  1024 in the witness — exactly one RAMB18 at 16 bits wide.  One address holds one
    #: element, so this is the memory's depth; there is one name for it, not two.
    nelem: HwParam[int] = 1024

    def __post_init__(self) -> None:
        super().__post_init__()
        owner = f"{type(self).__name__} '{self.name}'"
        check_bram_element(self.element_type, owner)
        d = int(self.nelem)
        #: Port A — the accessor writes through it.
        self.wr_port = BramIFSlave(sim=self.sim, name=f"{self.name}_wr",
                                   element_type=self.element_type, nelem=d, access="write")
        #: Port B — the accessor reads through it.
        self.rd_port = BramIFSlave(sim=self.sim, name=f"{self.name}_rd",
                                   element_type=self.element_type, nelem=d, access="read")
        self.add_endpoint(self.wr_port)
        self.add_endpoint(self.rd_port)
        #: The storage, for pysim — ``nelem`` **elements**, in the element's own dtype rather than a
        #: uniform word.  A ``Float32`` memory holds float32s here, not a packing of them, which is
        #: what makes a stored value readable as itself and (later) referenceable in place; over a
        #: word element it is the array it always was.  Zeroed, like the RTL's uninitialized array is
        #: *not* — reading a word that was never written gives 0 here and X (or stale data) there,
        #: which is why the design, not the memory, is what has to guarantee "rd trails wr".
        self.storage = np.zeros(d, dtype=_storage_dtype(self.element_type, owner))

    def store(self, addr: int, value) -> None:
        """pysim write.  Refuses an out-of-range address rather than wrapping it, because the RTL
        wraps silently (``mem[addr[AW-1:0]]``) and a silent wrap is the bug this catches early."""
        self._check(addr)
        self.storage[addr] = value

    def load(self, addr: int):
        """pysim read — one element, in the element's own Python type."""
        self._check(addr)
        return self.storage[addr].item()

    def _check(self, addr: int) -> None:
        if not 0 <= int(addr) < int(self.nelem):
            raise IndexError(
                f"{type(self).__name__} '{self.name}': address {addr} is outside 0..{int(self.nelem)-1}. "
                f"The RTL would index mem[addr[AW-1:0]] and alias it onto a live word without a word "
                f"of warning; pysim refuses instead.")

    @property
    def dwidth(self) -> int:
        """``DW`` — bits per element, **derived** from :attr:`element_type`.

        Keeps the Verilog's own name for the number the Verilog is parameterized by, so
        :meth:`rtl_module` and :meth:`get_rm` read the same word the RTL does.
        """
        return int(self.element_type().get_bitwidth())

    @property
    def addr_bits(self) -> int:
        """The memory's address width, ``AW`` — ``log2(depth)``, refusing a non-power-of-two depth.

        The Verilog indexes ``mem[a_addr[AW-1:0]]``, so a depth that is not a power of two would
        alias silently: address 1024 in a 1000-word memory would wrap to 24 and the write would land
        on live data.  Refused rather than rounded up, because rounding up would quietly buy storage
        the caller did not ask for and still not make the wrap go away.
        """
        d = int(self.nelem)
        aw = d.bit_length() - 1
        if d <= 0 or (1 << aw) != d:
            raise ValueError(
                f"T2pBram nelem must be a power of two (got {d}): the Verilog addresses "
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
            # The feature names stay ``depth``/``dwidth`` -- geometry vocabulary, and the key
            # every stored measurement is already filed under.  They are sourced from the
            # declaration now, not declared twice.
            params_fn=lambda comp: {"depth": int(comp.nelem), "dwidth": int(comp.dwidth)},
            formulas={"bram": lambda f: ramb18_count(f["depth"], f["dwidth"]),
                      "uram": lambda f: 0},
        )
