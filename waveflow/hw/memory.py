from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from enum import Enum

from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import DynParam, HwModule
from waveflow.simulation.simobj import ProcessGen


class AddrUnit(Enum):
    byte = 0
    word = 1


class MemMgr:
    """Allocation policy and address arithmetic — **the manager, not the memory**.

    The second of the three memory objects (see ``docs/guide/memory/``).  It owns **no bytes**: it
    answers *where* something goes and *how* an address maps to a word index, which is what an OS
    allocator, a linker script, or a hand-rolled arena does.  Separating it from :class:`Memory`
    means the placement policy can be reasoned about, replaced, or reused without dragging a
    backing store along — and it collapses the duplication that came from two classes each growing
    their own ``alloc``/``free``.

    Two responsibilities, both pure:

    **Addressing.** :meth:`addr_to_index` / :meth:`index_to_addr` convert between the caller's
    address space and word indices, per :class:`AddrUnit`.  Byte addressing (the AXI/DDR
    convention) divides by the word's byte width and enforces alignment; word addressing is the
    identity.  This is the *only* place the convention is implemented.

    **Placement.** :meth:`place` runs a first-fit search over the occupied index ranges it is
    handed and returns the starting index for ``nwords``.  It is given the ranges rather than
    tracking them, so it can never disagree with whoever owns the storage — the byte store stays
    the single source of truth about what is occupied.
    """

    def __init__(
        self,
        word_size: int = 32,
        addr_size: int = 32,
        nwords_tot: int | None = None,
        addr_unit: AddrUnit = AddrUnit.byte,
    ) -> None:
        self.word_size = word_size
        self.addr_size = addr_size
        self.nwords_tot = nwords_tot
        self.addr_unit = addr_unit

    # -- addressing -------------------------------------------------------------------------

    @property
    def word_nbytes(self) -> int:
        """Bytes per word.  Raises when the word is not a whole number of bytes."""
        if self.word_size % 8 != 0:
            raise ValueError("Byte addressing requires word_size to be a multiple of 8 bits.")
        return self.word_size // 8

    def addr_to_index(self, addr: int) -> int:
        """Convert a caller-space address to a word index (the inverse of :meth:`index_to_addr`)."""
        if self.addr_unit == AddrUnit.byte:
            nb = self.word_nbytes
            if addr % nb != 0:
                raise ValueError(
                    f"Address {addr} is not aligned to the word size of {nb} bytes."
                )
            return addr // nb
        if self.addr_unit == AddrUnit.word:
            return addr
        raise ValueError(f"Unsupported address unit: {self.addr_unit}")

    def index_to_addr(self, index: int) -> int:
        """Convert a word index to a caller-space address."""
        if self.addr_unit == AddrUnit.byte:
            return index * self.word_nbytes
        if self.addr_unit == AddrUnit.word:
            return index
        raise ValueError(f"Unsupported address unit: {self.addr_unit}")

    # -- placement --------------------------------------------------------------------------

    def place(self, nwords: int, occupied: list[tuple[int, int]]) -> int:
        """First-fit: return the starting **word index** for ``nwords`` given the *occupied* ranges.

        *occupied* is a list of ``(start_index, end_index)`` half-open ranges, sorted.  Raises
        ``MemoryError`` when no gap is large enough within ``nwords_tot``, and ``ValueError`` when
        ``nwords`` is not positive.
        """
        if nwords <= 0:
            raise ValueError("nwords must be a positive integer.")
        next_free = 0
        for start_index, end_index in occupied:
            if next_free + nwords <= start_index:
                break
            next_free = max(next_free, end_index)
        if self.nwords_tot is not None and next_free + nwords > self.nwords_tot:
            raise MemoryError(
                f"Unable to allocate {nwords} contiguous words in memory of size {self.nwords_tot}."
            )
        return next_free


