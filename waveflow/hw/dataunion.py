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


class DataUnion:
    """Variable-schema burst: a fixed header combined with one of N registered payload types.

    The base class is not for direct use. Call ``DataUnion.specialize(hdr_type)`` to obtain
    a concrete subclass bound to a specific ``DataUnionHdr``. Every serialized instance has
    the same total word count: ``hdr_nwords + max_payload_nwords``. Payloads shorter than
    the maximum are zero-padded on serialization so all instances have identical wire footprint.

    Example::

        SensorSchemaID = SchemaIDField.specialize(registry=sensor_reg, bitwidth=16)
        PacketHdr = DataUnionHdr.specialize(schema_id_type=SensorSchemaID)
        SensorDataUnion = DataUnion.specialize(hdr_type=PacketHdr)

        du = SensorDataUnion()
        du.payload = TempPacket(temp_raw=-42, sensor_id=7)
        words = du.serialize(word_bw=32)          # hdr_words + padded_payload_words

        rx = SensorDataUnion().deserialize(words, word_bw=32)
        print(rx.schema_id, int(rx.payload.temp_raw))  # 1, -42
    """

    hdr_type: ClassVar[type[DataUnionHdr] | None] = None
    registry: ClassVar[SchemaRegistry | None] = None
    include_filename: ClassVar[str] = ""
    include_dir: ClassVar[str | None] = None
    _specializations: ClassVar[dict[Any, type[DataUnion]]] = {}

    def __init__(self) -> None:
        hdr_type = self.__class__.hdr_type
        if hdr_type is None:
            raise TypeError(
                f"{self.__class__.__name__} is the base DataUnion class; "
                "call DataUnion.specialize(hdr_type) first."
            )
        self._hdr: DataUnionHdr = hdr_type()
        self._payload: DataList | None = None

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

    # ------------------------------------------------------------------
    # Size helpers
    # ------------------------------------------------------------------

    @classmethod
    def max_payload_bw(cls) -> int:
        """Return the maximum payload bitwidth across all registered schemas."""
        if not cls.registry._id_to_cls:
            return 0
        return max(s.get_bitwidth() for _, s in cls.registry.items())

    @classmethod
    def max_payload_nwords(cls, word_bw: int) -> int:
        """Return the maximum payload word count across all registered schemas."""
        if not cls.registry._id_to_cls:
            return 0
        return max(s.nwords_per_inst(word_bw) for _, s in cls.registry.items())

    @classmethod
    def nwords_per_inst(cls, word_bw: int) -> int:
        """Return the total serialized word count (header + max payload)."""
        return cls.hdr_type.nwords_per_inst(word_bw) + cls.max_payload_nwords(word_bw)

    # ------------------------------------------------------------------
    # Serialize / deserialize
    # ------------------------------------------------------------------

    def serialize(self, word_bw: int = 32) -> Words:
        """Serialize into a fixed-length word array (header + zero-padded payload).

        The total word count is always ``nwords_per_inst(word_bw)`` regardless
        of which payload type is stored.
        """
        if self._payload is None:
            raise ValueError("Cannot serialize DataUnion: payload is not set.")
        hdr_words = self._hdr.serialize(word_bw=word_bw)
        payload_words = self._payload.serialize(word_bw=word_bw)
        max_n = self.__class__.max_payload_nwords(word_bw)
        if len(payload_words) < max_n:
            pad = np.zeros(max_n - len(payload_words), dtype=payload_words.dtype)
            payload_words = np.concatenate([payload_words, pad])
        return np.concatenate([hdr_words, payload_words])

    def deserialize(self, words: Words, word_bw: int = 32) -> DataUnion:
        """Populate this DataUnion from a flat word array and return self."""
        hdr_type = self.__class__.hdr_type
        hdr_n = hdr_type.nwords_per_inst(word_bw)
        self._hdr = hdr_type().deserialize(words[:hdr_n], word_bw=word_bw)
        schema_id = int(self._hdr.schema_id)
        schema_cls = self.__class__.registry.get_class(schema_id)
        payload_n = schema_cls.nwords_per_inst(word_bw)
        self._payload = schema_cls().deserialize(words[hdr_n:hdr_n + payload_n], word_bw=word_bw)
        return self

    # ------------------------------------------------------------------
    # C++ code generation
    # ------------------------------------------------------------------

    @classmethod
    def cpp_class_name(cls) -> str:
        """Return the C++ struct name for this DataUnion."""
        return cls.__name__

    @classmethod
    def include_path(cls) -> str:
        """Return the header path relative to the code-generation root."""
        inc_dir = (cls.include_dir or ".").replace("\\", "/")
        inc_root = PurePosixPath(inc_dir)
        filename = cls.include_filename or f"{cls.__name__.lower()}_dataunion.h"
        if inc_root.as_posix() == ".":
            return filename
        return f"{inc_root.as_posix()}/{filename}"

    @classmethod
    def gen_include(
        cls,
        cfg: BuildConfig | None = None,
        word_bw_supported: list[int] | None = None,
        streamutils_dir: Path | str | None = None,
    ) -> Path:
        """Generate the DataUnion C++ header file and return its path.

        Parameters
        ----------
        cfg : BuildConfig | None
            Code-generation config. Uses current directory when omitted.
        word_bw_supported : list[int] | None
            Word widths to support in the generated read/write helpers. When
            omitted or empty, size helpers and read/write methods are not emitted.
        streamutils_dir : Path | str | None
            Directory containing ``streamutils_hls.h``, **relative to**
            ``cfg.root_dir``.  Defaults to ``"."`` (the build root itself).
        """
        if cfg is None:
            cfg = BuildConfig()
        if word_bw_supported is None:
            word_bw_supported = []

        sutils_dir = Path(streamutils_dir) if streamutils_dir is not None else Path(".")

        out_path = cfg.root_dir / cls.include_path()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        streamutils_hls_path = cfg.root_dir / sutils_dir / "streamutils_hls.h"
        streamutils_hls_include = os.path.relpath(
            streamutils_hls_path, start=out_path.parent
        ).replace("\\", "/")

        hdr_type = cls.hdr_type
        hdr_abs = cfg.root_dir / hdr_type.include_path()
        hdr_rel = os.path.relpath(hdr_abs, start=out_path.parent).replace("\\", "/")

        schema_rels: list[str] = []
        for _, schema_cls in cls.registry.items():
            schema_abs = cfg.root_dir / schema_cls.include_path()
            schema_rels.append(
                os.path.relpath(schema_abs, start=out_path.parent).replace("\\", "/")
            )

        guard_raw = re.sub(r"[^A-Za-z0-9]+", "_", cls.include_path()).strip("_").upper()
        guard = re.sub(r"_+", "_", guard_raw)

        lines: list[str] = [
            f"#ifndef {guard}",
            f"#define {guard}",
            "",
            "#include <ap_int.h>",
            "#include <hls_stream.h>",
            "#if __has_include(<hls_axi_stream.h>)",
            "#include <hls_axi_stream.h>",
            "#else",
            "#include <ap_axi_sdata.h>",
            "#endif",
            f'#include "{streamutils_hls_include}"',
            "",
            f'#include "{hdr_rel}"',
        ]
        for rel in schema_rels:
            lines.append(f'#include "{rel}"')
        lines.extend([
            "",
            cls._gen_include_decl(word_bw_supported=word_bw_supported),
            "",
            f"#endif // {guard}",
        ])

        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out_path

    @classmethod
    def _gen_include_decl(cls, word_bw_supported: list[int] | None = None, framed: bool = False) -> str:
        """Return the C++ struct body for this DataUnion."""
        if word_bw_supported is None:
            word_bw_supported = []

        hdr_type = cls.hdr_type
        registry = cls.registry
        struct_name = cls.cpp_class_name()
        hdr_cpp = hdr_type.cpp_class_name()

        mpbw = cls.max_payload_bw()
        if mpbw == 0:
            mpbw = 1

        lines: list[str] = [f"struct {struct_name} {{"]
        lines.append(f"    {hdr_cpp} header;")
        lines.append(f"    ap_uint<{mpbw}> payload_bits;")
        lines.append("")
        lines.append(f"    static constexpr int max_payload_bw = {mpbw};")
        lines.append(f"    static constexpr int hdr_bw = {hdr_type.get_bitwidth()};")
        lines.append(f"    static constexpr int bitwidth = hdr_bw + max_payload_bw;")

        if word_bw_supported:
            # nwords() template
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
                n = cls.nwords_per_inst(bw)
                lines.extend([
                    "",
                    f"    static constexpr int nwords_value(word_bw_tag<{bw}>) {{",
                    f"        return {n};",
                    "    }",
                ])
            lines.extend([
                "",
                "    template<int word_bw>",
                "    static constexpr int nwords() {",
                "        return nwords_value(word_bw_tag<word_bw>{});",
                "    }",
            ])

            # write_array — fallback template + per-bw overloads + wrapper
            lines.extend([
                "",
                "    template<int word_bw>",
                f"    static void write_array_impl(word_bw_tag<word_bw>, const {struct_name}* self, ap_uint<word_bw> x[]) {{",
                '        static_assert(word_bw < 0, "Unsupported word_bw for write_array");',
                "        (void)self; (void)x;",
                "    }",
            ])
            for bw in word_bw_supported:
                hdr_n = hdr_type.nwords_per_inst(bw)
                pay_n = cls.max_payload_nwords(bw)
                lines.extend([
                    "",
                    f"    static void write_array_impl(word_bw_tag<{bw}>, const {struct_name}* self, ap_uint<{bw}> x[]) {{",
                    f"        self->header.write_array<{bw}>(x);",
                ])
                for i in range(pay_n):
                    high = min((i + 1) * bw - 1, mpbw - 1)
                    low = i * bw
                    lines.append(f"        x[{hdr_n + i}] = self->payload_bits.range({high}, {low});")
                lines.append("    }")
            lines.extend([
                "",
                "    template<int word_bw>",
                "    void write_array(ap_uint<word_bw> x[]) const {",
                "        write_array_impl(word_bw_tag<word_bw>{}, this, x);",
                "    }",
            ])

            # read_array — fallback template + per-bw overloads + wrapper
            lines.extend([
                "",
                "    template<int word_bw>",
                f"    static void read_array_impl(word_bw_tag<word_bw>, {struct_name}* self, ap_uint<word_bw> x[]) {{",
                '        static_assert(word_bw < 0, "Unsupported word_bw for read_array");',
                "        (void)self; (void)x;",
                "    }",
            ])
            for bw in word_bw_supported:
                hdr_n = hdr_type.nwords_per_inst(bw)
                pay_n = cls.max_payload_nwords(bw)
                lines.extend([
                    "",
                    f"    static void read_array_impl(word_bw_tag<{bw}>, {struct_name}* self, ap_uint<{bw}> x[]) {{",
                    f"        self->header.read_array<{bw}>(x);",
                    "        self->payload_bits = 0;",
                ])
                for i in range(pay_n):
                    high = min((i + 1) * bw - 1, mpbw - 1)
                    low = i * bw
                    lines.append(f"        self->payload_bits.range({high}, {low}) = x[{hdr_n + i}];")
                lines.append("    }")
            lines.extend([
                "",
                "    template<int word_bw>",
                "    void read_array(ap_uint<word_bw> x[]) {",
                "        read_array_impl(word_bw_tag<word_bw>{}, this, x);",
                "    }",
            ])

        # Type-specific getters and setters for each registered schema
        lines.append("")
        for _, schema_cls in registry.items():
            cls_cpp = schema_cls.cpp_class_name()
            bw = schema_cls.get_bitwidth()
            lines.extend([
                f"    {cls_cpp} get_{schema_cls.__name__}() const {{",
                f"        return {cls_cpp}::unpack_from_uint(payload_bits.range({bw - 1}, 0));",
                "    }",
                f"    void set_{schema_cls.__name__}(const {cls_cpp}& p) {{",
                "        payload_bits = 0;",
                f"        payload_bits.range({bw - 1}, 0) = {cls_cpp}::pack_to_uint(p);",
                "    }",
            ])

        lines.append("};")
        return "\n".join(lines)
