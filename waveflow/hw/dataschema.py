"""Class-driven schema architecture.

The key design shift is:

- schema structure lives on the class
- runtime values live on the instance

Schema classes are ``Buildable`` build steps: call ``MySchema.as_buildable()``
to obtain a :class:`DataSchemaStep` that generates C++ include headers when
its ``run(cfg)`` method is invoked.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from enum import IntEnum
import math
import posixpath
from pathlib import Path, PurePosixPath
import re
from typing import Any, ClassVar, TypeAlias

import numpy as np
from numpy.typing import NDArray

from waveflow.utils.fixputils import MAX_WIDTH
from waveflow.hw.param import Param, defer_if_symbolic, resolve_elements

Words: TypeAlias = NDArray[np.uint32] | NDArray[np.uint64]

from waveflow.build.build import Buildable, BuildConfig, BuildResult


# Vitis HLS caps ``ap_uint<N>`` at 8191 bits.  A schema whose whole-object packed
# width exceeds this cannot be packed into a single ``ap_uint`` word in
# synthesizable code (C-sim's software ``ap_uint`` tolerates wider values, which
# is why this only bites at C-synthesis).  Large buffers must be accessed
# element-wise / by burst (``array_utils``) rather than packed whole.
HLS_AP_UINT_MAX_BITWIDTH = 8191


class DataSchema(ABC):
    """Abstract base class for schema nodes.

    Subclasses define structural metadata at the class level. Instances hold only
    runtime values.
    """

    include_dir: ClassVar[str] = "."
    include_filename: ClassVar[str | None] = None
    cpp_repr: ClassVar[str | None] = None
    can_gen_include: ClassVar[bool] = True
    allowed_specialize_kwargs: ClassVar[set[str]] = {
        "include_dir",
        "include_filename",
        "cpp_repr",
    }

    @classmethod
    @abstractmethod
    def get_bitwidth(cls) -> int:
        """Return the packed hardware bitwidth for this schema node."""

    @classmethod
    def cpp_class_name(cls) -> str:
        """Return the C++ type name used for code generation."""
        return cls.cpp_repr or cls.__name__

    @classmethod
    @abstractmethod
    def init_value(cls) -> Any:
        """Return the initial Python-side runtime representation for this schema."""

    @classmethod
    def get_dependencies(cls) -> list[type[DataSchema]]:
        """Return generated-schema dependencies required by this schema header."""
        return []

    @classmethod
    def get_utility_includes(cls) -> list[str]:
        """Return paths to utility headers this schema (or its dependencies) requires.

        Default: empty. ``DataArray`` overrides to return its element type's
        array-utils header. ``DataList`` overrides to recursively collect from
        its member schemas. Scalar field types return empty unless they appear
        inside a ``DataArray``.

        Paths are emitted verbatim as ``#include "<path>"`` lines in the
        generated component header; they must match the file layout that
        ``ArrayUtilsStep`` (or whichever step produces the utility header) writes.
        """
        return []

    @classmethod
    def get_codegen_includes(cls) -> list[str]:
        """Return raw ``#include`` directive bodies the generated C++ for this element type
        needs for its ``cpp_type`` (e.g. ``"<complex>"`` for a ``std::complex<…>`` element).

        Each entry is emitted verbatim as ``#include <body>`` in the generated array-utils
        header (the body carries its own ``<…>`` / ``"…"`` delimiters).  Default: empty
        (scalar fields get their types from the always-included ``streamutils_hls.h``)."""
        return []

    @staticmethod
    def _get_indent(level: int) -> str:
        """Return consistent indentation for generated C++ code."""
        return "    " * level

    @staticmethod
    def _scope_local_lines(lines: list[str]) -> list[str]:
        """Wrap generated statements in a local C++ scope to avoid name collisions."""
        return ["    {"] + [f"    {line}" for line in lines] + ["    }"]

    @classmethod
    def to_uint_expr(cls, value_expr: str) -> str:
        """Return a C++ expression that packs a value into unsigned bits."""
        return f"{cls.cpp_class_name()}::pack_to_uint({value_expr})"

    @classmethod
    def to_uint_value_expr(cls, value_expr: str) -> str:
        """Return a C++ expression that packs a value expression to unsigned bits."""
        return cls.to_uint_expr(value_expr)

    @classmethod
    def get_param_str(cls, write: bool) -> str:
        """Return optional runtime parameter declarations for generated read/write helpers."""
        _ = write
        return ""

    def to_dict(self) -> Any:
        """Return a JSON-serializable Python representation of this schema value."""
        return self.val

    def from_dict(self, data: Any) -> DataSchema:
        """Populate this schema from a Python representation and return ``self``."""
        self.val = data
        return self

    @staticmethod
    def _to_jsonable(value: Any) -> Any:
        """Convert nested values into types accepted by ``json.dumps``."""
        if isinstance(value, dict):
            return {key: DataSchema._to_jsonable(val) for key, val in value.items()}

        if isinstance(value, (list, tuple)):
            return [DataSchema._to_jsonable(val) for val in value]

        if isinstance(value, np.ndarray):
            return [DataSchema._to_jsonable(val) for val in value.tolist()]

        if isinstance(value, np.generic):
            return value.item()

        return value

    def to_json(
        self,
        file_path: str | Path | None = None,
        indent: int | None = 2,
    ) -> str:
        """Serialize this schema value to JSON, optionally writing it to disk."""
        payload = self._to_jsonable(self.to_dict())
        json_str = json.dumps(payload, indent=indent)

        if file_path is not None:
            out_path = Path(file_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json_str, encoding="utf-8")

        return json_str

    def from_json(self, json_input: str | Path) -> DataSchema:
        """Load this schema value from a JSON string or JSON file path."""
        if isinstance(json_input, Path):
            raw = json_input.read_text(encoding="utf-8")
        elif isinstance(json_input, str):
            path_candidate = Path(json_input)
            if path_candidate.exists() and path_candidate.is_file():
                raw = path_candidate.read_text(encoding="utf-8")
            else:
                raw = json_input
        else:
            raise TypeError("from_json expects a JSON string or a pathlib.Path.")

        return self.from_dict(json.loads(raw))

    @classmethod
    def nwords_per_inst(cls, word_bw: int) -> int:
        """Return the number of packed words needed for one instance at ``word_bw``."""
        if word_bw <= 0:
            raise ValueError("word_bw must be positive.")

        packed = np.asarray(cls().serialize(word_bw=word_bw))
        if packed.ndim == 0:
            return 1
        return int(packed.shape[0])

    # -- max / active size APIs ------------------------------------------------
    # The "max" (class-level) APIs give the worst-case allocation size, used for
    # codegen and buffer sizing.  The "active" (instance-level) APIs give the
    # actual serialized size for the current runtime value.  For fixed-size
    # schemas both are identical; ``VarDataArray`` and ``DataList`` containers
    # that hold one override the active variants to return the true live size.

    @classmethod
    def get_bitwidth_max(cls) -> int:
        """Return the maximum (declared/static) bitwidth for this schema type.

        Alias for :meth:`get_bitwidth` that makes the max/active distinction
        explicit.  Subclasses should override ``get_bitwidth`` rather than this
        method.
        """
        return cls.get_bitwidth()

    def get_bitwidth_active(self) -> int:
        """Return the active bitwidth for this runtime value.

        For fixed-size schemas this equals :meth:`get_bitwidth_max`.
        ``VarDataArray`` overrides this to return ``nbits_len + len(val) *
        elem_bitwidth``; ``DataList`` overrides it to sum its children's active
        bitwidths.
        """
        return self.__class__.get_bitwidth_max()

    @classmethod
    def nwords_max(cls, word_bw: int) -> int:
        """Return the maximum (declared/static) word count at ``word_bw``.

        Alias for :meth:`nwords_per_inst` with a clearer name.
        """
        return cls.nwords_per_inst(word_bw)

    def nwords_active(self, word_bw: int) -> int:
        """Return the active serialized word count for this runtime value.

        Uses :meth:`_active_size_recursive` to mirror the actual serialization
        cursor movement, so the result is always consistent with what
        :meth:`serialize` produces.
        """
        if word_bw <= 0:
            raise ValueError("word_bw must be positive.")
        curr_ipos, curr_iword = self._active_size_recursive(
            word_bw=word_bw, ipos0=0, iword0=0
        )
        n = curr_iword + (1 if curr_ipos > 0 else 0)
        return n if n > 0 else 1

    def _active_size_recursive(
        self,
        word_bw: int,
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        """Simulate the serialization cursor advance for the active value.

        Default delegates to :meth:`_serialize_recursive` with a scratch words
        list, which is correct for fixed-size schemas (cursor movement is
        independent of values).  ``DataList`` overrides this to walk children
        individually so that variable-length children propagate their active
        cursor correctly.  ``VarDataArray`` relies on the default because its
        ``_serialize_recursive`` already uses the active element count.
        """
        words: list[int] = [0]
        return self._serialize_recursive(
            word_bw=word_bw, words=words, ipos0=ipos0, iword0=iword0
        )

    @classmethod
    def _max_cursor_step(
        cls,
        word_bw: int,
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        """Advance a packing cursor by this schema's **maximum** serialized size.

        Used by :meth:`DataList.nwords_per_inst` to compute the worst-case word
        count when one or more children are variable-length (e.g.
        :class:`VarDataArray`).

        Default: serialize a default (zero-initialized) instance.  For
        fixed-size schemas this is identical to the actual max size.
        :class:`VarDataArray` overrides this to simulate max-length layout.
        :class:`DataList` overrides this to walk its children's max steps.
        """
        words: list[int] = [0]
        return cls()._serialize_recursive(
            word_bw=word_bw, words=words, ipos0=ipos0, iword0=iword0
        )

    def serialize(self, word_bw: int = 32) -> Words:
        """Serialize this runtime value into packed hardware words."""
        if word_bw <= 0:
            raise ValueError("word_bw must be positive.")

        words: list[int] = [0]
        final_ipos, final_iword = self._serialize_recursive(
            word_bw=word_bw,
            words=words,
            ipos0=0,
            iword0=0,
        )

        n_words = final_iword + (1 if final_ipos > 0 else 0)
        if n_words == 0:
            n_words = 1

        words = words[:n_words]

        if word_bw <= 32:
            return np.array(
                [np.uint32(word & ((1 << min(word_bw, 32)) - 1)) for word in words],
                dtype=np.uint32,
            )

        if word_bw <= 64:
            return np.array(
                [np.uint64(word & ((1 << word_bw) - 1)) for word in words],
                dtype=np.uint64,
            )

        chunks_per_word = math.ceil(word_bw / 64)
        out = np.zeros((n_words, chunks_per_word), dtype=np.uint64)
        mask64 = (1 << 64) - 1
        for row_idx, word in enumerate(words):
            for chunk_idx in range(chunks_per_word):
                out[row_idx, chunk_idx] = np.uint64((word >> (64 * chunk_idx)) & mask64)
        return out

    def _serialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        raise NotImplementedError(f"{self.__class__.__name__} does not implement serialization.")

    def deserialize(self, packed: Words, word_bw: int = 32) -> DataSchema:
        """Deserialize packed hardware words into this runtime value."""
        if word_bw <= 0:
            raise ValueError("word_bw must be positive.")

        arr = np.asarray(packed)
        words: list[int] = []

        if word_bw <= 64:
            if arr.ndim == 0:
                arr = arr.reshape(1)
            elif arr.ndim != 1:
                raise ValueError("For word_bw <= 64, packed must be a 1D array-like.")

            mask = (1 << word_bw) - 1
            words = [int(value) & mask for value in arr]
        else:
            chunks_per_word = math.ceil(word_bw / 64)
            if arr.ndim != 2:
                raise ValueError("For word_bw > 64, packed must be a 2D array-like.")
            if arr.shape[1] != chunks_per_word:
                raise ValueError(
                    f"For word_bw={word_bw}, packed must have shape (n_words, {chunks_per_word})."
                )

            mask = (1 << word_bw) - 1
            for row in arr:
                word = 0
                for chunk_idx, chunk in enumerate(row):
                    word |= int(np.uint64(chunk)) << (64 * chunk_idx)
                words.append(word & mask)

        if not words:
            words = [0]

        self._deserialize_recursive(
            word_bw=word_bw,
            words=words,
            ipos0=0,
            iword0=0,
        )
        return self

    # -- vectorized numpy transfer hooks (the open/closed fast path) ------------
    # Array serializers (``MMIFMaster.read_array`` / ``write_array``,
    # ``ArrayTransferIF``) call these to move a whole array of ``cls`` elements as one
    # numpy reinterpret instead of a per-element loop.  The default returns ``None`` —
    # "no vectorized path; fall back to the generic recursive (de)serializer."  A field
    # type opts in by overriding them (or, for scalar fields, just ``_numpy_elem_dtype``);
    # there is no central type switch in the transport layer to edit per new type.
    @classmethod
    def to_words_numpy(cls, values: Any, word_bw: int) -> Words | None:
        """Vectorized pack of an array of ``cls`` element values into hardware words, or
        ``None`` if ``cls`` has no numpy fast path (the caller then falls back to the
        recursive serializer)."""
        return None

    @classmethod
    def from_words_numpy(cls, words: Any, count: int, word_bw: int) -> np.ndarray | None:
        """Vectorized unpack of ``count`` ``cls`` elements from ``words``, or ``None`` for
        no fast path (the caller then falls back to the recursive deserializer)."""
        return None

    def write_uint32_file(self, file_path: str | Path) -> Path:
        """Serialize this schema to 32-bit words and write them to a binary file."""
        out_path = Path(file_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        words = np.asarray(self.serialize(word_bw=32), dtype="<u4")
        words.tofile(out_path)
        return out_path

    def read_uint32_file(self, file_path: str | Path) -> DataSchema:
        """Read 32-bit packed words from a binary file and deserialize this schema."""
        in_path = Path(file_path)
        words = np.fromfile(in_path, dtype="<u4")
        return self.deserialize(words, word_bw=32)

    def _deserialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        raise NotImplementedError(f"{self.__class__.__name__} does not implement deserialization.")

    @classmethod
    def from_uint_expr(cls, uint_expr: str) -> str:
        """Return a C++ expression that reconstructs a value from unsigned bits."""
        return f"{cls.cpp_class_name()}::unpack_from_uint({uint_expr})"

    @classmethod
    def gen_pack(cls, indent_level: int = 0) -> str:
        """Return the C++ pack helper emitted for this schema."""
        raise NotImplementedError(f"{cls.__name__} does not implement pack generation.")

    @classmethod
    def gen_unpack(cls, indent_level: int = 0) -> str:
        """Return the C++ unpack helper emitted for this schema."""
        raise NotImplementedError(f"{cls.__name__} does not implement unpack generation.")

    @classmethod
    def gen_write(
        cls,
        word_bw: int | None = None,
        dst_type: str = "array",
        word_bw_supported: list[int] | None = None,
        indent_level: int = 0,
    ) -> str:
        """Return C++ code that writes this schema to a hardware destination."""
        if word_bw_supported is None:
            if word_bw is None:
                raise ValueError("word_bw must be provided when word_bw_supported is omitted.")
            word_bw_supported = [word_bw]

        if not word_bw_supported:
            raise ValueError("word_bw_supported must contain at least one value.")

        for bw in word_bw_supported:
            if bw <= 0:
                raise ValueError(f"word_bw values must be positive. Got {bw}.")

        # framed_stream (plans/memstream_inband.md): the axi4_stream method, renamed.  framed_word is
        # field-identical to axi4s_word, so the only differences are the stream C-type, the method
        # name, and the per-beat writer (write_axi4_word -> the shared write_boundary_word<framed_word>).
        # Generate-then-rename keeps one source of truth for the boundary-write logic.
        if dst_type == "framed_stream":
            import re
            axi = cls.gen_write(dst_type="axi4_stream", word_bw_supported=word_bw_supported,
                                indent_level=indent_level)
            axi = axi.replace("write_axi4_stream", "write_framed_stream")
            axi = axi.replace("streamutils::axi4s_word", "streamutils::framed_word")
            axi = re.sub(r"streamutils::write_axi4_word<(\d+)>",
                         r"streamutils::write_boundary_word<streamutils::framed_word<\1>, \1>", axi)
            return axi

        if dst_type not in {"array", "stream", "axi4_stream"}:
            raise ValueError(f"Unsupported dst_type: {dst_type}.")

        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)
        i2 = cls._get_indent(indent_level + 2)
        param_str = cls.get_param_str(write=True)
        suffix = f", {param_str}" if param_str else ""
        param_names = [part.split("=")[0].strip().split()[-1] for part in param_str.split(", ")] if param_str else []
        call_suffix = "" if not param_names else ", " + ", ".join(param_names)
        cls_name = cls.cpp_class_name()

        if dst_type == "array":
            impl_name = "write_array_impl"
            wrapper_signature = f"{indent}template<int word_bw>\n{indent}void write_array(ap_uint<word_bw> x[]{suffix}) const {{"
            impl_signature = "ap_uint<{bw}> x[]"
            wrapper_call = f"{i1}{impl_name}<word_bw>::run(this, x{call_suffix});"
            target = "x"
            unsupported_msg = "Unsupported word_bw for write_array"
        elif dst_type == "stream":
            impl_name = "write_stream_impl"
            wrapper_signature = (
                f"{indent}template<int word_bw>\n"
                f"{indent}void write_stream(hls::stream<ap_uint<word_bw>> &s{suffix}) const {{"
            )
            impl_signature = "hls::stream<ap_uint<{bw}>> &s"
            wrapper_call = f"{i1}{impl_name}<word_bw>::run(this, s{call_suffix});"
            target = "s"
            unsupported_msg = "Unsupported word_bw for write_stream"
        else:
            impl_name = "write_axi4_stream_impl"
            wrapper_signature = (
                f"{indent}template<int word_bw>\n"
                f"{indent}void write_axi4_stream("
                f"hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true{suffix}) const {{"
            )
            impl_signature = "hls::stream<streamutils::axi4s_word<{bw}>> &s, bool tlast"
            wrapper_call = f"{i1}{impl_name}<word_bw>::run(this, s, tlast{call_suffix});"
            target = "s"
            unsupported_msg = "Unsupported word_bw for write_axi4_stream"

        lines = [
            f"{indent}template<int word_bw>",
            f"{indent}static void {impl_name}(word_bw_tag<word_bw>, const {cls_name}* self, {impl_signature.format(bw='word_bw')}{suffix}) {{",
            f'{i1}static_assert(word_bw < 0, "{unsupported_msg}");',
            f"{i1}(void)self;",
        ]

        for base_name in [target, *param_names]:
            lines.append(f"{i1}(void){base_name};")
        if dst_type == "axi4_stream":
            lines.append(f"{i1}(void)tlast;")
        lines.append(f"{indent}}}")

        for bw in word_bw_supported:
            lines.extend([
                "",
                f"{indent}static void {impl_name}(word_bw_tag<{bw}>, const {cls_name}* self, {impl_signature.format(bw=bw)}{suffix}) {{",
            ])

            if dst_type != "array":
                lines.append(f"{i2}ap_uint<{bw}> w = 0;")

            final_lines, final_ipos, _ = cls._gen_write_recursive(
                word_bw=bw,
                dst_type=dst_type,
                target=target,
                ipos0=0,
                iword0=0,
                prefix="self->",
            )

            if dst_type == "axi4_stream" and final_ipos == 0:
                marker = f"streamutils::write_axi4_word<{bw}>({target}, w, "
                for line_idx in range(len(final_lines) - 1, -1, -1):
                    if marker in final_lines[line_idx]:
                        final_lines[line_idx] = final_lines[line_idx].replace(
                            ", false);",
                            ", tlast);",
                        )
                        break

            for line in final_lines:
                if line.startswith("    "):
                    line = line[4:]
                lines.append(f"{i1}{line}" if line else "")

            if dst_type != "array" and final_ipos > 0:
                if dst_type == "stream":
                    lines.append(f"{i1}{target}.write(w);")
                else:
                    lines.append(f"{i1}streamutils::write_axi4_word<{bw}>({target}, w, tlast);")

            lines.append(f"{indent}}}")

        lines.extend([
            "",
            *wrapper_signature.splitlines(),
            wrapper_call.replace(f"{impl_name}<word_bw>::run(", f"{impl_name}(word_bw_tag<word_bw>{{}}, "),
            f"{indent}}}",
        ])
        return "\n".join(lines)

    @classmethod
    def _gen_write_recursive(
        cls,
        word_bw: int,
        dst_type: str = "array",
        target: str = "x",
        ipos0: int = 0,
        iword0: int = 0,
        prefix: str = "",
        member_name: str | None = None,
    ) -> tuple[list[str], int, int]:
        """Return recursive write statements and final packing state."""
        raise NotImplementedError(f"{cls.__name__} does not implement write generation.")

    @classmethod
    def gen_read(
        cls,
        word_bw: int | None = None,
        src_type: str = "array",
        word_bw_supported: list[int] | None = None,
        indent_level: int = 0,
    ) -> str:
        """Return C++ code that reads this schema from a hardware source."""
        if word_bw_supported is None:
            if word_bw is None:
                raise ValueError("word_bw must be provided when word_bw_supported is omitted.")
            word_bw_supported = [word_bw]

        if not word_bw_supported:
            raise ValueError("word_bw_supported must contain at least one value.")

        for bw in word_bw_supported:
            if bw <= 0:
                raise ValueError(f"word_bw values must be positive. Got {bw}.")

        # framed_stream (plans/memstream_inband.md) reuses the axi4_stream method VERBATIM and renames
        # it: framed_word is field-identical to axi4s_word (.data/.last), so the only differences are
        # the stream C-type and the method name.  Generating-then-renaming keeps ONE source of truth
        # for the boundary-read logic and touches no recursive generator.
        if src_type == "framed_stream":
            axi = cls.gen_read(src_type="axi4_stream", word_bw_supported=word_bw_supported,
                               indent_level=indent_level)
            return (axi.replace("read_axi4_stream", "read_framed_stream")
                       .replace("streamutils::axi4s_word", "streamutils::framed_word"))

        if src_type not in {"array", "stream", "axi4_stream"}:
            raise ValueError(f"Unsupported src_type: {src_type}.")

        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)
        i2 = cls._get_indent(indent_level + 2)
        param_str = cls.get_param_str(write=False)
        suffix = f", {param_str}" if param_str else ""
        param_names = [part.split("=")[0].strip().split()[-1] for part in param_str.split(", ")] if param_str else []
        call_suffix = "" if not param_names else ", " + ", ".join(param_names)
        cls_name = cls.cpp_class_name()

        if src_type == "array":
            impl_name = "read_array_impl"
            wrapper_signature = f"{indent}template<int word_bw>\n{indent}void read_array(const ap_uint<word_bw> x[]{suffix}) {{"
            impl_signature = "const ap_uint<{bw}> x[]"
            wrapper_call = f"{i1}{impl_name}<word_bw>::run(this, x{call_suffix});"
            source = "x"
            unsupported_msg = "Unsupported word_bw for read_array"
            compat_wrapper_signature = None
            compat_wrapper_call = None
        elif src_type == "stream":
            impl_name = "read_stream_impl"
            wrapper_signature = (
                f"{indent}template<int word_bw>\n"
                f"{indent}void read_stream(hls::stream<ap_uint<word_bw>> &s{suffix}) {{"
            )
            impl_signature = "hls::stream<ap_uint<{bw}>> &s"
            wrapper_call = f"{i1}{impl_name}<word_bw>::run(this, s{call_suffix});"
            source = "s"
            unsupported_msg = "Unsupported word_bw for read_stream"
            compat_wrapper_signature = None
            compat_wrapper_call = None
        else:
            impl_name = "read_axi4_stream_impl"
            wrapper_signature = (
                f"{indent}template<int word_bw>\n"
                f"{indent}void read_axi4_stream("
                f"hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl{suffix}) {{"
            )
            compat_wrapper_signature = (
                f"{indent}template<int word_bw>\n"
                f"{indent}void read_axi4_stream("
                f"hls::stream<streamutils::axi4s_word<word_bw>> &s{suffix}) {{"
            )
            impl_signature = "hls::stream<streamutils::axi4s_word<{bw}>> &s, streamutils::tlast_status &tl"
            wrapper_call = f"{i1}{impl_name}<word_bw>::run(this, s, tl{call_suffix});"
            compat_wrapper_call = f"{i1}streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;\n{i1}read_axi4_stream<word_bw>(s, tl{call_suffix});"
            source = "s"
            unsupported_msg = "Unsupported word_bw for read_axi4_stream"

        lines = [
            f"{indent}template<int word_bw>",
            f"{indent}static void {impl_name}(word_bw_tag<word_bw>, {cls_name}* self, {impl_signature.format(bw='word_bw')}{suffix}) {{",
            f'{i1}static_assert(word_bw < 0, "{unsupported_msg}");',
            f"{i1}(void)self;",
        ]
        for base_name in [source, *param_names]:
            lines.append(f"{i1}(void){base_name};")
        if src_type == "axi4_stream":
            lines.append(f"{i1}(void)tl;")
        lines.append(f"{indent}}}")

        for bw in word_bw_supported:
            lines.extend([
                "",
                f"{indent}static void {impl_name}(word_bw_tag<{bw}>, {cls_name}* self, {impl_signature.format(bw=bw)}{suffix}) {{",
            ])

            if src_type in {"stream", "axi4_stream"}:
                lines.append(f"{i2}ap_uint<{bw}> w = 0;")
            if src_type == "axi4_stream":
                lines.append(f"{i2}tl = streamutils::tlast_status::no_tlast;")
                lines.append(f"{i2}bool last = false;")

            final_lines, _, _ = cls._gen_read_recursive(
                word_bw=bw,
                src_type=src_type,
                source=source,
                ipos0=0,
                iword0=0,
                prefix="self->",
            )

            for line in final_lines:
                if line.startswith("    "):
                    line = line[4:]
                lines.append(f"{i1}{line}" if line else "")

            if src_type == "axi4_stream":
                lines.extend([
                    f"{i1}if (last) {{",
                    f"{i2}tl = streamutils::tlast_status::tlast_at_end;",
                    f"{i1}}}",
                ])

            lines.append(f"{indent}}}")

        lines.extend([
            "",
            *wrapper_signature.splitlines(),
            wrapper_call.replace(f"{impl_name}<word_bw>::run(", f"{impl_name}(word_bw_tag<word_bw>{{}}, "),
            f"{indent}}}",
        ])
        if compat_wrapper_signature is not None and compat_wrapper_call is not None:
            lines.extend([
                "",
                *compat_wrapper_signature.splitlines(),
                *compat_wrapper_call.splitlines(),
                f"{indent}}}",
            ])
        return "\n".join(lines)

    @classmethod
    def _gen_read_recursive(
        cls,
        word_bw: int,
        src_type: str = "array",
        source: str = "x",
        ipos0: int = 0,
        iword0: int = 0,
        prefix: str = "",
        member_name: str | None = None,
    ) -> tuple[list[str], int, int]:
        """Return recursive read statements and final packing state."""
        raise NotImplementedError(f"{cls.__name__} does not implement read generation.")

    def is_close(
        self,
        other: DataSchema,
        rel_tol: float | None = None,
        abs_tol: float | None = 1e-8,
    ) -> bool:
        """Compare runtime values with another schema instance."""
        _ = rel_tol, abs_tol
        if not isinstance(other, self.__class__):
            return False
        return self.val == other.val

    @staticmethod
    def _camel_to_snake(name: str) -> str:
        """Convert a class name to a snake_case stem for generated filenames."""
        return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()

    @classmethod
    def default_include_filename(cls) -> str:
        """Return the default generated include filename for this schema class."""
        return f"{cls._camel_to_snake(cls.__name__)}.h"

    @classmethod
    def resolved_include_filename(cls) -> str:
        """Return the configured include filename or the default derived name."""
        return cls.include_filename or cls.default_include_filename()

    @classmethod
    def default_tb_include_filename(cls) -> str:
        """Return the default generated TB include filename for this schema class."""
        filename = PurePosixPath(cls.default_include_filename())
        suffix = filename.suffix or ".h"
        return f"{filename.stem}_tb{suffix}"

    @classmethod
    def resolved_tb_include_filename(cls) -> str:
        """Return the generated TB include filename for this schema class."""
        filename = PurePosixPath(cls.resolved_include_filename())
        suffix = filename.suffix or ".h"
        return f"{filename.stem}_tb{suffix}"

    @classmethod
    def include_path(cls) -> str:
        """Return the schema header path relative to the code-generation root."""
        include_dir = (cls.include_dir or ".").replace("\\", "/")
        include_root = PurePosixPath(include_dir)
        filename = cls.resolved_include_filename()
        if include_root.as_posix() == ".":
            return filename
        return f"{include_root.as_posix()}/{filename}"

    @classmethod
    def tb_include_path(cls) -> str:
        """Return the schema TB header path relative to the code-generation root."""
        include_dir = (cls.include_dir or ".").replace("\\", "/")
        include_root = PurePosixPath(include_dir)
        filename = cls.resolved_tb_include_filename()
        if include_root.as_posix() == ".":
            return filename
        return f"{include_root.as_posix()}/{filename}"

    @classmethod
    def relative_include_path_to(cls, dependency: type[DataSchema]) -> str:
        """Return the include path from this schema header to a dependency header."""
        current_dir = posixpath.dirname(cls.include_path()) or "."
        return posixpath.relpath(dependency.include_path(), start=current_dir)

    @classmethod
    def relative_tb_include_path_to(cls, dependency: type[DataSchema]) -> str:
        """Return the TB include path from this schema TB header to a dependency TB header."""
        current_dir = posixpath.dirname(cls.tb_include_path()) or "."
        return posixpath.relpath(dependency.tb_include_path(), start=current_dir)

    @classmethod
    def include_guard(cls) -> str:
        """Return a deterministic include guard derived from the include path."""
        guard = re.sub(r"[^A-Za-z0-9]+", "_", cls.include_path()).strip("_").upper()
        return re.sub(r"_+", "_", guard)

    @classmethod
    def tb_include_guard(cls) -> str:
        """Return a deterministic include guard for the TB header."""
        guard = re.sub(r"[^A-Za-z0-9]+", "_", cls.tb_include_path()).strip("_").upper()
        return re.sub(r"_+", "_", guard)

    @classmethod
    def tb_member_macro(cls) -> str:
        """Return the preprocessor symbol enabling TB-only member declarations."""
        return f"WAVEFLOW_ENABLE_{cls.tb_include_guard()}_MEMBERS"

    @classmethod
    def validate_specialize_kwargs(cls, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Validate structural overrides accepted by ``specialize()`` methods."""
        unknown = sorted(set(kwargs) - cls.allowed_specialize_kwargs)
        if unknown:
            allowed = ", ".join(sorted(cls.allowed_specialize_kwargs))
            unknown_str = ", ".join(unknown)
            raise TypeError(
                f"Unknown specialization keyword(s) for {cls.__name__}: {unknown_str}. "
                f"Allowed keys: {allowed}."
            )

        validated = dict(kwargs)
        if "include_dir" in validated and not isinstance(validated["include_dir"], str):
            raise TypeError("include_dir must be a string.")
        if "include_filename" in validated:
            include_filename = validated["include_filename"]
            if include_filename is not None and not isinstance(include_filename, str):
                raise TypeError("include_filename must be a string or None.")
        if "cpp_repr" in validated:
            cpp_repr = validated["cpp_repr"]
            if cpp_repr is not None and not isinstance(cpp_repr, str):
                raise TypeError("cpp_repr must be a string or None.")
        return validated

    @classmethod
    def merge_specialize_attrs(
        cls,
        base_attrs: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge validated structural overrides into subclass attributes."""
        merged = dict(base_attrs)
        merged.update(cls.validate_specialize_kwargs(kwargs))
        return merged

    @classmethod
    def _gen_include_decl(cls, word_bw_supported: list[int] | None = None, framed: bool = False) -> str:
        """Return the synthesizable declaration body emitted inside a generated header."""
        raise NotImplementedError(f"{cls.__name__} does not implement generated includes.")

    @classmethod
    def _gen_tb_member_declarations(cls, indent_level: int = 0) -> str:
        _ = indent_level
        return ""

    @classmethod
    def _gen_tb_member_definitions(cls, indent_level: int = 0) -> str:
        _ = indent_level
        return ""

    @classmethod
    def as_buildable(
        cls,
        word_bw_supported: list[int] | None = None,
    ) -> "DataSchemaStep":
        """Return a :class:`DataSchemaStep` that generates C++ headers for this schema.

        Parameters
        ----------
        word_bw_supported : list[int] | None, optional
            Word widths to emit read helpers for.  Validated on construction.

        Returns
        -------
        DataSchemaStep
            Call ``.run(cfg)`` on the result to write the headers to disk.
        """
        return DataSchemaStep(cls, word_bw_supported)

    @classmethod
    def _gen_json_member_declarations(cls, indent_level: int = 0) -> str:
        indent = cls._get_indent(indent_level)
        lines = [
            f"{indent}void dump_json(std::ostream& os, int indent = 2, int level = 0) const;",
            f"{indent}void load_json(const std::string& json_text, size_t& pos);",
            f"{indent}void load_json(std::istream& is);",
            f"{indent}void dump_json_file(const char* file_path, int indent = 2) const;",
            f"{indent}void load_json_file(const char* file_path);",
        ]
        return "\n".join(lines)

    @classmethod
    def _gen_json_member_definitions(cls, indent_level: int = 0) -> str:
        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)
        lines = [
            f"{indent}inline void {cls.cpp_class_name()}::dump_json(std::ostream& os, int indent, int level) const {{",
            f"{i1}const int step = (indent < 0) ? 0 : indent;",
        ]

        for line in cls._gen_dump_json_recursive(
            prefix="this->",
            os_name="os",
            depth_expr="level",
            indent_var="step",
        ):
            if line.startswith("    "):
                line = line[4:]
            lines.append(f"{i1}{line}" if line else "")

        lines.extend([
            f"{indent}}}",
            "",
            f"{indent}inline void {cls.cpp_class_name()}::load_json(const std::string& json_text, size_t& pos) {{",
        ])

        for line in cls._gen_load_json_recursive(
            prefix="this->",
            json_var="json_text",
            pos_var="pos",
            ctx="root",
        ):
            if line.startswith("    "):
                line = line[4:]
            lines.append(f"{i1}{line}" if line else "")

        lines.extend([
            f"{indent}}}",
            "",
            f"{indent}inline void {cls.cpp_class_name()}::load_json(std::istream& is) {{",
            f"{i1}std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());",
            f"{i1}size_t pos = 0;",
            f"{i1}streamutils::json_skip_ws(json_text, pos);",
            f"{i1}this->load_json(json_text, pos);",
            f"{i1}streamutils::json_skip_ws(json_text, pos);",
            f"{i1}if (pos != json_text.size()) {{",
            f'{i1}    throw std::runtime_error("Trailing characters after JSON object.");',
            f"{i1}}}",
            f"{indent}}}",
            "",
            f"{indent}inline void {cls.cpp_class_name()}::dump_json_file(const char* file_path, int indent) const {{",
            f"{i1}std::ofstream ofs(file_path);",
            f"{i1}if (!ofs) {{",
            f'{i1}    throw std::runtime_error("Failed to open output JSON file.");',
            f"{i1}}}",
            f"{i1}this->dump_json(ofs, indent);",
            f"{indent}}}",
            "",
            f"{indent}inline void {cls.cpp_class_name()}::load_json_file(const char* file_path) {{",
            f"{i1}std::ifstream ifs(file_path);",
            f"{i1}if (!ifs) {{",
            f'{i1}    throw std::runtime_error("Failed to open input JSON file.");',
            f"{i1}}}",
            f"{i1}this->load_json(ifs);",
            f"{indent}}}",
        ])
        return "\n".join(lines)


class DataField(DataSchema):
    """Base class for scalar fields.

    Structural properties such as bitwidth and C++ type are defined on the class.
    Instances hold the runtime value in ``self.val``.
    """

    bitwidth: ClassVar[int | None] = None
    cpp_type: ClassVar[str | None] = None

    def __init__(self, value: Any = None):
        self._val = self.__class__.init_value()
        if value is not None:
            self.val = value

    @property
    def val(self) -> Any:
        """Return the runtime value stored in this field instance."""
        return self._val

    @val.setter
    def val(self, value: Any) -> None:
        self._val = self._convert(value)

    def _convert(self, value: Any) -> Any:
        """Convert a runtime value into this field's Python representation."""
        return value

    def _value_to_field_bits(self, current_val: Any) -> int:
        """Convert the runtime value to a raw integer bit pattern."""
        return int(current_val)

    def _field_bits_to_value(self, field_bits: int) -> Any:
        """Convert a raw integer bit pattern to a runtime value."""
        return int(field_bits)

    # -- vectorized numpy transfer (scalar fast path) --------------------------
    # A scalar field whose stored value maps to a single numpy dtype enables the
    # vectorized array transfer by overriding ``_numpy_elem_dtype`` alone; the pack /
    # unpack below is then a pure numpy reinterpret (one element per word, the same layout
    # the recursive serializer produces when ``nwords_per_inst == 1``).
    @classmethod
    def _numpy_elem_dtype(cls) -> np.dtype | None:
        """The numpy dtype of one stored element value, or ``None`` if this field has no
        direct numpy reinterpret.  Scalar fields (``IntField`` / ``FloatField``) override."""
        return None

    @classmethod
    def to_words_numpy(cls, values: Any, word_bw: int) -> Words | None:
        elem_dtype = cls._numpy_elem_dtype()
        if elem_dtype is None or not isinstance(values, np.ndarray):
            return None
        if cls.nwords_per_inst(word_bw) != 1:
            return None
        word_dtype = np.dtype(np.uint32 if word_bw <= 32 else np.uint64)
        arr = np.ascontiguousarray(values, dtype=elem_dtype).reshape(-1)
        if arr.dtype.itemsize == word_dtype.itemsize:
            return arr.view(word_dtype)
        unsigned_same = np.dtype(f"u{arr.dtype.itemsize}")
        return arr.view(unsigned_same).astype(word_dtype)

    @classmethod
    def from_words_numpy(cls, words: Any, count: int, word_bw: int) -> np.ndarray | None:
        elem_dtype = cls._numpy_elem_dtype()
        if elem_dtype is None or cls.nwords_per_inst(word_bw) != 1:
            return None
        words = np.ascontiguousarray(words)
        if words.dtype.itemsize == elem_dtype.itemsize:
            out = words.view(elem_dtype)
        else:
            unsigned_same = np.dtype(f"u{elem_dtype.itemsize}")
            out = words.astype(unsigned_same).view(elem_dtype)
        return out[:count]

    @classmethod
    def to_uint_expr(cls, value_expr: str) -> str:
        return f"(ap_uint<{cls.get_bitwidth()}>)(static_cast<unsigned int>({value_expr}))"

    @classmethod
    def to_uint_value_expr(cls, value_expr: str) -> str:
        return f"(ap_uint<{cls.get_bitwidth()}>)(static_cast<unsigned int>({value_expr}))"

    @classmethod
    def from_uint_expr(cls, uint_expr: str) -> str:
        return f"({cls.cpp_class_name()})({uint_expr})"

    @classmethod
    def gen_pack(cls, indent_level: int = 0) -> str:
        _ = indent_level
        return ""

    @classmethod
    def gen_unpack(cls, indent_level: int = 0) -> str:
        _ = indent_level
        return ""

    @classmethod
    def _gen_dump_json_recursive(
        cls,
        prefix: str,
        member_name: str | None,
        os_name: str,
        depth_expr: str,
        indent_var: str,
    ) -> list[str]:
        _ = depth_expr, indent_var
        if member_name is None:
            raise ValueError(f"{cls.__name__} JSON generation requires a member_name.")
        value_expr = f"{prefix}{member_name}"
        cpp_name = cls.cpp_class_name()

        if getattr(cls, "enum_type", None) is not None:
            value_expr = f"static_cast<int>({value_expr})"
        elif cpp_name.startswith("ap_uint"):
            value_expr = f"static_cast<unsigned long long>({value_expr})"
        elif cpp_name.startswith("ap_int"):
            value_expr = f"static_cast<long long>({value_expr})"

        return [f"{os_name} << {value_expr};"]

    @classmethod
    def _json_load_expr(cls, json_var: str, pos_var: str) -> str:
        return f"static_cast<{cls.cpp_class_name()}>(streamutils::json_parse_number({json_var}, {pos_var}))"

    @classmethod
    def _gen_load_json_recursive(
        cls,
        prefix: str,
        member_name: str | None,
        json_var: str,
        pos_var: str,
        ctx: str,
    ) -> list[str]:
        _ = ctx
        if member_name is None:
            raise ValueError(f"{cls.__name__} JSON load generation requires a member_name.")
        return [f"{prefix}{member_name} = {cls._json_load_expr(json_var=json_var, pos_var=pos_var)};"]

    @classmethod
    def _gen_write_recursive(
        cls,
        word_bw: int,
        dst_type: str = "array",
        target: str = "x",
        ipos0: int = 0,
        iword0: int = 0,
        prefix: str = "",
        member_name: str | None = None,
    ) -> tuple[list[str], int, int]:
        bitwidth = cls.get_bitwidth()
        if member_name is None:
            raise ValueError(f"{cls.__name__} write generation requires a member_name.")
        if bitwidth > word_bw:
            raise ValueError(
                f"Field '{member_name}' with bitwidth {bitwidth} cannot fit into word_bw={word_bw}."
            )

        lines: list[str] = []
        curr_ipos = ipos0
        curr_iword = iword0

        if curr_ipos + bitwidth > word_bw:
            if dst_type == "stream":
                lines.append(f"    {target}.write(w);")
                lines.append("    w = 0;")
            elif dst_type == "axi4_stream":
                lines.append(f"    streamutils::write_axi4_word<{word_bw}>({target}, w, false);")
                lines.append("    w = 0;")

            curr_iword += 1
            curr_ipos = 0

        lhs = "w" if dst_type != "array" else f"{target}[{curr_iword}]"
        val_expr = cls.to_uint_expr(f"{prefix}{member_name}")

        if curr_ipos == 0 and bitwidth == word_bw:
            lines.append(f"    {lhs} = {val_expr};")
        else:
            if dst_type == "array" and curr_ipos == 0:
                lines.append(f"    {lhs} = 0;")
            high = curr_ipos + bitwidth - 1
            low = curr_ipos
            lines.append(f"    {lhs}.range({high}, {low}) = {val_expr};")

        curr_ipos += bitwidth

        if curr_ipos == word_bw:
            if dst_type == "stream":
                lines.append(f"    {target}.write(w);")
                lines.append("    w = 0;")
            elif dst_type == "axi4_stream":
                lines.append(f"    streamutils::write_axi4_word<{word_bw}>({target}, w, false);")
                lines.append("    w = 0;")

            curr_iword += 1
            curr_ipos = 0

        return lines, curr_ipos, curr_iword

    @classmethod
    def _gen_read_recursive(
        cls,
        word_bw: int,
        src_type: str = "array",
        source: str = "x",
        ipos0: int = 0,
        iword0: int = 0,
        prefix: str = "",
        member_name: str | None = None,
    ) -> tuple[list[str], int, int]:
        bitwidth = cls.get_bitwidth()
        if member_name is None:
            raise ValueError(f"{cls.__name__} read generation requires a member_name.")
        if bitwidth > word_bw:
            raise ValueError(
                f"Field '{member_name}' with bitwidth {bitwidth} cannot fit into word_bw={word_bw}."
            )

        lines: list[str] = []
        curr_ipos = ipos0
        curr_iword = iword0

        if curr_ipos + bitwidth > word_bw:
            curr_iword += 1
            curr_ipos = 0

        if src_type == "stream" and curr_ipos == 0:
            lines.append(f"    w = {source}.read();")
        elif src_type == "axi4_stream" and curr_ipos == 0:
            lines.extend([
                "    if (last) {",
                "        tl = streamutils::tlast_status::tlast_early;",
                "        return;",
                "    }",
                "    {",
                f"        auto axis_word = {source}.read();",
                "        w = axis_word.data;",
                "        last = axis_word.last;",
                "    }",
            ])

        word_expr = f"{source}[{curr_iword}]" if src_type == "array" else "w"

        if curr_ipos == 0 and bitwidth == word_bw:
            rhs_expr = word_expr
        else:
            high = curr_ipos + bitwidth - 1
            low = curr_ipos
            rhs_expr = f"{word_expr}.range({high}, {low})"

        assign_expr = cls.from_uint_expr(rhs_expr)
        lines.append(f"    {prefix}{member_name} = {assign_expr};")

        curr_ipos += bitwidth
        if curr_ipos == word_bw:
            curr_iword += 1
            curr_ipos = 0

        return lines, curr_ipos, curr_iword

    def _serialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        bitwidth = self.__class__.get_bitwidth()
        if bitwidth > word_bw:
            raise ValueError(
                f"Field '{self.__class__.__name__}' with bitwidth {bitwidth} cannot fit into word_bw={word_bw}."
            )

        curr_ipos = ipos0
        curr_iword = iword0

        if curr_ipos + bitwidth > word_bw:
            curr_iword += 1
            curr_ipos = 0

        while len(words) <= curr_iword:
            words.append(0)

        current_val = self.val
        mask = (1 << bitwidth) - 1
        field_bits = self._value_to_field_bits(current_val) & mask
        words[curr_iword] |= field_bits << curr_ipos

        curr_ipos += bitwidth
        if curr_ipos == word_bw:
            curr_iword += 1
            curr_ipos = 0

        return curr_ipos, curr_iword

    def _deserialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        bitwidth = self.__class__.get_bitwidth()
        if bitwidth > word_bw:
            raise ValueError(
                f"Field '{self.__class__.__name__}' with bitwidth {bitwidth} cannot fit into word_bw={word_bw}."
            )

        curr_ipos = ipos0
        curr_iword = iword0

        if curr_ipos + bitwidth > word_bw:
            curr_iword += 1
            curr_ipos = 0

        word = 0 if curr_iword >= len(words) else words[curr_iword]
        mask = (1 << bitwidth) - 1
        field_bits = (word >> curr_ipos) & mask
        self.val = self._field_bits_to_value(field_bits)

        curr_ipos += bitwidth
        if curr_ipos == word_bw:
            curr_iword += 1
            curr_ipos = 0

        return curr_ipos, curr_iword

    @classmethod
    def get_bitwidth(cls) -> int:
        if cls.bitwidth is None:
            raise TypeError(f"{cls.__name__} does not define a class-level bitwidth.")
        return cls.bitwidth

    @classmethod
    def cpp_class_name(cls) -> str:
        if cls.cpp_repr is not None:
            return cls.cpp_repr
        if cls.cpp_type is None:
            raise TypeError(f"{cls.__name__} does not define a class-level cpp_type.")
        return cls.cpp_type

    def is_close(
        self,
        other: DataSchema,
        rel_tol: float | None = None,
        abs_tol: float | None = 1e-8,
    ) -> bool:
        _ = rel_tol, abs_tol
        if not isinstance(other, self.__class__):
            return False

        v1 = self.val
        v2 = other.val

        if isinstance(v1, np.ndarray) or isinstance(v2, np.ndarray):
            return bool(np.array_equal(np.asarray(v1), np.asarray(v2)))

        return v1 == v2


class IntField(DataField):
    """
    Integer scalar field for arbitrary bitwidth and signedness.

    ``specialize()`` creates a new subclass whose structural properties are fixed at
    the class level. Those subclasses can then be used directly in schema
    definitions.
    """

    bitwidth: ClassVar[int] = 32
    signed: ClassVar[bool] = True
    cpp_type: ClassVar[str] = "ap_int<32>"
    can_gen_include: ClassVar[bool] = False
    _specializations: ClassVar[dict[tuple[Any, ...], type[IntField]]] = {}

    @classmethod
    def _numpy_elem_dtype(cls) -> np.dtype | None:
        bw = cls.get_bitwidth()
        if bw > 64:
            return None  # multi-word ints: no single-dtype reinterpret; use the loop
        signed = cls.signed
        if bw <= 8:
            return np.dtype(np.int8 if signed else np.uint8)
        if bw <= 16:
            return np.dtype(np.int16 if signed else np.uint16)
        if bw <= 32:
            return np.dtype(np.int32 if signed else np.uint32)
        return np.dtype(np.int64 if signed else np.uint64)

    @classmethod
    @defer_if_symbolic
    def specialize(
        cls,
        bitwidth: int,
        signed: bool = True,
        **kwargs: Any,
    ) -> type[IntField]:
        """Return a cached specialized ``IntField`` subclass.

        Parameters
        ----------
        bitwidth : int
            Number of bits in the integer representation. Must be positive.
        signed : bool, default=True
            Whether the integer type is signed. If False, the specialized class
            represents an unsigned integer.
        **kwargs : Any
            Optional structural metadata overrides such as ``include_dir`` and
            ``include_filename``.

        Returns
        -------
        type[IntField]
            A specialized ``IntField`` subclass with class-level attributes such as
            ``bitwidth``, ``signed``, and ``cpp_type``.

        Notes
        -----
        Repeated calls with the same ``bitwidth`` and ``signed`` values return the
        same cached subclass.
        """
        if bitwidth <= 0:
            raise ValueError("bitwidth must be positive.")

        overrides = cls.validate_specialize_kwargs(kwargs)
        override_items = tuple(sorted(overrides.items()))

        key = (cls, int(bitwidth), bool(signed), override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        sign_prefix = "Int" if signed else "UInt"
        cpp_type = f"ap_int<{bitwidth}>" if signed else f"ap_uint<{bitwidth}>"
        subclass_name = f"{sign_prefix}{bitwidth}"

        specialized_attrs = cls.merge_specialize_attrs(
            {
                "bitwidth": int(bitwidth),
                "signed": bool(signed),
                "cpp_type": cpp_type,
                "__module__": cls.__module__,
                "__doc__": (
                    f"Specialized integer field: bitwidth={bitwidth}, signed={signed}."
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

        bitwidth = self.__class__.get_bitwidth()
        signed = self.__class__.signed

        if bitwidth <= 64:
            mask = (1 << bitwidth) - 1
            wrapped = int(value) & mask

            if signed:
                sign_bit = 1 << (bitwidth - 1)
                if wrapped & sign_bit:
                    wrapped -= 1 << bitwidth
                return np.int32(wrapped) if bitwidth <= 32 else np.int64(wrapped)

            return np.uint32(wrapped) if bitwidth <= 32 else np.uint64(wrapped)

        num_words = math.ceil(bitwidth / 64)

        if isinstance(value, np.ndarray):
            arr = np.array(value, dtype=np.uint64)
            if arr.size != num_words:
                raise ValueError(
                    f"Array size mismatch for {self.__class__.__name__}: "
                    f"expected {num_words} words, got {arr.size}."
                )
            return arr

        if isinstance(value, (list, tuple)):
            arr = np.array(value, dtype=np.uint64)
            if arr.size != num_words:
                raise ValueError(
                    f"Array size mismatch for {self.__class__.__name__}: "
                    f"expected {num_words} words, got {arr.size}."
                )
            return arr

        mask = (1 << bitwidth) - 1
        wrapped = int(value) & mask
        arr = np.zeros(num_words, dtype=np.uint64)
        for idx in range(num_words):
            arr[idx] = np.uint64((wrapped >> (64 * idx)) & 0xFFFFFFFFFFFFFFFF)
        return arr

    @classmethod
    def init_value(cls) -> Any:
        """Return the initial Python-side integer representation for this field."""
        bitwidth = cls.get_bitwidth()
        if bitwidth > 64:
            num_words = math.ceil(bitwidth / 64)
            return np.zeros(num_words, dtype=np.uint64)

        if cls.signed:
            return np.int32(0) if bitwidth <= 32 else np.int64(0)

        return np.uint32(0) if bitwidth <= 32 else np.uint64(0)

    @classmethod
    def to_uint_expr(cls, value_expr: str) -> str:
        return value_expr

    @classmethod
    def to_uint_value_expr(cls, value_expr: str) -> str:
        return value_expr

    def _value_to_field_bits(self, current_val: Any) -> int:
        bitwidth = self.__class__.get_bitwidth()
        if bitwidth > 64 and isinstance(current_val, np.ndarray):
            field_bits = 0
            for idx, word in enumerate(current_val.astype(np.uint64)):
                field_bits |= int(word) << (64 * idx)
            return field_bits
        return int(current_val)

    @classmethod
    def _json_load_expr(cls, json_var: str, pos_var: str) -> str:
        if cls.signed:
            return (
                f"static_cast<{cls.cpp_class_name()}>"
                f"(static_cast<long long>(streamutils::json_parse_number({json_var}, {pos_var})))"
            )
        return (
            f"static_cast<{cls.cpp_class_name()}>"
            f"(static_cast<unsigned long long>(streamutils::json_parse_number({json_var}, {pos_var})))"
        )

    @classmethod
    def _gen_write_recursive(
        cls,
        word_bw: int,
        dst_type: str = "array",
        target: str = "x",
        ipos0: int = 0,
        iword0: int = 0,
        prefix: str = "",
        member_name: str | None = None,
    ) -> tuple[list[str], int, int]:
        bitwidth = cls.get_bitwidth()
        if bitwidth <= word_bw:
            return super()._gen_write_recursive(
                word_bw=word_bw,
                dst_type=dst_type,
                target=target,
                ipos0=ipos0,
                iword0=iword0,
                prefix=prefix,
                member_name=member_name,
            )

        if member_name is None:
            raise ValueError(f"{cls.__name__} write generation requires a member_name.")

        lines: list[str] = []
        curr_ipos = ipos0
        curr_iword = iword0
        if curr_ipos > 0:
            if dst_type == "stream":
                lines.append(f"    {target}.write(w);")
                lines.append("    w = 0;")
            elif dst_type == "axi4_stream":
                lines.append(f"    streamutils::write_axi4_word<{word_bw}>({target}, w, false);")
                lines.append("    w = 0;")
            curr_iword += 1
            curr_ipos = 0

        value_expr = cls.to_uint_expr(f"{prefix}{member_name}")
        nwords = math.ceil(bitwidth / word_bw)
        for word_idx in range(nwords):
            low = word_idx * word_bw
            high = min(low + word_bw, bitwidth) - 1
            slice_expr = f"{value_expr}.range({high}, {low})"
            if dst_type == "array":
                lines.append(f"    {target}[{curr_iword}] = {slice_expr};")
            elif dst_type == "stream":
                lines.append(f"    w = {slice_expr};")
                lines.append(f"    {target}.write(w);")
                lines.append("    w = 0;")
            else:
                lines.append(f"    w = {slice_expr};")
                lines.append(f"    streamutils::write_axi4_word<{word_bw}>({target}, w, false);")
                lines.append("    w = 0;")
            curr_iword += 1

        return lines, curr_ipos, curr_iword

    @classmethod
    def _gen_read_recursive(
        cls,
        word_bw: int,
        src_type: str = "array",
        source: str = "x",
        ipos0: int = 0,
        iword0: int = 0,
        prefix: str = "",
        member_name: str | None = None,
    ) -> tuple[list[str], int, int]:
        bitwidth = cls.get_bitwidth()
        if bitwidth <= word_bw:
            return super()._gen_read_recursive(
                word_bw=word_bw,
                src_type=src_type,
                source=source,
                ipos0=ipos0,
                iword0=iword0,
                prefix=prefix,
                member_name=member_name,
            )

        if member_name is None:
            raise ValueError(f"{cls.__name__} read generation requires a member_name.")

        lines = [f"    ap_uint<{bitwidth}> field_bits = 0;"]
        curr_ipos = ipos0
        curr_iword = iword0
        if curr_ipos > 0:
            curr_iword += 1
            curr_ipos = 0

        nwords = math.ceil(bitwidth / word_bw)
        for word_idx in range(nwords):
            low = word_idx * word_bw
            high = min(low + word_bw, bitwidth) - 1
            width = high - low + 1

            if src_type == "array":
                lines.append(
                    f"    field_bits.range({high}, {low}) = {source}[{curr_iword}].range({width - 1}, 0);"
                )
            else:
                if src_type == "axi4_stream":
                    lines.extend([
                        "    if (last) {",
                        "        tl = streamutils::tlast_status::tlast_early;",
                        "        return;",
                        "    }",
                    ])
                    lines.append("    {")
                    lines.append(f"        auto axis_word = {source}.read();")
                    lines.append("        w = axis_word.data;")
                    lines.append("        last = axis_word.last;")
                    lines.append("    }")
                else:
                    lines.append(f"    w = {source}.read();")
                lines.append(f"    field_bits.range({high}, {low}) = w.range({width - 1}, 0);")
            curr_iword += 1

        lines.append(f"    {prefix}{member_name} = {cls.from_uint_expr('field_bits')};")
        return cls._scope_local_lines(lines), curr_ipos, curr_iword

    def _serialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        bitwidth = self.__class__.get_bitwidth()
        if bitwidth <= word_bw:
            return super()._serialize_recursive(
                word_bw=word_bw,
                words=words,
                ipos0=ipos0,
                iword0=iword0,
            )

        curr_ipos = ipos0
        curr_iword = iword0
        if curr_ipos > 0:
            curr_iword += 1
            curr_ipos = 0

        nwords = math.ceil(bitwidth / word_bw)
        while len(words) <= curr_iword + nwords - 1:
            words.append(0)

        field_bits = self._value_to_field_bits(self.val) & ((1 << bitwidth) - 1)
        mask = (1 << word_bw) - 1
        for word_idx in range(nwords):
            words[curr_iword] = (field_bits >> (word_idx * word_bw)) & mask
            curr_iword += 1

        return curr_ipos, curr_iword

    def _deserialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        bitwidth = self.__class__.get_bitwidth()
        if bitwidth <= word_bw:
            return super()._deserialize_recursive(
                word_bw=word_bw,
                words=words,
                ipos0=ipos0,
                iword0=iword0,
            )

        curr_ipos = ipos0
        curr_iword = iword0
        if curr_ipos > 0:
            curr_iword += 1
            curr_ipos = 0

        field_bits = 0
        nwords = math.ceil(bitwidth / word_bw)
        for word_idx in range(nwords):
            chunk_low = word_idx * word_bw
            chunk_width = min(word_bw, bitwidth - chunk_low)
            mask = (1 << chunk_width) - 1
            word = 0 if curr_iword >= len(words) else words[curr_iword]
            field_bits |= (word & mask) << chunk_low
            curr_iword += 1

        self.val = self._field_bits_to_value(field_bits)
        return curr_ipos, curr_iword


class MemAddr(IntField):
    """Unsigned address field specialized by bitwidth."""

    bitwidth: ClassVar[int] = 64
    signed: ClassVar[bool] = False
    cpp_type: ClassVar[str] = "ap_uint<64>"
    can_gen_include: ClassVar[bool] = False
    _specializations: ClassVar[dict[tuple[Any, ...], type[MemAddr]]] = {}

    @classmethod
    @defer_if_symbolic
    def specialize(cls, bitwidth: int = 64, **kwargs: Any) -> type[MemAddr]:
        """Return a cached specialized ``MemAddr`` subclass.

        Parameters
        ----------
        bitwidth : int, default=64
            Address width in bits. Must be positive.
        **kwargs : Any
            Optional structural metadata overrides such as ``include_dir`` and
            ``include_filename``.

        Returns
        -------
        type[MemAddr]
            A specialized unsigned address field subclass.
        """
        if bitwidth <= 0:
            raise ValueError("bitwidth must be positive.")

        overrides = cls.validate_specialize_kwargs(kwargs)
        override_items = tuple(sorted(overrides.items()))
        key = (cls, int(bitwidth), override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        subclass_name = f"MemAddr{bitwidth}"
        specialized_attrs = cls.merge_specialize_attrs(
            {
                "bitwidth": int(bitwidth),
                "signed": False,
                "cpp_type": f"ap_uint<{bitwidth}>",
                "__module__": cls.__module__,
                "__doc__": f"Specialized address field: bitwidth={bitwidth}.",
            },
            overrides,
        )
        specialized = type(subclass_name, (cls,), specialized_attrs)
        cls._specializations[key] = specialized
        return specialized


class FloatField(DataField):
    """Floating-point scalar field specialized by bitwidth."""

    bitwidth: ClassVar[int] = 32
    cpp_type: ClassVar[str] = "float"
    can_gen_include: ClassVar[bool] = False
    _specializations: ClassVar[dict[tuple[Any, ...], type[FloatField]]] = {}

    @classmethod
    def _numpy_elem_dtype(cls) -> np.dtype | None:
        return np.dtype(np.float64 if cls.get_bitwidth() == 64 else np.float32)

    @classmethod
    @defer_if_symbolic
    def specialize(cls, bitwidth: int = 32, **kwargs: Any) -> type[FloatField]:
        """Return a cached specialized ``FloatField`` subclass.

        Parameters
        ----------
        bitwidth : int, default=32
            Floating-point bitwidth. Supported values are ``32`` and ``64``.
        **kwargs : Any
            Optional structural metadata overrides such as ``include_dir`` and
            ``include_filename``.

        Returns
        -------
        type[FloatField]
            A specialized ``FloatField`` subclass with class-level attributes such
            as ``bitwidth`` and ``cpp_type``.

        Notes
        -----
        Repeated calls with the same ``bitwidth`` return the same cached subclass.
        """
        if bitwidth not in (32, 64):
            raise ValueError("FloatField only supports 32 or 64 bit widths.")

        overrides = cls.validate_specialize_kwargs(kwargs)
        override_items = tuple(sorted(overrides.items()))

        key = (cls, int(bitwidth), override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        cpp_type = "double" if bitwidth == 64 else "float"
        subclass_name = f"Float{bitwidth}"
        specialized_attrs = cls.merge_specialize_attrs(
            {
                "bitwidth": int(bitwidth),
                "cpp_type": cpp_type,
                "__module__": cls.__module__,
                "__doc__": f"Specialized floating-point field: bitwidth={bitwidth}.",
            },
            overrides,
        )
        specialized = type(subclass_name, (cls,), specialized_attrs)
        cls._specializations[key] = specialized
        return specialized

    def _convert(self, value: Any) -> Any:
        return np.float64(value) if self.__class__.get_bitwidth() == 64 else np.float32(value)

    @classmethod
    def init_value(cls) -> Any:
        """Return the initial Python-side floating-point representation."""
        return np.float64(0.0) if cls.get_bitwidth() == 64 else np.float32(0.0)

    @classmethod
    def to_uint_expr(cls, value_expr: str) -> str:
        if cls.get_bitwidth() == 64:
            return f"streamutils::double_to_uint({value_expr})"
        return f"streamutils::float_to_uint({value_expr})"

    @classmethod
    def to_uint_value_expr(cls, value_expr: str) -> str:
        return cls.to_uint_expr(value_expr)

    @classmethod
    def from_uint_expr(cls, uint_expr: str) -> str:
        if cls.get_bitwidth() == 64:
            return f"streamutils::uint_to_double((uint64_t)({uint_expr}))"
        if cls.get_bitwidth() != 32:
            raise ValueError("FloatField unpack supports only bitwidth 32 or 64.")
        return f"streamutils::uint_to_float((uint32_t)({uint_expr}))"

    def _value_to_field_bits(self, current_val: Any) -> int:
        bitwidth = self.__class__.get_bitwidth()
        if bitwidth == 32:
            return int(np.asarray(np.float32(current_val), dtype=np.float32).view(np.uint32))
        if bitwidth == 64:
            return int(np.asarray(np.float64(current_val), dtype=np.float64).view(np.uint64))
        raise ValueError(f"Unsupported FloatField bitwidth={bitwidth} for serialization.")

    def _field_bits_to_value(self, field_bits: int) -> Any:
        bitwidth = self.__class__.get_bitwidth()
        if bitwidth == 32:
            return np.asarray(np.uint32(field_bits), dtype=np.uint32).view(np.float32).item()
        if bitwidth == 64:
            return np.asarray(np.uint64(field_bits), dtype=np.uint64).view(np.float64).item()
        raise ValueError(f"Unsupported FloatField bitwidth={bitwidth} for deserialization.")

    def is_close(
        self,
        other: DataSchema,
        rel_tol: float | None = None,
        abs_tol: float | None = 1e-8,
    ) -> bool:
        if not isinstance(other, FloatField):
            return False

        if self.__class__.get_bitwidth() != other.__class__.get_bitwidth():
            return False

        v1 = float(self.val)
        v2 = float(other.val)

        if rel_tol is None and abs_tol is None:
            return v1 == v2

        kwargs: dict[str, float] = {}
        if rel_tol is not None:
            kwargs["rtol"] = rel_tol
        if abs_tol is not None:
            kwargs["atol"] = abs_tol
        return bool(np.isclose(v1, v2, **kwargs))


class EnumField(DataField):
    """Enum scalar field specialized by IntEnum type and optional bitwidth/default."""

    enum_type: ClassVar[type[IntEnum] | None] = None
    default_member: ClassVar[IntEnum | None] = None
    bitwidth: ClassVar[int | None] = None
    cpp_type: ClassVar[str | None] = None
    _specializations: ClassVar[dict[tuple[Any, ...], type[EnumField]]] = {}

    @classmethod
    def get_dependencies(cls) -> list[type[DataSchema]]:
        return []

    @classmethod
    @defer_if_symbolic
    def specialize(
        cls,
        enum_type: type[IntEnum],
        bitwidth: int | None = None,
        default: IntEnum | None = None,
        **kwargs: Any,
    ) -> type[EnumField]:
        """Return a cached specialized ``EnumField`` subclass.

        Parameters
        ----------
        enum_type : type[IntEnum]
            Enum type used by this field specialization. The enum must derive from
            ``IntEnum`` and currently must have non-negative values.
        bitwidth : int | None, optional
            Storage bitwidth for the enum field. If omitted, the minimum bitwidth
            required to represent the enum values is used.
        default : IntEnum | None, optional
            Default enum member used by ``init_value()``. If omitted, the first
            enum member is used.
        **kwargs : Any
            Optional structural metadata overrides such as ``include_dir`` and
            ``include_filename``.

        Returns
        -------
        type[EnumField]
            A specialized ``EnumField`` subclass with class-level attributes such
            as ``enum_type``, ``bitwidth``, ``cpp_type``, and ``default_member``.

        Notes
        -----
        Repeated calls with the same ``enum_type``, resolved ``bitwidth``, and
        default member return the same cached subclass.
        """
        if not issubclass(enum_type, IntEnum):
            raise TypeError("EnumField requires enum_type to derive from IntEnum.")

        enum_values = [int(member.value) for member in enum_type]
        if any(value < 0 for value in enum_values):
            raise ValueError("EnumField currently supports only non-negative IntEnum values.")

        min_width = (max(enum_values).bit_length() or 1) if enum_values else 1
        resolved_bitwidth = min_width if bitwidth is None else int(bitwidth)
        if resolved_bitwidth < min_width:
            raise ValueError(
                f"bitwidth={resolved_bitwidth} is too small for enum {enum_type.__name__}; "
                f"needs at least {min_width} bits."
            )

        resolved_default = list(enum_type)[0] if default is None else enum_type(default)
        overrides = cls.validate_specialize_kwargs(kwargs)
        enum_defaults = {
            "cpp_repr": enum_type.__name__,
            "include_filename": f"{cls._camel_to_snake(enum_type.__name__)}.h",
        }
        resolved_overrides = dict(enum_defaults)
        resolved_overrides.update(overrides)
        override_items = tuple(sorted(overrides.items()))
        resolved_override_items = tuple(sorted(resolved_overrides.items()))
        key = (
            cls,
            enum_type,
            resolved_bitwidth,
            int(resolved_default.value),
            resolved_override_items,
        )
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        subclass_name = f"{enum_type.__name__}EnumField"
        specialized_attrs = cls.merge_specialize_attrs(
            {
                "enum_type": enum_type,
                "default_member": resolved_default,
                "bitwidth": resolved_bitwidth,
                "cpp_type": enum_type.__name__,
                "__module__": cls.__module__,
                "__doc__": (
                    f"Specialized enum field: enum_type={enum_type.__name__}, "
                    f"bitwidth={resolved_bitwidth}."
                ),
            },
            resolved_overrides,
        )
        specialized = type(subclass_name, (cls,), specialized_attrs)
        cls._specializations[key] = specialized
        return specialized

    def _convert(self, value: Any) -> IntEnum:
        enum_type = self.__class__.enum_type
        if enum_type is None:
            raise TypeError(f"{self.__class__.__name__} does not define enum_type.")
        try:
            return enum_type(value)
        except ValueError as exc:
            raise ValueError(
                f"Value {value!r} is not a valid member of {enum_type.__name__}."
            ) from exc

    @classmethod
    def init_value(cls) -> IntEnum:
        """Return the initial enum member for this field."""
        if cls.default_member is None:
            raise TypeError(f"{cls.__name__} does not define a default enum member.")
        return cls.default_member

    @classmethod
    def to_uint_expr(cls, value_expr: str) -> str:
        return f"(ap_uint<{cls.get_bitwidth()}>)(static_cast<unsigned int>({value_expr}))"

    @classmethod
    def to_uint_value_expr(cls, value_expr: str) -> str:
        return f"(ap_uint<{cls.get_bitwidth()}>)(static_cast<unsigned int>({value_expr}))"

    @classmethod
    def from_uint_expr(cls, uint_expr: str) -> str:
        return f"static_cast<{cls.cpp_class_name()}>(static_cast<unsigned int>({uint_expr}))"

    @classmethod
    def _gen_include_decl(cls, word_bw_supported: list[int] | None = None, framed: bool = False) -> str:
        enum_type = cls.enum_type
        if enum_type is None:
            raise TypeError(f"{cls.__name__} does not define enum_type.")
        _ = word_bw_supported
        lines = [f"enum class {cls.cpp_class_name()} {{"]
        for member in enum_type:
            lines.append(f"    {member.name} = {member.value},")
        lines.append("};")
        return "\n".join(lines)

    @classmethod
    def _gen_tb_member_definitions(cls, indent_level: int = 0) -> str:
        enum_type = cls.enum_type
        if enum_type is None:
            raise TypeError(f"{cls.__name__} does not define enum_type.")

        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)
        lines = [
            f"{indent}inline const char* enum_to_string({cls.cpp_class_name()} value) {{",
            f"{i1}switch (value) {{",
        ]
        for member in enum_type:
            lines.extend([
                f"{i1}case {cls.cpp_class_name()}::{member.name}:",
                f"{i1}    return \"{member.name}\";",
            ])
        lines.extend([
            f"{i1}default:",
            f"{i1}    return \"UNKNOWN\";",
            f"{i1}}}",
            f"{indent}}}",
        ])
        return "\n".join(lines)

    def is_close(
        self,
        other: DataSchema,
        rel_tol: float | None = None,
        abs_tol: float | None = 1e-8,
    ) -> bool:
        _ = rel_tol, abs_tol
        if not isinstance(other, EnumField):
            return False

        if self.__class__.enum_type is not other.__class__.enum_type:
            return False

        return self.val == other.val

    @classmethod
    def _json_load_expr(cls, json_var: str, pos_var: str) -> str:
        return (
            f"static_cast<{cls.cpp_class_name()}>"
            f"(static_cast<long long>(streamutils::json_parse_number({json_var}, {pos_var})))"
        )


class BooleanField(IntField):
    """A 1-bit boolean flag field.

    A fixed ``ap_uint<1>`` :class:`IntField` (exact 1-bit ``m_axi`` packing) whose runtime
    value is a Python ``bool``.  ``_convert`` accepts ``bool`` / ``0`` / ``1`` and rejects
    any other value; it serializes / deserializes and code-generates like any other field
    (the stored bit is ``0`` / ``1``).  Use it directly (``BooleanField``) for a flag, the
    same way a default ``IntField`` is a 32-bit int.
    """

    bitwidth: ClassVar[int] = 1
    signed: ClassVar[bool] = False
    cpp_type: ClassVar[str] = "ap_uint<1>"
    can_gen_include: ClassVar[bool] = False
    _specializations: ClassVar[dict[tuple[Any, ...], type[BooleanField]]] = {}

    @classmethod
    def specialize(cls, **kwargs: Any) -> type[BooleanField]:  # type: ignore[override]
        """Return a cached ``BooleanField`` subclass.

        The width is fixed at 1 bit; only structural metadata overrides (such as
        ``include_dir`` / ``include_filename``) vary, so unlike :class:`IntField` this
        takes no ``bitwidth`` / ``signed`` parameters.
        """
        overrides = cls.validate_specialize_kwargs(kwargs)
        override_items = tuple(sorted(overrides.items()))
        key = (cls, override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        specialized_attrs = cls.merge_specialize_attrs(
            {
                "bitwidth": 1,
                "signed": False,
                "cpp_type": "ap_uint<1>",
                "__module__": cls.__module__,
                "__doc__": "1-bit boolean field (ap_uint<1>).",
            },
            overrides,
        )
        specialized = type("Boolean", (cls,), specialized_attrs)
        cls._specializations[key] = specialized
        return specialized

    def _convert(self, value: Any) -> bool:
        """Coerce to a Python ``bool``; accept ``bool`` / ``0`` / ``1``, reject the rest."""
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        if isinstance(value, (int, np.integer)) and int(value) in (0, 1):
            return bool(value)
        raise ValueError(
            f"{type(self).__name__} accepts only bool or 0/1, got {value!r}."
        )

    @classmethod
    def init_value(cls) -> bool:
        """Return the initial Python-side boolean value (``False``)."""
        return False


class DataList(DataSchema):
    """Structured aggregate with schema defined entirely at the class level.

    ``elements`` maps member names either to ``DataSchema`` subclasses or to
    metadata dictionaries containing a ``schema`` entry and optional metadata
    such as ``description``. Instances create child runtime objects from that
    class-level declaration.
    """

    element_inline_comment_threshold: ClassVar[int] = 56
    elements: ClassVar[dict[str, type[DataSchema] | Mapping[str, Any]]] = {}

    def __init__(self, **values: Any):
        object.__setattr__(self, "_children", {})

        for name, schema_cls in self._iter_element_schemas():
            child = schema_cls()
            self._children[name] = child

        for name, value in values.items():
            setattr(self, name, value)

    @classmethod
    def _normalize_element_definition(cls, name: str) -> dict[str, type[DataSchema] | str | None]:
        raw_definition = cls.elements[name]

        if isinstance(raw_definition, Mapping):
            unknown = sorted(set(raw_definition) - {"schema", "description"})
            if unknown:
                unknown_str = ", ".join(unknown)
                raise TypeError(
                    f"{cls.__name__}.elements['{name}'] has unsupported metadata key(s): "
                    f"{unknown_str}. Allowed keys: description, schema."
                )
            if "schema" not in raw_definition:
                raise TypeError(
                    f"{cls.__name__}.elements['{name}'] metadata must define a 'schema' entry."
                )
            schema_cls = raw_definition["schema"]
            description = raw_definition.get("description")
        else:
            schema_cls = raw_definition
            description = None

        if not isinstance(schema_cls, type) or not issubclass(schema_cls, DataSchema):
            raise TypeError(
                f"{cls.__name__}.elements['{name}']['schema'] must be a DataSchema subclass."
            )

        if description is not None:
            if not isinstance(description, str):
                raise TypeError(
                    f"{cls.__name__}.elements['{name}']['description'] must be a string if provided."
                )
            description = description.strip() or None

        return {
            "schema": schema_cls,
            "description": description,
        }

    @classmethod
    def _iter_elements(cls) -> list[tuple[str, dict[str, type[DataSchema] | str | None]]]:
        items = list(cls.elements.items())
        normalized: list[tuple[str, dict[str, type[DataSchema] | str | None]]] = []
        for name, _ in items:
            if not isinstance(name, str):
                raise TypeError(f"{cls.__name__}.elements keys must be strings.")
            normalized.append((name, cls._normalize_element_definition(name)))
        return normalized

    @classmethod
    def _iter_element_schemas(cls) -> list[tuple[str, type[DataSchema]]]:
        return [
            (name, definition["schema"])
            for name, definition in cls._iter_elements()
        ]

    @classmethod
    def get_element_schema(cls, name: str) -> type[DataSchema]:
        """Return the schema class for a named element."""
        try:
            return cls._normalize_element_definition(name)["schema"]
        except KeyError as exc:
            raise KeyError(f"{cls.__name__}.elements has no entry named '{name}'.") from exc

    @classmethod
    def get_element_description(cls, name: str) -> str | None:
        """Return the normalized description for a named element."""
        try:
            return cls._normalize_element_definition(name)["description"]
        except KeyError as exc:
            raise KeyError(f"{cls.__name__}.elements has no entry named '{name}'.") from exc

    @classmethod
    def get_element_definition(cls, name: str) -> dict[str, type[DataSchema] | str | None]:
        """Return the normalized metadata dictionary for a named element."""
        try:
            return dict(cls._normalize_element_definition(name))
        except KeyError as exc:
            raise KeyError(f"{cls.__name__}.elements has no entry named '{name}'.") from exc

    @classmethod
    def _format_member_comment(cls, description: str | None) -> list[str]:
        if description is None:
            return []
        if len(description) <= cls.element_inline_comment_threshold:
            return [f"//INLINE// {description}"]
        return [f"// {description}"]

    @classmethod
    def _gen_dump_json_recursive(
        cls,
        prefix: str,
        os_name: str,
        depth_expr: str,
        indent_var: str,
    ) -> list[str]:
        lines: list[str] = [f'{os_name} << "{{";']

        elements = cls._iter_element_schemas()
        if elements:
            lines.append(f'{os_name} << "\\n";')

        for idx, (name, schema_cls) in enumerate(elements):
            lines.append(
                f"for (int i = 0; i < ({depth_expr} + 1) * {indent_var}; ++i) {{ {os_name} << ' '; }}"
            )
            lines.append(f'{os_name} << "\\"{name}\\": ";')

            if issubclass(schema_cls, DataList):
                lines.append(f"{prefix}{name}.dump_json({os_name}, {indent_var}, {depth_expr} + 1);")
            else:
                lines.extend(
                    schema_cls._gen_dump_json_recursive(
                        prefix=prefix,
                        member_name=name,
                        os_name=os_name,
                        depth_expr=f"{depth_expr} + 1",
                        indent_var=indent_var,
                    )
                )

            if idx < len(elements) - 1:
                lines.append(f'{os_name} << ",";')
            lines.append(f'{os_name} << "\\n";')

        if elements:
            lines.append(
                f"for (int i = 0; i < ({depth_expr}) * {indent_var}; ++i) {{ {os_name} << ' '; }}"
            )

        lines.append(f'{os_name} << "}}";')
        return lines

    @classmethod
    def _gen_load_json_recursive(
        cls,
        prefix: str,
        json_var: str,
        pos_var: str,
        ctx: str,
    ) -> list[str]:
        lines: list[str] = [
            f"streamutils::json_expect_char({json_var}, {pos_var}, '{{');",
        ]

        elements = cls._iter_element_schemas()
        seen_flags = [f"seen_{ctx}_{name}" for name, _ in elements]
        for flag in seen_flags:
            lines.append(f"bool {flag} = false;")

        lines.extend([
            "bool first = true;",
            "while (true) {",
            f"    streamutils::json_skip_ws({json_var}, {pos_var});",
            f"    if ({pos_var} < {json_var}.size() && {json_var}[{pos_var}] == '}}') {{",
            f"        ++{pos_var};",
            "        break;",
            "    }",
            "    if (!first) {",
            f"        streamutils::json_expect_char({json_var}, {pos_var}, ',');",
            "    }",
            "    first = false;",
            f"    std::string key = streamutils::json_parse_string({json_var}, {pos_var});",
            f"    streamutils::json_expect_char({json_var}, {pos_var}, ':');",
        ])

        for idx, (name, schema_cls) in enumerate(elements):
            cond = "if" if idx == 0 else "else if"
            elem_prefix = prefix if not issubclass(schema_cls, DataList) else f"{prefix}{name}."

            lines.append(f'    {cond} (key == "{name}") {{')
            lines.append(f"        seen_{ctx}_{name} = true;")

            if issubclass(schema_cls, DataList):
                lines.append(f"        {elem_prefix[:-1]}.load_json({json_var}, {pos_var});")
            else:
                child_ctx = f"{ctx}_{name}"
                child_lines = schema_cls._gen_load_json_recursive(
                    prefix=prefix,
                    member_name=name,
                    json_var=json_var,
                    pos_var=pos_var,
                    ctx=child_ctx,
                )
                for child_line in child_lines:
                    lines.append(f"        {child_line}")

            lines.append("    }")

        lines.extend([
            "    else {",
            '        throw std::runtime_error("Malformed JSON: unexpected key for schema.");',
            "    }",
            "}",
        ])

        for name, _ in elements:
            lines.extend([
                f"if (!seen_{ctx}_{name}) {{",
                f'    throw std::runtime_error("Malformed JSON: missing required key \'{name}\'.");',
                "}",
            ])

        return lines

    @classmethod
    def gen_dump_json(cls, indent_level: int = 0) -> str:
        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)

        lines = [
            f"{indent}void dump_json(std::ostream& os, int indent = 2, int level = 0) const {{",
            f"{i1}const int step = (indent < 0) ? 0 : indent;",
        ]

        for line in cls._gen_dump_json_recursive(
            prefix="this->",
            os_name="os",
            depth_expr="level",
            indent_var="step",
        ):
            if line.startswith("    "):
                line = line[4:]
            lines.append(f"{i1}{line}" if line else "")

        lines.append(f"{indent}}}")
        return "\n".join(lines)

    @classmethod
    def gen_load_json(cls, indent_level: int = 0) -> str:
        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)

        lines = [
            f"{indent}void load_json(const std::string& json_text, size_t& pos) {{",
        ]

        for line in cls._gen_load_json_recursive(
            prefix="this->",
            json_var="json_text",
            pos_var="pos",
            ctx="root",
        ):
            if line.startswith("    "):
                line = line[4:]
            lines.append(f"{i1}{line}" if line else "")

        lines.extend([
            f"{indent}}}",
            "",
            f"{indent}void load_json(std::istream& is) {{",
            f"{i1}std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());",
            f"{i1}size_t pos = 0;",
            f"{i1}streamutils::json_skip_ws(json_text, pos);",
            f"{i1}this->load_json(json_text, pos);",
            f"{i1}streamutils::json_skip_ws(json_text, pos);",
            f"{i1}if (pos != json_text.size()) {{",
            f'{i1}    throw std::runtime_error("Trailing characters after JSON object.");',
            f"{i1}}}",
            f"{indent}}}",
        ])
        return "\n".join(lines)

    @classmethod
    def get_bitwidth(cls) -> int:
        return sum(schema_cls.get_bitwidth() for _, schema_cls in cls._iter_element_schemas())

    @classmethod
    def get_dependencies(cls) -> list[type[DataSchema]]:
        deps: list[type[DataSchema]] = []
        seen: set[type[DataSchema]] = set()
        for _, schema_cls in cls._iter_element_schemas():
            if not schema_cls.can_gen_include or schema_cls is cls or schema_cls in seen:
                continue
            seen.add(schema_cls)
            deps.append(schema_cls)
        return deps

    @classmethod
    def get_utility_includes(cls) -> list[str]:
        """Recursively collect utility-include paths from every member schema.

        Dedup happens at the consumer (``_collect_utility_includes`` in
        ``hwgen.py``); this method may return duplicates across members.
        """
        paths: list[str] = []
        for _, schema_cls in cls._iter_element_schemas():
            paths.extend(schema_cls.get_utility_includes())
        return paths

    @classmethod
    def gen_pack(cls, indent_level: int = 0) -> str:
        indent = cls._get_indent(indent_level)
        inner_indent = cls._get_indent(indent_level + 1)

        lines = [
            f"{indent}static ap_uint<bitwidth> pack_to_uint(const {cls.cpp_class_name()}& data) {{",
            f"{inner_indent}ap_uint<bitwidth> res = 0;",
        ]

        current_lsb = 0
        for name, schema_cls in cls._iter_element_schemas():
            width = schema_cls.get_bitwidth()
            high = current_lsb + width - 1
            low = current_lsb
            expr = schema_cls.to_uint_expr(f"data.{name}")
            lines.append(f"{inner_indent}res.range({high}, {low}) = {expr};")
            current_lsb += width

        lines.append(f"{inner_indent}return res;")
        lines.append(f"{indent}}}")
        return "\n".join(lines)

    @classmethod
    def gen_unpack(cls, indent_level: int = 0) -> str:
        indent = cls._get_indent(indent_level)
        inner_indent = cls._get_indent(indent_level + 1)

        lines = [
            f"{indent}static {cls.cpp_class_name()} unpack_from_uint(const ap_uint<bitwidth>& packed) {{",
            f"{inner_indent}{cls.cpp_class_name()} data;",
        ]

        current_lsb = 0
        for name, schema_cls in cls._iter_element_schemas():
            width = schema_cls.get_bitwidth()
            high = current_lsb + width - 1
            low = current_lsb
            rhs_expr = schema_cls.from_uint_expr(f"packed.range({high}, {low})")
            lines.append(f"{inner_indent}data.{name} = {rhs_expr};")
            current_lsb += width

        lines.append(f"{inner_indent}return data;")
        lines.append(f"{indent}}}")
        return "\n".join(lines)

    @classmethod
    def _gen_write_recursive(
        cls,
        word_bw: int,
        dst_type: str = "array",
        target: str = "x",
        ipos0: int = 0,
        iword0: int = 0,
        prefix: str = "",
        member_name: str | None = None,
    ) -> tuple[list[str], int, int]:
        _ = member_name
        lines: list[str] = []
        curr_ipos = ipos0
        curr_iword = iword0

        element_schemas = cls._iter_element_schemas()
        for idx, (name, schema_cls) in enumerate(element_schemas):
            if issubclass(schema_cls, DataList):
                elem_prefix = f"{prefix}{name}."
                elem_member_name = None
            else:
                elem_prefix = prefix
                elem_member_name = name

            elem_lines, curr_ipos, curr_iword = schema_cls._gen_write_recursive(
                word_bw=word_bw,
                dst_type=dst_type,
                target=target,
                ipos0=curr_ipos,
                iword0=curr_iword,
                prefix=elem_prefix,
                member_name=elem_member_name,
            )
            lines.extend(elem_lines)

        return lines, curr_ipos, curr_iword

    @classmethod
    def _gen_read_recursive(
        cls,
        word_bw: int,
        src_type: str = "array",
        source: str = "x",
        ipos0: int = 0,
        iword0: int = 0,
        prefix: str = "",
        member_name: str | None = None,
    ) -> tuple[list[str], int, int]:
        _ = member_name
        lines: list[str] = []
        curr_ipos = ipos0
        curr_iword = iword0

        element_schemas = cls._iter_element_schemas()
        for idx, (name, schema_cls) in enumerate(element_schemas):
            if issubclass(schema_cls, DataList):
                elem_prefix = f"{prefix}{name}."
                elem_member_name = None
            else:
                elem_prefix = prefix
                elem_member_name = name

            elem_lines, curr_ipos, curr_iword = schema_cls._gen_read_recursive(
                word_bw=word_bw,
                src_type=src_type,
                source=source,
                ipos0=curr_ipos,
                iword0=curr_iword,
                prefix=elem_prefix,
                member_name=elem_member_name,
            )
            lines.extend(elem_lines)

            if src_type == "axi4_stream":
                lines.append("    if (tl != streamutils::tlast_status::no_tlast) {")
                if idx < len(element_schemas) - 1:
                    lines.append("        tl = streamutils::tlast_status::tlast_early;")
                lines.append("        return;")
                lines.append("    }")

        return lines, curr_ipos, curr_iword

    @classmethod
    def _gen_include_decl(cls, word_bw_supported: list[int] | None = None, framed: bool = False) -> str:
        lines = [f"struct {cls.cpp_class_name()} {{"]
        for name, definition in cls._iter_elements():
            schema_cls = definition["schema"]
            description = definition["description"]
            comment_lines = cls._format_member_comment(description)
            if comment_lines and comment_lines[0].startswith("//INLINE// "):
                inline_comment = comment_lines[0][11:]
                lines.append(f"    {schema_cls.cpp_class_name()} {name};  // {inline_comment}")
            else:
                lines.extend(f"    {line}" for line in comment_lines)
                lines.append(f"    {schema_cls.cpp_class_name()} {name};")
        lines.append("")
        lines.append(f"    static constexpr int bitwidth = {cls.get_bitwidth()};")

        if word_bw_supported:
            lines.append("")
            lines.append("    template<int word_bw>")
            lines.append("    struct word_bw_tag {};")
            lines.append("")
            lines.append("    template<int word_bw>")
            lines.append("    static constexpr int nwords_value(word_bw_tag<word_bw>) {")
            lines.append('            static_assert(word_bw < 0, "Unsupported word_bw for nwords");')
            lines.append("            return 0;")
            lines.append("    }")
            for bw in word_bw_supported:
                lines.append("")
                lines.append(f"    static constexpr int nwords_value(word_bw_tag<{bw}>) {{")
                lines.append(f"            return {cls.nwords_per_inst(bw)};")
                lines.append("    }")
            lines.append("")
            lines.append("    template<int word_bw>")
            lines.append("    static constexpr int nwords() {")
            lines.append("        return nwords_value(word_bw_tag<word_bw>{});")
            lines.append("    }")

        pack_decl = cls.gen_pack(indent_level=1)
        if pack_decl:
            lines.append("")
            lines.extend(pack_decl.splitlines())

        unpack_decl = cls.gen_unpack(indent_level=1)
        if unpack_decl:
            lines.append("")
            lines.extend(unpack_decl.splitlines())

        if word_bw_supported:
            for dst_type in (("array", "stream", "axi4_stream") + (("framed_stream",) if framed else ())):
                lines.append("")
                lines.extend(
                    cls.gen_write(
                        dst_type=dst_type,
                        word_bw_supported=word_bw_supported,
                        indent_level=1,
                    ).splitlines()
                )

            for src_type in (("array", "stream", "axi4_stream") + (("framed_stream",) if framed else ())):
                lines.append("")
                lines.extend(
                    cls.gen_read(
                        src_type=src_type,
                        word_bw_supported=word_bw_supported,
                        indent_level=1,
                    ).splitlines()
                )

        tb_member_declarations = cls._gen_tb_member_declarations(indent_level=1)
        if tb_member_declarations:
            lines.extend([
                "",
                f"#ifdef {cls.tb_member_macro()}",
            ])
            lines.extend(tb_member_declarations.splitlines())
            lines.append("#endif")

        lines.append("};")
        return "\n".join(lines)

    @classmethod
    def _gen_tb_member_declarations(cls, indent_level: int = 0) -> str:
        return cls._gen_json_member_declarations(indent_level=indent_level)

    @classmethod
    def _gen_tb_member_definitions(cls, indent_level: int = 0) -> str:
        return cls._gen_json_member_definitions(indent_level=indent_level)

    @classmethod
    def init_value(cls) -> dict[str, Any]:
        """Return the initial nested Python representation for this aggregate."""
        return {name: schema_cls.init_value() for name, schema_cls in cls._iter_element_schemas()}

    def _serialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        curr_ipos = ipos0
        curr_iword = iword0

        for name, _ in self.__class__._iter_element_schemas():
            curr_ipos, curr_iword = self._children[name]._serialize_recursive(
                word_bw=word_bw,
                words=words,
                ipos0=curr_ipos,
                iword0=curr_iword,
            )

        return curr_ipos, curr_iword

    def _deserialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        curr_ipos = ipos0
        curr_iword = iword0

        for name, _ in self.__class__._iter_element_schemas():
            curr_ipos, curr_iword = self._children[name]._deserialize_recursive(
                word_bw=word_bw,
                words=words,
                ipos0=curr_ipos,
                iword0=curr_iword,
            )

        return curr_ipos, curr_iword

    def get_bitwidth_active(self) -> int:
        """Return the active bitwidth: sum of each child's active bitwidth.

        Overrides :meth:`DataSchema.get_bitwidth_active` so that a ``DataList``
        containing a :class:`VarDataArray` member propagates variable length
        upward automatically.
        """
        return sum(child.get_bitwidth_active() for child in self._children.values())

    def _active_size_recursive(
        self,
        word_bw: int,
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        """Walk each child's ``_active_size_recursive`` in order.

        Overrides :meth:`DataSchema._active_size_recursive` to chain children
        individually, so that a :class:`VarDataArray` child advances the cursor
        by its active (not max) size and the remaining fixed children continue
        from the updated cursor position.
        """
        curr_ipos = ipos0
        curr_iword = iword0
        for name, _ in self.__class__._iter_element_schemas():
            curr_ipos, curr_iword = self._children[name]._active_size_recursive(
                word_bw=word_bw,
                ipos0=curr_ipos,
                iword0=curr_iword,
            )
        return curr_ipos, curr_iword

    @classmethod
    def nwords_per_inst(cls, word_bw: int) -> int:
        """Return the maximum word count for one instance at ``word_bw``.

        Overrides the base implementation to use :meth:`_max_cursor_step` for
        each child schema so that :class:`VarDataArray` children contribute
        their worst-case (max-length) size rather than the default-instance
        (zero-length) size.
        """
        if word_bw <= 0:
            raise ValueError("word_bw must be positive.")
        curr_ipos = 0
        curr_iword = 0
        for _, schema_cls in cls._iter_element_schemas():
            curr_ipos, curr_iword = schema_cls._max_cursor_step(
                word_bw=word_bw, ipos0=curr_ipos, iword0=curr_iword
            )
        n = curr_iword + (1 if curr_ipos > 0 else 0)
        return n if n > 0 else 1

    @classmethod
    def _max_cursor_step(
        cls,
        word_bw: int,
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        """Advance cursor by the max layout of this ``DataList``."""
        curr_ipos = ipos0
        curr_iword = iword0
        for _, schema_cls in cls._iter_element_schemas():
            curr_ipos, curr_iword = schema_cls._max_cursor_step(
                word_bw=word_bw, ipos0=curr_ipos, iword0=curr_iword
            )
        return curr_ipos, curr_iword

    @property
    def val(self) -> dict[str, Any]:
        """Return a nested runtime snapshot of child values."""
        out: dict[str, Any] = {}
        for name, child in self._children.items():
            out[name] = child.val
        return out

    @val.setter
    def val(self, values: Mapping[str, Any]) -> None:
        if isinstance(values, self.__class__):
            values = values.val
        if not isinstance(values, Mapping):
            raise TypeError(f"{self.__class__.__name__}.val expects a mapping.")
        for name, value in values.items():
            setattr(self, name, value)

    def __getattr__(self, name: str) -> Any:
        children = self.__dict__.get("_children")
        if children is not None and name in children:
            child = children[name]
            if isinstance(child, DataField):
                return child.val
            if isinstance(child, DataList):
                return child
            if isinstance(child, DataArray):
                return child.val
            # Generic fallback for DataSchema subclasses added after DataArray
            # (e.g. VarDataArray).  All concrete DataSchema subclasses expose .val.
            if hasattr(child, "val"):
                return child.val
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        children = self.__dict__.get("_children")
        if children is not None and name in children:
            child = children[name]
            if isinstance(child, DataField):
                child.val = value
            elif isinstance(child, DataList):
                if isinstance(value, child.__class__):
                    child.val = value.val
                else:
                    child.val = value
            elif isinstance(child, DataArray):
                child.val = value
            else:
                # Generic fallback for DataSchema subclasses added after DataArray
                # (e.g. VarDataArray).  All concrete DataSchema subclasses expose .val.
                if hasattr(child, "val"):
                    child.val = value
                else:
                    raise TypeError(
                        f"Unsupported child schema type for attribute '{name}'."
                    )
            return
        object.__setattr__(self, name, value)

    def is_close(
        self,
        other: DataSchema,
        rel_tol: float | None = None,
        abs_tol: float | None = 1e-8,
    ) -> bool:
        if not isinstance(other, self.__class__):
            return False

        if set(self._children) != set(other._children):
            return False

        for name, child in self._children.items():
            if not child.is_close(other._children[name], rel_tol=rel_tol, abs_tol=abs_tol):
                return False

        return True


class ParamSchema(DataList):
    """A :class:`DataList` whose ``elements`` reference symbolic :class:`Param`\\ s — the
    **Level-2 declarative** parameterization.

    A subclass declares ``Param`` class attributes and a dict-literal ``elements`` that
    uses them (directly or via arithmetic such as ``2 * data_bw``).  Because the core
    ``specialize`` methods (``IntField`` / ``FloatField`` / ``EnumField`` / ``MemAddr`` /
    ``DataArray``) defer when given a symbolic argument, those ``elements`` entries are
    captured as :class:`~waveflow.hw.param.LazyField`\\ s.  The framework (via
    ``__init_subclass__``, not a metaclass — avoiding the ``DataList`` / ``ABCMeta``
    conflict) collects the params (naming them from their namespace keys), installs the
    default ``elements`` (params at their defaults) + a fresh per-class cache, and provides
    a generic cached :meth:`specialize`.

    ``specialize`` itself **defers when handed a symbolic value**, so a nested parameterized
    schema that shares a param (``"a": Region.specialize(awidth=awidth)``) becomes a
    ``LazyField`` the outer schema resolves — the cascade.  Nested resolution reuses the
    nested schema's own cache, so the same ``Region.specialize(awidth=64)`` class appears
    wherever it is referenced (shared schema identity)."""

    _params: ClassVar[dict[str, Param]] = {}
    _lazy_elements: ClassVar[dict[str, Any]] = {}
    _specializations: ClassVar[dict[Any, type["ParamSchema"]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Subclasses produced BY specialize() carry already-resolved concrete elements.
        if cls.__dict__.get("_param_specialized"):
            return
        params = {k: v for k, v in cls.__dict__.items() if isinstance(v, Param)}
        if not params and "elements" not in cls.__dict__:
            return                                  # intermediate/abstract subclass; nothing to do
        for name, param in params.items():
            param.name = name
        cls._params = params
        cls._lazy_elements = dict(cls.__dict__.get("elements", {}))
        cls._specializations = {}
        default_env = {name: param.default for name, param in params.items()}
        cls.elements = resolve_elements(cls._lazy_elements, default_env)

    @classmethod
    @defer_if_symbolic
    def specialize(cls, **vals: Any) -> type["ParamSchema"]:
        """Return a cached subclass with ``elements`` resolved for ``vals`` (defaults filled
        in).  Keyword-only; same params → the same class object.  Defers (returns a
        ``LazyField``) when any value is symbolic — the cascade for nested schemas."""
        unknown = set(vals) - set(cls._params)
        if unknown:
            raise TypeError(
                f"{cls.__name__}.specialize() got unknown param(s) {sorted(unknown)}; "
                f"valid params: {sorted(cls._params)}.")
        env = {name: vals.get(name, param.default) for name, param in cls._params.items()}
        key = (cls, tuple(sorted(env.items())))
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached
        suffix = "_".join(f"{name}{env[name]}" for name in sorted(env))
        specialized = type(
            f"{cls.__name__}_{suffix}" if suffix else cls.__name__,
            (cls,),
            {
                "elements": resolve_elements(cls._lazy_elements, env),
                "_param_specialized": True,
                "__module__": cls.__module__,
                "__doc__": cls.__doc__,
            },
        )
        cls._specializations[key] = specialized
        return specialized


def _freeze_for_key(value: Any) -> Any:
    """Return a hashable stand-in for *value*, for use in a specialization-cache key.

    Dicts become sorted key/value tuples (recursively), lists become tuples; everything else is
    returned unchanged.  Equal specs must freeze equal, so two ``specialize`` calls passing the
    same partition dict share one cached class rather than building two identical types.
    """
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze_for_key(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_freeze_for_key(v) for v in value)
    return value


class DataArray(DataSchema):
    """Class-driven array schema with fixed maximum shape and optional dynamic extent."""

    element_type: ClassVar[type[DataSchema] | None] = None
    max_shape: ClassVar[tuple[int, ...]] = (1,)
    static: ClassVar[bool] = True
    member_name: ClassVar[str] = "data"
    cpp_storage: ClassVar[str] = "struct"
    can_gen_include: ClassVar[bool] = False

    allowed_specialize_kwargs: ClassVar[set[str]] = DataSchema.allowed_specialize_kwargs | {
        "member_name",
        "cpp_storage",
    }
    _specializations: ClassVar[dict[tuple[Any, ...], type[DataArray]]] = {}

    def __init_subclass__(cls, **kwargs: Any):
        super().__init_subclass__(**kwargs)
        if cls is not DataArray and "can_gen_include" not in cls.__dict__:
            cls.can_gen_include = True
        # Validate cpp_storage whenever a subclass sets it.
        if "cpp_storage" in cls.__dict__:
            val = cls.__dict__["cpp_storage"]
            if val not in ("struct", "raw"):
                raise ValueError(
                    f"{cls.__name__}.cpp_storage={val!r} is invalid; "
                    f"must be 'struct' or 'raw'."
                )
            if val == "raw":
                if not getattr(cls, "static", True):
                    raise ValueError(
                        f"{cls.__name__}: cpp_storage='raw' requires static=True."
                    )
                shape = getattr(cls, "max_shape", ())
                if len(shape) != 1:
                    raise ValueError(
                        f"{cls.__name__}: cpp_storage='raw' requires len(max_shape)==1, "
                        f"got max_shape={shape!r}."
                    )

    def __init__(self, value: Any = None):
        self._val = self.__class__.init_value()
        if value is not None:
            self.val = value

    def __len__(self) -> int:
        return len(self.val)

    def __getitem__(self, key):
        return self.val[key]

    def __setitem__(self, key, value):
        self.val[key] = value

    def __array__(self, dtype=None) -> np.ndarray:
        return np.asarray(self.val, dtype=dtype)

    def __getattr__(self, name: str):
        # Delegate numpy ndarray attributes (shape, size, dtype, flatten, …) to self.val.
        # __getattr__ is only called when normal lookup fails, so instance attributes
        # like '_val' are never forwarded here.
        return getattr(self.val, name)

    def __repr__(self) -> str:
        elem = type(self).element_type
        elem_name = elem.__name__ if elem is not None else "?"
        return (
            f"{type(self).__name__}(element_type={elem_name}, shape={np.asarray(self.val).shape})"
        )

    # --- type-preserving arithmetic operators (sugar; full-precision, no loss) ---
    # `+`/`-`/`*` over two DataArrays return a new DataArray whose element format is
    # *derived* (full precision): int grows (a*b -> Wa+Wb, a+b -> max+1), fixed reuses
    # FixedField's derivation, float is numpy passthrough. Rounding stays an explicit
    # quantize(); `.val` stays the numpy escape hatch. See _datarray_binop.
    def __mul__(self, other: Any) -> "DataArray":
        return _datarray_binop(self, other, "mul")

    def __add__(self, other: Any) -> "DataArray":
        return _datarray_binop(self, other, "add")

    def __sub__(self, other: Any) -> "DataArray":
        return _datarray_binop(self, other, "sub")

    @classmethod
    def _normalized_shape(cls) -> tuple[int, ...]:
        shape = tuple(int(dim) for dim in cls.max_shape)
        if any(dim < 0 for dim in shape):
            raise ValueError("max_shape dimensions must be non-negative.")
        return shape

    @classmethod
    def _declared_count(cls) -> int:
        """Return the static declared element count (product of max_shape).

        Only valid for ``cpp_storage='raw'`` arrays (requires len(max_shape)==1).
        """
        if not cls.max_shape:
            raise ValueError(f"{cls.__name__} has no max_shape; cannot lower as raw array.")
        n = 1
        for dim in cls.max_shape:
            n *= int(dim)
        return n

    @classmethod
    def _element_type(cls) -> type[DataSchema]:
        if cls.element_type is None:
            raise TypeError(f"{cls.__name__} does not define element_type.")
        return cls.element_type

    @classmethod
    def _member_name(cls) -> str:
        return cls.member_name or "data"

    @classmethod
    def _storage_expr(cls, prefix: str = "", member_name: str | None = None) -> str:
        if member_name is None:
            return f"{prefix}{cls._member_name()}"
        return f"{prefix}{member_name}.{cls._member_name()}"

    @classmethod
    def _element_expr(
        cls,
        prefix: str = "",
        member_name: str | None = None,
        idx_names: list[str] | None = None,
    ) -> str:
        expr = cls._storage_expr(prefix=prefix, member_name=member_name)
        if idx_names:
            expr += "".join(f"[{idx}]" for idx in idx_names)
        return expr

    @classmethod
    def _element_template(cls) -> DataSchema:
        return cls._element_type()()

    @classmethod
    def _element_numpy_info(cls) -> tuple[np.dtype[Any] | None, tuple[int, ...]]:
        elem_init = cls._element_type().init_value()
        if isinstance(elem_init, np.generic):
            return np.asarray(elem_init).dtype, ()
        if isinstance(elem_init, np.ndarray):
            return elem_init.dtype, tuple(elem_init.shape)
        return None, ()

    @classmethod
    @defer_if_symbolic
    def specialize(
        cls,
        element_type: type[DataSchema],
        max_shape: tuple[int, ...] | list[int] = (1,),
        static: bool = True,
        member_name: str = "data",
        cpp_storage: str = "struct",
        **kwargs: Any,
    ) -> type[DataArray]:
        if not isinstance(element_type, type) or not issubclass(element_type, DataSchema):
            raise TypeError("element_type must be a DataSchema subclass.")
        shape = tuple(int(dim) for dim in max_shape)
        if any(dim < 0 for dim in shape):
            raise ValueError("max_shape dimensions must be non-negative.")
        if not member_name or not isinstance(member_name, str):
            raise TypeError("member_name must be a non-empty string.")
        if cpp_storage not in ("struct", "raw"):
            raise ValueError(
                f"cpp_storage={cpp_storage!r} is invalid; must be 'struct' or 'raw'."
            )
        if cpp_storage == "raw":
            if not static:
                raise ValueError("cpp_storage='raw' requires static=True.")
            if len(shape) != 1:
                raise ValueError(
                    f"cpp_storage='raw' requires len(max_shape)==1, got max_shape={shape!r}."
                )

        overrides = cls.validate_specialize_kwargs({**kwargs, "member_name": member_name})
        # An override may be a structured value (a dict/list), which is unhashable and so cannot
        # go into the specialization-cache key as-is.  Freeze it; two calls passing equal structures
        # must still hit one cached class rather than building two identical types.
        override_items = tuple(
            (k, _freeze_for_key(v)) for k, v in sorted(overrides.items())
        )
        key = (cls, element_type, shape, bool(static), cpp_storage, override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        subclass_name = f"{element_type.__name__}Array"
        specialized_attrs = cls.merge_specialize_attrs(
            {
                "element_type": element_type,
                "max_shape": shape,
                "static": bool(static),
                "member_name": member_name,
                "cpp_storage": cpp_storage,
                "can_gen_include": True,
                "__module__": cls.__module__,
                "__doc__": (
                    f"Specialized array schema: element_type={element_type.__name__}, "
                    f"max_shape={shape}, static={static}."
                ),
            },
            overrides,
        )
        specialized = type(subclass_name, (cls,), specialized_attrs)
        cls._specializations[key] = specialized
        return specialized

    @classmethod
    def validate_specialize_kwargs(cls, kwargs: dict[str, Any]) -> dict[str, Any]:
        validated = super().validate_specialize_kwargs(kwargs)
        if "member_name" in validated and not isinstance(validated["member_name"], str):
            raise TypeError("member_name must be a string.")
        return validated

    @classmethod
    def init_value(cls) -> Any:
        shape = cls._normalized_shape()
        def_shape = shape if cls.static else (0,) + shape[1:]
        elem_init = cls._element_type().init_value()

        if isinstance(elem_init, np.generic):
            return np.zeros(def_shape, dtype=np.asarray(elem_init).dtype)

        if isinstance(elem_init, np.ndarray):
            return np.zeros(def_shape + tuple(elem_init.shape), dtype=elem_init.dtype)

        def build(shape_tail: tuple[int, ...]) -> Any:
            if not shape_tail:
                return cls._element_type().init_value()
            return [build(shape_tail[1:]) for _ in range(shape_tail[0])]

        return build(def_shape)

    @property
    def val(self) -> Any:
        return self._val

    @val.setter
    def val(self, value: Any) -> None:
        self._val = self._convert(value)

    def _convert(self, value: Any) -> Any:
        if isinstance(value, self.__class__):
            value = value.val

        shape = self.__class__._normalized_shape()
        dtype, elem_tail_shape = self.__class__._element_numpy_info()

        if dtype is not None:
            arr = np.asarray(value, dtype=dtype)
            prefix_ndim = len(shape)
            if arr.ndim < prefix_ndim:
                raise ValueError(
                    f"{self.__class__.__name__}.val expects at least {prefix_ndim} dimensions; got {arr.ndim}."
                )

            data_shape = tuple(arr.shape[:prefix_ndim])
            tail_shape = tuple(arr.shape[prefix_ndim:])
            if tail_shape != elem_tail_shape:
                raise ValueError(
                    f"{self.__class__.__name__}.val has invalid element tail shape {tail_shape}; "
                    f"expected {elem_tail_shape}."
                )

            if self.__class__.static:
                if data_shape != shape:
                    raise ValueError(
                        f"{self.__class__.__name__}.val must have shape {shape + elem_tail_shape}; "
                        f"got {tuple(arr.shape)}."
                    )
            else:
                if prefix_ndim > 0:
                    if len(data_shape) != len(shape):
                        raise ValueError(
                            f"{self.__class__.__name__}.val must have {len(shape)} array dimensions; got {len(data_shape)}."
                        )
                    if data_shape[1:] != shape[1:]:
                        raise ValueError(
                            f"{self.__class__.__name__}.val trailing shape must match {shape[1:]}; got {data_shape[1:]}."
                        )
                    if data_shape[0] > shape[0]:
                        raise ValueError(
                            f"{self.__class__.__name__}.val first dimension {data_shape[0]} exceeds max {shape[0]}."
                        )

            return arr

        data = value
        for idxs in np.ndindex(tuple(np.asarray(value, dtype=object).shape)) if np.asarray(value, dtype=object).ndim > 0 else [()]:
            _ = idxs
            break
        return data

    @classmethod
    def get_bitwidth(cls) -> int:
        shape = cls._normalized_shape()
        n_elem = 1
        for dim in shape:
            n_elem *= dim
        return n_elem * cls._element_type().get_bitwidth()

    @classmethod
    def nwords_per_inst(cls, word_bw: int) -> int:
        if word_bw <= 0:
            raise ValueError("word_bw must be positive.")

        shape = cls._normalized_shape()
        n_elem = 1
        for dim in shape:
            n_elem *= dim

        child = cls._element_type()()
        curr_ipos = 0
        curr_iword = 0
        for _ in range(n_elem):
            curr_ipos, curr_iword = child._serialize_recursive(
                word_bw=word_bw,
                words=[0],
                ipos0=curr_ipos,
                iword0=curr_iword,
            )

        return curr_iword + (1 if curr_ipos > 0 else 0)

    @classmethod
    def get_dependencies(cls) -> list[type[DataSchema]]:
        elem_type = cls._element_type()
        if elem_type.can_gen_include and elem_type is not cls:
            return [elem_type]
        return []

    @classmethod
    def get_utility_includes(cls) -> list[str]:
        """``DataArray`` always pulls in the element type's array-utils header.

        Recurses into the element type so a ``DataArray`` of a composite
        picks up that composite's own utility includes transitively.
        """
        from waveflow.hw.arrayutils import _array_utils_include_path
        elem_type = cls._element_type()
        paths: list[str] = [_array_utils_include_path(elem_type)]
        paths.extend(elem_type.get_utility_includes())
        return paths

    @classmethod
    def get_param_str(cls, write: bool) -> str:
        _ = write
        if cls.static:
            return ""
        ndims = len(cls._normalized_shape())
        if ndims == 0:
            return ""
        return ", ".join(f"int n{i}=1" for i in range(ndims))

    @classmethod
    def gen_class_elems(cls, indent_level: int = 1) -> list[str]:
        indent = cls._get_indent(indent_level)
        suffix = "".join(f"[{dim}]" for dim in cls._normalized_shape())
        return [f"{indent}{cls._element_type().cpp_class_name()} {cls._member_name()}{suffix};"]

    @classmethod
    def _reject_oversized_whole_object_pack(cls) -> None:
        """Fail fast if this array's packed width exceeds the HLS ``ap_uint`` limit.

        Backstop for :meth:`gen_pack` / :meth:`gen_unpack`: the header emitter
        skips whole-object pack/unpack for such arrays (see ``can_pack_whole``),
        so this only fires if generation genuinely tries to pack a >8191-bit
        object — which would emit non-synthesizable ``ap_uint<bitwidth>`` C++.
        """
        width = cls.get_bitwidth()
        if width > HLS_AP_UINT_MAX_BITWIDTH:
            raise ValueError(
                f"DataArray {cls.cpp_class_name()} packs to {width} bits, exceeding "
                f"the HLS ap_uint limit of {HLS_AP_UINT_MAX_BITWIDTH} bits. A buffer "
                "this large cannot be packed into a single word; access it "
                "element-wise/burst (array_utils) instead of "
                "pack_to_uint/unpack_from_uint."
            )

    @classmethod
    def can_pack_whole(cls) -> bool:
        """Whether a whole-object ``pack_to_uint``/``unpack_from_uint`` is emittable.

        ``False`` for buffers wider than the HLS ``ap_uint`` limit, whose
        whole-object packing would be non-synthesizable.
        """
        return cls.get_bitwidth() <= HLS_AP_UINT_MAX_BITWIDTH

    @classmethod
    def gen_pack(cls, indent_level: int = 0) -> str:
        cls._reject_oversized_whole_object_pack()
        cls_name = cls.cpp_class_name()
        elem_bw = cls._element_type().get_bitwidth()
        shape = cls._normalized_shape()

        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)
        idx_names = [f"i{i}" for i in range(len(shape))]
        member_name = cls._member_name()
        elem_expr = f"data.{member_name}" + "".join(f"[{idx}]" for idx in idx_names)
        elem_uint_expr = cls._element_type().to_uint_value_expr(elem_expr)

        lines = [
            f"{indent}static ap_uint<bitwidth> pack_to_uint(const {cls_name}& data) {{",
            f"{i1}ap_uint<bitwidth> res = 0;",
            f"{i1}int bitpos = 0;",
        ]
        for level, dim in enumerate(shape):
            lines.append(
                f"{cls._get_indent(indent_level + 1 + level)}for (int {idx_names[level]} = 0; {idx_names[level]} < {dim}; ++{idx_names[level]}) {{"
            )
        body_indent = cls._get_indent(indent_level + 1 + len(shape))
        lines.append(f"{body_indent}res.range(bitpos + {elem_bw} - 1, bitpos) = {elem_uint_expr};")
        lines.append(f"{body_indent}bitpos += {elem_bw};")
        for level in range(len(shape) - 1, -1, -1):
            lines.append(f"{cls._get_indent(indent_level + 1 + level)}}}")
        lines.extend([f"{i1}return res;", f"{indent}}}"])
        return "\n".join(lines)

    @classmethod
    def gen_unpack(cls, indent_level: int = 0) -> str:
        cls._reject_oversized_whole_object_pack()
        cls_name = cls.cpp_class_name()
        elem_bw = cls._element_type().get_bitwidth()
        shape = cls._normalized_shape()

        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)
        idx_names = [f"i{i}" for i in range(len(shape))]
        member_name = cls._member_name()
        elem_lhs = f"data.{member_name}" + "".join(f"[{idx}]" for idx in idx_names)

        lines = [
            f"{indent}static {cls_name} unpack_from_uint(const ap_uint<bitwidth>& packed) {{",
            f"{i1}{cls_name} data;",
            f"{i1}int bitpos = 0;",
        ]
        for level, dim in enumerate(shape):
            lines.append(
                f"{cls._get_indent(indent_level + 1 + level)}for (int {idx_names[level]} = 0; {idx_names[level]} < {dim}; ++{idx_names[level]}) {{"
            )
        body_indent = cls._get_indent(indent_level + 1 + len(shape))
        slice_expr = f"packed.range(bitpos + {elem_bw} - 1, bitpos)"
        lines.append(f"{body_indent}{elem_lhs} = {cls._element_type().from_uint_expr(slice_expr)};")
        lines.append(f"{body_indent}bitpos += {elem_bw};")
        for level in range(len(shape) - 1, -1, -1):
            lines.append(f"{cls._get_indent(indent_level + 1 + level)}}}")
        lines.extend([f"{i1}return data;", f"{indent}}}"])
        return "\n".join(lines)

    @classmethod
    def gen_stream_helpers(
        cls,
        indent_level: int = 1,
        word_bw_supported: list[int] | None = None,
    ) -> str:
        elem_bw = cls._element_type().get_bitwidth()
        elem_cpp = cls._element_type().cpp_class_name()
        supported = sorted(set(int(bw) for bw in (word_bw_supported or [])))
        if not supported:
            return ""

        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)
        i2 = cls._get_indent(indent_level + 2)
        i3 = cls._get_indent(indent_level + 3)
        elem_type = cls._element_type()

        def emit_primary_impl(name: str, signature: str, extra_voids: list[str] | None = None) -> list[str]:
            out = [
                f"{indent}template<int word_bw>",
                f"{indent}static void {name}(word_bw_tag<word_bw>, {signature.format(bw='word_bw')}) {{",
                f'{i1}static_assert(word_bw < 0, "Unsupported word_bw for {name[:-5]}");',
            ]
            for void_name in (extra_voids or []):
                out.append(f"{i1}(void){void_name};")
            out.append(f"{indent}}}")
            return out

        def emit_read_impl(name: str, source_expr: str, source_kind: str) -> list[str]:
            out = emit_primary_impl(name, source_expr, ["s", "out", "n"])
            for bw in supported:
                pfv = bw // elem_bw
                out.extend([
                    "",
                    f"{indent}static void {name}(word_bw_tag<{bw}>, {source_expr.format(bw=bw)}) {{",
                    f"{i1}#pragma HLS INLINE",
                ])
                if pfv >= 2:
                    read_expr = "s.read()" if source_kind == "stream" else "s.read().data"
                    out.append(f"{i1}ap_uint<{bw}> w = {read_expr};")
                    for j in range(pfv):
                        lo = j * elem_bw
                        hi = lo + elem_bw - 1
                        out.append(f"{i1}if (n > {j}) {{")
                        out.append(f"{i2}out[{j}] = {elem_type.from_uint_expr(f'w.range({hi}, {lo})')};")
                        out.append(f"{i1}}}")
                else:
                    if elem_bw <= bw:
                        out.append(f"{i1}if (n > 0) {{")
                        read_expr = "s.read()" if source_kind == "stream" else "s.read().data"
                        out.append(f"{i2}ap_uint<{bw}> w = {read_expr};")
                        out.append(f"{i2}out[0] = {elem_type.from_uint_expr('w')};")
                        out.append(f"{i1}}}")
                    else:
                        out.append(f"{i1}if (n > 0) {{")
                        call_name = "read_stream" if source_kind == "stream" else "read_axi4_stream"
                        out.append(f"{i2}out[0].{call_name}<{bw}>(s);")
                        out.append(f"{i1}}}")
                out.append(f"{indent}}}")
            return out

        def emit_write_impl(name: str, signature: str, axi: bool = False) -> list[str]:
            voids = ["s", "in", "n"] if not axi else ["s", "in", "tlast", "n"]
            out = emit_primary_impl(name, signature, voids)
            for bw in supported:
                pfv = bw // elem_bw
                out.extend([
                    "",
                    f"{indent}static void {name}(word_bw_tag<{bw}>, {signature.format(bw=bw)}) {{",
                    f"{i1}#pragma HLS INLINE",
                ])
                if pfv >= 2:
                    out.append(f"{i1}ap_uint<{bw}> w = 0;")
                    for j in range(pfv):
                        lo = j * elem_bw
                        hi = lo + elem_bw - 1
                        out.append(f"{i1}if (n > {j}) {{")
                        out.append(f"{i2}w.range({hi}, {lo}) = {elem_type.to_uint_value_expr(f'in[{j}]')};")
                        out.append(f"{i1}}}")
                    if axi:
                        out.append(f"{i1}streamutils::write_axi4_word<{bw}>(s, w, tlast);")
                    else:
                        out.append(f"{i1}s.write(w);")
                else:
                    if elem_bw <= bw:
                        out.append(f"{i1}if (n > 0) {{")
                        out.append(f"{i2}ap_uint<{bw}> w = {elem_type.to_uint_value_expr('in[0]')};")
                        if axi:
                            out.append(f"{i2}streamutils::write_axi4_word<{bw}>(s, w, tlast);")
                        else:
                            out.append(f"{i2}s.write(w);")
                        out.append(f"{i1}}}")
                    else:
                        out.append(f"{i1}if (n > 0) {{")
                        call_name = "write_axi4_stream" if axi else "write_stream"
                        tail = ", tlast" if axi else ""
                        out.append(f"{i2}in[0].{call_name}<{bw}>(s{tail});")
                        out.append(f"{i1}}}")
                out.append(f"{indent}}}")
            return out

        lines = [
            f"{indent}template<int word_bw>",
            f"{indent}static constexpr int pf() {{",
            f"{i1}return word_bw / {elem_bw};",
            f"{indent}}}",
            "",
        ]
        lines.extend(emit_read_impl(
            "read_stream_elem_impl",
            f"hls::stream<ap_uint<{{bw}}>>& s, {elem_cpp}* out, int n",
            "stream",
        ))
        lines.extend([
            "",
            f"{indent}template<int word_bw>",
            f"{indent}static void read_stream_elem(hls::stream<ap_uint<word_bw>>& s, {elem_cpp} out[pf<word_bw>()], int n = pf<word_bw>()) {{",
            f"{i1}#pragma HLS INLINE",
            f"{i1}read_stream_elem_impl(word_bw_tag<word_bw>{{}}, s, out, n);",
            f"{indent}}}",
            "",
        ])
        lines.extend(emit_read_impl(
            "read_axi4_stream_elem_impl",
            f"hls::stream<streamutils::axi4s_word<{{bw}}>>& s, {elem_cpp}* out, int n",
            "axi4_stream",
        ))
        lines.extend([
            "",
            f"{indent}template<int word_bw>",
            f"{indent}static void read_axi4_stream_elem(hls::stream<streamutils::axi4s_word<word_bw>>& s, {elem_cpp} out[pf<word_bw>()], int n = pf<word_bw>()) {{",
            f"{i1}#pragma HLS INLINE",
            f"{i1}read_axi4_stream_elem_impl(word_bw_tag<word_bw>{{}}, s, out, n);",
            f"{indent}}}",
            "",
        ])
        lines.extend(emit_write_impl(
            "write_stream_elem_impl",
            f"hls::stream<ap_uint<{{bw}}>>& s, const {elem_cpp}* in, int n",
            axi=False,
        ))
        lines.extend([
            "",
            f"{indent}template<int word_bw>",
            f"{indent}static void write_stream_elem(hls::stream<ap_uint<word_bw>>& s, const {elem_cpp} in[pf<word_bw>()], int n = pf<word_bw>()) {{",
            f"{i1}#pragma HLS INLINE",
            f"{i1}write_stream_elem_impl(word_bw_tag<word_bw>{{}}, s, in, n);",
            f"{indent}}}",
            "",
        ])
        lines.extend(emit_write_impl(
            "write_axi4_stream_elem_impl",
            f"hls::stream<streamutils::axi4s_word<{{bw}}>>& s, const {elem_cpp}* in, bool tlast, int n",
            axi=True,
        ))
        lines.extend([
            "",
            f"{indent}template<int word_bw>",
            f"{indent}static void write_axi4_stream_elem(hls::stream<streamutils::axi4s_word<word_bw>>& s, const {elem_cpp} in[pf<word_bw>()], bool tlast = false, int n = pf<word_bw>()) {{",
            f"{i1}#pragma HLS INLINE",
            f"{i1}write_axi4_stream_elem_impl(word_bw_tag<word_bw>{{}}, s, in, tlast, n);",
            f"{indent}}}",
        ])
        return "\n".join(lines)

    @classmethod
    def gen_nwords_len_helpers(
        cls,
        indent_level: int = 1,
        word_bw_supported: list[int] | None = None,
    ) -> str:
        if cls.static:
            return ""
        supported = sorted(set(int(bw) for bw in (word_bw_supported or [])))
        if not supported:
            return ""

        shape = cls._normalized_shape()
        ndims = len(shape)
        if ndims == 0:
            return ""

        elem_bw = cls._element_type().get_bitwidth()
        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)
        i2 = cls._get_indent(indent_level + 2)
        params = ", ".join(f"int n{i}=1" for i in range(ndims))
        n_eff_names = [f"n{i}_eff" for i in range(ndims)]
        n_total_expr = " * ".join(n_eff_names)
        lines = [
            f"{indent}template<int word_bw>",
            f"{indent}static int nwords_len_impl(word_bw_tag<word_bw>, {params}) {{",
            f'{i1}static_assert(word_bw < 0, "Unsupported word_bw for nwords_len");',
            f"{i1}return 0;",
            f"{indent}}}",
        ]
        for bw in supported:
            lines.extend([
                "",
                f"{indent}static int nwords_len_impl(word_bw_tag<{bw}>, {params}) {{",
            ])
            for d, dim in enumerate(shape):
                lines.append(f"{i1}const int {n_eff_names[d]} = (n{d} < 0) ? 0 : ((n{d} > {dim}) ? {dim} : n{d});")
            lines.append(f"{i1}const int n_total = {n_total_expr};")
            pf = bw // elem_bw if elem_bw > 0 else 0
            if pf >= 1:
                lines.append(f"{i1}return (n_total + {pf} - 1) / {pf};")
            else:
                lines.append(f"{i1}return n_total * {cls._element_type().nwords_per_inst(bw)};")
            lines.append(f"{indent}}}")
        arg_names = ", ".join(f"n{i}" for i in range(ndims))
        lines.extend([
            "",
            f"{indent}template<int word_bw>",
            f"{indent}static int nwords_len({params}) {{",
            f"{i1}return nwords_len_impl(word_bw_tag<word_bw>{{}}, {arg_names});",
            f"{indent}}}",
        ])
        return "\n".join(lines)

    @classmethod
    def _gen_write_recursive(
        cls,
        word_bw: int,
        dst_type: str = "array",
        target: str = "x",
        ipos0: int = 0,
        iword0: int = 0,
        prefix: str = "",
        member_name: str | None = None,
    ) -> tuple[list[str], int, int]:
        pre_lines: list[str] = []
        start_ipos = ipos0
        start_iword = iword0
        if start_ipos > 0:
            if dst_type == "stream":
                pre_lines.append(f"    {target}.write(w);")
                pre_lines.append("    w = 0;")
            elif dst_type == "axi4_stream":
                pre_lines.append(f"    streamutils::write_axi4_word<{word_bw}>({target}, w, false);")
                pre_lines.append("    w = 0;")
            start_iword += 1
            start_ipos = 0

        shape = cls._normalized_shape()
        ndims = len(shape)
        if ndims == 0:
            return pre_lines, start_ipos, start_iword

        elem_type = cls._element_type()
        elem_bw = elem_type.get_bitwidth()
        pf = word_bw // elem_bw if elem_bw > 0 else 0
        words_per_elem = elem_type.nwords_per_inst(word_bw)
        idx_names = [f"i{i}" for i in range(ndims)]
        n_eff_names = [f"n{i}_eff" for i in range(ndims)]
        elem_expr = cls._element_expr(prefix=prefix, member_name=member_name, idx_names=idx_names)
        elem_uint_expr = elem_type.to_uint_value_expr(elem_expr)
        lines = list(pre_lines)

        for d in range(ndims):
            if cls.static:
                lines.append(f"    const int {n_eff_names[d]} = {shape[d]};")
            else:
                lines.append(f"    const int {n_eff_names[d]} = (n{d} < 0) ? 0 : ((n{d} > {shape[d]}) ? {shape[d]} : n{d});")
        n_total_expr = " * ".join(n_eff_names) if n_eff_names else "1"
        decl_end = len(lines)

        if pf >= 2:
            if ndims == 1:
                n0_eff = n_eff_names[0]
                out_idx_init = start_iword if dst_type == "array" else 0
                lines.insert(decl_end, f"    int out_idx = {out_idx_init};")
                lines.append(f"    for (int i = 0; i < {n0_eff}; i += {pf}) {{")
                lines.append("        #pragma HLS PIPELINE II=1")
                if dst_type == "array":
                    lines.append(f"        ap_uint<{word_bw}> w = 0;")
                else:
                    lines.append("        w = 0;")
                for j in range(pf):
                    lo = j * elem_bw
                    hi = lo + elem_bw - 1
                    lane_expr = elem_type.to_uint_value_expr(
                        cls._element_expr(prefix=prefix, member_name=member_name, idx_names=[f"i + {j}"])
                    )
                    lines.append(f"        if (i + {j} < {n0_eff}) {{")
                    lines.append(f"            w.range({hi}, {lo}) = {lane_expr};")
                    lines.append("        }")
                if dst_type == "array":
                    lines.append(f"        {target}[out_idx++] = w;")
                elif dst_type == "stream":
                    lines.append(f"        {target}.write(w);")
                    lines.append("        out_idx++;")
                else:
                    lines.append(f"        streamutils::write_axi4_word<{word_bw}>({target}, w, false);")
                    lines.append("        out_idx++;")
                lines.append("    }")
                next_iword = start_iword + cls.nwords_per_inst(word_bw) if cls.static else iword0
                return cls._scope_local_lines(lines), 0, next_iword

            lines.insert(decl_end, "    int elem_idx = 0;")
            out_idx_init = start_iword if dst_type == "array" else 0
            lines.insert(decl_end + 1, f"    int out_idx = {out_idx_init};")
            for d in range(ndims):
                lines.append(f"    for (int {idx_names[d]} = 0; {idx_names[d]} < {n_eff_names[d]}; ++{idx_names[d]}) {{")
            body_indent = "    " * (ndims + 1)
            lines.append(f"{body_indent}const int slot = (elem_idx % {pf});")
            for j in range(pf):
                lo = j * elem_bw
                hi = lo + elem_bw - 1
                cond = "if" if j == 0 else "else if"
                assign_lhs = "w" if dst_type != "array" else f"{target}[out_idx]"
                lines.append(f"{body_indent}{cond} (slot == {j}) {{")
                if dst_type == "array" and j == 0:
                    lines.append(f"{body_indent}    {assign_lhs} = 0;")
                lines.append(f"{body_indent}    {assign_lhs}.range({hi}, {lo}) = {elem_uint_expr};")
                lines.append(f"{body_indent}}}")
            lines.append(f"{body_indent}elem_idx++;")
            lines.append(f"{body_indent}if (slot == {pf - 1}) {{")
            if dst_type == "array":
                lines.append(f"{body_indent}    out_idx++;")
            elif dst_type == "stream":
                lines.append(f"{body_indent}    {target}.write(w);")
                lines.append(f"{body_indent}    w = 0;")
                lines.append(f"{body_indent}    out_idx++;")
            else:
                lines.append(f"{body_indent}    streamutils::write_axi4_word<{word_bw}>({target}, w, false);")
                lines.append(f"{body_indent}    w = 0;")
                lines.append(f"{body_indent}    out_idx++;")
            lines.append(f"{body_indent}}}")
            for d in range(ndims):
                lines.append(f"{'    ' * (ndims - d)}}}")
            lines.append(f"    if ((elem_idx % {pf}) != 0) {{")
            if dst_type == "array":
                lines.append("        out_idx++;")
            elif dst_type == "stream":
                lines.append(f"        {target}.write(w);")
            else:
                lines.append(f"        streamutils::write_axi4_word<{word_bw}>({target}, w, false);")
            lines.append("    }")
            next_iword = start_iword + cls.nwords_per_inst(word_bw) if cls.static else iword0
            return cls._scope_local_lines(lines), 0, next_iword

        if pf == 1:
            out_idx_init = start_iword if dst_type == "array" else 0
            lines.insert(decl_end, f"    int out_idx = {out_idx_init};")
            for d in range(ndims):
                lines.append(f"    for (int {idx_names[d]} = 0; {idx_names[d]} < {n_eff_names[d]}; ++{idx_names[d]}) {{")
            body_indent = "    " * (ndims + 1)
            if dst_type == "array":
                lines.append(f"{body_indent}{target}[out_idx++] = {elem_uint_expr};")
            elif dst_type == "stream":
                lines.append(f"{body_indent}w = {elem_uint_expr};")
                lines.append(f"{body_indent}{target}.write(w);")
                lines.append(f"{body_indent}out_idx++;")
            else:
                lines.append(f"{body_indent}w = {elem_uint_expr};")
                lines.append(f"{body_indent}streamutils::write_axi4_word<{word_bw}>({target}, w, false);")
                lines.append(f"{body_indent}out_idx++;")
            for d in range(ndims):
                lines.append(f"{'    ' * (ndims - d)}}}")
            next_iword = start_iword + cls.nwords_per_inst(word_bw) if cls.static else iword0
            return cls._scope_local_lines(lines), 0, next_iword

        if issubclass(elem_type, DataField):
            raise ValueError(
                f"Array element '{elem_type.__name__}' has bitwidth {elem_bw} > word_bw={word_bw}; "
                "DataField elements cannot be split across words."
            )
        out_idx_init = start_iword if dst_type == "array" else 0
        lines.insert(decl_end, f"    int out_idx = {out_idx_init};")
        for d in range(ndims):
            lines.append(f"    for (int {idx_names[d]} = 0; {idx_names[d]} < {n_eff_names[d]}; ++{idx_names[d]}) {{")
        body_indent = "    " * (ndims + 1)
        if dst_type == "array":
            lines.append(f"{body_indent}{elem_expr}.template write_array<{word_bw}>(&{target}[out_idx]);")
            lines.append(f"{body_indent}out_idx += {words_per_elem};")
        elif dst_type == "stream":
            lines.append(f"{body_indent}{elem_expr}.template write_stream<{word_bw}>({target});")
            lines.append(f"{body_indent}out_idx += {words_per_elem};")
        else:
            lines.append(f"{body_indent}{elem_expr}.template write_axi4_stream<{word_bw}>({target}, false);")
            lines.append(f"{body_indent}out_idx += {words_per_elem};")
        for d in range(ndims):
            lines.append(f"{'    ' * (ndims - d)}}}")
        next_iword = start_iword + cls.nwords_per_inst(word_bw) if cls.static else iword0
        return cls._scope_local_lines(lines), 0, next_iword

    @classmethod
    def _gen_read_recursive(
        cls,
        word_bw: int,
        src_type: str = "array",
        source: str = "x",
        ipos0: int = 0,
        iword0: int = 0,
        prefix: str = "",
        member_name: str | None = None,
    ) -> tuple[list[str], int, int]:
        start_ipos = ipos0
        start_iword = iword0
        if start_ipos > 0:
            start_iword += 1
            start_ipos = 0

        shape = cls._normalized_shape()
        ndims = len(shape)
        if ndims == 0:
            return [], start_ipos, start_iword

        elem_type = cls._element_type()
        elem_bw = elem_type.get_bitwidth()
        pf = word_bw // elem_bw if elem_bw > 0 else 0
        words_per_elem = elem_type.nwords_per_inst(word_bw)
        idx_names = [f"i{i}" for i in range(ndims)]
        n_eff_names = [f"n{i}_eff" for i in range(ndims)]
        elem_expr = cls._element_expr(prefix=prefix, member_name=member_name, idx_names=idx_names)
        lines: list[str] = []
        for d in range(ndims):
            if cls.static:
                lines.append(f"    const int {n_eff_names[d]} = {shape[d]};")
            else:
                lines.append(f"    const int {n_eff_names[d]} = (n{d} < 0) ? 0 : ((n{d} > {shape[d]}) ? {shape[d]} : n{d});")
        n_total_expr = " * ".join(n_eff_names) if n_eff_names else "1"
        lines.insert(len(n_eff_names), f"    int in_idx = {start_iword};")

        def assign_from_uint(uint_expr: str) -> str:
            return f"{elem_expr} = {elem_type.from_uint_expr(uint_expr)};"

        if pf >= 2:
            if ndims == 1:
                n0_eff = n_eff_names[0]
                if src_type == "axi4_stream":
                    lines.append("    int i = 0;")
                    lines.append(f"    for (; i < {n0_eff}; i += {pf}) {{")
                else:
                    lines.append(f"    for (int i = 0; i < {n0_eff}; i += {pf}) {{")
                lines.append("        #pragma HLS PIPELINE II=1")
                if src_type == "array":
                    lines.append(f"        ap_uint<{word_bw}> w = {source}[in_idx++];")
                elif src_type == "stream":
                    lines.append(f"        w = {source}.read();")
                    lines.append("        in_idx++;")
                else:
                    lines.append("        if (last) {")
                    lines.append("            break;")
                    lines.append("        }")
                    lines.append("        {")
                    lines.append(f"            auto axis_word = {source}.read();")
                    lines.append("            w = axis_word.data;")
                    lines.append("            last = axis_word.last;")
                    lines.append("        }")
                    lines.append("        in_idx++;")
                for j in range(pf):
                    lo = j * elem_bw
                    hi = lo + elem_bw - 1
                    rhs_expr = elem_type.from_uint_expr(f"w.range({hi}, {lo})")
                    lines.append(f"        if (i + {j} < {n0_eff}) {{")
                    lines.append(
                        f"            {cls._element_expr(prefix=prefix, member_name=member_name, idx_names=[f'i + {j}'])} = {rhs_expr};"
                    )
                    lines.append("        }")
                if src_type == "axi4_stream":
                    lines.append("        if (last) {")
                    lines.append("            break;")
                    lines.append("        }")
                lines.append("    }")
                if src_type == "axi4_stream":
                    lines.append(f"    if ((i + {pf}) < {n0_eff}) {{")
                    lines.append("        tl = streamutils::tlast_status::tlast_early;")
                    lines.append("        return;")
                    lines.append("    }")
                next_iword = start_iword + cls.nwords_per_inst(word_bw) if cls.static else iword0
                return cls._scope_local_lines(lines), 0, next_iword

            lines.insert(len(n_eff_names) + (2 if src_type != "array" else 1), "    int elem_idx = 0;")
            if src_type == "axi4_stream":
                lines.insert(len(n_eff_names) + (3 if src_type != "array" else 2), "    bool stop = false;")
            for d in range(ndims):
                loop_cond = f"{idx_names[d]} < {n_eff_names[d]}"
                if src_type == "axi4_stream":
                    loop_cond += " && !stop"
                lines.append(f"    for (int {idx_names[d]} = 0; {loop_cond}; ++{idx_names[d]}) {{")
            body_indent = "    " * (ndims + 1)
            lines.append(f"{body_indent}const int slot = (elem_idx % {pf});")
            if src_type == "stream":
                lines.append(f"{body_indent}if (slot == 0) {{ w = {source}.read(); }}")
            elif src_type == "axi4_stream":
                lines.append(f"{body_indent}if (slot == 0) {{")
                lines.append(f"{body_indent}    if (last) {{")
                lines.append(f"{body_indent}        stop = true;")
                lines.append(f"{body_indent}    }} else {{")
                lines.append(f"{body_indent}        auto axis_word = {source}.read();")
                lines.append(f"{body_indent}        w = axis_word.data;")
                lines.append(f"{body_indent}        last = axis_word.last;")
                lines.append(f"{body_indent}    }}")
                lines.append(f"{body_indent}}}")
                lines.append(f"{body_indent}if (stop) {{")
                lines.append(f"{body_indent}    break;")
                lines.append(f"{body_indent}}}")
            for j in range(pf):
                lo = j * elem_bw
                hi = lo + elem_bw - 1
                cond = "if" if j == 0 else "else if"
                word_expr = f"{source}[in_idx]" if src_type == "array" else "w"
                lines.append(f"{body_indent}{cond} (slot == {j}) {{")
                lines.append(f"{body_indent}    {assign_from_uint(f'{word_expr}.range({hi}, {lo})')}")
                lines.append(f"{body_indent}}}")
            lines.append(f"{body_indent}elem_idx++;")
            lines.append(f"{body_indent}if (slot == {pf - 1}) {{ in_idx++; }}")
            for d in range(ndims):
                lines.append(f"{'    ' * (ndims - d)}}}")
            if src_type == "axi4_stream":
                lines.append("    if (stop) {")
                lines.append("        tl = streamutils::tlast_status::tlast_early;")
                lines.append("        return;")
                lines.append("    }")
            lines.append(f"    if (({n_total_expr}) > 0 && (({n_total_expr}) % {pf}) != 0) {{")
            lines.append("        in_idx++;")
            lines.append("    }")
            next_iword = start_iword + cls.nwords_per_inst(word_bw) if cls.static else iword0
            return cls._scope_local_lines(lines), 0, next_iword

        if pf == 1:
            if src_type == "axi4_stream":
                lines.insert(len(n_eff_names) + 1, "    int elem_idx = 0;")
                lines.insert(len(n_eff_names) + 2, "    bool stop = false;")
            for d in range(ndims):
                loop_cond = f"{idx_names[d]} < {n_eff_names[d]}"
                if src_type == "axi4_stream":
                    loop_cond += " && !stop"
                lines.append(f"    for (int {idx_names[d]} = 0; {loop_cond}; ++{idx_names[d]}) {{")
            body_indent = "    " * (ndims + 1)
            if src_type == "array":
                lines.append(f"{body_indent}{assign_from_uint(f'{source}[in_idx]')}")
                lines.append(f"{body_indent}in_idx++;")
            elif src_type == "stream":
                lines.append(f"{body_indent}w = {source}.read();")
                lines.append(f"{body_indent}{assign_from_uint('w')}")
                lines.append(f"{body_indent}in_idx++;")
            else:
                lines.append(f"{body_indent}{{")
                lines.append(f"{body_indent}    auto axis_word = {source}.read();")
                lines.append(f"{body_indent}    w = axis_word.data;")
                lines.append(f"{body_indent}    last = axis_word.last;")
                lines.append(f"{body_indent}}}")
                lines.append(f"{body_indent}{assign_from_uint('w')}")
                lines.append(f"{body_indent}in_idx++;")
                lines.append(f"{body_indent}elem_idx++;")
                lines.append(f"{body_indent}if (last && elem_idx < ({n_total_expr})) {{")
                lines.append(f"{body_indent}    stop = true;")
                lines.append(f"{body_indent}}}")
            for d in range(ndims):
                lines.append(f"{'    ' * (ndims - d)}}}")
            if src_type == "axi4_stream":
                lines.append("    if (stop) {")
                lines.append("        tl = streamutils::tlast_status::tlast_early;")
                lines.append("        return;")
                lines.append("    }")
            next_iword = start_iword + cls.nwords_per_inst(word_bw) if cls.static else iword0
            return cls._scope_local_lines(lines), 0, next_iword

        if issubclass(elem_type, DataField):
            raise ValueError(
                f"Array element '{elem_type.__name__}' has bitwidth {elem_bw} > word_bw={word_bw}; "
                "DataField elements cannot be split across words."
            )
        if src_type == "axi4_stream":
            lines.append("    int elem_count = 0;")
            lines.append("    bool stop = false;")
        for d in range(ndims):
            loop_cond = f"{idx_names[d]} < {n_eff_names[d]}"
            if src_type == "axi4_stream":
                loop_cond += " && !stop"
            lines.append(f"    for (int {idx_names[d]} = 0; {loop_cond}; ++{idx_names[d]}) {{")
        body_indent = "    " * (ndims + 1)
        if src_type == "array":
            lines.append(f"{body_indent}{elem_expr}.template read_array<{word_bw}>(&{source}[in_idx]);")
            lines.append(f"{body_indent}in_idx += {words_per_elem};")
        elif src_type == "stream":
            lines.append(f"{body_indent}{elem_expr}.template read_stream<{word_bw}>({source});")
            lines.append(f"{body_indent}in_idx += {words_per_elem};")
        else:
            lines.append(f"{body_indent}streamutils::tlast_status elem_tl = streamutils::tlast_status::no_tlast;")
            lines.append(f"{body_indent}{elem_expr}.template read_axi4_stream<{word_bw}>({source}, elem_tl);")
            lines.append(f"{body_indent}in_idx += {words_per_elem};")
            lines.append(f"{body_indent}elem_count++;")
            lines.append(f"{body_indent}if (elem_tl == streamutils::tlast_status::tlast_early) {{")
            lines.append(f"{body_indent}    tl = elem_tl;")
            lines.append(f"{body_indent}    stop = true;")
            lines.append(f"{body_indent}}}")
            lines.append(f"{body_indent}if (elem_tl == streamutils::tlast_status::tlast_at_end) {{")
            lines.append(
                f"{body_indent}    tl = (elem_count < ({n_total_expr})) ? streamutils::tlast_status::tlast_early : streamutils::tlast_status::tlast_at_end;"
            )
            lines.append(f"{body_indent}    stop = true;")
            lines.append(f"{body_indent}}}")
        for d in range(ndims):
            lines.append(f"{'    ' * (ndims - d)}}}")
        next_iword = start_iword + cls.nwords_per_inst(word_bw) if cls.static else iword0
        return cls._scope_local_lines(lines), 0, next_iword

    @classmethod
    def _gen_dump_json_recursive(
        cls,
        prefix: str,
        member_name: str | None,
        os_name: str,
        depth_expr: str,
        indent_var: str,
    ) -> list[str]:
        _ = depth_expr, indent_var
        shape = cls._normalized_shape()
        ndims = len(shape)
        idx_names = [f"i{i}" for i in range(ndims)]
        elem_expr = cls._element_expr(prefix=prefix, member_name=member_name, idx_names=idx_names)
        lines: list[str] = []

        def emit_value(indent: str) -> None:
            elem_type = cls._element_type()
            if issubclass(elem_type, DataList):
                lines.append(f"{indent}{elem_expr}.dump_json({os_name}, indent, level + 1);")
                return
            if issubclass(elem_type, DataField):
                val_expr = elem_expr
                cpp_name = elem_type.cpp_class_name()
                if getattr(elem_type, 'enum_type', None) is not None:
                    val_expr = f"static_cast<int>({val_expr})"
                elif cpp_name.startswith("ap_uint"):
                    val_expr = f"static_cast<unsigned long long>({val_expr})"
                elif cpp_name.startswith("ap_int"):
                    val_expr = f"static_cast<long long>({val_expr})"
                lines.append(f"{indent}{os_name} << {val_expr};")
                return
            lines.append(f"{indent}{elem_expr}.dump_json({os_name}, indent, level + 1);")

        def emit_level(level: int, indent: str) -> None:
            if level == ndims:
                emit_value(indent)
                return
            idx = idx_names[level]
            dim = shape[level]
            lines.append(f'{indent}{os_name} << "[";')
            lines.append(f"{indent}for (int {idx} = 0; {idx} < {dim}; ++{idx}) {{")
            lines.append(f'{indent}    if ({idx} > 0) {{ {os_name} << ","; }}')
            emit_level(level + 1, indent + "    ")
            lines.append(f"{indent}}}")
            lines.append(f'{indent}{os_name} << "]";')

        emit_level(0, "")
        return lines

    @classmethod
    def gen_dump_json(cls, indent_level: int = 0) -> str:
        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)
        lines = [
            f"{indent}void dump_json(std::ostream& os, int indent = 2, int level = 0) const {{",
            f"{i1}const int step = (indent < 0) ? 0 : indent;",
        ]
        for line in cls._gen_dump_json_recursive(
            prefix="this->",
            member_name=None,
            os_name="os",
            depth_expr="level",
            indent_var="step",
        ):
            if line.startswith("    "):
                line = line[4:]
            lines.append(f"{i1}{line}" if line else "")
        lines.append(f"{indent}}}")
        return "\n".join(lines)

    @classmethod
    def gen_load_json(cls, indent_level: int = 0) -> str:
        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)
        lines = [f"{indent}void load_json(const std::string& json_text, size_t& pos) {{"]
        for line in cls._gen_load_json_recursive(
            prefix="this->",
            member_name=None,
            json_var="json_text",
            pos_var="pos",
            ctx="root",
        ):
            if line.startswith("    "):
                line = line[4:]
            lines.append(f"{i1}{line}" if line else "")
        lines.extend([
            f"{indent}}}",
            "",
            f"{indent}void load_json(std::istream& is) {{",
            f"{i1}std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());",
            f"{i1}size_t pos = 0;",
            f"{i1}streamutils::json_skip_ws(json_text, pos);",
            f"{i1}this->load_json(json_text, pos);",
            f"{i1}streamutils::json_skip_ws(json_text, pos);",
            f"{i1}if (pos != json_text.size()) {{",
            f'{i1}    throw std::runtime_error("Trailing characters after JSON object.");',
            f"{i1}}}",
            f"{indent}}}",
        ])
        return "\n".join(lines)

    @classmethod
    def _gen_load_json_recursive(
        cls,
        prefix: str,
        member_name: str | None,
        json_var: str,
        pos_var: str,
        ctx: str,
    ) -> list[str]:
        _ = ctx
        shape = cls._normalized_shape()
        ndims = len(shape)
        idx_names = [f"i{i}" for i in range(ndims)]
        elem_expr = cls._element_expr(prefix=prefix, member_name=member_name, idx_names=idx_names)
        lines: list[str] = []

        def emit_value(indent: str) -> None:
            elem_type = cls._element_type()
            if issubclass(elem_type, DataList):
                lines.append(f"{indent}{elem_expr}.load_json({json_var}, {pos_var});")
                return
            if issubclass(elem_type, DataField):
                lines.append(f"{indent}{elem_expr} = {elem_type._json_load_expr(json_var=json_var, pos_var=pos_var)};")
                return
            lines.append(f"{indent}{elem_expr}.load_json({json_var}, {pos_var});")

        def emit_level(level: int, indent: str) -> None:
            if level == ndims:
                emit_value(indent)
                return
            idx = idx_names[level]
            dim = shape[level]
            lines.append(f"{indent}streamutils::json_expect_char({json_var}, {pos_var}, '[');")
            lines.append(f"{indent}for (int {idx} = 0; {idx} < {dim}; ++{idx}) {{")
            lines.append(f"{indent}    if ({idx} > 0) {{")
            lines.append(f"{indent}        streamutils::json_expect_char({json_var}, {pos_var}, ',');")
            lines.append(f"{indent}    }}")
            emit_level(level + 1, indent + "    ")
            lines.append(f"{indent}}}")
            lines.append(f"{indent}streamutils::json_expect_char({json_var}, {pos_var}, ']');")

        emit_level(0, "")
        return lines

    @classmethod
    def _gen_include_decl(cls, word_bw_supported: list[int] | None = None, framed: bool = False) -> str:
        lines = [f"struct {cls.cpp_class_name()} {{"]
        lines.extend(cls.gen_class_elems(indent_level=1))
        lines.append("")
        lines.append(f"    static constexpr int bitwidth = {cls.get_bitwidth()};")
        if word_bw_supported:
            lines.append("")
            lines.append("    template<int word_bw>")
            lines.append("    struct word_bw_tag {};")
            lines.append("")
            lines.append("    template<int word_bw>")
            lines.append("    static constexpr int nwords_value(word_bw_tag<word_bw>) {")
            lines.append('            static_assert(word_bw < 0, "Unsupported word_bw for nwords");')
            lines.append("            return 0;")
            lines.append("    }")
            for bw in word_bw_supported:
                lines.append("")
                lines.append(f"    static constexpr int nwords_value(word_bw_tag<{bw}>) {{")
                lines.append(f"            return {cls.nwords_per_inst(bw)};")
                lines.append("    }")
            lines.append("")
            lines.append("    template<int word_bw>")
            lines.append("    static constexpr int nwords() {")
            lines.append("        return nwords_value(word_bw_tag<word_bw>{});")
            lines.append("    }")
        nwords_len_helpers = cls.gen_nwords_len_helpers(indent_level=1, word_bw_supported=word_bw_supported)
        if nwords_len_helpers:
            lines.append("")
            lines.extend(nwords_len_helpers.splitlines())
        # Whole-object pack/unpack is non-synthesizable past the HLS ap_uint
        # limit (a large buffer cannot ride a single word); skip it for such
        # arrays so the header still compiles under C-synthesis.  Element-wise /
        # burst access (array_utils, below) is how these buffers are used.
        if cls.can_pack_whole():
            pack_decl = cls.gen_pack(indent_level=1)
            if pack_decl:
                lines.append("")
                lines.extend(pack_decl.splitlines())
            unpack_decl = cls.gen_unpack(indent_level=1)
            if unpack_decl:
                lines.append("")
                lines.extend(unpack_decl.splitlines())
        stream_helpers = cls.gen_stream_helpers(indent_level=1, word_bw_supported=word_bw_supported)
        if stream_helpers:
            lines.append("")
            lines.extend(stream_helpers.splitlines())
        if word_bw_supported:
            for dst_type in (("array", "stream", "axi4_stream") + (("framed_stream",) if framed else ())):
                lines.append("")
                lines.extend(cls.gen_write(dst_type=dst_type, word_bw_supported=word_bw_supported, indent_level=1).splitlines())
            for src_type in (("array", "stream", "axi4_stream") + (("framed_stream",) if framed else ())):
                lines.append("")
                lines.extend(cls.gen_read(src_type=src_type, word_bw_supported=word_bw_supported, indent_level=1).splitlines())
        tb_member_declarations = cls._gen_tb_member_declarations(indent_level=1)
        if tb_member_declarations:
            lines.extend([
                "",
                f"#ifdef {cls.tb_member_macro()}",
            ])
            lines.extend(tb_member_declarations.splitlines())
            lines.append("#endif")
        lines.append("};")
        return "\n".join(lines)

    @classmethod
    def _gen_tb_member_declarations(cls, indent_level: int = 0) -> str:
        return cls._gen_json_member_declarations(indent_level=indent_level)

    @classmethod
    def _gen_tb_member_definitions(cls, indent_level: int = 0) -> str:
        indent = cls._get_indent(indent_level)
        i1 = cls._get_indent(indent_level + 1)
        lines = [
            f"{indent}inline void {cls.cpp_class_name()}::dump_json(std::ostream& os, int indent, int level) const {{",
            f"{i1}const int step = (indent < 0) ? 0 : indent;",
        ]
        for line in cls._gen_dump_json_recursive(
            prefix="this->",
            member_name=None,
            os_name="os",
            depth_expr="level",
            indent_var="step",
        ):
            if line.startswith("    "):
                line = line[4:]
            lines.append(f"{i1}{line}" if line else "")

        lines.extend([
            f"{indent}}}",
            "",
            f"{indent}inline void {cls.cpp_class_name()}::load_json(const std::string& json_text, size_t& pos) {{",
        ])
        for line in cls._gen_load_json_recursive(
            prefix="this->",
            member_name=None,
            json_var="json_text",
            pos_var="pos",
            ctx="root",
        ):
            if line.startswith("    "):
                line = line[4:]
            lines.append(f"{i1}{line}" if line else "")

        lines.extend([
            f"{indent}}}",
            "",
            f"{indent}inline void {cls.cpp_class_name()}::load_json(std::istream& is) {{",
            f"{i1}std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());",
            f"{i1}size_t pos = 0;",
            f"{i1}streamutils::json_skip_ws(json_text, pos);",
            f"{i1}this->load_json(json_text, pos);",
            f"{i1}streamutils::json_skip_ws(json_text, pos);",
            f"{i1}if (pos != json_text.size()) {{",
            f'{i1}    throw std::runtime_error("Trailing characters after JSON object.");',
            f"{i1}}}",
            f"{indent}}}",
            "",
            f"{indent}inline void {cls.cpp_class_name()}::dump_json_file(const char* file_path, int indent) const {{",
            f"{i1}std::ofstream ofs(file_path);",
            f"{i1}if (!ofs) {{",
            f'{i1}    throw std::runtime_error("Failed to open output JSON file.");',
            f"{i1}}}",
            f"{i1}this->dump_json(ofs, indent);",
            f"{indent}}}",
            "",
            f"{indent}inline void {cls.cpp_class_name()}::load_json_file(const char* file_path) {{",
            f"{i1}std::ifstream ifs(file_path);",
            f"{i1}if (!ifs) {{",
            f'{i1}    throw std::runtime_error("Failed to open input JSON file.");',
            f"{i1}}}",
            f"{i1}this->load_json(ifs);",
            f"{indent}}}",
        ])
        return "\n".join(lines)

    def write_uint32_file(
        self,
        file_path: str | Path,
        write_slice: Any = None,
        nwrite: int | None = None,
    ) -> Path:
        if write_slice is not None and nwrite is not None:
            raise ValueError("Specify only one of write_slice or nwrite.")
        if nwrite is not None and nwrite < 0:
            raise ValueError("nwrite must be non-negative.")

        shape = self.__class__._normalized_shape()
        data = self.val if self.val is not None else self.__class__.init_value()
        arr = np.asarray(data)
        if arr.ndim > 0:
            if write_slice is None and nwrite is None:
                default_nwrite = int(shape[0]) if len(shape) > 0 else arr.shape[0]
                write_slice = (slice(0, default_nwrite),) + (slice(None),) * (arr.ndim - 1)
            elif nwrite is not None:
                write_slice = (slice(0, int(nwrite)),) + (slice(None),) * (arr.ndim - 1)
        else:
            if nwrite is not None:
                if int(nwrite) == 0:
                    write_slice = np.s_[:0]
                elif int(nwrite) == 1:
                    write_slice = ()
                else:
                    raise ValueError("nwrite > 1 is invalid for scalar-valued DataArray.")

        selected = arr if write_slice is None else arr[write_slice]
        selected_shape = tuple(selected.shape)
        words: list[int] = [0]
        curr_ipos = 0
        curr_iword = 0
        child = self.__class__._element_type()()
        if selected_shape:
            for idxs in np.ndindex(selected_shape):
                child.val = selected[idxs]
                curr_ipos, curr_iword = child._serialize_recursive(32, words, curr_ipos, curr_iword)
        else:
            child.val = selected.item() if isinstance(selected, np.ndarray) else selected
            curr_ipos, curr_iword = child._serialize_recursive(32, words, curr_ipos, curr_iword)
        n_words = curr_iword + (1 if curr_ipos > 0 else 0)
        out_words = np.asarray(words[:n_words], dtype="<u4") if n_words > 0 else np.asarray([], dtype="<u4")
        out_path = Path(file_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_words.tofile(out_path)
        return out_path

    def _serialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        shape = self.__class__._normalized_shape()
        curr_ipos = ipos0
        curr_iword = iword0
        data = self.val if self.val is not None else self.__class__.init_value()
        child = self.__class__._element_type()()

        def get_elem(container: Any, idxs: tuple[int, ...]) -> Any:
            ref = container
            for idx in idxs:
                ref = ref[idx]
            return ref

        if len(shape) == 0:
            child.val = data
            return child._serialize_recursive(word_bw, words, curr_ipos, curr_iword)

        for idxs in np.ndindex(shape):
            child.val = get_elem(data, idxs)
            curr_ipos, curr_iword = child._serialize_recursive(word_bw, words, curr_ipos, curr_iword)
        return curr_ipos, curr_iword

    def _deserialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        shape = self.__class__._normalized_shape()
        curr_ipos = ipos0
        curr_iword = iword0
        data = self.__class__.init_value()
        child = self.__class__._element_type()()

        def set_elem(container: Any, idxs: tuple[int, ...], value: Any) -> None:
            ref = container
            for idx in idxs[:-1]:
                ref = ref[idx]
            if idxs:
                ref[idxs[-1]] = value

        if len(shape) == 0:
            curr_ipos, curr_iword = child._deserialize_recursive(word_bw, words, curr_ipos, curr_iword)
            self._val = child.val
            return curr_ipos, curr_iword

        for idxs in np.ndindex(shape):
            curr_ipos, curr_iword = child._deserialize_recursive(word_bw, words, curr_ipos, curr_iword)
            set_elem(data, idxs, child.val)
        self._val = data
        return curr_ipos, curr_iword

    def is_close(
        self,
        other: DataSchema,
        rel_tol: float | None = None,
        abs_tol: float | None = 1e-8,
    ) -> bool:
        if not isinstance(other, self.__class__):
            return False
        v1 = np.asarray(self.val)
        v2 = np.asarray(other.val)
        if v1.shape != v2.shape:
            return False
        if np.issubdtype(v1.dtype, np.floating) or np.issubdtype(v2.dtype, np.floating):
            kwargs: dict[str, float] = {}
            if rel_tol is not None:
                kwargs["rtol"] = rel_tol
            if abs_tol is not None:
                kwargs["atol"] = abs_tol
            return bool(np.all(np.isclose(v1.astype(float), v2.astype(float), **kwargs)))
        return bool(np.array_equal(v1, v2))


class VarDataArray(DataSchema):
    """Variable-length array schema with an in-band length field.

    Stores at most ``len_max`` elements of ``elem_type``.  The serialized layout
    is **length-first** and **bit-contiguous**::

        [length : nbits_len][elem0][elem1]...[elemN-1]

    where ``N = len(self.val) <= len_max`` is the active element count.

    The *declared / max* bitwidth (used for codegen and buffer allocation) is::

        get_bitwidth() == nbits_len + len_max * elem_type.get_bitwidth()

    The *active* bitwidth for a specific instance is::

        get_bitwidth_active() == nbits_len + len(val) * elem_type.get_bitwidth()

    Use :meth:`specialize` to create a concrete subclass::

        UInt16VarArr = VarDataArray.specialize(
            elem_type=IntField.specialize(16, signed=False),
            len_max=32,
        )
        obj = UInt16VarArr()
        obj.val = np.array([10, 20, 30], dtype=np.uint32)

    Parameters
    ----------
    elem_type:
        A :class:`DataSchema` subclass that defines the element type.  Also
        accepted as the ``elem_type`` keyword for ``specialize()``.
    len_max:
        Maximum active length.  Non-negative; zero is allowed (always empty).
    nbits_len:
        Bit-width of the in-band length field.  If omitted, defaults to
        ``max(1, int(len_max).bit_length())`` which can represent ``0..len_max``.
    """

    elem_type: ClassVar[type[DataSchema] | None] = None
    len_max: ClassVar[int] = 0
    nbits_len: ClassVar[int] = 1
    can_gen_include: ClassVar[bool] = False
    allowed_specialize_kwargs: ClassVar[set[str]] = DataSchema.allowed_specialize_kwargs
    _specializations: ClassVar[dict[tuple[Any, ...], type["VarDataArray"]]] = {}

    def __init__(self, value: Any = None):
        self._val = self.__class__.init_value()
        if value is not None:
            self.val = value

    # -- element type accessor -------------------------------------------------

    @classmethod
    def _elem_type(cls) -> type[DataSchema]:
        if cls.elem_type is None:
            raise TypeError(f"{cls.__name__} does not define elem_type.")
        return cls.elem_type

    # -- specialize ------------------------------------------------------------

    @classmethod
    @defer_if_symbolic
    def specialize(
        cls,
        elem_type: type[DataSchema],
        len_max: int,
        nbits_len: int | None = None,
        **kwargs: Any,
    ) -> type["VarDataArray"]:
        """Return a cached subclass with the given element type and max length.

        Parameters
        ----------
        elem_type:
            The element schema class.
        len_max:
            Maximum active length (``>= 0``).
        nbits_len:
            Bit-width of the length field.  Defaults to
            ``max(1, int(len_max).bit_length())``.
        **kwargs:
            Structural overrides (``include_dir``, ``include_filename``,
            ``cpp_repr``).
        """
        if not isinstance(elem_type, type) or not issubclass(elem_type, DataSchema):
            raise TypeError("elem_type must be a DataSchema subclass.")
        len_max = int(len_max)
        if len_max < 0:
            raise ValueError("len_max must be non-negative.")
        if nbits_len is None:
            nbits_len = max(1, len_max.bit_length())
        else:
            nbits_len = int(nbits_len)
            if nbits_len < 1:
                raise ValueError("nbits_len must be at least 1.")

        overrides = cls.validate_specialize_kwargs(kwargs)
        override_items = tuple(sorted(overrides.items()))
        key = (cls, elem_type, len_max, nbits_len, override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        subclass_name = f"{elem_type.__name__}VarArray"
        specialized_attrs = cls.merge_specialize_attrs(
            {
                "elem_type": elem_type,
                "len_max": len_max,
                "nbits_len": nbits_len,
                "can_gen_include": True,
                "__module__": cls.__module__,
                "__doc__": (
                    f"Specialized VarDataArray: elem_type={elem_type.__name__}, "
                    f"len_max={len_max}, nbits_len={nbits_len}."
                ),
            },
            overrides,
        )
        specialized = type(subclass_name, (cls,), specialized_attrs)
        cls._specializations[key] = specialized
        return specialized

    # -- runtime value ---------------------------------------------------------

    @classmethod
    def init_value(cls) -> Any:
        """Return an empty (zero-length) active value."""
        if cls.elem_type is None:
            return []
        elem_init = cls._elem_type().init_value()
        if isinstance(elem_init, np.generic):
            return np.zeros((0,), dtype=np.asarray(elem_init).dtype)
        if isinstance(elem_init, np.ndarray):
            return np.zeros((0,) + tuple(elem_init.shape), dtype=elem_init.dtype)
        return []

    def __len__(self) -> int:
        """Return the active number of elements."""
        return len(self._val)

    def __repr__(self) -> str:
        elem = type(self).elem_type
        elem_name = elem.__name__ if elem is not None else "?"
        return (
            f"{type(self).__name__}(elem_type={elem_name}, "
            f"len={len(self._val)}, len_max={type(self).len_max})"
        )

    @property
    def val(self) -> Any:
        """Return the active array (length ``<= len_max``)."""
        return self._val

    @val.setter
    def val(self, value: Any) -> None:
        if isinstance(value, self.__class__):
            value = value.val
        self._val = self._convert(value)

    def _convert(self, value: Any) -> Any:
        cls = self.__class__
        elem_cls = cls._elem_type()
        elem_init = elem_cls.init_value()

        if isinstance(elem_init, np.generic):
            arr = np.asarray(value, dtype=np.asarray(elem_init).dtype)
            if arr.ndim == 0:
                arr = arr.reshape(1)
            elif arr.ndim != 1:
                raise ValueError(
                    f"{cls.__name__}.val expects a 1-D array for scalar elements; "
                    f"got shape {arr.shape}."
                )
        elif isinstance(elem_init, np.ndarray):
            elem_shape = tuple(elem_init.shape)
            arr = np.asarray(value, dtype=elem_init.dtype)
            expected_ndim = 1 + len(elem_shape)
            if arr.ndim != expected_ndim:
                raise ValueError(
                    f"{cls.__name__}.val expects a {expected_ndim}-D array; "
                    f"got {arr.ndim}."
                )
            if tuple(arr.shape[1:]) != elem_shape:
                raise ValueError(
                    f"{cls.__name__}.val element shape must be {elem_shape}; "
                    f"got {arr.shape[1:]}."
                )
        else:
            arr = list(value)

        active = len(arr)
        if active > cls.len_max:
            raise ValueError(
                f"{cls.__name__}.val length {active} exceeds len_max={cls.len_max}."
            )
        return arr

    # -- size APIs (max / active) ----------------------------------------------

    @classmethod
    def get_bitwidth(cls) -> int:
        """Return the maximum packed bitwidth (length field + max elements)."""
        return cls.nbits_len + cls.len_max * cls._elem_type().get_bitwidth()

    def get_bitwidth_active(self) -> int:
        """Return the active packed bitwidth for the current value."""
        return (
            self.__class__.nbits_len
            + len(self._val) * self.__class__._elem_type().get_bitwidth()
        )

    @classmethod
    def nwords_per_inst(cls, word_bw: int) -> int:
        """Return the maximum word count (length field + ``len_max`` elements)."""
        if word_bw <= 0:
            raise ValueError("word_bw must be positive.")

        # Simulate cursor for max-length layout.
        IntFieldLen = IntField.specialize(cls.nbits_len, signed=False)
        len_field = IntFieldLen()
        words: list[int] = [0]
        curr_ipos, curr_iword = len_field._serialize_recursive(word_bw, words, 0, 0)

        child = cls._elem_type()()
        for _ in range(cls.len_max):
            curr_ipos, curr_iword = child._serialize_recursive(
                word_bw, words, curr_ipos, curr_iword
            )

        n = curr_iword + (1 if curr_ipos > 0 else 0)
        return n if n > 0 else 1

    @classmethod
    def _max_cursor_step(
        cls,
        word_bw: int,
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        """Advance cursor by the **maximum** (len_max-length) serialized layout.

        Overrides :meth:`DataSchema._max_cursor_step` so that when a
        :class:`DataList` calls this method to compute its worst-case
        :meth:`nwords_per_inst`, it gets the full max-length cursor advance
        rather than the zero-length default-instance advance.
        """
        IntFieldLen = IntField.specialize(cls.nbits_len, signed=False)
        words: list[int] = [0]
        curr_ipos, curr_iword = IntFieldLen()._serialize_recursive(
            word_bw, words, ipos0, iword0
        )
        child = cls._elem_type()()
        for _ in range(cls.len_max):
            curr_ipos, curr_iword = child._serialize_recursive(
                word_bw, words, curr_ipos, curr_iword
            )
        return curr_ipos, curr_iword

    # -- serialization ---------------------------------------------------------

    def _serialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        cls = self.__class__
        active_len = len(self._val)

        # Write the length field first.
        IntFieldLen = IntField.specialize(cls.nbits_len, signed=False)
        len_field = IntFieldLen()
        len_field.val = active_len
        curr_ipos, curr_iword = len_field._serialize_recursive(
            word_bw, words, ipos0, iword0
        )

        # Write the active elements.
        child = cls._elem_type()()
        for i in range(active_len):
            child.val = self._val[i]
            curr_ipos, curr_iword = child._serialize_recursive(
                word_bw, words, curr_ipos, curr_iword
            )

        return curr_ipos, curr_iword

    def _deserialize_recursive(
        self,
        word_bw: int,
        words: list[int],
        ipos0: int = 0,
        iword0: int = 0,
    ) -> tuple[int, int]:
        cls = self.__class__

        # Read the length field.
        IntFieldLen = IntField.specialize(cls.nbits_len, signed=False)
        len_field = IntFieldLen()
        curr_ipos, curr_iword = len_field._deserialize_recursive(
            word_bw, words, ipos0, iword0
        )
        active_len = int(len_field.val)
        if active_len > cls.len_max:
            raise ValueError(
                f"{cls.__name__}: deserialized length {active_len} exceeds "
                f"len_max={cls.len_max}."
            )

        # Read the active elements.
        child = cls._elem_type()()
        elem_init = cls._elem_type().init_value()

        if isinstance(elem_init, np.generic):
            dtype = np.asarray(elem_init).dtype
            result: Any = np.zeros(active_len, dtype=dtype)
            for i in range(active_len):
                curr_ipos, curr_iword = child._deserialize_recursive(
                    word_bw, words, curr_ipos, curr_iword
                )
                result[i] = child.val
        elif isinstance(elem_init, np.ndarray):
            result = np.zeros((active_len,) + tuple(elem_init.shape), dtype=elem_init.dtype)
            for i in range(active_len):
                curr_ipos, curr_iword = child._deserialize_recursive(
                    word_bw, words, curr_ipos, curr_iword
                )
                result[i] = child.val
        else:
            result = []
            for i in range(active_len):
                curr_ipos, curr_iword = child._deserialize_recursive(
                    word_bw, words, curr_ipos, curr_iword
                )
                result.append(child.val)

        self._val = result
        return curr_ipos, curr_iword

    # -- equality / closeness --------------------------------------------------

    def is_close(
        self,
        other: DataSchema,
        rel_tol: float | None = None,
        abs_tol: float | None = 1e-8,
    ) -> bool:
        if not isinstance(other, self.__class__):
            return False
        if len(self._val) != len(other._val):
            return False
        if len(self._val) == 0:
            return True
        v1 = np.asarray(self._val)
        v2 = np.asarray(other._val)
        if v1.shape != v2.shape:
            return False
        if np.issubdtype(v1.dtype, np.floating) or np.issubdtype(v2.dtype, np.floating):
            kwargs: dict[str, float] = {}
            if rel_tol is not None:
                kwargs["rtol"] = rel_tol
            if abs_tol is not None:
                kwargs["atol"] = abs_tol
            return bool(np.all(np.isclose(v1.astype(float), v2.astype(float), **kwargs)))
        return bool(np.array_equal(v1, v2))

    def to_dict(self) -> Any:
        """Return a JSON-serializable representation (list of element values)."""
        if isinstance(self._val, np.ndarray):
            return self._val.tolist()
        return list(self._val)

    def from_dict(self, data: Any) -> "VarDataArray":
        """Populate this instance from a list of element values."""
        self.val = data
        return self

    # -- C++ code generation ---------------------------------------------------

    @classmethod
    def cpp_class_name(cls) -> str:
        if cls.cpp_repr is not None:
            return cls.cpp_repr
        return cls.__name__

    @classmethod
    def get_dependencies(cls) -> list[type[DataSchema]]:
        elem_cls = cls._elem_type()
        if elem_cls.can_gen_include and elem_cls is not cls:
            return [elem_cls]
        return []

    @classmethod
    def _gen_include_decl(cls, word_bw_supported: list[int] | None = None, framed: bool = False) -> str:
        """Generate a C++ struct for this ``VarDataArray``.

        The struct holds an in-band ``len`` field and a fixed-capacity ``data``
        array.  For :class:`DataField` element types the struct also emits
        ``write_array`` / ``read_array`` helpers for each requested word width.
        Complex element types (``DataList``, nested ``DataArray``) produce only
        the data members and static constants; write/read helpers for those
        cases are left for a future implementation.
        """
        cls_name = cls.cpp_class_name()
        elem_cls = cls._elem_type()
        elem_cpp = elem_cls.cpp_class_name()
        elem_bw = elem_cls.get_bitwidth()

        i1 = cls._get_indent(1)
        i2 = cls._get_indent(2)
        i3 = cls._get_indent(3)

        lines = [f"struct {cls_name} {{"]
        lines.append(f"{i1}ap_uint<{cls.nbits_len}> len;")
        lines.append(f"{i1}{elem_cpp} data[{cls.len_max}];")
        lines.append("")
        lines.append(f"{i1}static constexpr int len_max = {cls.len_max};")
        lines.append(f"{i1}static constexpr int nbits_len = {cls.nbits_len};")
        lines.append(f"{i1}static constexpr int bitwidth = {cls.get_bitwidth()};")

        if word_bw_supported:
            # nwords_active<word_bw>() — runtime cursor simulation.
            # Only valid when elem_bw <= word_bw (same constraint as DataField).
            lines.append("")
            lines.append(f"{i1}// nwords_active<word_bw>(): word count for the active length.")
            lines.append(f"{i1}template<int word_bw>")
            lines.append(f"{i1}int nwords_active() const {{")
            lines.append(f"{i2}int ipos = 0, iword = 0;")
            lines.append(f"{i2}// length field: {cls.nbits_len} bits")
            lines.append(f"{i2}if (ipos + {cls.nbits_len} > word_bw) {{ ++iword; ipos = 0; }}")
            lines.append(f"{i2}ipos += {cls.nbits_len};")
            lines.append(f"{i2}if (ipos == word_bw) {{ ++iword; ipos = 0; }}")
            lines.append(f"{i2}// active elements: {elem_bw} bits each")
            lines.append(f"{i2}// Clamp to len_max: `len` is nbits_len wide, so it can encode values ABOVE len_max (4 bits -> 0..15 when len_max=8). Unclamped, an out-of-range len (it is read off the WIRE) indexes past data[len_max]. The clamp also gives HLS a compile-time trip count, which `i < len` alone does not.")
            lines.append(
                f"{i2}const int n_act = static_cast<int>(this->len) < len_max"
                f" ? static_cast<int>(this->len) : len_max;"
            )
            lines.append(f"{i2}for (int i = 0; i < n_act; ++i) {{")
            lines.append(f"{i3}if (ipos + {elem_bw} > word_bw) {{ ++iword; ipos = 0; }}")
            lines.append(f"{i3}ipos += {elem_bw};")
            lines.append(f"{i3}if (ipos == word_bw) {{ ++iword; ipos = 0; }}")
            lines.append(f"{i2}}}")
            lines.append(f"{i2}return iword + (ipos > 0 ? 1 : 0);")
            lines.append(f"{i1}}}")

            # Per-word-width write_array / read_array (DataField elements only).
            if issubclass(elem_cls, DataField):
                for bw in word_bw_supported:
                    lines.append("")
                    lines.append(
                        f"{i1}void write_array(ap_uint<{bw}> x[]) const {{"
                    )
                    lines.append(f"{i2}const int n_out = nwords_active<{bw}>();")
                    lines.append(f"{i2}for (int w = 0; w < n_out; ++w) x[w] = 0;")
                    lines.append(f"{i2}int ipos = 0, iword = 0;")
                    lines.append(f"{i2}// length field")
                    lines.append(
                        f"{i2}if (ipos + {cls.nbits_len} > {bw}) {{ ++iword; ipos = 0; }}"
                    )
                    lines.append(
                        f"{i2}x[iword].range(ipos + {cls.nbits_len} - 1, ipos) = this->len;"
                    )
                    lines.append(f"{i2}ipos += {cls.nbits_len};")
                    lines.append(f"{i2}if (ipos == {bw}) {{ ++iword; ipos = 0; }}")
                    lines.append(f"{i2}// active elements")
                    lines.append(f"{i2}// Clamp to len_max: `len` is nbits_len wide, so it can encode values ABOVE len_max (4 bits -> 0..15 when len_max=8). Unclamped, an out-of-range len (it is read off the WIRE) indexes past data[len_max]. The clamp also gives HLS a compile-time trip count, which `i < len` alone does not.")
                    lines.append(
                        f"{i2}const int n_act = static_cast<int>(this->len) < len_max"
                        f" ? static_cast<int>(this->len) : len_max;"
                    )
                    lines.append(
                        f"{i2}for (int i = 0; i < n_act; ++i) {{"
                    )
                    elem_uint = elem_cls.to_uint_value_expr("this->data[i]")
                    lines.append(f"{i3}ap_uint<{elem_bw}> elem_bits = {elem_uint};")
                    lines.append(
                        f"{i3}if (ipos + {elem_bw} > {bw}) {{ ++iword; ipos = 0; }}"
                    )
                    lines.append(
                        f"{i3}x[iword].range(ipos + {elem_bw} - 1, ipos) = elem_bits;"
                    )
                    lines.append(f"{i3}ipos += {elem_bw};")
                    lines.append(f"{i3}if (ipos == {bw}) {{ ++iword; ipos = 0; }}")
                    lines.append(f"{i2}}}")
                    lines.append(f"{i1}}}")

                    lines.append("")
                    lines.append(
                        f"{i1}void read_array(const ap_uint<{bw}> x[]) {{"
                    )
                    lines.append(f"{i2}int ipos = 0, iword = 0;")
                    lines.append(f"{i2}// length field")
                    lines.append(
                        f"{i2}if (ipos + {cls.nbits_len} > {bw}) {{ ++iword; ipos = 0; }}"
                    )
                    lines.append(
                        f"{i2}this->len = x[iword].range(ipos + {cls.nbits_len} - 1, ipos);"
                    )
                    lines.append(f"{i2}ipos += {cls.nbits_len};")
                    lines.append(f"{i2}if (ipos == {bw}) {{ ++iword; ipos = 0; }}")
                    lines.append(f"{i2}// active elements")
                    lines.append(f"{i2}// Clamp to len_max: `len` is nbits_len wide, so it can encode values ABOVE len_max (4 bits -> 0..15 when len_max=8). Unclamped, an out-of-range len (it is read off the WIRE) indexes past data[len_max]. The clamp also gives HLS a compile-time trip count, which `i < len` alone does not.")
                    lines.append(
                        f"{i2}const int n_act = static_cast<int>(this->len) < len_max"
                        f" ? static_cast<int>(this->len) : len_max;"
                    )
                    lines.append(
                        f"{i2}for (int i = 0; i < n_act; ++i) {{"
                    )
                    lines.append(
                        f"{i3}if (ipos + {elem_bw} > {bw}) {{ ++iword; ipos = 0; }}"
                    )
                    lines.append(
                        f"{i3}ap_uint<{elem_bw}> elem_bits = "
                        f"x[iword].range(ipos + {elem_bw} - 1, ipos);"
                    )
                    lines.append(
                        f"{i3}this->data[i] = {elem_cls.from_uint_expr('elem_bits')};"
                    )
                    lines.append(f"{i3}ipos += {elem_bw};")
                    lines.append(f"{i3}if (ipos == {bw}) {{ ++iword; ipos = 0; }}")
                    lines.append(f"{i2}}}")
                    lines.append(f"{i1}}}")

        lines.append("};")
        return "\n".join(lines)


class DataSchemaStep(Buildable):
    """Buildable that generates C++ include headers for a :class:`DataSchema` class.

    Obtain one via :meth:`DataSchema.as_buildable` rather than instantiating
    directly.

    When used standalone (without a :class:`~waveflow.build.build.BuildDag`),
    streamutils is assumed to reside at the build root (``output_dir="."``).
    For full dependency tracking and a configurable streamutils location, add
    this step to a ``BuildDag`` that already contains a
    :class:`~waveflow.build.streamutils.StreamUtilsStep`.

    Outputs
    -------
    ``"include"``
        The synthesizable ``*.h`` header.
    ``"tb_include"``
        The testbench ``*_tb.h`` header.
    """

    def __init__(
        self,
        schema_cls: type[DataSchema],
        word_bw_supported: list[int] | None = None,
        include_dir: str | None = None,
        include_filename: str | None = None,
        framed: bool = False,
    ) -> None:
        if not schema_cls.can_gen_include:
            raise ValueError(
                f"{schema_cls.__name__} does not support standalone include generation."
            )
        for bw in (word_bw_supported or []):
            if bw <= 0:
                raise ValueError(f"word_bw values must be positive. Got {bw}.")
        self._schema = schema_cls
        self._word_bw: list[int] = list(word_bw_supported) if word_bw_supported else []
        #: When True, also emit the framed_stream read/write methods (plans/memstream_inband.md).
        #: Default False -> a schema's header is byte-identical to before.
        self._framed: bool = framed
        self._include_dir: str = include_dir if include_dir is not None else schema_cls.include_dir
        self._include_filename: str | None = include_filename
        self._streamutils_output_dir: Path = Path(".")
        super().__init__()  # _schema is set, so _default_name() works

    def _default_name(self) -> str:
        return f"{self._schema.__name__}Step"

    # ------------------------------------------------------------------
    # Path helpers (instance-level, respect _include_dir/_include_filename)
    # ------------------------------------------------------------------

    def include_path(self) -> str:
        """Return the header path relative to the build root."""
        include_root = PurePosixPath((self._include_dir or ".").replace("\\", "/"))
        filename = self._include_filename or self._schema.resolved_include_filename()
        if include_root.as_posix() == ".":
            return filename
        return f"{include_root.as_posix()}/{filename}"

    def tb_include_path(self) -> str:
        """Return the TB header path relative to the build root."""
        include_root = PurePosixPath((self._include_dir or ".").replace("\\", "/"))
        filename = self._schema.resolved_tb_include_filename()
        if include_root.as_posix() == ".":
            return filename
        return f"{include_root.as_posix()}/{filename}"

    def include_guard(self) -> str:
        """Return the include guard macro name for the synthesizable header."""
        guard = re.sub(r"[^A-Za-z0-9]+", "_", self.include_path()).strip("_").upper()
        return re.sub(r"_+", "_", guard)

    def tb_include_guard(self) -> str:
        """Return the include guard macro name for the TB header."""
        guard = re.sub(r"[^A-Za-z0-9]+", "_", self.tb_include_path()).strip("_").upper()
        return re.sub(r"_+", "_", guard)

    def _relative_include_path_to(self, dep_step: DataSchemaStep) -> str:
        current_dir = posixpath.dirname(self.include_path()) or "."
        return posixpath.relpath(dep_step.include_path(), start=current_dir)

    def _relative_tb_include_path_to(self, dep_step: DataSchemaStep) -> str:
        current_dir = posixpath.dirname(self.tb_include_path()) or "."
        return posixpath.relpath(dep_step.tb_include_path(), start=current_dir)

    # ------------------------------------------------------------------
    # Buildable contract
    # ------------------------------------------------------------------

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {
            "include": Path(self.include_path()),
            "tb_include": Path(self.tb_include_path()),
        }

    def _member_codegen_include_lines(self) -> list[str]:
        """Return ``#include`` lines a member element's ``cpp_type`` needs (its
        ``get_codegen_includes`` bodies, e.g. ``"wf_cint.h"`` for an integer-inner
        ``ComplexField`` element), deduped + order-preserved.  Only the direct element
        types are gathered: a nested ``DataSchema`` member emits its own header (pulled in
        by :meth:`_dep_include_lines`), so its includes need not be re-emitted here."""
        iter_elems = getattr(self._schema, "_iter_element_schemas", None)
        if iter_elems is None:
            return []
        lines: list[str] = []
        seen: set[str] = set()
        for _, schema_cls in iter_elems():
            for body in schema_cls.get_codegen_includes():
                line = f"#include {body}"
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
        return lines

    def _dep_include_lines(self) -> list[str]:
        """Return ``#include`` lines for schema dependencies."""
        dep_step_map = {d._schema: d for d in self.deps if isinstance(d, DataSchemaStep)}
        lines = []
        for dep_schema in self._schema.get_dependencies():
            if dep_schema in dep_step_map:
                rel = self._relative_include_path_to(dep_step_map[dep_schema])
            else:
                rel = self._schema.relative_include_path_to(dep_schema)
            lines.append(f'#include "{rel}"')
        return lines

    def _dep_tb_include_lines(self) -> list[str]:
        """Return TB ``#include`` lines for schema dependencies."""
        dep_step_map = {d._schema: d for d in self.deps if isinstance(d, DataSchemaStep)}
        lines = []
        for dep_schema in self._schema.get_dependencies():
            if dep_schema in dep_step_map:
                rel = self._relative_tb_include_path_to(dep_step_map[dep_schema])
            else:
                rel = self._schema.relative_tb_include_path_to(dep_schema)
            lines.append(f'#include "{rel}"')
        return lines

    def _render_include(self, config: BuildConfig) -> str:
        out_path = config.root_dir / self.include_path()
        sutils_hls = (
            config.root_dir / self._streamutils_output_dir / "streamutils_hls.h"
        )
        sutils_hls_rel = os.path.relpath(sutils_hls, start=out_path.parent).replace("\\", "/")

        lines = [
            f"#ifndef {self.include_guard()}",
            f"#define {self.include_guard()}",
            "",
            "#include <ap_int.h>",
            "#include <hls_stream.h>",
            "#if __has_include(<hls_axi_stream.h>)",
            "#include <hls_axi_stream.h>",
            "#else",
            "#include <ap_axi_sdata.h>",
            "#endif",
            f'#include "{sutils_hls_rel}"',
            "",
        ]

        # Includes that a member element's cpp_type needs beyond streamutils_hls.h (e.g. the
        # wf_cint.h / <complex> for a ComplexField element), so the struct header is
        # self-contained — nested DataSchema members get their own headers via dep includes.
        member_include_lines = self._member_codegen_include_lines()
        if member_include_lines:
            lines.extend(member_include_lines)
            lines.append("")

        dep_include_lines = self._dep_include_lines()
        if dep_include_lines:
            lines.extend(dep_include_lines)
            lines.append("")

        lines.append(self._schema._gen_include_decl(word_bw_supported=self._word_bw, framed=self._framed))
        lines.extend([
            "",
            f"#endif // {self.include_guard()}",
        ])
        return "\n".join(lines)

    def _render_tb_include(self, config: BuildConfig) -> str:
        out_path = config.root_dir / self.include_path()
        tb_out_path = config.root_dir / self.tb_include_path()
        sutils_tb = (
            config.root_dir / self._streamutils_output_dir / "streamutils_tb.h"
        )
        sutils_tb_rel = os.path.relpath(sutils_tb, start=tb_out_path.parent).replace("\\", "/")
        synth_from_tb = os.path.relpath(out_path, start=tb_out_path.parent).replace("\\", "/")

        dep_tb_lines = self._dep_tb_include_lines()

        tb_lines = [
            f"#ifndef {self.tb_include_guard()}",
            f"#define {self.tb_include_guard()}",
            "",
        ]

        tb_member_definitions = self._schema._gen_tb_member_definitions(indent_level=0)
        if tb_member_definitions:
            tb_lines.extend([
                "#include <cctype>",
                "#include <cstdlib>",
                "#include <fstream>",
                "#include <iostream>",
                "#include <iterator>",
                "#include <stdexcept>",
                "#include <string>",
                f'#include "{sutils_tb_rel}"',
                "",
            ])
            if dep_tb_lines:
                tb_lines.extend(dep_tb_lines)
                tb_lines.append("")
            tb_lines.extend([
                f"#define {self._schema.tb_member_macro()}",
                f'#include "{synth_from_tb}"',
                f"#undef {self._schema.tb_member_macro()}",
                "",
                tb_member_definitions,
            ])
        else:
            tb_lines.append(f'#include "{synth_from_tb}"')

        tb_lines.extend([
            "",
            f"#endif // {self.tb_include_guard()}",
        ])
        return "\n".join(tb_lines)

    def generate(self, key: str, config: BuildConfig) -> str:
        if key == "include":
            return self._render_include(config)
        if key == "tb_include":
            return self._render_tb_include(config)
        raise KeyError(f"Unknown output key: {key!r}")

    # ------------------------------------------------------------------
    # Dependency resolution
    # ------------------------------------------------------------------

    def resolve_deps(self, other_steps: list) -> None:
        """Wire this step to its antecedents in *other_steps*.

        Finds the single :class:`~waveflow.build.streamutils.StreamUtilsStep`
        and the :class:`DataSchemaStep` for each schema dependency.  Raises
        ``ValueError`` when required steps are missing.
        """
        from waveflow.build.streamutils import StreamUtilsStep

        self.deps = []

        su_steps = [s for s in other_steps if isinstance(s, StreamUtilsStep)]
        if not su_steps:
            raise ValueError(
                f"{self.name}: No StreamUtilsStep found. "
                "Register a StreamUtilsStep before this DataSchemaStep."
            )
        if len(su_steps) > 1:
            raise ValueError(
                f"{self.name}: Multiple StreamUtilsStep instances found. "
                "Only one is supported per BuildDag."
            )
        su_step = su_steps[0]
        self._streamutils_output_dir = su_step.output_dir
        self.deps.append(su_step)

        for dep_schema in self._schema.get_dependencies():
            dep_step = next(
                (
                    s for s in other_steps
                    if isinstance(s, DataSchemaStep) and s._schema is dep_schema
                ),
                None,
            )
            if dep_step is not None:
                self.deps.append(dep_step)


__all__ = [
    "DataSchema",
    "DataSchemaStep",
    "DataField",
    "IntField",
    "MemAddr",
    "FloatField",
    "EnumField",
    "BooleanField",
    "DataList",
    "DataArray",
    "VarDataArray",
    "ParamSchema",
]


# ---------------------------------------------------------------------------
# DataArray arithmetic operators (sugar over the per-type functions)
# ---------------------------------------------------------------------------
def _wrap_datarray(val: Any, elem: type[DataSchema]) -> DataArray:
    """Wrap a numpy array + element type into a fresh DataArray[elem]."""
    arr = np.asarray(val)
    shape = arr.shape if arr.shape else (1,)
    return DataArray.specialize(elem, max_shape=tuple(shape))(arr.reshape(shape))


def _elem_kind(elem: type[DataSchema]) -> str:
    if getattr(elem, "is_complex_field", False):    # ComplexField (marker; avoids a cycle)
        return "complex"
    if hasattr(elem, "get_format"):                 # FixedField (duck-typed; avoids a cycle)
        return "fixed"
    if isinstance(elem, type) and issubclass(elem, IntField):
        return "int"
    if isinstance(elem, type) and issubclass(elem, FloatField):
        return "float"
    return "other"


def _int_binop(a: DataArray, b: DataArray, op: str) -> DataArray:
    ea, eb = a.element_type, b.element_type
    if bool(ea.signed) != bool(eb.signed):
        raise NotImplementedError(
            "mixed signed/unsigned integer arithmetic is not supported in v1 (numpy would "
            "coerce int64/uint64 to float64); use a common signedness first.")
    Wa, Wb = ea.get_bitwidth(), eb.get_bitwidth()
    if op == "mul":
        Wr, signed_r = Wa + Wb, bool(ea.signed)
    else:                                            # add / sub: +1 carry bit
        Wr = max(Wa, Wb) + 1
        signed_r = True if op == "sub" else bool(ea.signed)   # subtraction may go negative
    if Wr > MAX_WIDTH:
        raise NotImplementedError(
            f"integer {op} result width {Wr} exceeds the {MAX_WIDTH}-bit limit "
            "(numpy would silently wrap); wide (>64-bit) support is Future.")
    dtype = np.int64 if signed_r else np.uint64
    av = np.asarray(a.val).astype(dtype)
    bv = np.asarray(b.val).astype(dtype)
    out = av * bv if op == "mul" else (av + bv if op == "add" else av - bv)
    return _wrap_datarray(out, IntField.specialize(Wr, signed_r))


def _float_binop(a: DataArray, b: DataArray, op: str) -> DataArray:
    av, bv = np.asarray(a.val), np.asarray(b.val)
    out = av * bv if op == "mul" else (av + bv if op == "add" else av - bv)
    bw = 64 if out.dtype == np.float64 else 32       # numpy passthrough; no growth
    return _wrap_datarray(out, FloatField.specialize(bw))


def _datarray_binop(a: DataArray, b: Any, op: str) -> DataArray:
    if not isinstance(b, DataArray):
        raise TypeError(
            f"DataArray '{op}' needs both operands to be DataArray (got {type(b).__name__}); "
            "scalar/array operands are not supported in v1 — use .val for raw numpy.")
    ka, kb = _elem_kind(a.element_type), _elem_kind(b.element_type)
    if ka != kb or ka == "other":
        raise TypeError(
            f"cannot apply '{op}' to {a.element_type.__name__} and {b.element_type.__name__} "
            "arrays (operands must be the same numeric kind: int, fixed, or float).")
    if ka == "complex":
        # sugar over the ComplexField functions; lazy import avoids a module cycle
        from waveflow.hw.complexfield import cadd as _cadd, cmult as _cmult, csub as _csub
        return {"mul": _cmult, "add": _cadd, "sub": _csub}[op](a, b)
    if ka == "fixed":
        # sugar over the existing FixedField functions; lazy import avoids a module cycle
        from waveflow.hw.fixpoint import add as _fadd, mult as _fmult, sub as _fsub
        return {"mul": _fmult, "add": _fadd, "sub": _fsub}[op](a, b)
    if ka == "int":
        return _int_binop(a, b, op)
    return _float_binop(a, b, op)