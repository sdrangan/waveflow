"""bram.py — on-chip memory shared **between** modules, realized as hand-written Verilog.

The storage category that has no expression inside a Vitis kernel.  Vitis has no model of memory
shared **between processes**: an array crossing two ``hls::task`` bodies becomes a synchronizing PIPO
channel — silently, with a handshake that **stalls the writer** — and if one process writes what
another reads, the dataflow checker refuses the whole thing::

    INFO:  [HLS 200-741] Implementing PIPO rx_buf_r_RAM_T2P_BRAM_1R1W using a single memory
    ERROR: [HLS 200-976] Argument 'buf_r' failed dataflow checking:
                         Cannot read as well as write over function parameter.

**Read that error for what it says.**  It is the *dataflow* checker objecting to an argument crossing
two task bodies — one process writing what another reads.  It is **not** a prohibition on a
bidirectional ``mode=bram`` port, and this module's docstrings used to cite it as though it were.
One task reading and writing one ``bram`` port is accepted, and measured to be
(``plans/typed_transfer_codec.md`` S5b); see :func:`bram_storage_type` for what that costs and why it
is safe.

The refusal is not an oversight either.  DATAFLOW's promise is that the parallel result equals the
sequential C result, and a shared buffer with independent pointers has no sequential-C meaning at all
— whether ``buf[rd]`` sees the old value or the new one depends on *when*, which C does not express.
So the division is about **who owns the correctness argument**: for a channel the tool owns it and
enforces it with handshakes; for a memory like this one the designer owns it, and the tool does not
interfere.

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

from waveflow.hw.clock import Clock
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


#: What an accessor may do through a BRAM port.  ``"readwrite"`` is the bidirectional one, and it
#: costs a different ``storage_type`` — see :func:`bram_storage_type`.
BRAM_ACCESS = ("read", "write", "readwrite")


def check_bram_access(access: str, owner: str) -> str:
    """Validate *access* and return it.  One vocabulary, checked in one place."""
    if access not in BRAM_ACCESS:
        raise ValueError(
            f"{owner}: access must be one of {BRAM_ACCESS}, got {access!r}. The direction is "
            f"declared, never inferred — a port whose direction is guessed from how a body happens "
            f"to use it is a port whose storage_type can be wrong (see bram_storage_type).")
    return access


def bram_storage_type(access: str) -> str:
    """The ``storage_type=`` a ``mode=bram`` port's pragma must carry, **derived from** *access*.

    > **THE WRAPPER WIRES ONE PHYSICAL MEMORY PORT PER DECLARED BRAM PORT, SO THE PRAGMA MUST FORBID
    > VITIS FROM USING TWO.**

    That invariant used to hold by accident of direction: a unidirectional port needs one access per
    cycle, so Vitis only ever used the ``_A`` half and the ``_B`` half came out tied to constants.
    A read-write port breaks the accident, and it breaks it **silently** — measured
    (``plans/typed_transfer_codec.md`` S5b, Vitis HLS 2025.1):

    ==================  ==========  =========  ==================================  ===============
    ``storage_type``    compute II  write II   the ``_B`` half of the pair         wrapper-safe
    ==================  ==========  =========  ==================================  ===============
    ``ram_1wnr``        1           1          **DRIVEN** — live ``Addr_B``/``EN_B``   **NO**
    ``ram_1p``          2           1          not declared at all                 yes
    ==================  ==========  =========  ==================================  ===============

    Under ``ram_1wnr`` Vitis reaches II=1 on an in-place loop by **reading on port B while writing on
    port A** — and the wrapper wires only the A halves, so those reads reach a dangling port.  X or
    stale data, a clean ``csynth``, and nothing visible until RTL.  ``ram_1p`` does not declare the
    ``_B`` half at all, which is why it is *structurally* safe rather than safe-by-convention: no
    wrapper can mis-wire a port that is not there.

    The II=2 is the honest price, and it is a consequence rather than a property of in-place work:
    one physical port, two accesses per element.  (The alternative — wiring both halves of one
    declared port to both ports of the memory, II=1 in place — consumes the whole true-dual-port
    memory and leaves nothing for a concurrent reader.  It neighbours the array-partitioning
    question and is deferred to it.)
    """
    return "ram_1p" if check_bram_access(access, "bram_storage_type") == "readwrite" else "ram_1wnr"


def bram_emits_b_half(storage_type: str) -> bool:
    """Whether Vitis declares the **second** (``_B``) half of a ``bram`` port pair.

    A fact about ``storage_type``, so it lives beside :func:`bram_storage_type` rather than being
    re-derived from ``access`` somewhere else.  ``ram_1wnr`` emits all fourteen signals whether or
    not the kernel uses the second port — the B half comes out tied to constants for a
    unidirectional body.  ``ram_1p`` does not declare it at all, which is exactly what makes it safe
    against a wrapper that has one physical port to give.

    **The wrapper has to ask.**  It connects the kernel's memory pins by name, and a name that is
    not there is an elaboration error rather than a silent one — but only after csynth, so the
    question belongs here where the answer is already known.
    """
    return str(storage_type) != "ram_1p"


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


def native_dtype(element_type: type[DataSchema]) -> np.dtype | None:
    """The element's **own** numpy dtype, or ``None`` when it has none — S3b's line, drawn once.

    ``None`` is the whole distinction Case 3 turns on.  An element with a native dtype is stored as
    itself, so a slice of the memory *is* the elements and writes through it alias.  An element
    without one is stored as its packed word, so anything element-typed has to deserialize — a fresh
    object, and writes to it reach nothing.  Both :func:`_storage_dtype` and
    :func:`check_array_ref_element` read this same call, so "what pysim stores" and "what may be
    referenced" cannot drift apart.

    ``getattr`` rather than a plain call, because ``_numpy_elem_dtype`` is declared on ``DataField``
    and not on ``DataSchema``: a composite element type does not have the method at all, and
    "absent" means exactly what "returned None" means.  Reaching for it directly raised
    ``AttributeError`` from :func:`_storage_dtype` — so a composite-element memory could not even be
    constructed, which is not the refusal anybody intended.
    """
    hook = getattr(element_type, "_numpy_elem_dtype", None)
    return hook() if hook is not None else None


def check_array_ref_element(element_type: type[DataSchema], owner: str) -> np.dtype:
    """Refuse an element that cannot be **referenced**, and return the dtype that can.

    Case 3's one hard rule (S3b), and the failure it prevents is in the tree already:
    ``_DirectBackedMMIFMaster.as_words()`` returns a genuine numpy view, but its ``as_array()`` goes
    through ``arrayutils.read_array``, which does ``array_obj = array_cls(); deserialize(...)`` —
    **a fresh object, unconditionally**.  So an ``as_*`` method silently degrades from a view to a
    copy the moment typed elements are asked for, and every write to what it returns reaches
    nothing.  A reference API that is a view for some element types and a copy for others is worse
    than no reference API, so this refuses instead.

    The verdict depends only on the **declared** element type — nothing about a call, a binding or a
    run — which is what makes it answerable before anything runs; see
    :attr:`BramIFMaster.supports_array_ref`.  Case 1's copying ``read_array`` / ``write_array``
    stay available for every element type, and are the answer for a composite one.
    """
    dtype = native_dtype(element_type)
    if dtype is None:
        raise ValueError(
            f"{owner}: {element_type.__name__} has no native numpy dtype, so it is stored as its "
            f"PACKED WORD and a reference to it cannot be a reference to the elements. Referencing "
            f"would have to deserialize into a fresh object, and every write to that object would "
            f"reach nothing — silently. Use the copying read_array / write_array for a composite "
            f"element, or declare an element type that has a dtype.")
    return np.dtype(dtype)


def _storage_dtype(element_type: type[DataSchema], owner: str) -> np.dtype:
    """The numpy dtype one element is **stored as** in pysim.

    The element's own dtype when it has one (``Float32`` -> ``float32``, so a float in the memory is
    a float, not a packing of one).  A composite element has none, and then the storage is the
    element's packed word — honest about what it is, and the reason a reference view over such a
    memory is refused rather than silently copied (S3b, :func:`check_array_ref_element`).
    """
    dtype = native_dtype(element_type)
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

    :attr:`access` says what this end *does* — ``"read"``, ``"write"`` or ``"readwrite"`` — and it is
    declared rather than inferred because a **different pragma** follows from it: see
    :attr:`storage_type`.  A direction guessed from how a body happens to index the array is a
    ``storage_type`` that can be wrong, and wrong in the way that survives ``csynth`` and appears
    only at RTL.
    """

    #: What one address holds — the C++ array's **element type**.  Everything the port needs in
    #: bits is derived from it (see :attr:`bitwidth`); nothing restates it.
    element_type: type[DataSchema] = _DEFAULT_ELEMENT
    #: Elements — the C++ array's **size**.  Not decoration: ``mode=bram`` on an unsized pointer
    #: silently degrades to an ``ap_vld`` scalar port, so this number is what makes the pragma take
    #: effect.  One address holds one element, so it is also the memory's depth.
    nelem: int = 1024
    #: What this accessor does through the port: ``"read"``, ``"write"`` or ``"readwrite"``.
    #: Decides :attr:`storage_type`.
    access: str = "read"
    type_name = 'bram_if_master'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        check_bram_access(self.access, f"BramIFMaster '{self.name}'")
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
    def storage_type(self) -> str:
        """The pragma's ``storage_type=``, **derived** from :attr:`access` — never a constant.

        See :func:`bram_storage_type` for the invariant and the measurement.  It lives here rather
        than in the emitter because it is a property of the port, and because a constant in the
        emitter is exactly what was silently wrong the moment ``access`` gained a third value.
        """
        return bram_storage_type(self.access)

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
    # `self.buf_w.write(i, x)`, not `yield from`, and the absence of the yield is the statement
    # that no simulated time passes.
    #
    # **An addressed read is `read`.**  These were `mem_read` / `mem_write`; the `mem_` prefix
    # distinguished nothing, because a `BramIFMaster` only ever addresses memory — there is no
    # other kind of read on this port to tell it apart from.  `MMIFMaster.read` / `.write` are
    # the same operation on the other addressed endpoint and already spell it this way.
    #
    # A stream keeps `get`, and that is not an oversight: a stream read is a destructive
    # DEQUEUE and an addressed read is not.  Three verbs, three meanings — a `get` dequeues,
    # a `read` looks at an address, an `acquire` takes a lease (`SobIFSlave`).  See
    # `plans/interface_docs_and_naming.md` Part 3.

    def write(self, addr: int, value) -> None:
        """Write one **element** through this port (pysim).  Refused on a read port.

        *value* is not coerced to ``int``: the memory is typed, so a ``Float32`` memory takes a
        float and stores a float.  Over a word element type this is the same call it always was.
        """
        self._check_access("write", "write")
        self._memory().store(int(addr), value)

    def read(self, addr: int):
        """Read one **element** through this port (pysim).  Refused on a write port."""
        self._check_access("read", "read")
        return self._memory().load(int(addr))

    # -- Case 3: in place, and the reason is TIMING rather than copies -------------------------
    #
    # A kernel computing against a BRAM transfers nothing: in C++ it is `foo(&buf[addr], n)`, and the
    # function reads and writes the memory through its port.  Modelling that as read_array + compute
    # + write_array would invent TWO transfers that do not exist and charge the design for them --
    # a wrong number, not a cosmetic loss.  So `array_ref` elapses no simulated time, and (like
    # read / write above) the absence of the `yield` is the statement that says so.
    #
    # THE CALLER OWNS THE TIMING, because the cost is the compute loop's II x n rather than a
    # transfer.  What this endpoint owes the caller is the number to compute from --
    # `accesses_per_cycle`, and `ii_for()` over it -- so a body multiplies a declared rate instead of
    # a guessed one.

    @property
    def supports_array_ref(self) -> bool:
        """Whether :meth:`array_ref` is available on this port — a fact about the **declaration**.

        It depends only on :attr:`element_type`, so a design can be checked for it before anything
        runs, which is what "refused at declaration time, not at the call site" means here.  The
        port stays perfectly usable without it: Case 1's copying ops serve every element type.
        """
        return native_dtype(self.element_type) is not None

    @property
    def accesses_per_cycle(self) -> int:
        """Element accesses this port can serve per cycle — **1**, and flat on purpose.

        A true-dual-port memory is one access per cycle *per port*, and a declared ``bram`` port is
        one physical port (that is the invariant :func:`bram_storage_type` enforces).  So the number
        is 1 and the interesting arithmetic is the body's: see :meth:`ii_for`.

        It is flat until the array-partitioning question is **measured**.  ``ARRAY_PARTITION`` on a
        ``mode=bram`` interface array does work in Vitis, but it emits N separate port pairs — N
        physical memories in the wrapper — which is a topology change rather than a rate
        coefficient.  Until that is gated, carrying a factor nobody has checked would be the kind of
        invented number this endpoint exists to avoid.
        """
        return 1

    def ii_for(self, accesses_per_element: int) -> int:
        """The initiation interval a loop doing *accesses_per_element* accesses through **this port**
        achieves — arithmetic over :attr:`accesses_per_cycle`, not a second source for it.

        Port contention is the whole content: ``y[i] = f(x[i])`` with both references through the
        *same* port is 2 accesses per element and therefore II=2; through the two ports of a
        true-dual-port memory it is 1 each and II=1.  Which is why a reference is per-port — it comes
        off the master endpoint — and why a body's timing depends on which ports its references came
        from.

        **Measured, not asserted** (S5b): a read-modify-write loop over one ``ram_1p`` port csynths
        at II=2, and the write-only loop beside it at II=1.
        """
        n = int(accesses_per_element)
        if n < 1:
            raise ValueError(
                f"BramIFMaster '{self.name}'.ii_for: a loop touching this port does at least one "
                f"access per element, got {accesses_per_element!r}.")
        return -(-n // self.accesses_per_cycle)          # ceil, at one access/cycle/port

    def array_ref(self, addr: int, count: int) -> np.ndarray:
        """A **live view** of elements ``[addr, addr+count)`` — no transfer, and no simulated time.

        Element-typed (the view's dtype is :attr:`element_type`'s own — nothing is passed, because
        the storage already has a type) and extent-bounded, so it is range-checked here the way
        :meth:`read` / :meth:`write` already are.

        **Directional, and enforced rather than advised.**  :attr:`access` already says what this
        port does, so a ``"read"`` port hands back a view with ``flags.writeable = False`` and a
        stray write *raises* instead of silently reaching nothing.  ``"write"`` and ``"readwrite"``
        hand back a writable one.

        The one asymmetry, stated rather than hidden: numpy has no write-only array, so a
        ``"write"`` port's view cannot refuse *reads* the way :meth:`read` does.  The direction
        that can be enforced is.

        Refused for an element type with no native numpy dtype — see
        :func:`check_array_ref_element` for why a copy-in-disguise is worse than a refusal.
        """
        check_array_ref_element(self.element_type, f"BramIFMaster '{self.name}'.array_ref")
        view = self._memory().block_ref(int(addr), int(count))
        if self.access == "read":
            view.flags.writeable = False
        return view

    # -- Case 2: pipelined vector access ------------------------------------------------------
    #
    # The LT model for a BRAM port is the simplest in the repo, and every term is PUBLISHED rather
    # than invented:
    #
    #   throughput  1 element per cycle per port -- a true-dual-port memory is one access per cycle
    #               per port, and `access` already says which port this is.
    #   fill        READ_LATENCY cycles before the first read answer, reached through the bound
    #               BramIF from the memory's Verilog localparam.  A pipeline fill: paid ONCE per
    #               transfer, not per element.  A write has none (address and data go together).
    #   anchoring   `t_start` is `QueuedTransferIF._push_to_endpoint`'s convention, unchanged: the
    #               burst is treated as having begun at `t_start` and the wait shortens if that is
    #               in the past, so two anchored phases OVERLAP and cost max(a, b), not a + b.
    #
    # Which makes both ops the stream's own model with the memory's number plugged in: `read` is
    # `latency_init + nwords` cycles with `latency_init = READ_LATENCY`, and its `tstart` is
    # back-calculated exactly as `StreamIFSlave.get_pipelined` back-calculates its own.  There is no
    # second anchoring convention here, deliberately.

    def read_pipelined(self, element_type: type[DataSchema], count: int, addr: int):
        """Read *count* elements from *addr*; return ``(data, tstart)``.  Refused on a write port.

        *data* is a :class:`~waveflow.hw.dataschema.DataArray` of *element_type*, so it feeds
        :meth:`~waveflow.hw.interface.StreamIFMaster.write_pipelined` directly.  *tstart* is when the
        **first** element arrived, back-calculated from completion — the anchor a consumer passes on
        so its own phase overlaps this one instead of queuing behind it.

        The call elapses ``READ_LATENCY + count`` cycles: the fill, then one element per cycle.
        """
        from waveflow.hw.arrayutils import array

        self._check_access("read", "read_pipelined")
        self._check_element(element_type, "read_pipelined")
        n = int(count)
        period = 1.0 / float(self._clk().freq)
        yield self.timeout((int(self.read_latency) + n) * period)
        values = self._memory().load_block(int(addr), n)
        tstart = self.env.now - max(0, n - 1) * period
        return array(element_type, values), tstart

    def write_pipelined(self, data, addr: int, t_start: float | None = None):
        """Write *data* to *addr*, the burst **anchored** at *t_start*.  Refused on a read port.

        *data* is a ``DataArray`` of this port's element type, or a plain array of element values.
        The call elapses ``count`` cycles (II=1, no fill), and with *t_start* in the past it elapses
        less — that shortening is the overlap, and it is
        :meth:`~waveflow.hw.interface.StreamIFMaster.write_pipelined`'s contract verbatim.
        ``t_start=None`` is the ordinary blocking write.
        """
        from waveflow.hw.dataschema import DataArray

        self._check_access("write", "write_pipelined")
        if isinstance(data, DataArray):
            self._check_element(type(data).element_type, "write_pipelined")
            values = data.val
        else:
            values = data
        values = np.asarray(values).reshape(-1)
        dly = values.shape[0] / float(self._clk().freq)
        if t_start is not None:
            dly = max(0.0, dly + (t_start - self.env.now))
        if dly > 0:
            yield self.timeout(dly)
        self._memory().store_block(int(addr), values)

    def _check_access(self, want: str, op: str) -> None:
        """Refuse an operation this port did not declare it does.  ``"readwrite"`` permits both.

        The refusal is not bookkeeping: what a port does decides its ``storage_type``
        (:func:`bram_storage_type`), so a body that writes through a port declared ``"read"`` is a
        body whose pragma lets Vitis use a second physical port the wrapper never wired.
        """
        if self.access != want and self.access != "readwrite":
            raise ValueError(
                f"BramIFMaster '{self.name}'.{op}: the port declares access={self.access!r}, so a "
                f"{want} through it is not what it was wired for. Declare access='readwrite' if "
                f"it genuinely does both — that is a real option now, and it changes the port's "
                f"storage_type to ram_1p so the wrapper's single physical port stays sufficient.")

    def _check_element(self, element_type: type[DataSchema], op: str) -> None:
        """Refuse a transfer typed differently from the port it goes through.

        The call-site half of :meth:`BramIF.bind`'s element check, and the same failure it prevents:
        two types of one width line up at every address and hand back a correctly-shaped wrong
        number, with nothing downstream in a position to notice."""
        if element_type is not self.element_type:
            raise ValueError(
                f"BramIFMaster '{self.name}'.{op}: the transfer is typed "
                f"{element_type.__name__} but the port's element is {self.element_type.__name__}. "
                f"Same width or not, those are different numbers.")

    def _clk(self):
        iface = self.interface
        clk = getattr(iface, "clk", None) if iface is not None else None
        if clk is None:
            raise ValueError(
                f"BramIFMaster '{self.name}': a pipelined transfer is measured in CYCLES, and this "
                f"port's BramIF carries no clock. Pass clk= when constructing the BramIF. (The "
                f"untimed read / write need none, which is why it is optional.)")
        return clk

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
    *is* declared here is :attr:`access` — what the accessor does through this port — because the
    memory's safety argument is written in those terms: ``bram_t2p.v``'s ``$error`` fires when one
    port writes the address the other is touching, and it can only be *written* if each port says
    which it is.  Note that the memory itself is symmetric — both its ports carry ``din``, ``we``
    **and** ``dout``, which is what *true* dual-port means — so this is a statement about how a
    design uses the memory, never a limit the hardware imposes.

    One endpoint is one *port* of the memory, not the memory: :class:`T2pBram` carries two.
    """

    #: What one address holds.  The memory's element, not the port's opinion of it —
    #: :meth:`BramIF.bind` refuses a bind where the two sides name different types.
    element_type: type[DataSchema] = _DEFAULT_ELEMENT
    #: Elements addressable through this port.  Part of the endpoint rather than only the module,
    #: because the kernel-side C++ parameter is a **sized** array and its size comes from here — an
    #: unsized pointer with ``mode=bram`` silently degrades to an ``ap_vld`` scalar port.
    nelem: int = 1024
    #: What the **accessor** does through this port: ``"read"``, ``"write"`` or ``"readwrite"``.
    #: A *restatement* of the accessor's own declaration, not a permission grant — which is why
    #: :meth:`BramIF.bind` requires the two to be identical rather than merely compatible.
    access: str = "read"
    type_name = 'bram_if_slave'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        check_bram_access(self.access, f"BramIFSlave '{self.name}'")
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

    #: The clock the port pair is timed in — the accessor's, which the wrapper ties to the memory's.
    #: **Optional**, because the scalar ``read`` / ``write`` are untimed and need none; a
    #: pipelined transfer is measured in cycles and refuses loudly without it, the same shape as
    #: :attr:`read_latency` refusing when unbound.
    clk: "Clock | None" = None

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
                f"port declares {s.access!r}. These are two statements of ONE fact — what happens "
                f"through this port — so they are required to be identical rather than merely "
                f"compatible: a memory port that says 'read' while a 'readwrite' accessor writes "
                f"through it is the read-during-write collision the memory's $error exists to "
                f"catch, and a memory port that claims more than the accessor does is a claim "
                f"nothing checks. Declare the same thing on both ends."
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
    #: What the accessor does through each port, ``(port A, port B)``.  The default is the 1R1W usage
    #: the witness has.  ``bram_t2p.v`` is symmetric — both ports carry ``din``, ``we`` *and*
    #: ``dout`` — so this describes how a design **uses** the memory, never a limit the hardware
    #: imposes; port A may also be ``"readwrite"``.
    #:
    #: **Port B may not write, and that is refused rather than left to go wrong.**  The ``$error``
    #: in ``bram_t2p.v`` is written one-sided — *A writes while B touches the same address* — so a
    #: writing B port would be invisible to the design's only real check.  Lifting the restriction
    #: means making that assertion symmetric, which edits ``bram_t2p.v`` and therefore every
    #: example's copied ``xsi/bram_t2p.v``; see ``plans/typed_transfer_codec.md`` S5.
    port_access: tuple[str, str] = ("write", "read")

    def __post_init__(self) -> None:
        super().__post_init__()
        owner = f"{type(self).__name__} '{self.name}'"
        check_bram_element(self.element_type, owner)
        a_access, b_access = (check_bram_access(x, f"{owner} port {side}")
                              for x, side in zip(self.port_access, "AB"))
        if b_access != "read":
            raise ValueError(
                f"{owner}: port_access={tuple(self.port_access)!r} lets port B write. bram_t2p.v's "
                f"$error asserts 'A writes while B touches the same address' — one-sided — so a "
                f"write on B is the read-during-write collision the memory would NOT catch. Put the "
                f"writing port on A, or make that assertion symmetric first (one line, but it "
                f"changes every example's copied xsi/bram_t2p.v).")
        d = int(self.nelem)
        #: Port A — the accessor writes through it, and may read it too.
        self.wr_port = BramIFSlave(sim=self.sim, name=f"{self.name}_wr",
                                   element_type=self.element_type, nelem=d, access=a_access)
        #: Port B — the accessor reads through it.
        self.rd_port = BramIFSlave(sim=self.sim, name=f"{self.name}_rd",
                                   element_type=self.element_type, nelem=d, access=b_access)
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

    # The vector twins.  Not a convenience over `store`/`load` in a loop: a per-element loop is what
    # the Case 2 ops exist to remove from design bodies, and it would be no better hidden inside the
    # transport.  The range check is the same one, applied to the whole extent BEFORE any of it
    # lands — a half-written refused block is worse than a refused one.

    def store_block(self, addr: int, values) -> None:
        """pysim write of a whole extent, starting at *addr*."""
        arr = np.asarray(values).reshape(-1)
        self._check_block(int(addr), int(arr.shape[0]))
        self.storage[int(addr):int(addr) + int(arr.shape[0])] = arr

    def load_block(self, addr: int, count: int) -> np.ndarray:
        """pysim read of *count* elements starting at *addr*, as a copy in the element's dtype.

        A **copy**, and deliberately: this is a transfer (Case 2), so the caller gets values that
        have left the memory.  A live view over the same storage is Case 3's ``array_ref``, which is
        a different operation with a different cost and is not built yet.
        """
        self._check_block(int(addr), int(count))
        return np.array(self.storage[int(addr):int(addr) + int(count)])

    def block_ref(self, addr: int, count: int) -> np.ndarray:
        """A **live numpy view** of elements ``[addr, addr+count)`` — the Case 3 primitive.

        The counterpart to :meth:`load_block`, and the difference is the whole point: that one
        copies because it models a transfer, this one aliases because nothing moved.  Writes through
        the returned array land in :attr:`storage` itself.

        Direction is **not** applied here — it belongs to the *port*, not the memory, and
        :meth:`BramIFMaster.array_ref` is where a read-only port marks its view unwritable.  The
        same range check as every other access: refused rather than wrapped, because the RTL wraps
        silently.
        """
        self._check_block(int(addr), int(count))
        return self.storage[int(addr):int(addr) + int(count)]

    def _check_block(self, addr: int, count: int) -> None:
        if count < 0 or addr < 0 or addr + count > int(self.nelem):
            raise IndexError(
                f"{type(self).__name__} '{self.name}': the extent [{addr}, {addr + count}) is "
                f"outside 0..{int(self.nelem)-1}. The RTL would index mem[addr[AW-1:0]] and alias "
                f"the overhang onto live words without a word of warning; pysim refuses instead.")

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
