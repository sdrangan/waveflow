"""DataUnion: schema registry, schema-ID field, length field, and variable-schema burst."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

import numpy as np

from waveflow.build.build import BuildConfig
from waveflow.hw.dataschema import DataList, DataSchema, IntField, Words


class SchemaRegistry:
    """Bidirectional mapping between integer IDs and DataSchema subclasses.

    Each registry has a *name* used as a C++ identifier prefix in code generation.
    There is no global singleton; every design creates its own registry.
    """

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("SchemaRegistry name must be a non-empty string.")
        self.name = name
        self._id_to_cls: dict[int, type[DataSchema]] = {}
        self._cls_to_id: dict[type[DataSchema], int] = {}

    def register(self, schema_cls: type[DataSchema], schema_id: int) -> None:
        """Register *schema_cls* under *schema_id*. Raises ValueError on collision."""
        if not isinstance(schema_id, int) or schema_id < 0:
            raise ValueError(f"schema_id must be a non-negative int; got {schema_id!r}.")
        if schema_id in self._id_to_cls:
            raise ValueError(
                f"ID {schema_id} is already registered to "
                f"{self._id_to_cls[schema_id].__name__} in registry '{self.name}'."
            )
        if schema_cls in self._cls_to_id:
            raise ValueError(
                f"Class {schema_cls.__name__} is already registered with "
                f"ID {self._cls_to_id[schema_cls]} in registry '{self.name}'."
            )
        self._id_to_cls[schema_id] = schema_cls
        self._cls_to_id[schema_cls] = schema_id

    def get_id(self, schema_cls: type[DataSchema]) -> int:
        """Return the integer ID for *schema_cls*. Raises KeyError if not registered."""
        if schema_cls not in self._cls_to_id:
            raise KeyError(
                f"Class {schema_cls.__name__} is not registered in registry '{self.name}'."
            )
        return self._cls_to_id[schema_cls]

    def get_class(self, schema_id: int) -> type[DataSchema]:
        """Return the DataSchema subclass for *schema_id*. Raises KeyError if not registered."""
        if schema_id not in self._id_to_cls:
            raise KeyError(
                f"ID {schema_id} is not registered in registry '{self.name}'."
            )
        return self._id_to_cls[schema_id]

    def contains_id(self, schema_id: int) -> bool:
        """Return True if *schema_id* is registered."""
        return schema_id in self._id_to_cls

    def contains_class(self, schema_cls: type[DataSchema]) -> bool:
        """Return True if *schema_cls* is registered."""
        return schema_cls in self._cls_to_id

    @property
    def registered_ids(self) -> frozenset[int]:
        """Return the set of registered integer IDs."""
        return frozenset(self._id_to_cls)

    def items(self) -> Iterable[tuple[int, type[DataSchema]]]:
        """Return (id, schema_cls) pairs sorted by ID, for deterministic codegen."""
        return sorted(self._id_to_cls.items())

    def next_id(self) -> int:
        """Return the next auto-assigned ID: ``max(registered) + 1``, or 0 if empty."""
        return max(self._id_to_cls) + 1 if self._id_to_cls else 0


def register_schema(schema_id: int | None = None, *, registry: SchemaRegistry):
    """Class decorator: register a DataSchema subclass in *registry*.

    Parameters
    ----------
    schema_id : int | None, optional
        Integer ID to assign. If omitted, the registry assigns the next
        available ID (``max(registered) + 1``, or 0 if the registry is empty).
    registry : SchemaRegistry
        Target registry. Keyword-only.

    Example::

        reg = SchemaRegistry("MySys")

        @register_schema(schema_id=1, registry=reg)
        class SensorPacket(DataList):
            elements = {"x": IntField.specialize(8, signed=False)}

        @register_schema(registry=reg)   # auto-assigned: 2
        class ControlWord(DataList):
            elements = {"opcode": IntField.specialize(8, signed=False)}
    """
    def decorator(cls: type[DataSchema]) -> type[DataSchema]:
        sid = schema_id if schema_id is not None else registry.next_id()
        registry.register(cls, sid)
        return cls
    return decorator


class SchemaIDField(IntField):
    """Unsigned integer field whose valid values are restricted to a SchemaRegistry.

    The base class is not for direct use in schema definitions. Call
    ``SchemaIDField.specialize(registry, bitwidth)`` to obtain a concrete subclass
    bound to a specific registry. Registered IDs are validated on every assignment
    and on deserialization.
    """

    registry: ClassVar[SchemaRegistry | None] = None
    signed: ClassVar[bool] = False
    can_gen_include: ClassVar[bool] = False
    _specializations: ClassVar[dict[tuple[Any, ...], type[SchemaIDField]]] = {}

    @classmethod
    def specialize(  # type: ignore[override]
        cls,
        registry: SchemaRegistry,
        bitwidth: int = 16,
        **kwargs: Any,
    ) -> type[SchemaIDField]:
        """Return a cached SchemaIDField subclass bound to *registry*.

        Parameters
        ----------
        registry : SchemaRegistry
            Registry that defines the valid schema IDs. Required.
        bitwidth : int, default=16
            Storage width in bits. Must be positive.
        **kwargs :
            Optional overrides (``include_dir``, ``include_filename``, ``cpp_repr``).
        """
        if not isinstance(registry, SchemaRegistry):
            raise TypeError("registry must be a SchemaRegistry instance.")
        if bitwidth <= 0:
            raise ValueError("bitwidth must be positive.")

        overrides = cls.validate_specialize_kwargs(kwargs)
        override_items = tuple(sorted(overrides.items()))
        key = (cls, id(registry), int(bitwidth), override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        enum_cpp_name = f"{registry.name}SchemaID"
        subclass_name = f"{registry.name}SchemaIDField"
        specialized_attrs = cls.merge_specialize_attrs(
            {
                "registry": registry,
                "bitwidth": int(bitwidth),
                "signed": False,
                "cpp_type": enum_cpp_name,
                "can_gen_include": True,
                "include_filename": f"{registry.name.lower()}_schema_id.h",
                "__module__": cls.__module__,
                "__doc__": (
                    f"SchemaIDField for registry '{registry.name}', bitwidth={bitwidth}."
                ),
            },
            overrides,
        )
        specialized = type(subclass_name, (cls,), specialized_attrs)
        cls._specializations[key] = specialized
        return specialized

    def _convert(self, value: Any) -> Any:
        if isinstance(value, (float, np.floating)) and not float(value).is_integer():
            raise ValueError(
                f"Cannot assign non-integer value {value!r} to {self.__class__.__name__}."
            )
        raw = int(value)
        bitwidth = self.__class__.get_bitwidth()
        if raw < 0:
            raise ValueError(
                f"{self.__class__.__name__} does not accept negative values; got {raw}."
            )
        max_val = (1 << bitwidth) - 1
        if raw > max_val:
            raise ValueError(
                f"Value {raw} exceeds maximum {max_val} for "
                f"{self.__class__.__name__} with bitwidth={bitwidth}."
            )
        result = np.uint32(raw) if bitwidth <= 32 else np.uint64(raw)
        registry = self.__class__.registry
        if registry is not None and not registry.contains_id(raw):
            raise ValueError(
                f"Value {raw!r} is not a registered schema ID "
                f"in registry '{registry.name}'."
            )
        return result

    # -- C++ conversions -------------------------------------------------------------------
    #
    # The Python side of this field is an integer, so it inherits IntField -- but the C++ side
    # is a *scoped* enum (`enum class`, see _gen_include_decl below), and a scoped enum has no
    # implicit conversion to or from an integer.  IntField's identity `to_uint_expr` therefore
    # emits `res.range(hi, lo) = data.schema_id;` (no viable operator=), and DataField's C-style
    # `from_uint_expr` emits `(SensorSchemaID)(packed.range(hi, lo))` (no conversion from
    # ap_range_ref to a scoped enum).  Both are hard compile errors in HLS.
    #
    # Route through `unsigned int` in each direction, matching EnumField -- the other field whose
    # C++ representation is a scoped enum -- so the two stay consistent.

    @classmethod
    def to_uint_expr(cls, value_expr: str) -> str:
        return f"(ap_uint<{cls.get_bitwidth()}>)(static_cast<unsigned int>({value_expr}))"

    @classmethod
    def to_uint_value_expr(cls, value_expr: str) -> str:
        return cls.to_uint_expr(value_expr)

    @classmethod
    def from_uint_expr(cls, uint_expr: str) -> str:
        return f"static_cast<{cls.cpp_class_name()}>(static_cast<unsigned int>({uint_expr}))"

    @classmethod
    def _gen_include_decl(cls, word_bw_supported: list[int] | None = None, framed: bool = False) -> str:
        registry = cls.registry
        if registry is None:
            raise TypeError(
                f"{cls.__name__} does not have an associated registry; "
                "call SchemaIDField.specialize(registry, bitwidth) first."
            )
        _ = (word_bw_supported, framed)
        bitwidth = cls.get_bitwidth()
        enum_name = cls.cpp_class_name()
        lines = [f"enum class {enum_name} : uint{bitwidth}_t {{"]
        for schema_id, schema_cls in registry.items():
            lines.append(f"    {schema_cls.__name__} = {schema_id},")
        lines.append("};")
        return "\n".join(lines)


class LengthField(IntField):
    """Unsigned integer field for word counts or byte lengths.

    Unlike IntField, negative values and values exceeding the bitwidth raise
    ValueError instead of being silently masked.
    """

    signed: ClassVar[bool] = False
    _specializations: ClassVar[dict[tuple[Any, ...], type[LengthField]]] = {}

    @classmethod
    def specialize(  # type: ignore[override]
        cls,
        bitwidth: int = 16,
        **kwargs: Any,
    ) -> type[LengthField]:
        """Return a cached LengthField subclass.

        Parameters
        ----------
        bitwidth : int, default=16
            Storage width in bits. Must be positive.
        **kwargs :
            Optional overrides (``include_dir``, ``include_filename``, ``cpp_repr``).
        """
        if bitwidth <= 0:
            raise ValueError("bitwidth must be positive.")

        overrides = cls.validate_specialize_kwargs(kwargs)
        override_items = tuple(sorted(overrides.items()))
        key = (cls, int(bitwidth), override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        cpp_type = f"ap_uint<{bitwidth}>"
        subclass_name = f"Length{bitwidth}"
        specialized_attrs = cls.merge_specialize_attrs(
            {
                "bitwidth": int(bitwidth),
                "signed": False,
                "cpp_type": cpp_type,
                "__module__": cls.__module__,
                "__doc__": f"LengthField with bitwidth={bitwidth}.",
            },
            overrides,
        )
        specialized = type(subclass_name, (cls,), specialized_attrs)
        cls._specializations[key] = specialized
        return specialized

    def _convert(self, value: Any) -> Any:
        if isinstance(value, (float, np.floating)) and not float(value).is_integer():
            raise ValueError(
                f"Cannot assign non-integer value {value!r} to {self.__class__.__name__}."
            )
        raw = int(value)
        if raw < 0:
            raise ValueError(
                f"{self.__class__.__name__} does not accept negative values; got {raw}."
            )
        bitwidth = self.__class__.get_bitwidth()
        max_val = (1 << bitwidth) - 1
        if raw > max_val:
            raise ValueError(
                f"Value {raw} exceeds maximum {max_val} for "
                f"{self.__class__.__name__} with bitwidth={bitwidth}."
            )
        return np.uint32(raw) if bitwidth <= 32 else np.uint64(raw)


class DataUnionHdr(DataList):
    """DataList header carrying a schema_id and a payload word count (nwords).

    The base class has empty elements and is not for direct use in schema
    definitions. Call ``DataUnionHdr.specialize(schema_id_type)`` to produce a
    concrete header class. An optional ``length_id_type`` adds a ``nwords``
    field to the header for protocols that carry explicit payload length.

    For fixed-size schemas the ``nwords`` field is redundant because the
    receiver can compute ``schema_cls.nwords_per_inst(word_bw)`` directly from
    the registry. Include ``nwords`` only when the payload length cannot be
    derived from the schema class alone (dynamic schemas, streaming fragments,
    forward-compatibility with unknown schema IDs).

    Example::

        SensorSchemaID = SchemaIDField.specialize(registry=my_reg, bitwidth=16)

        # schema-ID only (recommended for fixed-size schemas)
        PacketHeader = DataUnionHdr.specialize(SensorSchemaID)

        # with explicit payload length
        Length16 = LengthField.specialize(bitwidth=16)
        PacketHeaderWithLen = DataUnionHdr.specialize(SensorSchemaID, Length16)
    """

    elements: ClassVar[dict] = {}
    schema_id_type: ClassVar[type[SchemaIDField] | None] = None
    length_id_type: ClassVar[type[LengthField] | None] = None
    _specializations: ClassVar[dict[tuple[Any, ...], type[DataUnionHdr]]] = {}

    @classmethod
    def specialize(
        cls,
        schema_id_type: type[SchemaIDField],
        length_id_type: type[LengthField] | None = None,
    ) -> type[DataUnionHdr]:
        """Return a cached DataUnionHdr subclass.

        Parameters
        ----------
        schema_id_type : type[SchemaIDField]
            A SchemaIDField subclass from ``SchemaIDField.specialize(registry, bitwidth)``.
        length_id_type : type[LengthField] | None, optional
            A LengthField subclass from ``LengthField.specialize(bitwidth)``. When
            provided, the header gains a ``nwords`` field carrying the payload word
            count. When omitted (default), the header contains only ``schema_id``.
        """
        if not (isinstance(schema_id_type, type) and issubclass(schema_id_type, SchemaIDField)):
            raise TypeError("schema_id_type must be a SchemaIDField subclass.")
        if length_id_type is not None and not (
            isinstance(length_id_type, type) and issubclass(length_id_type, LengthField)
        ):
            raise TypeError("length_id_type must be a LengthField subclass or None.")

        key = (cls, schema_id_type, length_id_type)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        registry = getattr(schema_id_type, "registry", None)
        prefix = registry.name if registry is not None else "DataUnion"
        subclass_name = f"{prefix}Hdr"

        elements: dict[str, Any] = {"schema_id": schema_id_type}
        if length_id_type is not None:
            elements["nwords"] = length_id_type

        doc = f"DataUnionHdr with {schema_id_type.__name__} (schema_id)"
        if length_id_type is not None:
            doc += f" and {length_id_type.__name__} (nwords)"
        doc += "."

        specialized_attrs = {
            "elements": elements,
            "schema_id_type": schema_id_type,
            "length_id_type": length_id_type,
            "__module__": cls.__module__,
            "__doc__": doc,
        }
        specialized = type(subclass_name, (cls,), specialized_attrs)
        cls._specializations[key] = specialized
        return specialized


class DataUnion(DataSchema):
    """One message of several types: a header carrying the payload's ``schema_id``, then the payload.

    A :class:`~waveflow.hw.dataschema.DataSchema` like any other, so it is a field of a ``DataList``,
    a ``DataSchemaStep`` generates its header, and the generated struct has every reader and writer
    -- array, stream, AXI4-Stream and framed.  The base class is not for direct use: call
    ``DataUnion.specialize(hdr_type)``.

    **Fixed length.**  The header comes first, in its own layout; the payload starts at the next word
    boundary, in the registered type's own word layout, zero-padded to the longest registered payload.
    So every instance is ``hdr_nwords + max_payload_nwords`` words at a given width, whatever its type
    (``docs/guide/schema/python/dataunion.md``).  A variable number of messages of different lengths is
    a tagged union on a stream instead (``tagged_union.md``).

    In C++ the struct holds the header and one member per registered type, named after the type in
    snake case; ``get_<Type>()`` / ``set_<Type>(p)`` read one and set it with its ``schema_id``.

    Example::

        SensorSchemaID = SchemaIDField.specialize(registry=sensor_reg, bitwidth=16)
        PacketHdr = DataUnionHdr.specialize(schema_id_type=SensorSchemaID)
        SensorDataUnion = DataUnion.specialize(hdr_type=PacketHdr)

        du = SensorDataUnion(TempPacket(temp_raw=-42, sensor_id=7))
        words = du.serialize(word_bw=32)          # hdr_words + padded_payload_words

        rx = SensorDataUnion().deserialize(words, word_bw=32)
        print(rx.schema_id, int(rx.payload.temp_raw))  # 1, -42
    """

    hdr_type: ClassVar[type[DataUnionHdr] | None] = None
    registry: ClassVar[SchemaRegistry | None] = None
    include_filename: ClassVar[str | None] = None
    can_gen_include: ClassVar[bool] = False
    _specializations: ClassVar[dict[Any, type[DataUnion]]] = {}

    def __init__(self, payload: DataList | None = None) -> None:
        hdr_type = self.__class__.hdr_type
        if hdr_type is None:
            raise TypeError(
                f"{self.__class__.__name__} is the base DataUnion class; "
                "call DataUnion.specialize(hdr_type) first."
            )
        self._hdr: DataUnionHdr = hdr_type()
        self._payload: DataList | None = None
        if payload is not None:
            self.payload = payload

    # ------------------------------------------------------------------
    # specialize()
    # ------------------------------------------------------------------

    @classmethod
    def specialize(cls, hdr_type: type[DataUnionHdr]) -> type[DataUnion]:
        """Return a cached DataUnion subclass bound to *hdr_type*.

        Parameters
        ----------
        hdr_type : type[DataUnionHdr]
            A specialized DataUnionHdr from ``DataUnionHdr.specialize(schema_id_type)``.
            The registry is derived from ``hdr_type.schema_id_type.registry``.
        """
        if not (isinstance(hdr_type, type) and issubclass(hdr_type, DataUnionHdr)):
            raise TypeError("hdr_type must be a DataUnionHdr subclass.")

        key = (cls, hdr_type)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        sid_type = getattr(hdr_type, "schema_id_type", None)
        registry = getattr(sid_type, "registry", None) if sid_type is not None else None
        if registry is None:
            raise TypeError(
                f"Cannot derive registry from {hdr_type.__name__}: "
                "schema_id_type.registry is None. "
                "Use DataUnionHdr.specialize(SchemaIDField.specialize(registry=..., bitwidth=...))."
            )

        reg_name = registry.name
        subclass_name = f"{reg_name}DataUnion"
        attrs: dict[str, Any] = {
            "hdr_type": hdr_type,
            "registry": registry,
            "include_filename": f"{reg_name.lower()}_dataunion.h",
            "can_gen_include": True,
            "__module__": cls.__module__,
            "__doc__": f"DataUnion for registry '{reg_name}' with {hdr_type.__name__} header.",
        }
        specialized = type(subclass_name, (cls,), attrs)
        cls._specializations[key] = specialized
        return specialized

    # ------------------------------------------------------------------
    # Runtime properties
    # ------------------------------------------------------------------

    @property
    def schema_id(self) -> int:
        """Return the schema_id stored in the header."""
        return int(self._hdr.schema_id)

    @property
    def payload(self) -> DataList | None:
        """Return the current payload instance, or None if not yet set."""
        return self._payload

    @payload.setter
    def payload(self, value: DataList) -> None:
        """Assign a payload and automatically update the header's schema_id."""
        sid = self.__class__.registry.get_id(value.__class__)
        self._hdr.schema_id = sid
        self._payload = value

    @property
    def hdr(self) -> DataUnionHdr:
        """Return the header instance."""
        return self._hdr

    @property
    def val(self) -> dict[str, Any]:
        """The header and the payload as nested dicts (:meth:`to_dict`)."""
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {"header": self._hdr.to_dict(),
                "payload": None if self._payload is None else self._payload.to_dict()}

    def from_dict(self, data: dict[str, Any]) -> DataUnion:
        self._hdr.from_dict(data["header"])
        schema_cls = self.__class__.registry.get_class(int(self._hdr.schema_id))
        payload = data.get("payload")
        self._payload = None if payload is None else schema_cls().from_dict(payload)
        return self

    @classmethod
    def init_value(cls) -> dict[str, Any]:
        return {"header": cls.hdr_type.init_value(), "payload": None}

    # ------------------------------------------------------------------
    # Size helpers
    # ------------------------------------------------------------------

    @classmethod
    def _payload_types(cls) -> list[tuple[int, type[DataSchema]]]:
        return list(cls.registry.items()) if cls.registry is not None else []

    @classmethod
    def max_payload_bw(cls) -> int:
        """Return the maximum payload bitwidth across all registered schemas."""
        return max((s.get_bitwidth() for _, s in cls._payload_types()), default=0)

    @classmethod
    def max_payload_nwords(cls, word_bw: int) -> int:
        """Return the maximum payload word count across all registered schemas."""
        return max((s.nwords_per_inst(word_bw) for _, s in cls._payload_types()), default=0)

    @classmethod
    def get_bitwidth(cls) -> int:
        """The header's bits plus the longest payload's."""
        return cls.hdr_type.get_bitwidth() + cls.max_payload_bw()

    @classmethod
    def _nwords_per_inst_uncached(cls, word_bw: int) -> int:
        return cls.hdr_type.nwords_per_inst(word_bw) + cls.max_payload_nwords(word_bw)

    @classmethod
    def _max_cursor_step(cls, word_bw: int, ipos0: int = 0, iword0: int = 0) -> tuple[int, int]:
        ipos, iword = cls.hdr_type._max_cursor_step(word_bw, ipos0, iword0)
        if ipos > 0:
            iword, ipos = iword + 1, 0
        return 0, iword + cls.max_payload_nwords(word_bw)

    # ------------------------------------------------------------------
    # Serialize / deserialize: the header, then the payload from the next word, padded
    # ------------------------------------------------------------------

    def _serialize_recursive(self, word_bw: int, words: list[int], ipos0: int = 0,
                             iword0: int = 0) -> tuple[int, int]:
        if self._payload is None:
            raise ValueError("Cannot serialize DataUnion: payload is not set.")
        ipos, iword = self._hdr._serialize_recursive(word_bw=word_bw, words=words, ipos0=ipos0,
                                                     iword0=iword0)
        if ipos > 0:
            iword, ipos = iword + 1, 0
        end = iword + self.__class__.max_payload_nwords(word_bw)
        while len(words) < end:
            words.append(0)
        self._payload._serialize_recursive(word_bw=word_bw, words=words, ipos0=0, iword0=iword)
        return 0, end

    def _active_size_recursive(self, word_bw: int, ipos0: int = 0,
                               iword0: int = 0) -> tuple[int, int]:
        return self.__class__._max_cursor_step(word_bw, ipos0, iword0)

    def _deserialize_recursive(self, word_bw: int, words: list[int], ipos0: int = 0,
                               iword0: int = 0) -> tuple[int, int]:
        self._hdr = self.__class__.hdr_type()
        ipos, iword = self._hdr._deserialize_recursive(word_bw=word_bw, words=words, ipos0=ipos0,
                                                       iword0=iword0)
        if ipos > 0:
            iword, ipos = iword + 1, 0
        end = iword + self.__class__.max_payload_nwords(word_bw)
        padded = list(words) + [0] * max(0, end - len(words))
        schema_cls = self.__class__.registry.get_class(int(self._hdr.schema_id))
        self._payload = schema_cls()
        self._payload._deserialize_recursive(word_bw=word_bw, words=padded, ipos0=0, iword0=iword)
        return 0, end

    def is_close(self, other: DataSchema, rel_tol: float | None = None,
                 abs_tol: float | None = 1e-8) -> bool:
        if not isinstance(other, self.__class__) or self.schema_id != other.schema_id:
            return False
        if self._payload is None or other.payload is None:
            return self._payload is other.payload
        return self._payload.is_close(other.payload, rel_tol=rel_tol, abs_tol=abs_tol)

    # ------------------------------------------------------------------
    # C++ code generation
    # ------------------------------------------------------------------

    @classmethod
    def cpp_class_name(cls) -> str:
        """Return the C++ struct name for this DataUnion."""
        return cls.__name__

    @classmethod
    def get_dependencies(cls) -> list[type[DataSchema]]:
        """The header's type, then each registered payload type, by id."""
        return [cls.hdr_type, *(s for _, s in cls._payload_types())]

    @classmethod
    def _member(cls, schema_cls: type[DataSchema]) -> str:
        """The struct member holding a payload of *schema_cls*."""
        return cls._camel_to_snake(schema_cls.__name__)

    @classmethod
    def gen_include(
        cls,
        cfg: BuildConfig | None = None,
        word_bw_supported: list[int] | None = None,
        streamutils_dir: Path | str | None = None,
    ) -> Path:
        """Generate the DataUnion's C++ header and return its path: a ``DataSchemaStep`` run alone
        (in a build DAG, add a ``DataSchemaStep`` instead).  *streamutils_dir* is where
        ``streamutils_hls.h`` is, relative to ``cfg.root_dir`` (default: the root)."""
        from waveflow.hw.dataschema import DataSchemaStep

        cfg = cfg if cfg is not None else BuildConfig()
        step = DataSchemaStep(cls, list(word_bw_supported or []))
        if streamutils_dir is not None:
            step._streamutils_output_dir = Path(streamutils_dir)
        res = step.run(cfg)
        if not res.success:
            raise RuntimeError(res.message)
        return cfg.root_dir / cls.include_path()

    @classmethod
    def _gen_include_decl(cls, word_bw_supported: list[int] | None = None,
                          framed: bool = False) -> str:
        """The struct: the header, one member per registered type, accessors, the word counts, and
        every reader and writer the base generates through :meth:`_gen_write_recursive` /
        :meth:`_gen_read_recursive`."""
        word_bw_supported = list(word_bw_supported or [])
        hdr_cpp = cls.hdr_type.cpp_class_name()
        sid_cpp = cls.hdr_type.schema_id_type.cpp_class_name()
        lines = [f"struct {cls.cpp_class_name()} {{", f"    {hdr_cpp} header;"]
        for _, s in cls._payload_types():
            lines.append(f"    {s.cpp_class_name()} {cls._member(s)};")
        lines.append("")
        lines.append(f"    static constexpr int bitwidth = {cls.get_bitwidth()};")
        lines.append("")
        for _, s in cls._payload_types():
            n, m = s.cpp_class_name(), cls._member(s)
            lines.extend([
                f"    {n} get_{s.__name__}() const {{ return {m}; }}",
                f"    void set_{s.__name__}(const {n}& p) {{",
                f"        header.schema_id = {sid_cpp}::{s.__name__};",
                f"        {m} = p;",
                "    }",
            ])
        if word_bw_supported:
            lines.extend([
                "",
                "    template<int word_bw>",
                "    struct word_bw_tag {};",
                "",
                "    template<int word_bw>",
                "    static constexpr int nwords_value(word_bw_tag<word_bw>) {",
                '        static_assert(word_bw < 0, "Unsupported word_bw for nwords");',
                "        return 0;",
                "    }",
            ])
            for bw in word_bw_supported:
                lines.extend([
                    "",
                    f"    static constexpr int nwords_value(word_bw_tag<{bw}>) {{",
                    f"        return {cls.nwords_per_inst(bw)};",
                    "    }",
                ])
            lines.extend([
                "",
                "    template<int word_bw>",
                "    static constexpr int nwords() {",
                "        return nwords_value(word_bw_tag<word_bw>{});",
                "    }",
            ])
            kinds = ("array", "stream", "axi4_stream") + (("framed_stream",) if framed else ())
            for kind in kinds:
                lines.append("")
                lines.extend(cls.gen_write(dst_type=kind, word_bw_supported=word_bw_supported,
                                           indent_level=1).splitlines())
            for kind in kinds:
                lines.append("")
                lines.extend(cls.gen_read(src_type=kind, word_bw_supported=word_bw_supported,
                                          indent_level=1).splitlines())
        lines.append("};")
        return "\n".join(lines)

    @classmethod
    def _gen_write_recursive(cls, word_bw: int, dst_type: str = "array", target: str = "x",
                             ipos0: int = 0, iword0: int = 0, prefix: str = "",
                             member_name: str | None = None) -> tuple[list[str], int, int]:
        """The header by its own generator; the rest of its word flushed; then the payload, written by
        its type's ``write_array`` into a local word array (one case per type) and sent word by word,
        padded to the longest payload."""
        from waveflow.hw.dataschema import _TLAST_MARKS, _array_beat_last

        p = f"{prefix}{member_name}." if member_name else prefix
        lines, ipos, iword = cls.hdr_type._gen_write_recursive(
            word_bw=word_bw, dst_type=dst_type, target=target, ipos0=ipos0, iword0=iword0,
            prefix=f"{p}header.", member_name=None)
        if ipos > 0:
            if dst_type == "stream":
                lines += [f"    {target}.write(w);", "    w = 0;"]
            elif dst_type == "axi4_stream":
                lines += [f"    streamutils::write_axi4_word<{word_bw}>({target}, w, false);",
                          "    w = 0;"]
            iword, ipos = iword + 1, 0
        n = cls.max_payload_nwords(word_bw)
        if n == 0:
            return lines, 0, iword
        sid_cpp = cls.hdr_type.schema_id_type.cpp_class_name()
        lines += [
            "    {",
            f"        ap_uint<{word_bw}> pw[{n}];",
            f"        for (int k = 0; k < {n}; ++k) pw[k] = 0;",
            f"        switch ({p}header.schema_id) {{",
        ]
        for _, s in cls._payload_types():
            lines.append(f"        case {sid_cpp}::{s.__name__}: "
                         f"{p}{cls._member(s)}.write_array<{word_bw}>(pw); break;")
        lines += ["        default: break;", "        }"]
        if dst_type == "array":
            lines.append(f"        for (int k = 0; k < {n}; ++k) {target}[{iword} + k] = pw[k];")
        elif dst_type == "stream":
            lines.append(f"        for (int k = 0; k < {n}; ++k) {target}.write(pw[k]);")
        else:
            marks = _TLAST_MARKS.get()
            aid = next(marks) if marks is not None else None
            last = _array_beat_last(aid, f"k == {n - 1}")
            lines.append(f"        for (int k = 0; k < {n}; ++k) "
                         f"streamutils::write_axi4_word<{word_bw}>({target}, pw[k], {last});")
        lines.append("    }")
        return lines, 0, iword + n

    @classmethod
    def _gen_read_recursive(cls, word_bw: int, src_type: str = "array", source: str = "x",
                            ipos0: int = 0, iword0: int = 0, prefix: str = "",
                            member_name: str | None = None) -> tuple[list[str], int, int]:
        """The mirror of :meth:`_gen_write_recursive`: the header, then the padded payload words into
        a local array, read by the type ``schema_id`` names."""
        p = f"{prefix}{member_name}." if member_name else prefix
        lines, ipos, iword = cls.hdr_type._gen_read_recursive(
            word_bw=word_bw, src_type=src_type, source=source, ipos0=ipos0, iword0=iword0,
            prefix=f"{p}header.", member_name=None)
        if ipos > 0:
            iword, ipos = iword + 1, 0
        n = cls.max_payload_nwords(word_bw)
        if n == 0:
            return lines, 0, iword
        sid_cpp = cls.hdr_type.schema_id_type.cpp_class_name()
        lines += ["    {", f"        ap_uint<{word_bw}> pw[{n}];"]
        if src_type == "array":
            lines.append(f"        for (int k = 0; k < {n}; ++k) pw[k] = {source}[{iword} + k];")
        elif src_type == "stream":
            lines.append(f"        for (int k = 0; k < {n}; ++k) pw[k] = {source}.read();")
        else:
            lines += [
                f"        for (int k = 0; k < {n}; ++k) {{",
                "            if (last) {",
                "                tl = streamutils::tlast_status::tlast_early;",
                "                return;",
                "            }",
                f"            auto axis_word = {source}.read();",
                "            pw[k] = axis_word.data;",
                "            last = axis_word.last;",
                "        }",
            ]
        lines.append(f"        switch ({p}header.schema_id) {{")
        for _, s in cls._payload_types():
            lines.append(f"        case {sid_cpp}::{s.__name__}: "
                         f"{p}{cls._member(s)}.read_array<{word_bw}>(pw); break;")
        lines += ["        default: break;", "        }", "    }"]
        return lines, 0, iword + n
