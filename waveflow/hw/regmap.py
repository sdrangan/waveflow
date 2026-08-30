"""
regmap.py — Register-map abstraction for Waveflow.

Provides a Python-native register map that bridges a DataSchema-typed backing
store to an AXI-Lite-compatible MMIFSlave endpoint.

Public API
----------
RegAccess            — Enum of host access modes
RegField             — Dataclass declaring one field (schema, access, hooks)
RegMap               — Ordered collection of RegFields with numpy word buffers
RegMapAccessError    — Raised on access-mode violation or bad offset
RegMapMMIFSlave      — MMIFSlave subclass dispatching to a RegMap
VitisRegMap          — RegMap mirroring Vitis's s_axilite control layout
VitisRegMapMMIFSlave — RegMapMMIFSlave with Vitis kernel launch lifecycle
Bit                  — IntField.specialize(bitwidth=1, signed=False)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal

import numpy as np

from waveflow.hw.dataschema import DataSchema, IntField, Words
from waveflow.hw.hwstmt import SynthCallStmt
from waveflow.hw.memif import MMIFMaster, MMIFSlave
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen

# Single-bit unsigned integer field alias.
# Could become a real class in a future version.
Bit: type[IntField] = IntField.specialize(bitwidth=1, signed=False)

# One whole 32-bit bus word — the Vitis GIER/IER/ISR registers.
Uint32Word: type[IntField] = IntField.specialize(bitwidth=32, signed=False)


# ---------------------------------------------------------------------------
# Synthesizable IR nodes for regmap accesses
# ---------------------------------------------------------------------------


@dataclass
class RegMapGetStmt(SynthCallStmt):
    """Synthesizable read of a regmap field — emits an AXI-Lite scalar read."""


@dataclass
class RegMapSetStmt(SynthCallStmt):
    """Synthesizable write to a regmap field — emits an AXI-Lite scalar write."""


# ---------------------------------------------------------------------------
# Access mode enum
# ---------------------------------------------------------------------------


class RegAccess(Enum):
    """Host access mode for a register field.

    | Mode | Host read | Host write | Owner read | Owner write |
    |------|-----------|------------|------------|-------------|
    | R    | OK        | rejected   | OK         | OK          |
    | W    | rejected  | OK         | OK         | OK          |
    | RW   | OK        | OK         | OK         | OK          |
    | W1C  | OK        | clear bits | OK         | OK          |
    | W1S  | OK        | set+clear  | OK         | OK          |
    """

    R   = "R"
    W   = "W"
    RW  = "RW"
    W1C = "W1C"
    W1S = "W1S"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RegMapAccessError(RuntimeError):
    """Raised on host access-mode violation, offset miss, or unaligned address."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bit_width_of(schema: type[DataSchema]) -> int:
    """Return the bit width of a scalar *schema*, for bit-packed fields.

    Only schemas carrying an explicit ``bitwidth`` (``IntField`` and its
    specializations, ``EnumField``, …) may be packed into a shared word.
    """
    width = getattr(schema, "bitwidth", None)
    if not isinstance(width, int):
        raise ValueError(
            f"Schema {schema.__name__} has no integer 'bitwidth'; it cannot be "
            "packed into a shared word (bit_offset=...)."
        )
    return width


# ---------------------------------------------------------------------------
# RegField — one field declaration
# ---------------------------------------------------------------------------


@dataclass
class RegField:
    """Declaration of one register map field.

    Parameters
    ----------
    schema:      DataSchema subclass (IntField, EnumField, DataArray, …)
    access:      Host access mode
    description: Free-text; included in generated documentation
    on_write:    Hook fired per-word after the backing store update
    on_read:     Hook fired per-word after reading the backing store
    offset:      Manual byte offset; None = auto-assign in declaration order
    bit_offset:  Bit position of this field's LSB *within* the word at
                 ``offset``.  ``None`` (default) = the field owns its word(s)
                 exclusively.  When set, several fields share one word (as
                 Vitis packs ap_start/ap_done/ap_idle/ap_ready into 0x00);
                 ``offset`` must then be given explicitly and the schema must
                 be a scalar with a ``bitwidth`` that fits in one bus word.
    """

    schema:      type[DataSchema]
    access:      RegAccess
    description: str = ""
    on_write:    Callable[[str, int, int], None] | None = None
    on_read:     Callable[[str, int, int], None] | None = None
    offset:      int | None = None
    bit_offset:  int | None = None
    is_vitis_auto: bool = False   # True for fields Vitis auto-generates (ap_start, etc.)
                                  # — present in PySim but skipped in C++ codegen.