class Memory(object):
    """
    Sparse memory model for Waveflow.

    Attributes
    ----------
    
    segments : dict
        A dictionary of the allocated memory segments.  The key is the starting address.
        The value is a numpy array representing the memory segment.  Consistent with the 
        DataSchema.serialize method, if the word size is <= 32 bits, memory segments are 
        stored as uint32 arrays.  Else if the word size is <= 64 bits, 
        memory segments are stored as uint64 arrays.  If the word size is > 64 bits, 
        memory as [nelem, nwords64] where nwords64 = ceil(word_size / 64) and 
        each word is stored as a uint64.  
    """

    def __init__(
            self, 
            word_size : int =32, 
            addr_size : int =32,
            nwords_tot : int | None = None,
            addr_unit : AddrUnit = AddrUnit.byte):
        """
        Parameters
        ----------
        word_size : int
            The size of a word in bits.  Default is 32 bits.
        addr_size : int
            The size of an address in bits.  Default is 32 bits.
        nwords_tot : int | None
            The total number of words in the memory.  If None, the memory is unbounded.  
            Default is None.
        addr_unit : AddrUnit
            Specifies the *meaning* of an address:
        
            - AddrUnit.byte:
                The address is a byte address (AXI4 / Pynq / DDR style).
                The simulator will convert byte addresses to word indices
                using (addr // (word_size // 8)).

            - AddrUnit.word:
                The address is a word index (HLS-local array style).
                The simulator will treat the address directly as an index
                into the underlying numpy word array.

            Default is byte-addressable.
        """
        self.word_size = word_size
        self.addr_size = addr_size
        self.addr_unit = addr_unit
        self.nwords_tot = nwords_tot
        self.segments = {}
        #: The allocator/addressing policy.  Memory keeps the bytes; the manager decides where
        #: they go and how an address maps to a word index (one implementation, not two).
        self.mgr = MemMgr(word_size=word_size, addr_size=addr_size,
                          nwords_tot=nwords_tot, addr_unit=addr_unit)

    # Addressing is the MemMgr's job — Memory holds bytes and asks the manager where they go.
    # These stay as (private) methods so the ~dozen internal call sites read unchanged.
    def _addr_to_index(self, addr : int) -> int:
        return self.mgr.addr_to_index(addr)

    def _index_to_addr(self, index: int) -> int:
        return self.mgr.index_to_addr(index)

    def _segment_shape(self, nwords: int) -> tuple[int, ...]:
        if self.word_size <= 32:
            return (nwords,)
        if self.word_size <= 64:
            return (nwords,)
        return (nwords, (self.word_size + 63) // 64)

    def _segment_dtype(self):
        if self.word_size <= 32:
            return np.uint32
        return np.uint64

    def _segment_bounds(self) -> list[tuple[int, int]]:
        bounds = []
        for start_addr, segment in self.segments.items():
            start_index = self._addr_to_index(start_addr)
            end_index = start_index + int(segment.shape[0])
            bounds.append((start_index, end_index))
        bounds.sort()
        return bounds

    def _locate_segment(self, addr: int) -> tuple[np.ndarray, int, int, int]:
        addr_index = self._addr_to_index(addr)
        for start_addr, segment in self.segments.items():
            start_index = self._addr_to_index(start_addr)
            end_index = start_index + int(segment.shape[0])
            if start_index <= addr_index < end_index:
                return segment, addr_index - start_index, start_index, end_index
        raise ValueError(f"Address {addr} does not fall within any allocated segment.")
    
    def alloc(
            self,
            nwords : int
    ) -> int:
        """
        Allocate a contiguous memory segment.

        Parameters
        ----------
        nwords : int
            Number of contiguous words to allocate.

        Returns
        -------
        int
            Starting address of the allocated segment. The returned address uses
            the configured ``addr_unit`` semantics, so it is either a byte address
            or a word index.

        Raises
        ------
        ValueError
            If ``nwords`` is not positive.
        MemoryError
            If a contiguous segment of ``nwords`` words cannot be found within
            the configured memory bounds.

        Notes
        -----
        Allocation uses a first-fit search over the existing sparse segments.
        The newly allocated segment is zero-initialized and stored in
        ``self.segments`` under its starting address.
        """
        # Placement is policy (the manager); creating the segment is storage (here).  The manager
        # is HANDED the occupied ranges rather than tracking them, so the byte store stays the one
        # source of truth about what is occupied and the two can never disagree.
        next_free = self.mgr.place(nwords, self._segment_bounds())
        addr = self._index_to_addr(next_free)
        self.segments[addr] = np.zeros(
            self._segment_shape(nwords),
            dtype=self._segment_dtype(),
        )
        return addr

    def free(
            self,
            addr : int
    ):
        """
        Free a previously allocated memory segment.

        Parameters
        ----------
        addr : int
            Starting address of the segment to free, expressed in the configured
            ``addr_unit``.

        Raises
        ------
        KeyError
            If ``addr`` is not the starting address of an allocated segment.

        Notes
        -----
        Only full segments can be freed. Passing an address inside an allocated
        segment, rather than its start address, raises ``KeyError``.
        """
        if addr not in self.segments:
            raise KeyError(f"No allocated segment starts at address {addr}.")
        del self.segments[addr]

    def read(
            self,
            addr : int,
            nwords : int = 1
    ) -> np.ndarray:
        """
        Read a contiguous slice from an allocated memory segment.

        Parameters
        ----------
        addr : int
            Starting address of the read, expressed in the configured
            ``addr_unit``.
        nwords : int
            Number of words to read. Default is 1.

        Returns
        -------
        np.ndarray
            Copy of the requested data. For word sizes above 64 bits, the result
            has shape ``(nwords, ceil(word_size / 64))``.

        Raises
        ------
        ValueError
            If ``nwords`` is not positive, if ``addr`` is not within an allocated
            segment, or if the requested range extends past the end of the segment.
        """
        if nwords <= 0:
            raise ValueError("nwords must be a positive integer.")

        segment, offset, _, end_index = self._locate_segment(addr)
        addr_index = self._addr_to_index(addr)
        if addr_index + nwords > end_index:
            raise ValueError(
                f"Read of {nwords} words from address {addr} exceeds the bounds of its segment."
            )

        return np.array(segment[offset:offset + nwords], copy=True)

    def write(
            self,
            addr : int,
            data : np.ndarray
    ):
        """
        Write a contiguous slice into an allocated memory segment.

        Parameters
        ----------
        addr : int
            Starting address of the write, expressed in the configured
            ``addr_unit``.
        data : np.ndarray
            Array of words to write. The leading dimension is interpreted as the
            number of words. For word sizes above 64 bits, each word must be
            represented by ``ceil(word_size / 64)`` uint64 chunks.

        Raises
        ------
        ValueError
            If ``data`` is empty, if ``addr`` is not within an allocated segment,
            if the write would extend past the end of the segment, or if ``data``
            does not match the segment word layout.
        """
        segment, offset, _, end_index = self._locate_segment(addr)

        data_arr = np.asarray(data, dtype=segment.dtype)
        if segment.ndim == 1:
            if data_arr.ndim == 0:
                data_arr = data_arr.reshape(1)
            elif data_arr.ndim != 1:
                raise ValueError("Data shape is incompatible with the memory word layout.")
        else:
            if data_arr.ndim == 1 and data_arr.shape[0] == segment.shape[1]:
                data_arr = data_arr.reshape(1, -1)
            elif data_arr.ndim != segment.ndim or data_arr.shape[1:] != segment.shape[1:]:
                raise ValueError("Data shape is incompatible with the memory word layout.")

        nwords = int(data_arr.shape[0])
        if nwords <= 0:
            raise ValueError("data must contain at least one word.")

        addr_index = self._addr_to_index(addr)
        if addr_index + nwords > end_index:
            raise ValueError(
                f"Write of {nwords} words from address {addr} exceeds the bounds of its segment."
            )

        segment[offset:offset + nwords] = data_arr


# ---------------------------------------------------------------------------
# _DirectBackedMMIFMaster — MMIFMaster backed directly by a Memory object
# ---------------------------------------------------------------------------

@dataclass
class _DirectBackedMMIFMaster:
    """
    MMIFMaster-compatible endpoint that reads/writes a Memory directly,
    bypassing any AXI interface.  Used internally by MemoryMod.

    read() and write() are generator functions that yield no SimPy events
    (zero simulation time).  as_words(), as_array(), as_schema() provide
    zero-copy reference access to the pre-allocated inline block.
    """

    name: str
    sim: Any
    bitwidth: int = 32

    def __post_init__(self) -> None:
        self._mem: Memory | None = None
        self._base_addr: int | None = None

    def _bind_memory(self, mem: Memory, base_addr: int | None) -> None:
        self._mem = mem
        self._base_addr = base_addr

    # ------------------------------------------------------------------
    # Raw word transfers (generator functions; zero sim time)
    # ------------------------------------------------------------------

    def write(self, words: Any, global_addr: int) -> Any:
        assert self._mem is not None, "_DirectBackedMMIFMaster not bound to Memory"
        self._mem.write(global_addr, words)
        if False:
            yield  # makes this a generator function

    def read(self, nwords: int, global_addr: int) -> Any:
        assert self._mem is not None, "_DirectBackedMMIFMaster not bound to Memory"
        return self._mem.read(global_addr, nwords)
        yield  # noqa: unreachable — makes this a generator function

    # ------------------------------------------------------------------
    # Schema convenience methods (delegate to Memory, zero sim time)
    # ------------------------------------------------------------------

    def write_schema(self, obj: Any, addr: int, word_bw: int = 32) -> Any:
        yield from self.write(obj.serialize(word_bw=word_bw), addr)

    def read_schema(self, schema_type: type, addr: int, word_bw: int = 32) -> Any:
        nwords = schema_type.nwords_per_inst(word_bw)
        words = yield from self.read(nwords, addr)
        return schema_type().deserialize(words, word_bw=word_bw)

    def write_array(self, elements: Any, element_type: type, addr: int, word_bw: int = 32) -> Any:
        from waveflow.hw.arrayutils import write_array
        from waveflow.hw.dataschema import DataArray
        if isinstance(elements, DataArray):
            packed = write_array(elements, word_bw=word_bw)
        else:
            packed = write_array(elements, elem_type=element_type, word_bw=word_bw)
        yield from self.write(packed, addr)

    def read_array(self, element_type: type, count: int, addr: int, word_bw: int = 32) -> Any:
        from waveflow.hw.arrayutils import read_array
        words = yield from self.read(element_type.nwords_per_inst(word_bw) * count, addr)
        return read_array(words, element_type, word_bw, shape=count)

    # ------------------------------------------------------------------
    # Reference access (inline only — zero copy, zero sim time)
    # ------------------------------------------------------------------

    def as_words(self) -> np.ndarray:
        """Return a direct numpy view of the pre-allocated inline block."""
        if self._mem is None or self._base_addr is None:
            raise RuntimeError(
                "as_words() requires an inline MemoryMod with a pre-allocated block"
            )
        return self._mem.segments[self._base_addr]

    def as_array(self, elem_type: type) -> Any:
        """Return a :class:`~waveflow.hw.dataschema.DataArray` view of the inline block."""
        from waveflow.hw.arrayutils import read_array
        words = self.as_words()
        nwpe = elem_type.nwords_per_inst(self.bitwidth)
        count = len(words) // nwpe
        return read_array(words, elem_type, self.bitwidth, shape=count)

    def as_schema(self, schema_type: type) -> Any:
        """Return a schema instance deserialized from the inline block."""
        nwords = schema_type.nwords_per_inst(self.bitwidth)
        words = self.as_words()[:nwords]
        return schema_type().deserialize(words, word_bw=self.bitwidth)


# ---------------------------------------------------------------------------
# MemoryMod — an HwModule wrapping a Memory with MM interface endpoints
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemSeg:
    """One memory region tied to a burst bundle — mirrors the C++ ``wfbfm::MemSeg``.

    For a **load**, the bundle's words go to ``[off, off+len)`` (``length`` ignored — the bundle's own
    length is used).  For a **dump**, ``[off, off+length)`` is written to ``bundle``.  ``off``/``length``
    are word indices; ``bundle`` is a path rooted by the run dir (e.g. ``"vectors/mem_in"``).
    """
    off: int = 0
    length: int = 0
    bundle: str = ""

    def to_cpp(self) -> str:
        """Render as the C++ aggregate initializer ``{off, length, "bundle"}`` (field order matches
        ``wfbfm::MemSeg``: off, len, bundle)."""
        return f'{{{int(self.off)}, {int(self.length)}, "{self.bundle}"}}'


@dataclass
class MemoryMod(HwModule):
    """
    A latency-modeling :class:`~waveflow.hw.hw_module.HwModule` that wraps a
    :class:`Memory` and exposes MM interface endpoints.

    ``m_mm`` is a directly-backed master (zero-latency, for the owner's use).
    ``s_mm`` is an :class:`~waveflow.hw.memif.MMIFSlave` for external AXI-MM
    connections.

    In a generated XSI testbench this maps to a :class:`~waveflow.build.xsi.FlatMemory` — the arena
    the AXI-MM slave models serve out of.  It is declared ``shared``: two ``m_axi`` bundles
    (gmem0 read + gmem1 write) are backed by ONE memory, so the emitter constructs the arena once
    and hands it to both slave models rather than making one per bundle.

    Latency model
    -------------
    The memory models *access* latency; the interconnect models *bus* latency,
    and the two **compose** (they are not double-counted).  Each access on the
    ``s_mm`` slave path consumes
    ``(latency_init + nwords * latency_per_word) / clk.freq`` simulation seconds
    before touching the backing store.  The interconnect adds its own
    bus/wire/arbitration latency around the callback, so a read's total time is
    ``bus_request + memory_access + bus_return``.

    The ``m_mm`` direct master stays **zero-latency** — it models the owner
    reading its *own* inline block (a local C array in HLS), so no bus/access
    delay applies.  The latency model is only on the ``s_mm`` path.

    When ``inline=True`` (default) the full ``nwords_tot`` capacity is
    pre-allocated as one block; ``m_mm.as_words()`` / ``as_array()`` /
    ``as_schema()`` return direct views (zero sim time, maps to a local C
    array in HLS).

    When ``inline=False`` external callers use ``alloc()`` / ``free()`` to
    carve out regions, then access them via their own wired ``MMIFMaster``.
    """

    word_size: int = 32        # bits per word
    addr_size: int = 32        # address bits
    nwords_tot: int = 2 ** 20  # capacity in words
    inline: bool = True        # True = local BRAM; False = external DDR
    clk: Clock = field(default_factory=lambda: Clock(freq=1.0))
    latency_init: float = 0.0       # fixed cycles per access
    latency_per_word: float = 0.0   # cycles per word
    addr_unit: AddrUnit = AddrUnit.byte
    half_duplex: bool = False
    """When ``True`` the ``s_mm`` slave's read and write channels are one shared
    resource, so reads and writes to this memory mutually exclude — a single-port
    memory (a DDR model that shares R/W bandwidth, or single-port BRAM).  The default
    (full-duplex) gives independent AR/R and AW/W channels (real AXI)."""

    #: XSI memory config (:class:`DynParam`\\ s): regions to load from bundles in ``pre_sim`` and dump
    #: to bundles in ``post_sim``.  Empty (default) => the memory does nothing at pre/post_sim.  Set for
    #: a generated XSI testbench; the harness emits them as ``mem.load_segs = { ... };``.  Unused in
    #: pysim (the memory is seeded by the testbench directly).
    load_segs: DynParam[list[MemSeg]] = field(default_factory=list)
    dump_segs: DynParam[list[MemSeg]] = field(default_factory=list)
    #: pysim run-time anchor for the (relative) ``load_segs``/``dump_segs`` bundle paths — set by
    #: whoever materializes the scenario (an example/build dir or a temp dir).  ``None`` → resolve
    #: against the process cwd.  Runtime config; never emitted to codegen.
    root: Any = None

    def bfm_model(self):
        """XSI twin: the ``FlatMemory`` arena the AXI-MM slave models serve out of.

        ``shared`` is the point: two ``m_axi`` bundles are backed by ONE memory, so the emitter
        constructs this once and hands it to both slave models rather than one per bundle.  Which
        *kind* of slave serves each bundle (read vs write) is deliberately NOT this component's
        business — it comes from the DUT boundary port's own kind, so a memory need not know how it
        will be driven.

        The C++ arguments come from this component's own fields, so nothing example-specific leaks
        into the framework.

        NOTE: the XSI slave models are independent and un-arbitrated, whereas
        :class:`~waveflow.hw.memif.AXIMMCrossBarIF` models contention.  The two therefore describe
        different systems and should be expected to disagree on timing — see
        ``plans/xsi_tb_codegen.md``.
        """
        from waveflow.build.composite_gen import BfmModel
        return BfmModel(
            "FlatMemory",
            ports=("s_mm",),
            extra_args=(str(int(self.nwords_tot)), str(int(self.word_size) // 8)),
            shared="mem",
        )

    def __post_init__(self) -> None:
        # SimObj.__post_init__ assigns the default name and registers this
        # object with the Simulation (sim.add_obj) before we build endpoints.
        super().__post_init__()

        self._mem = Memory(
            word_size=self.word_size,
            addr_size=self.addr_size,
            nwords_tot=self.nwords_tot,
            addr_unit=self.addr_unit,
        )

        self._base_addr: int | None
        if self.inline:
            self._base_addr = self._mem.alloc(self.nwords_tot)
        else:
            self._base_addr = None

        self.m_mm = _DirectBackedMMIFMaster(
            name=f'{self.name}_m_mm',
            sim=self.sim,
            bitwidth=self.word_size,
        )
        self.m_mm.__post_init__()
        self.m_mm._bind_memory(self._mem, self._base_addr)

        from waveflow.hw.memif import MMIFSlave
        self.s_mm = MMIFSlave(
            name=f'{self.name}_s_mm',
            sim=self.sim,
            bitwidth=self.word_size,
            half_duplex=self.half_duplex,
            rx_write_proc=self._on_write,
            rx_read_proc=self._on_read,
            # untimed snapshot read for MMIFMaster.poll_until (its bus cost is
            # modeled aggregately by the poller registry, so the peek is free).
            peek_read=lambda nwords, local_addr: self._mem.read(local_addr, nwords),
        )
        # The endpoint surface is `s_mm` ALONE, and the exclusion of `m_mm` is principled rather
        # than curated (plans/design_cut.md S1/S3).  `s_mm` is the bus-facing port: it is what gets
        # wired into a graph, what `bfm_model().ports` names, and what crosses a cut.  `m_mm` is a
        # direct, zero-latency back door the owner uses to poke the store without a bus — it is
        # never bound to an interface anywhere in the tree.  Registering it would make it a phantom
        # boundary port in `derive_boundary` and an uncoverable one in the `xsi_bfm_model` check,
        # which is how the coverage predicate found this in the first place.
        self.add_endpoint(self.s_mm)

    def pre_sim(self) -> None:
        """Seed the memory from ``load_segs`` — the pysim mirror of the C++ ``FlatMemory::pre_sim``.

        Empty ``load_segs`` (the default) => no-op: an in-process-seeded testbench sets nothing here.
        A file-driven testbench sets ``load_segs`` (and :attr:`root`), and the memory loads each
        region's bundle just like the RTL memory does — so both backends read the same ``mem_in``
        bytes at the same lifecycle point.  Bundle paths are resolved against :attr:`root` at run time
        (like the :class:`~waveflow.simulation.stream_tb.StreamDriver`), so a relative
        ``"vectors/mem_in"`` works from any cwd.
        """
        super().pre_sim()
        if not self.load_segs:
            return
        from pathlib import Path

        from waveflow.utils.burst_io import read_burst_bundle

        bpw = int(self.word_size) // 8
        for seg in self.load_segs:
            p = Path(seg.bundle)
            if self.root is not None and not p.is_absolute():
                p = Path(self.root) / p
            words = np.concatenate([np.asarray(b, dtype=np.uint64) for b in read_burst_bundle(p)])
            self._mem.write(seg.off * bpw, words)

    def _access_delay(self, nwords: int) -> float:
        """Modeled RAM access time (seconds) for an *nwords*-word transfer."""
        return (self.latency_init + nwords * self.latency_per_word) / self.clk.freq

    def _on_write(self, words: Any, local_addr: int) -> ProcessGen[None]:
        # Decision 5: model the access latency *before* the data op; the
        # interconnect already wraps this callback and waits for it, so the
        # caller observes bus + access time.
        yield self.timeout(self._access_delay(len(words)))
        self._mem.write(local_addr, words)

    def _on_read(self, nwords: int, local_addr: int) -> ProcessGen[np.ndarray]:
        yield self.timeout(self._access_delay(nwords))
        return self._mem.read(local_addr, nwords)

    def alloc(self, nwords: int) -> int:
        """Allocate a region in the backing Memory (system-level use)."""
        return self._mem.alloc(nwords)

    def free(self, addr: int) -> None:
        """Free a previously allocated region."""
        self._mem.free(addr)

    # ------------------------------------------------------------------
    # Testbench-side direct access (zero sim time)
    # ------------------------------------------------------------------
    #
    # These mirror the hand-written histogram testbench's flat-array access
    # (``mgr.alloc`` + ``write_array(buf, mem+widx, n)`` / ``read_array(...)``):
    # a sequential ``HwTestbench.main()`` populates and inspects memory by
    # direct indexing, not over an ``m_axi`` master.  They are recognised by
    # the testbench codegen (see ``plans/aximm_codegen.md`` decision 9) and
    # lower to ``MemMgr::alloc`` + ``<elem>_array_utils::{write,read}_array``.

    def alloc_array(self, data: Any, elem_type: type, count: int | None = None) -> int:
        """Allocate a region sized for *count* elements and populate it.

        Returns the **byte** start address (``addr_unit`` semantics), matching
        the order-preserving ``Memory.alloc`` first-fit allocation (decision 8).
        """
        from waveflow.hw.arrayutils import get_nwords, write_array
        n = len(data) if count is None else int(count)
        nwords = get_nwords(elem_type, word_bw=self.word_size, shape=n)
        addr = self._mem.alloc(nwords)
        packed = write_array(np.asarray(data)[:n], elem_type=elem_type,
                             word_bw=self.word_size)
        self._mem.write(addr, packed)
        return addr

    def read_array(self, addr: int, elem_type: type, count: int) -> Any:
        """Read *count* elements from *addr* (byte address) and deserialize."""
        from waveflow.hw.arrayutils import get_nwords, read_array
        nwords = get_nwords(elem_type, word_bw=self.word_size, shape=int(count))
        words = self._mem.read(addr, nwords)
        return read_array(words, elem_type, word_bw=self.word_size, shape=int(count))
    