# ---------------------------------------------------------------------------
# RegMap — ordered field collection with numpy backing buffers
# ---------------------------------------------------------------------------


class RegMap:
    """
    Ordered collection of RegFields with per-field numpy word buffers.

    The backing store is word-aligned; each field's words are consecutive.
    Host-side access goes through write_word / read_word (called by
    RegMapMMIFSlave). Owner-side access goes through get / set.

    A field declaring ``bit_offset`` is *packed*: it occupies a bit range of
    the word at its ``offset`` and shares that word with other bit-fields.
    Packed fields still get their own backing buffer holding the unshifted
    value, so ``get`` / ``set`` are identical for packed and unpacked fields;
    only the bus-level word composition differs.
    """

    #: Byte offset spacing between successive auto-assigned fields.  The base
    #: RegMap packs fields tightly (one word each); VitisRegMap overrides this
    #: to reproduce Vitis's data-word + control-word stride.
    _auto_start: int = 0

    def __init__(self, fields: dict[str, RegField], bitwidth: int = 32) -> None:
        self.bitwidth = bitwidth
        self._fields: dict[str, RegField] = dict(fields)
        word_bytes = bitwidth // 8

        # W1C / W1S require single-word scalar fields.
        for name, f in fields.items():
            if f.access in (RegAccess.W1C, RegAccess.W1S):
                nw = f.schema.nwords_per_inst(bitwidth)
                if nw != 1:
                    raise ValueError(
                        f"Field '{name}': {f.access.value} requires a single-word "
                        f"field but schema occupies {nw} words."
                    )

        # ------------------------------------------------------------------
        # Offset assignment
        # ------------------------------------------------------------------
        occupied: set[int] = set()
        self._offsets: dict[str, int] = {}
        self._bit_offsets: dict[str, int] = {}
        self._packed_words: dict[int, list[str]] = {}

        # Pass 1a: packed bit-fields.  Several may share one word, so overlap
        # is checked per *bit* rather than per word.
        bit_claimed: dict[int, int] = {}
        for name, f in fields.items():
            if f.bit_offset is None:
                continue
            if f.offset is None:
                raise ValueError(
                    f"Field '{name}' sets bit_offset={f.bit_offset} but no offset; "
                    "packed fields must name the word they live in."
                )
            width = _bit_width_of(f.schema)
            if f.bit_offset < 0 or f.bit_offset + width > bitwidth:
                raise ValueError(
                    f"Field '{name}': bits [{f.bit_offset}:{f.bit_offset + width - 1}] "
                    f"do not fit in a {bitwidth}-bit word."
                )
            mask = ((1 << width) - 1) << f.bit_offset
            if bit_claimed.get(f.offset, 0) & mask:
                raise ValueError(
                    f"Field '{name}' at offset 0x{f.offset:02x} bit {f.bit_offset} "
                    "overlaps another packed field's bits."
                )
            bit_claimed[f.offset] = bit_claimed.get(f.offset, 0) | mask
            self._offsets[name] = f.offset
            self._bit_offsets[name] = f.bit_offset
            self._packed_words.setdefault(f.offset, []).append(name)
            occupied.add(f.offset)

        # Pass 1b: manually-placed whole-word fields; detect overlaps (including
        # against packed words, which pass 1a has already marked occupied).
        for name, f in fields.items():
            if f.offset is None or f.bit_offset is not None:
                continue
            nwords = f.schema.nwords_per_inst(bitwidth)
            for k in range(nwords):
                pos = f.offset + k * word_bytes
                if pos in occupied:
                    raise ValueError(
                        f"Field '{name}' at offset 0x{f.offset:x} overlaps "
                        f"with another field at byte 0x{pos:x}."
                    )
                occupied.add(pos)
            self._offsets[name] = f.offset

        # Pass 2: auto-assign remaining fields in declaration order.
        next_free = self._auto_start
        align = self._auto_align_bytes(word_bytes)
        for name, f in fields.items():
            if f.offset is not None:
                continue
            nwords = f.schema.nwords_per_inst(bitwidth)
            while not all(
                (next_free + k * word_bytes) not in occupied for k in range(nwords)
            ):
                next_free += align
            self._offsets[name] = next_free
            for k in range(nwords):
                occupied.add(next_free + k * word_bytes)
            next_free += self._auto_stride_bytes(nwords, word_bytes)

        # ------------------------------------------------------------------
        # Backing store — zero-initialised, one numpy array per field.
        # ------------------------------------------------------------------
        dtype = np.uint32 if bitwidth <= 32 else np.uint64
        self._buffers: dict[str, np.ndarray] = {
            name: np.zeros(f.schema.nwords_per_inst(bitwidth), dtype=dtype)
            for name, f in fields.items()
        }

    # ------------------------------------------------------------------
    # Auto-placement policy (overridden by VitisRegMap)
    # ------------------------------------------------------------------

    def _auto_align_bytes(self, word_bytes: int) -> int:
        """Granularity the auto-placer steps by when hunting for a free slot."""
        return word_bytes

    def _auto_stride_bytes(self, nwords: int, word_bytes: int) -> int:
        """Bytes consumed by an auto-placed field of *nwords* words.

        The base RegMap packs tightly: a field consumes exactly its own words.
        """
        return nwords * word_bytes

    # ------------------------------------------------------------------
    # Layout queries
    # ------------------------------------------------------------------

    def offset_of(self, name: str) -> int:
        """Return the byte offset of field *name*."""
        try:
            return self._offsets[name]
        except KeyError:
            raise KeyError(f"No field '{name}' in this RegMap.") from None

    def nwords_of(self, name: str) -> int:
        """Return the number of bus words occupied by field *name*."""
        return int(self._fields[name].schema.nwords_per_inst(self.bitwidth))

    def bit_offset_of(self, name: str) -> int | None:
        """Return the LSB bit position of packed field *name* within its word,
        or ``None`` if the field owns its word(s) exclusively."""
        if name not in self._fields:
            raise KeyError(f"No field '{name}' in this RegMap.")
        return self._bit_offsets.get(name)

    def bit_fields_at_offset(self, byte_offset: int) -> list[str]:
        """Return the packed field names sharing the word at *byte_offset*
        (in declaration order), or ``[]`` if that word is not bit-packed."""
        return list(self._packed_words.get(byte_offset, ()))

    def total_size_bytes(self) -> int:
        """Return the total byte span of this RegMap."""
        if not self._offsets:
            return 0
        word_bytes = self.bitwidth // 8
        return max(
            off + self.nwords_of(n) * word_bytes
            for n, off in self._offsets.items()
        )

    # ------------------------------------------------------------------
    # Owner-side value access
    # ------------------------------------------------------------------

    @synthesizable(stmt_class=RegMapGetStmt)
    def get(self, name: str) -> Any:
        """Deserialize the backing buffer and return a schema instance."""
        f = self._fields[name]
        return f.schema().deserialize(self._buffers[name], word_bw=self.bitwidth)

    @synthesizable(stmt_class=RegMapSetStmt)
    def set(self, name: str, value: Any) -> None:
        """Overwrite the field's backing buffer from a schema instance or raw value.

        Raw values are wrapped via ``schema(value)`` before serialization.
        W1C / W1S semantics are NOT applied; the buffer is overwritten directly.
        """
        f = self._fields[name]
        nwords = self.nwords_of(name)
        if not isinstance(value, f.schema):
            value = f.schema(value)  # type: ignore[call-arg]
        words = value.serialize(word_bw=self.bitwidth)
        if len(words) != nwords:
            raise ValueError(
                f"Serialized length {len(words)} != expected {nwords} for '{name}'."
            )
        self._buffers[name][:] = words

    # ------------------------------------------------------------------
    # Testbench file I/O (runtime side of the codegen-only TB spellings)
    # ------------------------------------------------------------------
    #
    # ``dut.regmap.read_uint32_file(...)`` / ``dut.regmap.write_status_json(...)``
    # are recognized by the *testbench extractor* as AST patterns and lowered to
    # C++ (``waveflow/build/hwcodegen.py``) — codegen never calls these methods.
    # They exist here so a single-process ``SeqTB.main()`` can also **run** in
    # Python (the Stage-2 runnable path): the runtime behaviour mirrors the
    # emitted C++ (read a packed uint32 field from disk; dump selected fields as
    # a JSON status record) so the Python golden matches the C-sim result.

    def read_uint32_file(self, name: str, file_path: str | Path) -> None:
        """Load field *name* from a uint32-packed binary file (runtime mirror of
        the emitted ``streamutils::read_uint32_file``)."""
        f = self._fields[name]
        value = f.schema().read_uint32_file(file_path)
        self.set(name, value)

    def write_status_json(self, file_path: str | Path, *, fields: list[str]) -> None:
        """Write the current values of *fields* as a JSON status record (runtime
        mirror of the emitted status-JSON writer).  Each field is dumped as an
        integer, matching the scalar regmap fields a status file carries."""
        import json

        if not fields:
            raise ValueError("write_status_json requires a non-empty fields list")
        data: dict[str, int] = {}
        for name in fields:
            val = self.get(name)
            data[name] = int(getattr(val, "val", val))
        out_path = Path(file_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Bus-level word access (used by RegMapMMIFSlave)
    # ------------------------------------------------------------------

    def field_name_at_offset(self, byte_offset: int) -> tuple[str, int]:
        """Return (field_name, sub_word_index) for the whole-word field at
        *byte_offset*, or raise.

        ``sub_word_index`` is the index of this word *within a multi-word
        field* — it is not a bit position.  Bit-packed words have no single
        owning field, so they raise here; use :meth:`bit_fields_at_offset`.
        """
        if byte_offset in self._packed_words:
            raise RegMapAccessError(
                f"Byte offset 0x{byte_offset:x} is a bit-packed word shared by "
                f"{self._packed_words[byte_offset]}; use bit_fields_at_offset()."
            )
        word_bytes = self.bitwidth // 8
        for name, off in self._offsets.items():
            if name in self._bit_offsets:
                continue
            for k in range(self.nwords_of(name)):
                if off + k * word_bytes == byte_offset:
                    return name, k
        raise RegMapAccessError(
            f"No field at byte offset 0x{byte_offset:x} in this RegMap."
        )

    def read_word(self, name: str, sub_word: int) -> int:
        """Return one word from the field's backing buffer."""
        return int(self._buffers[name][sub_word])

    def write_word(
        self,
        name: str,
        sub_word: int,
        value: int,
        *,
        source: Literal["host", "owner"],
    ) -> int:
        """Write one word, applying W1C masking for host writes.

        Returns the post-write buffer value (before any subsequent auto-clear).
        W1S auto-clear is handled by RegMapMMIFSlave, not here.
        """
        buf = self._buffers[name]
        f = self._fields[name]
        if source == "host" and f.access == RegAccess.W1C:
            # Clear bits set in 'value' from the backing store.
            buf[sub_word] = int(buf[sub_word]) & (~value)
        else:
            buf[sub_word] = value
        return int(buf[sub_word])

    # ------------------------------------------------------------------
    # Host-side bound access
    # ------------------------------------------------------------------

    def bind_master(self, master: MMIFMaster, base_addr: int = 0) -> "BoundRegMap":
        """Return a :class:`BoundRegMap` proxying host-side bus access at
        ``base_addr`` via ``master``.  The proxy looks up each field's
        schema and offset from this regmap, so callers issue reads/writes
        by field name instead of recomputing addresses and schema types
        at every call site.
        """
        return BoundRegMap(self, master, base_addr)


# ---------------------------------------------------------------------------
# BoundRegMap — host-side proxy mirroring RegMap.get/set over an MMIFMaster
# ---------------------------------------------------------------------------


class BoundRegMap:
    """Host-side proxy binding a :class:`RegMap` to an :class:`MMIFMaster`
    plus a base address.  Exposes ``get`` / ``set`` / ``start`` methods
    whose names mirror :meth:`RegMap.get` / :meth:`RegMap.set` (the
    kernel-side, in-process API), but route through the master bus.

    ``get`` returns a native Python value:

    - ``IntField`` (including ``Bit``) → ``int``
    - ``FloatField`` → ``float``
    - ``EnumField`` → the matching ``IntEnum`` member
    - ``DataArray`` / ``DataList`` / other → the schema instance

    All methods are coroutines — call sites use ``yield from``.
    """

    def __init__(self, regmap: RegMap, master: MMIFMaster, base_addr: int = 0) -> None:
        self._regmap = regmap
        self._master = master
        self._base_addr = base_addr

    @property
    def base_addr(self) -> int:
        return self._base_addr

    def set(self, name: str, value: Any) -> ProcessGen[None]:
        """Write ``value`` to field ``name`` over the master bus.

        Raw values are wrapped via ``schema(value)`` to match the kernel
        side's :meth:`RegMap.set` ergonomics.

        A bit-packed field costs the same single word write as an unpacked
        one: the word is composed with this field's bits in place and zeros
        elsewhere — no read-modify-write.  That mirrors the hardware, where a
        word write drives every writable bit of the word.

        Consequence: if a packed word holds *several* writable fields, setting
        one zeroes the others, exactly as it would on the bus.  A caller that
        needs to preserve a neighbour must compose the word itself.  This does
        not bite VitisRegMap's 0x00 control word, where ap_start is the only
        writable bit and ap_done / ap_idle / ap_ready are read-only.
        """
        f = self._regmap._fields[name]
        if not isinstance(value, f.schema):
            value = f.schema(value)
        addr = self._base_addr + self._regmap.offset_of(name)
        lo = self._regmap.bit_offset_of(name)
        if lo is None:
            yield from self._master.write_schema(value, addr=addr)
            return
        width = _bit_width_of(f.schema)
        words = value.serialize(word_bw=self._regmap.bitwidth)
        raw = (int(words[0]) & ((1 << width) - 1)) << lo
        yield from self._master.write_schema(self._word_schema()(raw), addr=addr)

    def get(self, name: str) -> ProcessGen[Any]:
        """Read field ``name`` over the master bus and return a native value.

        A bit-packed field costs one word read; its bits are extracted from
        the shared word.
        """
        f = self._regmap._fields[name]
        addr = self._base_addr + self._regmap.offset_of(name)
        lo = self._regmap.bit_offset_of(name)
        if lo is None:
            obj = yield from self._master.read_schema(f.schema, addr=addr)
            return self._to_native(obj, f.schema)
        word = yield from self._master.read_schema(self._word_schema(), addr=addr)
        width = _bit_width_of(f.schema)
        raw = (int(word.val) >> lo) & ((1 << width) - 1)
        dtype = np.uint32 if self._regmap.bitwidth <= 32 else np.uint64
        obj = f.schema().deserialize(
            np.array([raw], dtype=dtype), word_bw=self._regmap.bitwidth
        )
        return self._to_native(obj, f.schema)

    def _word_schema(self) -> type[IntField]:
        """Unsigned IntField spanning one whole bus word — used to move a
        bit-packed field's containing word across the bus."""
        return IntField.specialize(bitwidth=self._regmap.bitwidth, signed=False)

    def start(self) -> ProcessGen[None]:
        """Write 1 to ``ap_start`` (only valid on a :class:`VitisRegMap`)."""
        yield from self.set("ap_start", 1)

    def poll_end(
        self,
        interval: float,
        max_polls: int = 100,
        field: str = "ap_done",
        target: Any = 1,
    ) -> ProcessGen[Any]:
        """Poll ``field`` until it reads ``target``, with ``interval`` seconds between reads.

        Defaults to the standard ``ap_done == 1`` completion contract that
        :class:`VitisRegMap` auto-emits via :class:`VitisRegMapMMIFSlave` — so a
        typical kernel-launch flow is just::

            yield from rm.start()
            yield from rm.poll_end(interval=clk.period * 4, max_polls=64)

        ``interval`` is a real-time delay (seconds); the caller usually
        computes it from a clock period (e.g. ``4 * clk.period`` to poll
        every four cycles). Polling more aggressively than the bus can
        service stuffs the AXI-Lite link with redundant reads — choose
        ``interval`` to match the expected kernel runtime.

        Polling is a pedagogical / debugging convenience. Production hosts
        should wait on the AXI-Lite interrupt line instead.

        Raises :class:`RuntimeError` if ``target`` has not been observed
        after ``max_polls`` reads.
        """
        env = self._master.env
        for _ in range(max_polls):
            value = yield from self.get(field)
            if value == target:
                return value
            yield env.timeout(interval)
        raise RuntimeError(
            f"poll_end: '{field}' did not reach {target!r} after {max_polls} polls "
            f"(interval={interval} s); last value={value!r}."
        )

    @staticmethod
    def _to_native(obj: Any, schema_cls: type) -> Any:
        from waveflow.hw.dataschema import FloatField, IntField
        enum_type = getattr(schema_cls, "enum_type", None)
        if enum_type is not None:
            return enum_type(int(obj.val))
        if isinstance(schema_cls, type) and issubclass(schema_cls, IntField):
            return int(obj.val)
        if isinstance(schema_cls, type) and issubclass(schema_cls, FloatField):
            return float(obj.val)
        return obj


# ---------------------------------------------------------------------------
# RegMapMMIFSlave — wires MMIFSlave callbacks to a RegMap
# ---------------------------------------------------------------------------


@dataclass
class RegMapMMIFSlave(MMIFSlave):
    """MMIFSlave subclass that dispatches reads/writes to a RegMap.

    Wires ``rx_write_proc`` and ``rx_read_proc`` automatically; callers should
    not pass these kwargs.
    """

    #: The AXI4-Lite control port Vitis creates for a host-activated kernel.  It has a real kind, so
    #: a walk that meets one can SAY so -- ``_boundary_port`` still refuses to lower it (there is no
    #: ``ap_ctrl_none`` top with an ``s_axilite`` port) and ``BFM_DUALS`` records that no model
    #: drives it.  Naming the kind is what turns both refusals from "unknown endpoint type" into the
    #: actual diagnosis.  See plans/design_cut.md S2/S7.
    #:
    #: **This declaration is the ordering hazard, resolved.**  In the ``isinstance`` chain this
    #: replaced, the ``RegMapMMIFSlave`` test had to come before the ``MMIFSlave`` one; swapping two
    #: lines made an ``axilite_slave`` lower silently as ``mm_slave``.  Here the subclass's own
    #: declaration wins by inheritance, and there is no order to get wrong.
    boundary_kind: ClassVar[str] = "axilite_slave"

    regmap: RegMap = field(kw_only=True)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        if self.bitwidth != self.regmap.bitwidth:
            raise ValueError(
                f"Slave bitwidth ({self.bitwidth}) != RegMap bitwidth "
                f"({self.regmap.bitwidth})."
            )
        self.rx_write_proc = self._rx_write
        self.rx_read_proc = self._rx_read
        super().__post_init__()

    def _rx_write(self, words: Words, local_addr: int) -> ProcessGen[None]:
        """Per-word write dispatch: validate access, update buffer, fire hooks."""
        word_bytes = self.bitwidth // 8
        for i in range(len(words)):
            byte_addr = local_addr + i * word_bytes
            raw_val = int(words[i])
            packed = self.regmap.bit_fields_at_offset(byte_addr)
            if packed:
                self._write_packed_word(byte_addr, packed, raw_val)
                continue
            name, sub_word = self.regmap.field_name_at_offset(byte_addr)
            f = self.regmap._fields[name]
            if f.access == RegAccess.R:
                raise RegMapAccessError(
                    f"Host write to read-only field '{name}' at 0x{byte_addr:x}."
                )
            # Update backing store (W1C masking applied inside write_word).
            self.regmap.write_word(name, sub_word, raw_val, source="host")
            # Hook fires after backing-store update, before W1S auto-clear.
            if f.on_write is not None:
                f.on_write(name, sub_word, raw_val)
            # W1S: auto-clear the bit after the hook returns.
            if f.access == RegAccess.W1S:
                self.regmap._buffers[name][sub_word] = 0
        yield self.timeout(0)

    def _write_packed_word(
        self, byte_addr: int, packed: list[str], raw_val: int
    ) -> None:
        """Decompose a host write to a bit-packed word.

        Read-only bits *ignore* the write rather than raising: a packed word
        mixes R and W bits, and the hardware slave likewise decodes only the
        writable bits (e.g. Vitis's 0x00 latches ap_start from WDATA[0] and
        drops the rest).  Raising would make it impossible to write ap_start
        without also "writing" the read-only ap_done beside it.
        """
        for name in packed:
            f = self.regmap._fields[name]
            if f.access == RegAccess.R:
                continue
            lo = self.regmap.bit_offset_of(name)
            assert lo is not None
            width = _bit_width_of(f.schema)
            bits = (raw_val >> lo) & ((1 << width) - 1)
            self.regmap.write_word(name, 0, bits, source="host")
            if f.on_write is not None:
                f.on_write(name, 0, bits)
            if f.access == RegAccess.W1S:
                self.regmap._buffers[name][0] = 0

    def _rx_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        """Per-word read dispatch: validate access, read buffer, fire hooks."""
        word_bytes = self.bitwidth // 8
        dtype = np.uint32 if self.bitwidth <= 32 else np.uint64
        result = np.zeros(nwords, dtype=dtype)
        for i in range(nwords):
            byte_addr = local_addr + i * word_bytes
            packed = self.regmap.bit_fields_at_offset(byte_addr)
            if packed:
                result[i] = self._read_packed_word(packed)
                continue
            name, sub_word = self.regmap.field_name_at_offset(byte_addr)
            f = self.regmap._fields[name]
            if f.access == RegAccess.W:
                raise RegMapAccessError(
                    f"Host read from write-only field '{name}' at 0x{byte_addr:x}."
                )
            word_val = self.regmap.read_word(name, sub_word)
            # Hook fires after read, before return.
            if f.on_read is not None:
                f.on_read(name, sub_word, word_val)
            result[i] = word_val
        yield self.timeout(0)
        return result  # type: ignore[return-value]

    def _read_packed_word(self, packed: list[str]) -> int:
        """Compose a bit-packed word from its constituent fields' buffers.

        Write-only bits read back as 0 (the hardware drives no read data for
        them) rather than raising — see :meth:`_write_packed_word`.
        """
        word_val = 0
        for name in packed:
            f = self.regmap._fields[name]
            if f.access == RegAccess.W:
                continue
            lo = self.regmap.bit_offset_of(name)
            assert lo is not None
            width = _bit_width_of(f.schema)
            bits = self.regmap.read_word(name, 0)
            if f.on_read is not None:
                f.on_read(name, 0, bits)
            word_val |= (bits & ((1 << width) - 1)) << lo
        return word_val


# ---------------------------------------------------------------------------
# VitisRegMap — RegMap with Vitis ap_ctrl_hs conventions (v1)
# ---------------------------------------------------------------------------


class VitisRegMap(RegMap):
    """RegMap mirroring the s_axilite control layout Vitis HLS generates.

    Layout (verified against the ``simp_fun`` csynth artifacts —
    ``solution1/.autopilot/db/coregen/control.h`` and the ``ADDR_*``
    localparams in ``simp_fun_control_s_axi.v``)::

        0x00 : Control signals  bit0 ap_start (RW/COH), bit1 ap_done (R/COR),
                                bit2 ap_idle (R),       bit3 ap_ready (R/COR),
                                bit7 auto_restart (RW), bit9 interrupt (R)
        0x04 : Global Interrupt Enable Register (GIER)
        0x08 : IP Interrupt Enable Register (IER)
        0x0c : IP Interrupt Status Register (ISR)
        0x10 : first user field  (0x14 : its control word / reserved)
        0x18 : second user field (0x1c : …)
        …

    So the four control signals are *bits of one word at 0x00* — not registers
    of their own — and 32-bit scalar arguments start at 0x10 on an 8-byte
    stride (one data word plus one control/reserved word each).

    What this models, and what it does not
    -------------------------------------
    The *layout* mirrors Vitis.  The *side effects* are modelled only as far
    as the simulator needs:

    - ``ap_start`` is W1S here (auto-clears once the launch hook has run)
      where hardware is COH (clears on the ap_ready handshake).  Same net
      effect for a sim that launches synchronously.
    - ``ap_done`` / ``ap_ready`` are **not** clear-on-read here; they are set
      when ``on_start`` returns and cleared on the next ``ap_start``.  A host
      may therefore read ``ap_done`` repeatedly and keep seeing 1, where real
      hardware clears it on the first read of 0x00.
    - ``gier`` / ``ier`` / ``isr`` are plain storage: writing them enables no
      interrupt, and ``isr`` does not implement toggle-on-write.  There is no
      interrupt line in the simulation.
    - ``auto_restart`` (bit 7) and ``interrupt`` (bit 9) are not modelled.

    Nothing yet *enforces* that this layout tracks Vitis; ``control.h`` is the
    authoritative artifact a conformance test could check against.
    """

    #: User arguments begin after the 16-byte control/interrupt block.
    _auto_start: int = 0x10

    #: Byte offset of the packed control word.
    CTRL_OFFSET: int = 0x00
    #: Bytes reserved for control + interrupt registers before user fields.
    CTRL_BLOCK_BYTES: int = 0x10

    def _auto_align_bytes(self, word_bytes: int) -> int:
        # Vitis places every scalar argument on an 8-byte boundary.
        return 8

    def _auto_stride_bytes(self, nwords: int, word_bytes: int) -> int:
        """Bytes Vitis consumes per scalar argument: the field's data words
        plus one control word, rounded up to the 8-byte argument grid.

        Verified for 32-bit scalars (1 data word + 1 control word = 8 bytes:
        x@0x10/x_ctrl@0x14, a@0x18/a_ctrl@0x1c, …).  The generalization to
        multi-word fields follows the same data+control rule but is *not*
        verified against a local artifact — in particular Vitis maps array
        arguments on s_axilite as a BRAM-backed region, which this does not
        attempt to reproduce.
        """
        raw = (nwords + 1) * word_bytes
        return ((raw + 7) // 8) * 8

    def __init__(self, fields: dict[str, RegField], bitwidth: int = 32) -> None:
        if bitwidth != 32:
            raise ValueError(
                f"VitisRegMap requires bitwidth=32 (got {bitwidth}): the Vitis "
                "s_axilite control interface is a 32-bit bus, and the 0x00/0x04/"
                "0x08/0x0c control block is defined on 4-byte words."
            )
        for name, f in fields.items():
            if name.startswith("ap_"):
                raise ValueError(
                    f"Field name '{name}' begins with reserved prefix 'ap_'."
                )
            if f.offset is not None and f.offset < self.CTRL_BLOCK_BYTES:
                raise ValueError(
                    f"Field '{name}' specifies offset=0x{f.offset:02x}, which "
                    f"falls inside the reserved Vitis control block "
                    f"(0x00-0x{self.CTRL_BLOCK_BYTES - 1:02x}: ap_ctrl word, GIER, "
                    "IER, ISR). User fields start at 0x10."
                )
        ctrl: dict[str, RegField] = {
            "ap_start": RegField(
                Bit,
                RegAccess.W1S,
                offset=0x00,
                bit_offset=0,
                description="Start the kernel (Vitis ap_ctrl_hs; hardware is RW/COH)",
                is_vitis_auto=True,
            ),
            "ap_done": RegField(
                Bit,
                RegAccess.R,
                offset=0x00,
                bit_offset=1,
                description=(
                    "Set by the slave when on_start returns; cleared on ap_start "
                    "(hardware is R/COR)"
                ),
                is_vitis_auto=True,
            ),
            "ap_idle": RegField(
                Bit,
                RegAccess.R,
                offset=0x00,
                bit_offset=2,
                description="1 while no on_start invocation is in flight",
                is_vitis_auto=True,
            ),
            "ap_ready": RegField(
                Bit,
                RegAccess.R,
                offset=0x00,
                bit_offset=3,
                description=(
                    "Set alongside ap_done when on_start returns (hardware is "
                    "R/COR, and pulses independently on a pipelined kernel)"
                ),
                is_vitis_auto=True,
            ),
            "gier": RegField(
                Uint32Word,
                RegAccess.RW,
                offset=0x04,
                description="Global Interrupt Enable Register — storage only",
                is_vitis_auto=True,
            ),
            "ier": RegField(
                Uint32Word,
                RegAccess.RW,
                offset=0x08,
                description="IP Interrupt Enable Register — storage only",
                is_vitis_auto=True,
            ),
            "isr": RegField(
                Uint32Word,
                RegAccess.RW,
                offset=0x0C,
                description="IP Interrupt Status Register — storage only",
                is_vitis_auto=True,
            ),
        }
        super().__init__({**ctrl, **fields}, bitwidth=bitwidth)
        # ap_idle reads 1 out of reset: nothing is running yet.
        self.set("ap_idle", 1)

    def start(self, master: MMIFMaster, base_addr: int = 0) -> ProcessGen[None]:
        """Convenience host-side launch: write 1 to ``ap_start``."""
        yield from self.bind_master(master, base_addr).set("ap_start", 1)


# ---------------------------------------------------------------------------
# VitisRegMapMMIFSlave — owns the Vitis kernel launch lifecycle
# ---------------------------------------------------------------------------


@dataclass
class VitisRegMapMMIFSlave(RegMapMMIFSlave):
    """RegMapMMIFSlave that invokes an ``on_start`` generator on ap_start writes.

    When the host writes 1 to ``ap_start``:

    1. If ``on_start`` is already running, the write is silently ignored
       (mirrors Vitis ap_ctrl_hs gating by ap_idle).  The W1S auto-clear
       of ap_start still fires.
    2. Otherwise the slave spawns ``env.process(on_start())``, clears the
       ``ap_done`` / ``ap_ready`` bits of 0x00 and drops ``ap_idle``.
    3. When ``on_start`` returns, the slave raises ``ap_done`` / ``ap_ready``
       / ``ap_idle`` again.
    """

    regmap:   VitisRegMap                           = field(kw_only=True)
    on_start: Callable[[], ProcessGen[None]] | None = field(default=None, kw_only=True)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        ap_field = self.regmap._fields["ap_start"]
        if ap_field.on_write is not None:
            raise ValueError(
                "ap_start on_write hook is reserved for VitisRegMapMMIFSlave."
            )
        self._busy: bool = False
        ap_field.on_write = self._on_ap_start
        super().__post_init__()

    def _on_ap_start(self, name: str, sub_word: int, value: int) -> None:
        """Hook installed on ap_start; spawns _launch() if not busy.

        Clears the ``ap_done`` / ``ap_ready`` completion bits before launching
        so a subsequent host poll cannot see a stale completion from the
        previous transaction.  (Hardware clears them on read instead — see
        :class:`VitisRegMap`.)
        """
        if self._busy:
            return
        self.regmap.set("ap_done", 0)
        self.regmap.set("ap_ready", 0)
        self.regmap.set("ap_idle", 0)
        self._busy = True
        self.env.process(self._launch())

    def _launch(self) -> ProcessGen[None]:
        """Runs on_start() and raises the completion bits when it returns."""
        try:
            if self.on_start is not None:
                result = self.on_start()
                if result is not None:
                    yield from result
        finally:
            self.regmap.set("ap_done", 1)
            self.regmap.set("ap_ready", 1)
            self.regmap.set("ap_idle", 1)
            self._busy = False
