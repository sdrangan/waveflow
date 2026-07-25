"""Generate Vitis HLS array read and write helpers for packed element arrays.

Example
-------
```python
from waveflow.hw.arrayutils import gen_array_utils, read_array, write_array
from waveflow.build.build import BuildConfig
from waveflow.hw.dataschema import IntField

Int16 = IntField.specialize(16, signed=True)
path = gen_array_utils(Int16, [32, 64], cfg=BuildConfig(root_dir="include"))
print(path)

packed = write_array([1, 2, 3, 4], elem_type=Int16, word_bw=32)
unpacked = read_array(packed, elem_type=Int16, word_bw=32, shape=4)
```
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import posixpath
import re
from typing import Any, TypeVar

import numpy as np

from waveflow.build.build import Buildable, BuildConfig, BuildResult
from waveflow.hw.dataschema import DataArray, DataList, DataSchema, Words


T = TypeVar('T', bound=DataSchema)


def array(elem_type: type[T], data, static: bool = False) -> DataArray:
    """Construct a :class:`~waveflow.hw.dataschema.DataArray` instance wrapping *data*.

    Internally specializes :class:`~waveflow.hw.dataschema.DataArray` with the
    runtime shape derived from *data* and returns an instance whose ``.val``
    holds the underlying NumPy array.

    Parameters
    ----------
    elem_type : type[DataSchema]
        Element schema class for each entry in the array.
    data : array-like
        The array data, converted to :class:`numpy.ndarray` via
        :func:`numpy.asarray`.
    static : bool
        If ``True`` the resulting specialization has ``static=True`` (fixed
        maximum shape equal to the runtime shape).  Default ``False``.

    Returns
    -------
    DataArray
        A specialized :class:`~waveflow.hw.dataschema.DataArray` instance.
    """
    arr = np.asarray(data)
    shape = arr.shape if arr.ndim > 0 else (1,)
    cls = DataArray.specialize(
        element_type=elem_type,
        max_shape=shape,
        static=static,
    )
    inst = cls()
    inst.val = arr
    return inst


def _normalize_array_shape(shape: int | tuple[int, ...] | list[int]) -> tuple[int, ...]:
    if isinstance(shape, int):
        norm_shape = (int(shape),)
    else:
        norm_shape = tuple(int(dim) for dim in shape)

    if any(dim < 0 for dim in norm_shape):
        raise ValueError("shape dimensions must be non-negative.")

    return norm_shape


def write_array(arr: DataArray | Any, elem_type: type[T] | None = None, *, word_bw: int) -> Words:
    """Pack a Python array of schema elements into hardware words.

    Parameters
    ----------
    arr : DataArray or array-like
        Input data.  Pass a :class:`~waveflow.hw.dataschema.DataArray` to
        supply ``elem_type`` implicitly, or pass a plain array-like together
        with an explicit ``elem_type``.
    elem_type : type[DataSchema] or None
        Element schema class.  Required when *arr* is not a
        :class:`~waveflow.hw.dataschema.DataArray`;
        ignored (with a consistency check) when it is.
    word_bw : int
        Packed output word width in bits.  Must be passed as a keyword argument.

    Returns
    -------
    numpy.ndarray
        Packed hardware words as returned by ``DataSchema.serialize()``.
    """
    if isinstance(arr, DataArray):
        inferred_elem = type(arr).element_type
        if elem_type is not None and elem_type is not inferred_elem:
            raise TypeError(
                f"elem_type mismatch: DataArray carries {inferred_elem.__name__!r} "
                f"but elem_type={elem_type.__name__!r} was also supplied."
            )
        elem_type = inferred_elem
        np_arr = arr.val
    else:
        if elem_type is None:
            raise TypeError("elem_type must be provided when arr is not a DataArray.")
        if not isinstance(elem_type, type) or not issubclass(elem_type, DataSchema):
            raise TypeError("elem_type must be a DataSchema subclass.")
        np_arr = np.asarray(arr)

    if word_bw <= 0:
        raise ValueError("word_bw must be positive.")

    np_arr = np.asarray(np_arr)
    shape = tuple(int(dim) for dim in np_arr.shape)

    array_cls = DataArray.specialize(
        element_type=elem_type,
        max_shape=shape,
        static=True,
    )
    array_obj = array_cls()
    array_obj.val = np_arr
    return array_obj.serialize(word_bw=word_bw)


def write_uint32_file(
    arr: Any,
    elem_type: type[DataSchema],
    file_path: str | Path,
    write_slice: Any = None,
    nwrite: int | None = None,
) -> Path:
    """Pack an array into 32-bit words and write it to a binary file.

    Parameters
    ----------
    arr : Any
        Input array-like value.
    elem_type : type[DataSchema]
        Element schema class describing each array entry.
    file_path : str | Path
        Destination binary file path.
    write_slice : Any, optional
        Optional NumPy-style slice used to select a subset of ``arr`` before
        packing. This matches the behavior of ``DataArray.write_uint32_file``.
    nwrite : int | None, optional
        Convenience argument that selects the first ``nwrite`` entries along the
        leading dimension. Mutually exclusive with ``write_slice``.

    Returns
    -------
    pathlib.Path
        The written file path.
    """
    if write_slice is not None and nwrite is not None:
        raise ValueError("Specify only one of write_slice or nwrite.")
    if nwrite is not None and nwrite < 0:
        raise ValueError("nwrite must be non-negative.")

    np_arr = np.asarray(arr)
    if np_arr.ndim > 0:
        if nwrite is not None:
            write_slice = (slice(0, int(nwrite)),) + (slice(None),) * (np_arr.ndim - 1)
    else:
        if nwrite is not None:
            if int(nwrite) == 0:
                write_slice = np.s_[:0]
            elif int(nwrite) == 1:
                write_slice = ()
            else:
                raise ValueError("nwrite > 1 is invalid for scalar-valued arrays.")

    selected = np_arr if write_slice is None else np_arr[write_slice]
    words = np.asarray(write_array(selected, elem_type=elem_type, word_bw=32), dtype="<u4")

    out_path = Path(file_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    words.tofile(out_path)
    return out_path


def read_array(
    packed: Words,
    elem_type: type[T],
    word_bw: int,
    shape: int | tuple[int, ...] | list[int],
) -> DataArray:
    """Unpack hardware words into a :class:`~waveflow.hw.dataschema.DataArray` instance.

    Parameters
    ----------
    packed : Words
        Packed hardware words accepted by ``DataSchema.deserialize()``.
    elem_type : type[T]
        Element schema class describing each unpacked array entry.
    word_bw : int
        Packed input word width in bits.
    shape : int | tuple[int, ...] | list[int]
        Expected unpacked array shape. A scalar integer is treated as a 1D shape.

    Returns
    -------
    DataArray
        Unpacked :class:`~waveflow.hw.dataschema.DataArray` instance with
        ``element_type`` set to *elem_type*.
    """
    if not isinstance(elem_type, type) or not issubclass(elem_type, DataSchema):
        raise TypeError("elem_type must be a DataSchema subclass.")
    if word_bw <= 0:
        raise ValueError("word_bw must be positive.")

    norm_shape = _normalize_array_shape(shape)

    array_cls = DataArray.specialize(
        element_type=elem_type,
        max_shape=norm_shape,
        static=True,
    )
    array_obj = array_cls()
    array_obj.deserialize(np.asarray(packed), word_bw=word_bw)
    return array_obj


def get_nwords(
    elem_type: type[DataSchema],
    word_bw: int,
    shape: int | tuple[int, ...] | list[int],
) -> int:
    """Return the packed word count for an array shape at a given word width.

    Parameters
    ----------
    elem_type : type[DataSchema]
        Element schema class describing each array entry.
    word_bw : int
        Packed word width in bits.
    shape : int | tuple[int, ...] | list[int]
        Array shape whose serialized/deserialized storage size is requested.
        A scalar integer is treated as a 1D shape.

    Returns
    -------
    int
        Number of packed words consumed by ``deserialize`` input or produced by
        ``serialize`` output for the given array shape.
    """
    if not isinstance(elem_type, type) or not issubclass(elem_type, DataSchema):
        raise TypeError("elem_type must be a DataSchema subclass.")
    if word_bw <= 0:
        raise ValueError("word_bw must be positive.")

    norm_shape = _normalize_array_shape(shape)
    array_cls = DataArray.specialize(
        element_type=elem_type,
        max_shape=norm_shape,
        static=True,
    )
    return int(array_cls.nwords_per_inst(word_bw))


def read_uint32_file(
    file_path: str | Path,
    elem_type: type[DataSchema],
    shape: int | tuple[int, ...] | list[int],
) -> Any:
    """Read packed 32-bit words from a binary file into a Python array.

    Parameters
    ----------
    file_path : str | Path
        Source binary file path containing packed little-endian uint32 words.
    elem_type : type[DataSchema]
        Element schema class describing each unpacked array entry.
    shape : int | tuple[int, ...] | list[int]
        Expected unpacked array shape.

    Returns
    -------
    Any
        The unpacked Python-side array value.
    """
    in_path = Path(file_path)
    words = np.fromfile(in_path, dtype="<u4")
    return read_array(words, elem_type=elem_type, word_bw=32, shape=shape)


def _array_utils_stem(elem_type: type[DataSchema]) -> str:
    name = elem_type.__name__

    int_match = re.fullmatch(r"Int(\d+)", name)
    if int_match is not None:
        return f"int{int_match.group(1)}"

    uint_match = re.fullmatch(r"UInt(\d+)", name)
    if uint_match is not None:
        return f"uint{uint_match.group(1)}"

    return elem_type._camel_to_snake(name)


def _array_utils_filename(elem_type: type[DataSchema]) -> str:
    return f"{_array_utils_stem(elem_type)}_array_utils.h"


def _array_utils_tb_filename(elem_type: type[DataSchema]) -> str:
    return f"{_array_utils_stem(elem_type)}_array_utils_tb.h"


def _array_utils_include_path(elem_type: type[DataSchema]) -> str:
    include_dir = (elem_type.include_dir or ".").replace("\\", "/")
    include_root = PurePosixPath(include_dir)
    filename = _array_utils_filename(elem_type)
    if include_root.as_posix() == ".":
        return filename
    return f"{include_root.as_posix()}/{filename}"


def _array_utils_tb_include_path(elem_type: type[DataSchema]) -> str:
    include_dir = (elem_type.include_dir or ".").replace("\\", "/")
    include_root = PurePosixPath(include_dir)
    filename = _array_utils_tb_filename(elem_type)
    if include_root.as_posix() == ".":
        return filename
    return f"{include_root.as_posix()}/{filename}"


def _array_utils_include_guard(elem_type: type[DataSchema]) -> str:
    guard = re.sub(r"[^A-Za-z0-9]+", "_", _array_utils_include_path(elem_type)).strip("_").upper()
    return re.sub(r"_+", "_", guard)


def _array_utils_tb_include_guard(elem_type: type[DataSchema]) -> str:
    guard = re.sub(r"[^A-Za-z0-9]+", "_", _array_utils_tb_include_path(elem_type)).strip("_").upper()
    return re.sub(r"_+", "_", guard)


def _array_utils_namespace(elem_type: type[DataSchema]) -> str:
    return f"{_array_utils_stem(elem_type)}_array_utils"


def _relative_synth_include_from_tb(elem_type: type[DataSchema]) -> str:
    current_dir = posixpath.dirname(_array_utils_tb_include_path(elem_type)) or "."
    return posixpath.relpath(_array_utils_include_path(elem_type), start=current_dir)


def _relative_streamutils_tb_include(
    elem_type: type[DataSchema], root_dir: Path, su_dir: Path
) -> str:
    tb_out_path = root_dir / _array_utils_tb_include_path(elem_type)
    util_path = root_dir / su_dir / "streamutils_tb.h"
    include_path = os.path.relpath(util_path, start=tb_out_path.parent)
    return include_path.replace("\\", "/")


def _relative_include_for_elem(elem_type: type[DataSchema]) -> str | None:
    if not elem_type.can_gen_include:
        return None
    current_dir = posixpath.dirname(_array_utils_include_path(elem_type)) or "."
    return posixpath.relpath(elem_type.include_path(), start=current_dir)


def _relative_streamutils_include(
    elem_type: type[DataSchema], root_dir: Path, su_dir: Path
) -> str:
    out_path = root_dir / _array_utils_include_path(elem_type)
    util_path = root_dir / su_dir / "streamutils_hls.h"
    include_path = os.path.relpath(util_path, start=out_path.parent)
    return include_path.replace("\\", "/")


def _needs_streamutils_include(elem_type: type[DataSchema]) -> bool:
    if elem_type.can_gen_include:
        return False
    read_expr = elem_type.from_uint_expr("packed_bits")
    write_expr = elem_type.to_uint_value_expr("value")
    return "streamutils::" in read_expr or "streamutils::" in write_expr


def _get_read_recursive_lines(
    elem_type: type[DataSchema],
    word_bw: int,
    dst_expr: str,
    source_expr: str,
) -> list[str]:
    prefix = ""
    member_name: str | None = dst_expr
    if issubclass(elem_type, DataList):
        prefix = f"{dst_expr}."
        member_name = None

    kwargs = {
        "word_bw": word_bw,
        "src_type": "array",
        "source": source_expr,
        "ipos0": 0,
        "iword0": 0,
        "prefix": prefix,
        "member_name": member_name,
    }

    method = getattr(elem_type, "gen_read_recursive", None)
    if callable(method):
        result = method(**kwargs)
    else:
        result = elem_type._gen_read_recursive(**kwargs)

    if isinstance(result, tuple):
        lines = result[0]
    else:
        lines = result

    return [str(line) for line in lines]


def _get_write_recursive_lines(
    elem_type: type[DataSchema],
    word_bw: int,
    src_expr: str,
    target_expr: str,
) -> list[str]:
    prefix = ""
    member_name: str | None = src_expr
    if issubclass(elem_type, DataList):
        prefix = f"{src_expr}."
        member_name = None

    result = elem_type._gen_write_recursive(
        word_bw=word_bw,
        dst_type="array",
        target=target_expr,
        ipos0=0,
        iword0=0,
        prefix=prefix,
        member_name=member_name,
    )
    lines = result[0] if isinstance(result, tuple) else result
    return [str(line) for line in lines]


def _wide_stream_elem_body(
    elem_type: type[DataSchema], bw: int, i3: str, kind: str,
) -> list[str]:
    """Body of a stream-elem helper's wide-element branch (``elem_bw > word_bw``).

    ``DataList`` elements use their generated stream member methods (``read_stream`` etc.);
    every other element (scalar / ``ComplexField``) composes the **recursive serialization**
    over the stream, so a wide element (e.g. ``std::complex<double>`` at word_bw 64) packs in
    streams the same way it does in m_axi arrays -- no assumed member method.
    """
    if issubclass(elem_type, DataList):
        member = {
            "read_stream": f"out[0].template read_stream<{bw}>(s);",
            "read_axi4": f"out[0].template read_axi4_stream<{bw}>(s, tl);",
            "write_stream": f"in[0].template write_stream<{bw}>(s);",
            "write_axi4": f"in[0].template write_axi4_stream<{bw}>(s, tlast);",
        }[kind]
        return [f"{i3}{member}"]

    if kind in ("read_stream", "read_axi4"):
        src_type = "stream" if kind == "read_stream" else "axi4_stream"
        decls = [f"{i3}ap_uint<{bw}> w;"]
        if kind == "read_axi4":
            decls.append(f"{i3}bool last = false;")
        rec = elem_type._gen_read_recursive(
            word_bw=bw, src_type=src_type, source="s", prefix="", member_name="out[0]")[0]
    else:
        dst_type = "stream" if kind == "write_stream" else "axi4_stream"
        decls = [f"{i3}ap_uint<{bw}> w = 0;"]
        if kind == "write_axi4":
            decls.append(f"{i3}(void)tlast;")
        rec = elem_type._gen_write_recursive(
            word_bw=bw, dst_type=dst_type, target="s", prefix="", member_name="in[0]")[0]

    body = list(decls)
    for line in rec:
        stripped = line[4:] if line.startswith("    ") else line
        body.append(f"{i3}{stripped}" if stripped else "")
    return body


def _gen_read_axi4_stream_elem_specializations(
    elem_type: type[DataSchema],
    word_bw_supported: list[int],
    indent_level: int = 0,
) -> str:
    indent = elem_type._get_indent(indent_level)
    i1 = elem_type._get_indent(indent_level + 1)
    i2 = elem_type._get_indent(indent_level + 2)
    i3 = elem_type._get_indent(indent_level + 3)

    elem_bw = elem_type.get_bitwidth()

    lines = [
        "template<int word_bw>",
        f"{indent}struct read_axi4_stream_elem_impl {{",
        f"{i1}static void run(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* out, streamutils::tlast_status& tl, int n) {{",
        f'{i2}static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for read_axi4_stream_elem");',
        f"{i2}(void)s;",
        f"{i2}(void)out;",
        f"{i2}(void)tl;",
        f"{i2}(void)n;",
        f"{i1}}}",
        f"{indent}}};",
    ]

    for bw in word_bw_supported:
        pfv = bw // elem_bw if elem_bw > 0 else 0
        lines.extend([
            "",
            "template<>",
            f"{indent}struct read_axi4_stream_elem_impl<{bw}> {{",
            f"{i1}static void run(hls::stream<streamutils::axi4s_word<{bw}>>& s, value_type* out, streamutils::tlast_status& tl, int n) {{",
            f"{i2}#pragma HLS INLINE",
            f"{i2}tl = streamutils::tlast_status::no_tlast;",
        ])
        if pfv >= 2:
            lines.append(f"{i2}auto axis_word = s.read();")
            lines.append(f"{i2}ap_uint<{bw}> w = axis_word.data;")
            for j in range(pfv):
                lo = j * elem_bw
                hi = lo + elem_bw - 1
                rhs_expr = elem_type.from_uint_expr(f"w.range({hi}, {lo})")
                lines.append(f"{i2}if (n > {j}) {{")
                lines.append(f"{i3}out[{j}] = {rhs_expr};")
                lines.append(f"{i2}}}")
            lines.append(f"{i2}if (axis_word.last) {{")
            lines.append(f"{i3}tl = streamutils::tlast_status::tlast_at_end;")
            lines.append(f"{i2}}}")
        else:
            if elem_bw <= bw:
                lines.append(f"{i2}if (n > 0) {{")
                lines.append(f"{i3}auto axis_word = s.read();")
                lines.append(f"{i3}ap_uint<{bw}> w = axis_word.data;")
                lines.append(f"{i3}out[0] = {elem_type.from_uint_expr('w')};")
                lines.append(f"{i3}if (axis_word.last) {{")
                lines.append(f"{i3}    tl = streamutils::tlast_status::tlast_at_end;")
                lines.append(f"{i3}}}")
                lines.append(f"{i2}}}")
            else:
                lines.append(f"{i2}if (n > 0) {{")
                lines.extend(_wide_stream_elem_body(elem_type, bw, i3, "read_axi4"))
                lines.append(f"{i2}}}")
        lines.extend([
            f"{i1}}}",
            f"{indent}}};",
        ])

    # The public read_axi4_stream_elem wrappers are retired (serialization phase 2b); callers use
    # read_axi4_stream_lane (Phase 1a) or the bulk read_axi4_stream loop, both of which delegate to
    # read_axi4_stream_elem_impl<word_bw>::run above.
    return "\n".join(lines)


def _axi4_to_framed(text: str) -> str:
    """Rename an ``axi4_stream`` array-utils code block into its ``framed_stream`` twin.

    ``streamutils::framed_word<W>`` is field-identical to ``axi4s_word<W>`` (both carry ``.data`` and
    ``.last``); the ONLY differences are the method names, the stream C-type, and the per-beat *writer*
    (``write_axi4_word`` -> the shared ``write_boundary_word<framed_word>``, since a plain ``{data,last}``
    beat has no ``keep``/``strb`` to set).  Generating the axi4 form and renaming keeps ONE source of
    truth for the packing/tlast logic -- the same trick the schema layer uses for its single-value
    ``read_framed_stream`` / ``write_framed_stream`` (see ``DataSchema.gen_read`` / ``gen_write``).
    """
    text = text.replace("read_axi4_stream", "read_framed_stream")
    text = text.replace("write_axi4_stream", "write_framed_stream")
    text = text.replace("streamutils::axi4s_word", "streamutils::framed_word")
    text = re.sub(r"streamutils::write_axi4_word<(\d+)>",
                  r"streamutils::write_boundary_word<streamutils::framed_word<\1>, \1>", text)
    return text


def _gen_read_array_elem_specializations(
    elem_type: type[DataSchema],
    word_bw_supported: list[int],
    indent_level: int = 0,
) -> str:
    indent = elem_type._get_indent(indent_level)
    i1 = elem_type._get_indent(indent_level + 1)
    i2 = elem_type._get_indent(indent_level + 2)
    i3 = elem_type._get_indent(indent_level + 3)
    elem_bw = elem_type.get_bitwidth()

    lines = [
        "template<int word_bw>",
        f"{indent}struct read_array_elem_impl {{",
        f"{i1}static void run(const ap_uint<word_bw>* src, value_type* out, int n) {{",
        f'{i2}static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for read_array_elem");',
        f"{i2}(void)src;",
        f"{i2}(void)out;",
        f"{i2}(void)n;",
        f"{i1}}}",
        f"{indent}}};",
    ]

    for bw in word_bw_supported:
        pfv = bw // elem_bw if elem_bw > 0 else 0
        lines.extend([
            "",
            "template<>",
            f"{indent}struct read_array_elem_impl<{bw}> {{",
        ])
        # run_lane(w, k): unpack lane k (0..pf-1) of one packed word — the SINGLE source of the
        # per-lane packing contract, reused by run() below and by elem_read<W> (the random access).
        if pfv >= 1:
            lines.append(f"{i1}static value_type run_lane(const ap_uint<{bw}>& w, int k) {{")
            lines.append(f"{i2}#pragma HLS INLINE")
            lines.append(f"{i2}switch (k) {{")
            for j in range(pfv):
                lo = j * elem_bw
                hi = lo + elem_bw - 1
                rhs_expr = elem_type.from_uint_expr(f"w.range({hi}, {lo})")
                lines.append(f"{i3}case {j}: return {rhs_expr};")
            lines.append(f"{i2}}}")
            lines.append(f"{i2}return value_type();")
            lines.append(f"{i1}}}")
        lines.extend([
            f"{i1}static void run(const ap_uint<{bw}>* src, value_type* out, int n) {{",
            f"{i2}#pragma HLS INLINE",
        ])
        if pfv >= 2:
            lines.append(f"{i2}if (src == nullptr) {{")
            lines.append(f"{i3}return;")
            lines.append(f"{i2}}}")
            lines.append(f"{i2}ap_uint<{bw}> w = src[0];")
            for j in range(pfv):
                lines.append(f"{i2}if (n > {j}) {{")
                lines.append(f"{i3}out[{j}] = run_lane(w, {j});")
                lines.append(f"{i2}}}")
        else:
            if elem_bw <= bw:
                lines.append(f"{i2}if (n > 0 && src != nullptr) {{")
                lines.append(f"{i3}out[0] = {elem_type.from_uint_expr('src[0]')};")
                lines.append(f"{i2}}}")
            else:
                lines.append(f"{i2}if (n > 0 && src != nullptr) {{")
                recursive_lines = _get_read_recursive_lines(
                    elem_type=elem_type,
                    word_bw=bw,
                    dst_expr="out[0]",
                    source_expr="src",
                )
                for line in recursive_lines:
                    stripped = line[4:] if line.startswith("    ") else line
                    lines.append(f"{i3}{stripped}" if stripped else "")
                lines.append(f"{i2}}}")
        lines.extend([
            f"{i1}}}",
            f"{indent}}};",
        ])

    # The public read_array_elem wrapper is retired (serialization phase 2b); callers use
    # read_array_lane / read_array_slice, which delegate to read_array_elem_impl<word_bw>::run above.
    return "\n".join(lines)


def _gen_write_array_elem_specializations(
    elem_type: type[DataSchema],
    word_bw_supported: list[int],
    indent_level: int = 0,
) -> str:
    indent = elem_type._get_indent(indent_level)
    i1 = elem_type._get_indent(indent_level + 1)
    i2 = elem_type._get_indent(indent_level + 2)
    i3 = elem_type._get_indent(indent_level + 3)
    elem_bw = elem_type.get_bitwidth()

    lines = [
        "template<int word_bw>",
        f"{indent}struct write_array_elem_impl {{",
        f"{i1}static void run(const value_type* in, ap_uint<word_bw>* dst, int n) {{",
        f'{i2}static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for write_array_elem");',
        f"{i2}(void)in;",
        f"{i2}(void)dst;",
        f"{i2}(void)n;",
        f"{i1}}}",
        f"{indent}}};",
    ]

    for bw in word_bw_supported:
        pfv = bw // elem_bw if elem_bw > 0 else 0
        lines.extend([
            "",
            "template<>",
            f"{indent}struct write_array_elem_impl<{bw}> {{",
        ])
        # write_lane(w, k, v): pack value v into lane k (0..pf-1) of a word in place — the SINGLE
        # source of the per-lane packing contract, reused by run() below and by elem_write<W> (the
        # lane read-modify-write random access).
        if pfv >= 1:
            lines.append(f"{i1}static void write_lane(ap_uint<{bw}>& w, int k, const value_type& v) {{")
            lines.append(f"{i2}#pragma HLS INLINE")
            lines.append(f"{i2}switch (k) {{")
            for j in range(pfv):
                lo = j * elem_bw
                hi = lo + elem_bw - 1
                rhs_expr = elem_type.to_uint_value_expr("v")
                lines.append(f"{i3}case {j}: w.range({hi}, {lo}) = {rhs_expr}; break;")
            lines.append(f"{i2}}}")
            lines.append(f"{i1}}}")
        lines.extend([
            f"{i1}static void run(const value_type* in, ap_uint<{bw}>* dst, int n) {{",
            f"{i2}#pragma HLS INLINE",
        ])
        if pfv >= 2:
            lines.append(f"{i2}if (dst == nullptr) {{")
            lines.append(f"{i3}return;")
            lines.append(f"{i2}}}")
            lines.append(f"{i2}ap_uint<{bw}> w = 0;")
            for j in range(pfv):
                lines.append(f"{i2}if (n > {j}) {{")
                lines.append(f"{i3}write_lane(w, {j}, in[{j}]);")
                lines.append(f"{i2}}}")
            lines.append(f"{i2}dst[0] = w;")
        else:
            if elem_bw <= bw:
                lines.append(f"{i2}if (n > 0 && dst != nullptr) {{")
                lines.append(f"{i3}dst[0] = {elem_type.to_uint_value_expr('in[0]')};")
                lines.append(f"{i2}}}")
            else:
                lines.append(f"{i2}if (n > 0 && dst != nullptr) {{")
                recursive_lines = _get_write_recursive_lines(
                    elem_type=elem_type,
                    word_bw=bw,
                    src_expr="in[0]",
                    target_expr="dst",
                )
                for line in recursive_lines:
                    stripped = line[4:] if line.startswith("    ") else line
                    lines.append(f"{i3}{stripped}" if stripped else "")
                lines.append(f"{i2}}}")
        lines.extend([
            f"{i1}}}",
            f"{indent}}};",
        ])

    # The public write_array_elem wrapper is retired (serialization phase 2b); callers use
    # write_array_lane / write_array_slice, which delegate to write_array_elem_impl<word_bw>::run.
    return "\n".join(lines)


def _gen_read_stream_elem_specializations(
    elem_type: type[DataSchema],
    word_bw_supported: list[int],
    indent_level: int = 0,
) -> str:
    indent = elem_type._get_indent(indent_level)
    i1 = elem_type._get_indent(indent_level + 1)
    i2 = elem_type._get_indent(indent_level + 2)
    i3 = elem_type._get_indent(indent_level + 3)
    elem_bw = elem_type.get_bitwidth()

    lines = [
        "template<int word_bw>",
        f"{indent}struct read_stream_elem_impl {{",
        f"{i1}static void run(hls::stream<ap_uint<word_bw>>& s, value_type* out, int n) {{",
        f'{i2}static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for read_stream_elem");',
        f"{i2}(void)s;",
        f"{i2}(void)out;",
        f"{i2}(void)n;",
        f"{i1}}}",
        f"{indent}}};",
    ]

    for bw in word_bw_supported:
        pfv = bw // elem_bw if elem_bw > 0 else 0
        lines.extend([
            "",
            "template<>",
            f"{indent}struct read_stream_elem_impl<{bw}> {{",
            f"{i1}static void run(hls::stream<ap_uint<{bw}>>& s, value_type* out, int n) {{",
            f"{i2}#pragma HLS INLINE",
        ])
        if pfv >= 2:
            lines.append(f"{i2}ap_uint<{bw}> w = s.read();")
            for j in range(pfv):
                lo = j * elem_bw
                hi = lo + elem_bw - 1
                rhs_expr = elem_type.from_uint_expr(f"w.range({hi}, {lo})")
                lines.append(f"{i2}if (n > {j}) {{")
                lines.append(f"{i3}out[{j}] = {rhs_expr};")
                lines.append(f"{i2}}}")
        else:
            if elem_bw <= bw:
                lines.append(f"{i2}if (n > 0) {{")
                lines.append(f"{i3}ap_uint<{bw}> w = s.read();")
                lines.append(f"{i3}out[0] = {elem_type.from_uint_expr('w')};")
                lines.append(f"{i2}}}")
            else:
                lines.append(f"{i2}if (n > 0) {{")
                lines.extend(_wide_stream_elem_body(elem_type, bw, i3, "read_stream"))
                lines.append(f"{i2}}}")
        lines.extend([
            f"{i1}}}",
            f"{indent}}};",
        ])

    # The public read_stream_elem wrapper is retired (serialization phase 2b); callers use
    # read_stream_lane or the bulk read_stream loop, which delegate to read_stream_elem_impl::run.
    return "\n".join(lines)


def _gen_write_stream_elem_specializations(
    elem_type: type[DataSchema],
    word_bw_supported: list[int],
    indent_level: int = 0,
) -> str:
    indent = elem_type._get_indent(indent_level)
    i1 = elem_type._get_indent(indent_level + 1)
    i2 = elem_type._get_indent(indent_level + 2)
    i3 = elem_type._get_indent(indent_level + 3)
    elem_bw = elem_type.get_bitwidth()

    lines = [
        "template<int word_bw>",
        f"{indent}struct write_stream_elem_impl {{",
        f"{i1}static void run(hls::stream<ap_uint<word_bw>>& s, const value_type* in, int n) {{",
        f'{i2}static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for write_stream_elem");',
        f"{i2}(void)s;",
        f"{i2}(void)in;",
        f"{i2}(void)n;",
        f"{i1}}}",
        f"{indent}}};",
    ]

    for bw in word_bw_supported:
        pfv = bw // elem_bw if elem_bw > 0 else 0
        lines.extend([
            "",
            "template<>",
            f"{indent}struct write_stream_elem_impl<{bw}> {{",
            f"{i1}static void run(hls::stream<ap_uint<{bw}>>& s, const value_type* in, int n) {{",
            f"{i2}#pragma HLS INLINE",
        ])
        if pfv >= 2:
            lines.append(f"{i2}ap_uint<{bw}> w = 0;")
            for j in range(pfv):
                lo = j * elem_bw
                hi = lo + elem_bw - 1
                rhs_expr = elem_type.to_uint_value_expr(f"in[{j}]")
                lines.append(f"{i2}if (n > {j}) {{")
                lines.append(f"{i3}w.range({hi}, {lo}) = {rhs_expr};")
                lines.append(f"{i2}}}")
            lines.append(f"{i2}s.write(w);")
        else:
            if elem_bw <= bw:
                lines.append(f"{i2}if (n > 0) {{")
                lines.append(f"{i3}ap_uint<{bw}> w = {elem_type.to_uint_value_expr('in[0]')};")
                lines.append(f"{i3}s.write(w);")
                lines.append(f"{i2}}}")
            else:
                lines.append(f"{i2}if (n > 0) {{")
                lines.extend(_wide_stream_elem_body(elem_type, bw, i3, "write_stream"))
                lines.append(f"{i2}}}")
        lines.extend([
            f"{i1}}}",
            f"{indent}}};",
        ])

    # The public write_stream_elem wrapper is retired (serialization phase 2b); callers use
    # write_stream_lane or the bulk write_stream loop, which delegate to write_stream_elem_impl::run.
    return "\n".join(lines)


def _gen_write_axi4_stream_elem_specializations(
    elem_type: type[DataSchema],
    word_bw_supported: list[int],
    indent_level: int = 0,
) -> str:
    indent = elem_type._get_indent(indent_level)
    i1 = elem_type._get_indent(indent_level + 1)
    i2 = elem_type._get_indent(indent_level + 2)
    i3 = elem_type._get_indent(indent_level + 3)
    elem_bw = elem_type.get_bitwidth()

    lines = [
        "template<int word_bw>",
        f"{indent}struct write_axi4_stream_elem_impl {{",
        f"{i1}static void run(hls::stream<streamutils::axi4s_word<word_bw>>& s, const value_type* in, bool tlast, int n) {{",
        f'{i2}static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for write_axi4_stream_elem");',
        f"{i2}(void)s;",
        f"{i2}(void)in;",
        f"{i2}(void)tlast;",
        f"{i2}(void)n;",
        f"{i1}}}",
        f"{indent}}};",
    ]

    for bw in word_bw_supported:
        pfv = bw // elem_bw if elem_bw > 0 else 0
        lines.extend([
            "",
            "template<>",
            f"{indent}struct write_axi4_stream_elem_impl<{bw}> {{",
            f"{i1}static void run(hls::stream<streamutils::axi4s_word<{bw}>>& s, const value_type* in, bool tlast, int n) {{",
            f"{i2}#pragma HLS INLINE",
        ])
        if pfv >= 2:
            lines.append(f"{i2}ap_uint<{bw}> w = 0;")
            for j in range(pfv):
                lo = j * elem_bw
                hi = lo + elem_bw - 1
                rhs_expr = elem_type.to_uint_value_expr(f"in[{j}]")
                lines.append(f"{i2}if (n > {j}) {{")
                lines.append(f"{i3}w.range({hi}, {lo}) = {rhs_expr};")
                lines.append(f"{i2}}}")
            lines.append(f"{i2}streamutils::write_axi4_word<{bw}>(s, w, tlast);")
        else:
            if elem_bw <= bw:
                lines.append(f"{i2}if (n > 0) {{")
                lines.append(f"{i3}ap_uint<{bw}> w = {elem_type.to_uint_value_expr('in[0]')};")
                lines.append(f"{i3}streamutils::write_axi4_word<{bw}>(s, w, tlast);")
                lines.append(f"{i2}}}")
            else:
                lines.append(f"{i2}if (n > 0) {{")
                lines.extend(_wide_stream_elem_body(elem_type, bw, i3, "write_axi4"))
                lines.append(f"{i2}}}")
        lines.extend([
            f"{i1}}}",
            f"{indent}}};",
        ])

    # The public write_axi4_stream_elem wrapper is retired (serialization phase 2b); callers use
    # write_axi4_stream_lane or the bulk write_axi4_stream loop, both delegating to
    # write_axi4_stream_elem_impl<word_bw>::run above.
    return "\n".join(lines)


def _gen_lane_helpers(
    elem_type: type[DataSchema],
    indent_level: int = 0,
) -> str:
    """Regime-agnostic lane methods (Phase 1a) over all three interfaces.

    Each call moves the next ``LW = lane_capacity<W>() = max(1, pf)`` elements:

    - ``pf >= 1`` (``LW = pf``): one word/beat -> ``pf`` lanes; ``n`` (``1 <= n <= LW``) is the
      valid count (``pf`` for a full word, fewer only for the final partial one).
    - ``pf == 0`` (``LW = 1``, wide element): one element spanning ``ceil(elem/W)`` words/beats
      -> ``dst[0]``; ``n`` is ignored (always 1).

    Both regimes are already implemented by the corresponding ``*_elem_impl<W>::run`` structs (the
    ``pf >= 1`` lane logic and the ``pf == 0`` multi-word assembly the bulk ``read_array`` also
    does), so the lane methods delegate to them, passing ``pf >= 1 ? n : 1`` so the wide-element
    call always moves its single element regardless of the supplied ``n``.  (They call the
    ``*_impl::run`` structs, not the ``*_elem`` wrappers: the wrappers declare a
    ``value_type out[pf<W>()]`` buffer, which is an illegal zero-length array for ``pf == 0``,
    whereas ``run`` takes a plain ``value_type*``.)  Memory methods act at the given word pointer
    and do not advance -- the caller advances by ``get_nwords<W>(LW)`` words.
    """
    indent = elem_type._get_indent(indent_level)
    i1 = elem_type._get_indent(indent_level + 1)
    i2 = elem_type._get_indent(indent_level + 2)
    lc = "lane_capacity<word_bw>()"
    sel = "pf<word_bw>() >= 1 ? n : 1"
    axis = "hls::stream<streamutils::axi4s_word<word_bw>>&"

    return "\n".join([
        "// --- lane methods (Phase 1a): move LW = lane_capacity<W>() = max(1, pf) elements ---",
        "// dst is a buffer of length LW; pf >= 1 -> n valid lanes of one word/beat, pf == 0 ->",
        "// one wide element across ceil(elem/W) words/beats (n ignored).  Memory: the caller",
        "// advances the word pointer by get_nwords<W>(LW); streams self-sequence.",
        "",
        "template<int word_bw>",
        f"{indent}inline void read_array_lane(const ap_uint<word_bw>* src, value_type dst[{lc}], int n = {lc}) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}read_array_elem_impl<word_bw>::run(src, dst, {sel});",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void write_array_lane(const value_type src[{lc}], ap_uint<word_bw>* dst, int n = {lc}) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}write_array_elem_impl<word_bw>::run(src, dst, {sel});",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void read_stream_lane(hls::stream<ap_uint<word_bw>>& s, value_type dst[{lc}], int n = {lc}) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}read_stream_elem_impl<word_bw>::run(s, dst, {sel});",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void write_stream_lane(const value_type src[{lc}], hls::stream<ap_uint<word_bw>>& s, int n = {lc}) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}write_stream_elem_impl<word_bw>::run(s, src, {sel});",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void read_axi4_stream_lane({axis} s, value_type dst[{lc}], int n, streamutils::tlast_status& tl) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}read_axi4_stream_elem_impl<word_bw>::run(s, dst, tl, {sel});",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void read_axi4_stream_lane({axis} s, value_type dst[{lc}], int n = {lc}) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;",
        f"{i1}read_axi4_stream_lane<word_bw>(s, dst, n, tl);",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void write_axi4_stream_lane(const value_type src[{lc}], {axis} s, bool tlast = false, int n = {lc}) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}write_axi4_stream_elem_impl<word_bw>::run(s, src, tlast, {sel});",
        f"{indent}}}",
        "",
        "// --- framed_word lane methods: the same LW-element move over an internal framed_word{data,last}",
        "// beat (no keep/strb sidebands), for composite-internal edges.  Delegates to the framed impls",
        "// below, generated by renaming the axi4 impls (framed_word is field-identical to axi4s_word).",
        "template<int word_bw>",
        f"{indent}inline void read_framed_stream_lane(hls::stream<streamutils::framed_word<word_bw>>& s, value_type dst[{lc}], int n, streamutils::tlast_status& tl) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}read_framed_stream_elem_impl<word_bw>::run(s, dst, tl, {sel});",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void read_framed_stream_lane(hls::stream<streamutils::framed_word<word_bw>>& s, value_type dst[{lc}], int n = {lc}) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;",
        f"{i1}read_framed_stream_lane<word_bw>(s, dst, n, tl);",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void write_framed_stream_lane(const value_type src[{lc}], hls::stream<streamutils::framed_word<word_bw>>& s, bool tlast = false, int n = {lc}) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}write_framed_stream_elem_impl<word_bw>::run(s, src, tlast, {sel});",
        f"{indent}}}",
        "",
        "// --- element random access (Phase 3): read/write ONE packed element by index i ---",
        "// iw = i / LW, k = i % LW  (LW = lane_capacity<W>(), compile-time; a power-of-two LW is a",
        "// shift/mask).  Reuses the shared run_lane / write_lane (the single packing-contract source)",
        "// -- the word-granular random-access gather/scatter primitive Phase 4's Gather consumes.",
        "// Requires pf >= 1 (element fits in one word); a wide element (pf == 0) is not supported.",
        "template<int word_bw>",
        f"{indent}inline value_type elem_read(const ap_uint<word_bw>* src, int i) {{",
        f"{i1}#pragma HLS INLINE",
        f'{i1}static_assert(pf<word_bw>() >= 1, "elem_read requires pf>=1 (element fits in one word)");',
        f"{i1}return read_array_elem_impl<word_bw>::run_lane(src[i / lane_capacity<word_bw>()], "
        "i % lane_capacity<word_bw>());",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void elem_write(const value_type& v, ap_uint<word_bw>* dst, int i) {{",
        f"{i1}#pragma HLS INLINE",
        f'{i1}static_assert(pf<word_bw>() >= 1, "elem_write requires pf>=1 (element fits in one word)");',
        f"{i1}// Specialization: when pf == 1 (element fills the whole word), skip RMW and write directly.",
        f"{i1}// When pf > 1 (multiple lanes per word), fall back to lane read-modify-write.",
        f"{i1}if constexpr (pf<word_bw>() == 1) {{",
        f"{i2}// Fast path: element == word width. Direct write, no RMW.",
        f"{i2}dst[i] = {elem_type.to_uint_value_expr('v')};",
        f"{i1}}} else {{",
        f"{i2}// Slow path: multiple lanes per word. RMW to update one lane.",
        f"{i2}const int iw = i / lane_capacity<word_bw>();",
        f"{i2}ap_uint<word_bw> w = dst[iw];",
        f"{i2}write_array_elem_impl<word_bw>::write_lane(w, i % lane_capacity<word_bw>(), v);",
        f"{i2}dst[iw] = w;",
        f"{i1}}}",
        f"{indent}}}",
    ])


def _gen_slice_helpers(
    elem_type: type[DataSchema],
    indent_level: int = 0,
) -> str:
    """Element-indexed range methods (Phase 1b) over memory, built on the 1a lane methods.

    ``read_array_slice<W>(words, i0, i1, out)`` reads elements ``[i0, i1)`` into ``out[0 ..
    i1-i0)``; ``write_array_slice<W>(in, words, i0, i1)`` is the inverse.  The caller works in
    **element coordinates** -- no ``i0/PF`` in the kernel.

    Layout is word-aligned (matching the Python golden): element ``i`` is lane ``i % LW`` of group
    ``i / LW``, where ``LW = lane_capacity<W>()`` and a group spans ``WPU = get_nwords<W>(LW)``
    words -- ``1`` word holding ``pf`` lanes when ``pf >= 1``, or ``ceil(elem/W)`` words for one
    wide element when ``pf == 0``.  The walk keeps a running word pointer (advanced by ``WPU`` per
    group) and a per-group lane index, so there is no per-element divide; ``i0`` is located once
    with the compile-time-constant ``LW`` (a multiply/shift, not a hardware divider), correct for
    any ``pf`` -- non-powers-of-two and ``pf == 0`` included.

    For ``LW == 1`` (one element per ``WPU`` contiguous words -- the scalar ``pf == 1`` and the
    wide-element ``pf == 0`` regimes) no group is ever partial, so both directions emit a flat,
    fixed-trip affine loop that the HLS burst/dependence analyzer lowers to a **single fixed-length
    burst**.  The ``LW > 1`` (vectorized, multiple lanes per word) regime peels the read into a
    pipelined flat-affine middle burst plus a fixed-trip head/tail lane extract.

    ``write_array_slice`` is **pure-write** in every regime: it writes whole words covering
    ``[i0, i1)`` and clobbers the partial tail lane past ``i1`` -- safe because Waveflow arrays are
    **word-granular** (``MemMgr`` alloc rounds to whole words, so that lane is nobody's data).  It
    reads no memory, so the burst analyzer emits a single write burst *and* a load/compute/store
    DATAFLOW keeps its ping-pong overlap (an RMW read on a shared port makes Vitis serialize
    ``load(j+1)`` behind ``store(j)``).  Pure-write **requires ``i0`` word-aligned** (an unaligned
    head shares its word with the array's own earlier elements); whole-array and ``[0, n)`` writes --
    essentially every real call -- satisfy that.  For the rare unaligned / word-shared sub-range,
    ``write_array_slice_rmw`` boundary-peels a read-modify-write (aligned middle stays a pure-write
    burst; the ``<= 1`` partial head/tail group is RMW to preserve out-of-range neighbors).
    A statically-sized whole-array overload (range ``[0, N)``) deduces ``N``.
    """
    indent = elem_type._get_indent(indent_level)
    i1 = elem_type._get_indent(indent_level + 1)
    i2 = elem_type._get_indent(indent_level + 2)
    i3 = elem_type._get_indent(indent_level + 3)
    i4 = elem_type._get_indent(indent_level + 4)

    return "\n".join([
        "// --- range methods (Phase 1b): element-indexed [i0, i1) over memory, on the lane methods ---",
        "// Word-aligned layout: element i is lane (i % LW) of group (i / LW); a group is one word",
        "// (pf >= 1) or ceil(elem/W) words (pf == 0, one wide element). Walk groups with a running",
        "// word pointer (advance WPU = get_nwords<W>(LW) per group) + a per-group lane index -- no",
        "// per-element divide. Element coordinates throughout: the caller never computes i0/PF.",
        "",
        "// Regime tag: lane_capacity<W>() selects the slice form by overload resolution (the",
        "// word_bw_tag idiom, keyed on LW) -- no if constexpr. slice_lane_tag<1> (scalar pf==1 or",
        "// wide-element pf==0: one element per WPU contiguous words) takes the flat fixed-trip affine",
        "// path the burst analyzer lowers to a single fixed-length burst; the generic",
        "// slice_lane_tag<lw> (vectorized, lw lanes per word) keeps the group walk.",
        "template<int lw>",
        f"{indent}struct slice_lane_tag {{}};",
        "",
        "// read, generic (LW > 1): boundary-peel. The aligned middle groups are a pipelined flat-",
        "// affine burst read (one packed word per iteration, the burst analyzer streams it at one",
        "// word/cycle); the <= 1 partial head/tail group extracts only its in-range lanes with a",
        "// fixed LW-trip unrolled predicate.  (A single variable-trip group walk left the middle",
        "// read unpipelined -- each wide read paid full memory latency and the DATAFLOW stage stalled;",
        "// runtime-bounded head/tail loops also synthesize a 2^30 worst-case trip that poisons the",
        "// enclosing DATAFLOW interval.)",
        "template<int word_bw, int lw>",
        f"{indent}inline void read_array_slice_dispatch(slice_lane_tag<lw>, const ap_uint<word_bw>* words, int i0, int i1, value_type* out) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}constexpr int LW = lane_capacity<word_bw>();",
        f"{i1}constexpr int WPU = get_nwords<word_bw>(LW);",
        f"{i1}const int a0 = ((i0 + LW - 1) / LW) * LW;       // first fully-covered group base (>= i0)",
        f"{i1}const int a1 = (i1 / LW) * LW;                  // end of fully-covered groups (<= i1)",
        f"{i1}// head partial group [i0, min(a0, i1)): keep only in-range lanes.",
        f"{i1}if (a0 > i0) {{",
        f"{i2}const int gb = (i0 / LW) * LW;",
        f"{i2}value_type lane[LW];",
        f"{i2}read_array_lane<word_bw>(words + (gb / LW) * WPU, lane, LW);",
        f"{i2}for (int l = 0; l < LW; ++l) {{",
        f"{i3}#pragma HLS UNROLL",
        f"{i3}const int e = gb + l;",
        f"{i3}if (e >= i0 && e < i1) out[e - i0] = lane[l];",
        f"{i2}}}",
        f"{i1}}}",
        f"{i1}// aligned middle groups [a0, a1): pipelined flat-affine burst read.",
        f"{i1}if (a1 > a0) {{",
        f"{i2}const ap_uint<word_bw>* wp = words + (a0 / LW) * WPU;",
        f"{i2}const int ng = (a1 - a0) / LW;",
        f"{i2}const int obase = a0 - i0;",
        f"{i2}for (int g = 0; g < ng; ++g) {{",
        f"{i3}#pragma HLS PIPELINE II=1",
        f"{i3}value_type lane[LW];",
        f"{i3}read_array_lane<word_bw>(wp + g * WPU, lane, LW);",
        f"{i3}for (int l = 0; l < LW; ++l) {{",
        f"{i4}#pragma HLS UNROLL",
        f"{i4}out[obase + g * LW + l] = lane[l];",
        f"{i3}}}",
        f"{i2}}}",
        f"{i1}}}",
        f"{i1}// tail partial group [a1, i1): keep only in-range lanes (only when a1 >= i0).",
        f"{i1}if (a1 < i1 && a1 >= i0) {{",
        f"{i2}value_type lane[LW];",
        f"{i2}read_array_lane<word_bw>(words + (a1 / LW) * WPU, lane, LW);",
        f"{i2}for (int l = 0; l < LW; ++l) {{",
        f"{i3}#pragma HLS UNROLL",
        f"{i3}if (a1 + l < i1) out[a1 + l - i0] = lane[l];",
        f"{i2}}}",
        f"{i1}}}",
        f"{indent}}}",
        "",
        "// read, scalar (LW == 1): flat fixed-trip affine burst (one element per WPU contiguous words).",
        "template<int word_bw>",
        f"{indent}inline void read_array_slice_dispatch(slice_lane_tag<1>, const ap_uint<word_bw>* words, int i0, int i1, value_type* out) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}constexpr int WPU = get_nwords<word_bw>(1);",
        f"{i1}const ap_uint<word_bw>* wp = words + i0 * WPU;",
        f"{i1}const int n = i1 - i0;",
        f"{i1}for (int e = 0; e < n; ++e) {{",
        f"{i2}#pragma HLS PIPELINE II=1",
        f"{i2}read_array_lane<word_bw>(wp + e * WPU, out + e, 1);",
        f"{i1}}}",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void read_array_slice(const ap_uint<word_bw>* words, int i0, int i1, value_type* out) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}if (words == nullptr || out == nullptr || i1 <= i0) {{",
        f"{i2}return;",
        f"{i1}}}",
        f"{i1}read_array_slice_dispatch(slice_lane_tag<lane_capacity<word_bw>()>{{}}, words, i0, i1, out);",
        f"{indent}}}",
        "",
        "// write, generic (LW > 1): pure-write. Writes whole words covering [i0, i1); the partial tail",
        "// lane past i1 (up to the word boundary) is clobbered -- safe because Waveflow arrays are word-",
        "// granular (MemMgr alloc rounds to whole words, so that lane is nobody's data). No RMW read on",
        "// the memory bundle: the burst analyzer emits a single write burst, AND -- because the store",
        "// never reads memory -- a load/compute/store DATAFLOW keeps its ping-pong overlap (an RMW read",
        "// makes Vitis serialize load(j+1) behind store(j) on a shared port). REQUIRES i0 word-aligned;",
        "// an unaligned head shares its word with the array's own earlier elements -- use",
        "// write_array_slice_rmw for an unaligned / word-shared sub-range.",
        "template<int word_bw, int lw>",
        f"{indent}inline void write_array_slice_dispatch(slice_lane_tag<lw>, const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}constexpr int LW = lane_capacity<word_bw>();",
        f"{i1}constexpr int WPU = get_nwords<word_bw>(LW);",
        f"{i1}ap_uint<word_bw>* wp = words + (i0 / LW) * WPU;   // i0 word-aligned -> exact group base",
        f"{i1}const int n = i1 - i0;",
        f"{i1}const int ng = (n + LW - 1) / LW;                 // whole words to cover n (tail padded)",
        f"{i1}for (int g = 0; g < ng; ++g) {{",
        f"{i2}#pragma HLS PIPELINE II=1",
        f"{i2}value_type lane[LW];",
        f"{i2}for (int l = 0; l < LW; ++l) {{",
        f"{i3}#pragma HLS UNROLL",
        f"{i3}const int e = g * LW + l;",
        f"{i3}const int idx = (e < n) ? e : 0;             // in-bounds index (value muxed below)",
        f"{i3}lane[l] = (e < n) ? in[idx] : value_type();  // zero-pad the tail lane (matches golden)",
        f"{i2}}}",
        f"{i2}write_array_lane<word_bw>(lane, wp + g * WPU, LW);",
        f"{i1}}}",
        f"{indent}}}",
        "",
        "// write, scalar (LW == 1): pure-write flat affine burst -- no RMW read on the gmem bundle",
        "// (the unconditional RMW read is what forced the write to II=16).",
        "template<int word_bw>",
        f"{indent}inline void write_array_slice_dispatch(slice_lane_tag<1>, const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}constexpr int WPU = get_nwords<word_bw>(1);",
        f"{i1}ap_uint<word_bw>* wp = words + i0 * WPU;",
        f"{i1}const int n = i1 - i0;",
        f"{i1}for (int e = 0; e < n; ++e) {{",
        f"{i2}#pragma HLS PIPELINE II=1",
        f"{i2}write_array_lane<word_bw>(in + e, wp + e * WPU, 1);",
        f"{i1}}}",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void write_array_slice(const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}if (words == nullptr || in == nullptr || i1 <= i0) {{",
        f"{i2}return;",
        f"{i1}}}",
        f"{i1}write_array_slice_dispatch(slice_lane_tag<lane_capacity<word_bw>()>{{}}, in, words, i0, i1);",
        f"{indent}}}",
        "",
        "// write RMW, generic (LW > 1): boundary-peel read-modify-write for the RARE case of writing a",
        "// sub-range whose boundary word is shared with data to preserve (an unaligned i0, or",
        "// interleaved partial writes into one array). Prefer write_array_slice (pure-write) for word-",
        "// granular arrays -- the RMW read here makes a load/compute/store DATAFLOW serialize on a",
        "// shared memory port. Aligned middle groups are a read-free pure-write burst; the <=1 partial",
        "// head/tail group is RMW with a fixed LW-trip unrolled predicate (a runtime-bounded loop would",
        "// synthesize a 2^30 worst-case trip).",
        "template<int word_bw, int lw>",
        f"{indent}inline void write_array_slice_rmw_dispatch(slice_lane_tag<lw>, const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}constexpr int LW = lane_capacity<word_bw>();",
        f"{i1}constexpr int WPU = get_nwords<word_bw>(LW);",
        f"{i1}const int a0 = ((i0 + LW - 1) / LW) * LW;       // first fully-covered group base (>= i0)",
        f"{i1}const int a1 = (i1 / LW) * LW;                  // end of fully-covered groups (<= i1)",
        f"{i1}// head partial group [i0, min(a0, i1)): RMW, writing only in-range lanes.",
        f"{i1}if (a0 > i0) {{",
        f"{i2}const int gb = (i0 / LW) * LW;",
        f"{i2}ap_uint<word_bw>* wp = words + (gb / LW) * WPU;",
        f"{i2}value_type lane[LW];",
        f"{i2}read_array_lane<word_bw>(wp, lane, LW);",
        f"{i2}for (int l = 0; l < LW; ++l) {{",
        f"{i3}#pragma HLS UNROLL",
        f"{i3}const int e = gb + l;",
        f"{i3}if (e >= i0 && e < i1) lane[l] = in[e - i0];",
        f"{i2}}}",
        f"{i2}write_array_lane<word_bw>(lane, wp, LW);",
        f"{i1}}}",
        f"{i1}// aligned middle groups [a0, a1): read-free pure-write burst (one packed word each).",
        f"{i1}if (a1 > a0) {{",
        f"{i2}ap_uint<word_bw>* wp = words + (a0 / LW) * WPU;",
        f"{i2}const int ng = (a1 - a0) / LW;",
        f"{i2}const int obase = a0 - i0;",
        f"{i2}for (int g = 0; g < ng; ++g) {{",
        f"{i3}#pragma HLS PIPELINE II=1",
        f"{i3}value_type lane[LW];",
        f"{i3}for (int l = 0; l < LW; ++l) {{",
        f"{i4}#pragma HLS UNROLL",
        f"{i4}lane[l] = in[obase + g * LW + l];",
        f"{i3}}}",
        f"{i3}write_array_lane<word_bw>(lane, wp + g * WPU, LW);",
        f"{i2}}}",
        f"{i1}}}",
        f"{i1}// tail partial group [a1, i1): RMW, writing only in-range lanes. Only when a1 is an",
        f"{i1}// interior boundary the head didn't cover (a1 >= i0).",
        f"{i1}if (a1 < i1 && a1 >= i0) {{",
        f"{i2}ap_uint<word_bw>* wp = words + (a1 / LW) * WPU;",
        f"{i2}value_type lane[LW];",
        f"{i2}read_array_lane<word_bw>(wp, lane, LW);",
        f"{i2}for (int l = 0; l < LW; ++l) {{",
        f"{i3}#pragma HLS UNROLL",
        f"{i3}if (a1 + l < i1) lane[l] = in[a1 + l - i0];",
        f"{i2}}}",
        f"{i2}write_array_lane<word_bw>(lane, wp, LW);",
        f"{i1}}}",
        f"{indent}}}",
        "",
        "// write RMW, scalar (LW == 1): no partial words exist (one element per WPU words), so RMW",
        "// reduces to the pure-write flat affine burst.",
        "template<int word_bw>",
        f"{indent}inline void write_array_slice_rmw_dispatch(slice_lane_tag<1>, const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}constexpr int WPU = get_nwords<word_bw>(1);",
        f"{i1}ap_uint<word_bw>* wp = words + i0 * WPU;",
        f"{i1}const int n = i1 - i0;",
        f"{i1}for (int e = 0; e < n; ++e) {{",
        f"{i2}#pragma HLS PIPELINE II=1",
        f"{i2}write_array_lane<word_bw>(in + e, wp + e * WPU, 1);",
        f"{i1}}}",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void write_array_slice_rmw(const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}if (words == nullptr || in == nullptr || i1 <= i0) {{",
        f"{i2}return;",
        f"{i1}}}",
        f"{i1}write_array_slice_rmw_dispatch(slice_lane_tag<lane_capacity<word_bw>()>{{}}, in, words, i0, i1);",
        f"{indent}}}",
        "",
        "// Whole-array overloads (range [0, N)); N is deduced from the statically-sized buffer.",
        "template<int word_bw, int N>",
        f"{indent}inline void read_array_slice(const ap_uint<word_bw>* words, value_type (&out)[N]) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}read_array_slice<word_bw>(words, 0, N, out);",
        f"{indent}}}",
        "",
        "template<int word_bw, int N>",
        f"{indent}inline void write_array_slice(const value_type (&in)[N], ap_uint<word_bw>* words) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}write_array_slice<word_bw>(in, words, 0, N);",
        f"{indent}}}",
    ])


def _gen_stream_elem_helpers(
    elem_type: type[DataSchema],
    word_bw_supported: list[int],
    indent_level: int = 0,
) -> str:
    indent = elem_type._get_indent(indent_level)
    i1 = elem_type._get_indent(indent_level + 1)
    i2 = elem_type._get_indent(indent_level + 2)
    i3 = elem_type._get_indent(indent_level + 3)
    elem_bw = elem_type.get_bitwidth()

    lines = [
        "template<int word_bw>",
        f"{indent}static constexpr int pf() {{",
        f"{i1}return word_bw / {elem_bw};",
        f"{indent}}}",
        "",
        "// lane_capacity = max(1, pf): the lane-buffer size and loop step (call it LW). It is pf",
        "// in the vectorized regime (pf >= 1) and 1 in the wide-element regime (pf == 0).",
        "template<int word_bw>",
        f"{indent}static constexpr int lane_capacity() {{",
        f"{i1}return pf<word_bw>() >= 1 ? pf<word_bw>() : 1;",
        f"{indent}}}",
        "",
        _gen_read_array_elem_specializations(
            elem_type=elem_type,
            word_bw_supported=word_bw_supported,
            indent_level=indent_level,
        ),
        "",
        _gen_write_array_elem_specializations(
            elem_type=elem_type,
            word_bw_supported=word_bw_supported,
            indent_level=indent_level,
        ),
        "",
        _gen_read_stream_elem_specializations(
            elem_type=elem_type,
            word_bw_supported=word_bw_supported,
            indent_level=indent_level,
        ),
        "",
        _gen_read_axi4_stream_elem_specializations(
            elem_type=elem_type,
            word_bw_supported=word_bw_supported,
            indent_level=indent_level,
        ),
        "",
        _gen_write_stream_elem_specializations(
            elem_type=elem_type,
            word_bw_supported=word_bw_supported,
            indent_level=indent_level,
        ),
        "",
        _gen_write_axi4_stream_elem_specializations(
            elem_type=elem_type,
            word_bw_supported=word_bw_supported,
            indent_level=indent_level,
        ),
        "",
        # framed_word impls (internal composite edges): the axi4 impls renamed -- one source of truth.
        _axi4_to_framed(_gen_read_axi4_stream_elem_specializations(
            elem_type=elem_type, word_bw_supported=word_bw_supported, indent_level=indent_level)),
        "",
        _axi4_to_framed(_gen_write_axi4_stream_elem_specializations(
            elem_type=elem_type, word_bw_supported=word_bw_supported, indent_level=indent_level)),
        "",
        _gen_lane_helpers(elem_type=elem_type, indent_level=indent_level),
        "",
        _gen_slice_helpers(elem_type=elem_type, indent_level=indent_level),
        "",
        "template<int word_bw>",
        f"{indent}inline void read_stream(hls::stream<ap_uint<word_bw>>& s, value_type* dst, int len) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}if (dst == nullptr || len <= 0) {{",
        f"{i2}return;",
        f"{i1}}}",
        f"{i1}for (int i = 0; i < len; i += pf<word_bw>()) {{",
        f"{i2}read_stream_elem_impl<word_bw>::run(s, dst + i, len - i);",
        f"{i1}}}",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, streamutils::tlast_status& tl, int& nread, int len) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}tl = streamutils::tlast_status::no_tlast;",
        f"{i1}nread = 0;",
        f"{i1}if (dst == nullptr || len <= 0) {{",
        f"{i2}return;",
        f"{i1}}}",
        f"{i1}bool stop = false;",
        f"{i1}for (int i = 0; i < len && !stop; i += pf<word_bw>()) {{",
        f"{i2}streamutils::tlast_status lane_tl = streamutils::tlast_status::no_tlast;",
        f"{i2}const int lane_count = ((len - i) < pf<word_bw>()) ? (len - i) : pf<word_bw>();",
        f"{i2}read_axi4_stream_elem_impl<word_bw>::run(s, dst + i, lane_tl, len - i);",
        f"{i2}if (lane_tl == streamutils::tlast_status::tlast_early) {{",
        f"{i3}tl = lane_tl;",
        f"{i3}stop = true;",
        f"{i2}}}",
        f"{i2}if (lane_tl != streamutils::tlast_status::tlast_early) {{",
        f"{i3}nread += lane_count;",
        f"{i2}}}",
        f"{i2}if (lane_tl == streamutils::tlast_status::tlast_at_end) {{",
        f"{i3}tl = (i + pf<word_bw>() >= len) ? streamutils::tlast_status::tlast_at_end : streamutils::tlast_status::tlast_early;",
        f"{i3}stop = true;",
        f"{i2}}}",
        f"{i1}}}",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, streamutils::tlast_status& tl, int len) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}int nread = 0;",
        f"{i1}read_axi4_stream<word_bw>(s, dst, tl, nread, len);",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, int& nread, int len) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;",
        f"{i1}read_axi4_stream<word_bw>(s, dst, tl, nread, len);",
        f"{i1}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, int len) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;",
        f"{i1}int nread = 0;",
        f"{i1}read_axi4_stream<word_bw>(s, dst, tl, nread, len);",
        f"{i1}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void write_stream(hls::stream<ap_uint<word_bw>>& s, const value_type* src, int len) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}if (src == nullptr || len <= 0) {{",
        f"{i2}return;",
        f"{i1}}}",
        f"{i1}for (int i = 0; i < len; i += pf<word_bw>()) {{",
        f"{i2}write_stream_elem_impl<word_bw>::run(s, src + i, len - i);",
        f"{i1}}}",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, const value_type* src, bool tlast = true, int len = pf<word_bw>()) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}if (src == nullptr || len <= 0) {{",
        f"{i2}return;",
        f"{i1}}}",
        f"{i1}for (int i = 0; i < len; i += pf<word_bw>()) {{",
        f"{i2}const bool lane_tlast = (i + pf<word_bw>() >= len) ? tlast : false;",
        f"{i2}write_axi4_stream_elem_impl<word_bw>::run(s, src + i, lane_tlast, len - i);",
        f"{i1}}}",
        f"{indent}}}",
        "",
        "// --- framed_word bulk: LEN elements over an internal framed_word stream (length known; the",
        "// consumer knows how many to read, so no tlast early-stop -- tl just captures the final beat).",
        "template<int word_bw>",
        f"{indent}inline void read_framed_stream(hls::stream<streamutils::framed_word<word_bw>>& s, value_type* dst, streamutils::tlast_status& tl, int len) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}tl = streamutils::tlast_status::no_tlast;",
        f"{i1}if (dst == nullptr || len <= 0) {{",
        f"{i2}return;",
        f"{i1}}}",
        f"{i1}for (int i = 0; i < len; i += pf<word_bw>()) {{",
        f"{i2}streamutils::tlast_status lane_tl = streamutils::tlast_status::no_tlast;",
        f"{i2}read_framed_stream_elem_impl<word_bw>::run(s, dst + i, lane_tl, len - i);",
        f"{i2}if (lane_tl == streamutils::tlast_status::tlast_at_end) {{",
        f"{i3}tl = lane_tl;",
        f"{i2}}}",
        f"{i1}}}",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void read_framed_stream(hls::stream<streamutils::framed_word<word_bw>>& s, value_type* dst, int len) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;",
        f"{i1}read_framed_stream<word_bw>(s, dst, tl, len);",
        f"{indent}}}",
        "",
        "template<int word_bw>",
        f"{indent}inline void write_framed_stream(hls::stream<streamutils::framed_word<word_bw>>& s, const value_type* src, bool tlast = true, int len = pf<word_bw>()) {{",
        f"{i1}#pragma HLS INLINE",
        f"{i1}if (src == nullptr || len <= 0) {{",
        f"{i2}return;",
        f"{i1}}}",
        f"{i1}for (int i = 0; i < len; i += pf<word_bw>()) {{",
        f"{i2}const bool lane_tlast = (i + pf<word_bw>() >= len) ? tlast : false;",
        f"{i2}write_framed_stream_elem_impl<word_bw>::run(s, src + i, lane_tlast, len - i);",
        f"{i1}}}",
        f"{indent}}}",
    ]
    return "\n".join(lines)


def _gen_tb_helpers(elem_type: type[DataSchema], indent_level: int = 0) -> str:
    indent = elem_type._get_indent(indent_level)
    i1 = elem_type._get_indent(indent_level + 1)
    elem_bw = elem_type.get_bitwidth()

    lines = [
        "/**",
        " * @brief Read a packed uint32 binary file into an array of value_type.",
        " *",
        " * The input file must contain little-endian uint32 words produced by the",
        " * Python arrayutils.write_uint32_file helper.",
        " *",
        " * @param dst Pointer to the destination array.",
        " * @param file_path Path to the input binary file.",
        " * @param n0 Number of array elements to decode.",
        " */",
        f"{indent}inline void read_uint32_file_array(value_type* dst, const char* file_path, int n0) {{",
        f"{i1}if (n0 < 0) {{",
        f'{i1}    throw std::runtime_error("n0 must be non-negative.");',
        f"{i1}}}",
        f"{i1}std::ifstream ifs(file_path, std::ios::binary);",
        f"{i1}if (!ifs) {{",
        f'{i1}    throw std::runtime_error(std::string("Failed to open input file: ") + file_path);',
        f"{i1}}}",
        f"{i1}const int nwords = get_nwords<32>(n0);",
        f"{i1}std::vector<ap_uint<32>> words;",
        f"{i1}words.reserve(nwords);",
        f"{i1}for (int i = 0; i < nwords; ++i) {{",
        f"{i1}    words.push_back(streamutils::read_le_uint32(ifs));",
        f"{i1}}}",
        f"{i1}if (ifs.peek() != std::ifstream::traits_type::eof()) {{",
        f'{i1}    throw std::runtime_error(std::string("Unexpected trailing bytes in input file: ") + file_path);',
        f"{i1}}}",
        f"{i1}read_array_slice<32>(words.empty() ? nullptr : words.data(), 0, n0, dst);",
        f"{indent}}}",
        "",
        "/**",
        " * @brief Write an array of value_type to a packed uint32 binary file.",
        " *",
        " * The output file matches the little-endian uint32 format consumed by the",
        " * Python arrayutils.read_uint32_file helper.",
        " *",
        " * @param src Pointer to the source array.",
        " * @param file_path Path to the output binary file.",
        " * @param n0 Number of array elements to encode.",
        " */",
        f"{indent}inline void write_uint32_file_array(const value_type* src, const char* file_path, int n0) {{",
        f"{i1}if (n0 < 0) {{",
        f'{i1}    throw std::runtime_error("n0 must be non-negative.");',
        f"{i1}}}",
        f"{i1}std::ofstream ofs(file_path, std::ios::binary);",
        f"{i1}if (!ofs) {{",
        f'{i1}    throw std::runtime_error(std::string("Failed to open output file: ") + file_path);',
        f"{i1}}}",
        f"{i1}const int nwords = get_nwords<32>(n0);",
        f"{i1}std::vector<ap_uint<32>> words(nwords);",
        f"{i1}write_array_slice<32>(src, words.empty() ? nullptr : words.data(), 0, n0);",
        f"{i1}for (const auto& word : words) {{",
        f"{i1}    streamutils::write_le_uint32(ofs, static_cast<uint32_t>(word));",
        f"{i1}}}",
        f"{indent}}}",
    ]
    return "\n".join(lines)


def _gen_array_utils_content(
    elem_type: type[DataSchema],
    widths: list[int],
    root_dir: Path,
    su_dir: Path,
) -> tuple[str, str]:
    """Return (hls_content, tb_content) for the given element type and widths."""
    elem_include = _relative_include_for_elem(elem_type)
    include_guard = _array_utils_include_guard(elem_type)
    tb_include_guard = _array_utils_tb_include_guard(elem_type)
    namespace = _array_utils_namespace(elem_type)
    elem_cpp = elem_type.cpp_class_name()

    lines = [
        f"#ifndef {include_guard}",
        f"#define {include_guard}",
        "",
        "#include <ap_int.h>",
        "#include <hls_stream.h>",
        "#if __has_include(<hls_axi_stream.h>)",
        "#include <hls_axi_stream.h>",
        "#else",
        "#include <ap_axi_sdata.h>",
        "#endif",
        f'#include "{_relative_streamutils_include(elem_type, root_dir, su_dir)}"',
    ]

    # Raw type includes the element's cpp_type needs (e.g. <complex> / the wf_cint header
    # for a ComplexField element); the body carries its own <…> / "…" delimiters.
    for inc in elem_type.get_codegen_includes():
        lines.append(f"#include {inc}")

    if elem_include is not None:
        lines.append(f'#include "{elem_include}"')

    lines.extend([
        "",
        f"namespace {namespace} {{",
        "",
        f"using value_type = {elem_cpp};",
        f"static constexpr int value_bitwidth = {elem_type.get_bitwidth()};",
        "",
        "template<int>",
        "struct unsupported_word_bw { static constexpr bool value = false; };",
        "",
        "template<int word_bw>",
        "static constexpr int get_nwords(int len) {",
        "    return (len <= 0) ? 0 : ((len * value_bitwidth + word_bw - 1) / word_bw);",
        "}",
        "",
        _gen_stream_elem_helpers(elem_type=elem_type, word_bw_supported=widths),
    ])

    # The bulk memory read_array / write_array methods (greedy LSB-first packing) are retired
    # (serialization phase 2b): kernels now move resident arrays with the word-aligned
    # read_array_slice / write_array_slice (element coordinates) and the read_array_lane loop, both
    # emitted by _gen_slice_helpers / _gen_lane_helpers above.

    lines.extend([
        "",
        f"}}  // namespace {namespace}",
        "",
        f"#endif // {include_guard}",
    ])

    tb_lines = [
        f"#ifndef {tb_include_guard}",
        f"#define {tb_include_guard}",
        "",
        f'#include "{_relative_streamutils_tb_include(elem_type, root_dir, su_dir)}"',
        f'#include "{_relative_synth_include_from_tb(elem_type)}"',
        "",
        f"namespace {namespace} {{",
        "",
        _gen_tb_helpers(elem_type=elem_type),
        "",
        f"}}  // namespace {namespace}",
        "",
        f"#endif // {tb_include_guard}",
    ]

    return "\n".join(lines) + "\n", "\n".join(tb_lines) + "\n"


def gen_array_utils(
    elem_type: type[DataSchema],
    word_bw_supported: list[int],
    cfg: BuildConfig | None = None,
    streamutils_dir: Path | str | None = None,
) -> Path:
    """Generate a Vitis HLS header that reads and writes packed arrays of one element type.

    Parameters
    ----------
    elem_type : type[DataSchema]
        Element schema class to decode.
    word_bw_supported : list[int]
        Word widths to specialize in the generated header.
    cfg : BuildConfig | None, optional
        Output configuration. If omitted, uses ``BuildConfig()``.
    streamutils_dir : Path | str | None, optional
        Directory containing ``streamutils_hls.h`` relative to
        ``cfg.root_dir``.  Defaults to ``"."`` (the build root itself).

    Returns
    -------
    pathlib.Path
        The generated header path.
    """
    if not isinstance(elem_type, type) or not issubclass(elem_type, DataSchema):
        raise TypeError("elem_type must be a DataSchema subclass.")

    if cfg is None:
        cfg = BuildConfig()

    su_dir = Path(streamutils_dir) if streamutils_dir is not None else Path(".")

    widths = sorted({int(bw) for bw in word_bw_supported})
    if not widths:
        raise ValueError("word_bw_supported must contain at least one positive width.")

    for bw in widths:
        if bw <= 0:
            raise ValueError(f"word_bw values must be positive. Got {bw}.")

    out_path = cfg.root_dir / _array_utils_include_path(elem_type)
    tb_out_path = cfg.root_dir / _array_utils_tb_include_path(elem_type)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    hls_content, tb_content = _gen_array_utils_content(
        elem_type=elem_type,
        widths=widths,
        root_dir=cfg.root_dir,
        su_dir=su_dir,
    )

    out_path.write_text(hls_content, encoding="utf-8")
    tb_out_path.parent.mkdir(parents=True, exist_ok=True)
    tb_out_path.write_text(tb_content, encoding="utf-8")
    return out_path


class ArrayUtilsStep(Buildable):
    """Build step that generates packed-array helper headers for one element type.

    Wraps :func:`gen_array_utils` as a DAG-aware :class:`~waveflow.build.build.Buildable`.
    Add a :class:`~waveflow.build.streamutils.StreamUtilsStep` to the same
    :class:`~waveflow.build.build.BuildDag` before this step; it is
    discovered automatically by :meth:`resolve_deps`.

    Parameters
    ----------
    elem_type : type[DataSchema]
        Element schema class to decode.
    word_bw_supported : list[int]
        Word widths to specialize in the generated header.
    """

    def __init__(
        self,
        elem_type: type[DataSchema],
        word_bw_supported: list[int],
    ) -> None:
        if not isinstance(elem_type, type) or not issubclass(elem_type, DataSchema):
            raise TypeError("elem_type must be a DataSchema subclass.")
        widths = sorted({int(bw) for bw in word_bw_supported})
        if not widths:
            raise ValueError("word_bw_supported must contain at least one positive width.")
        for bw in widths:
            if bw <= 0:
                raise ValueError(f"word_bw values must be positive. Got {bw}.")
        self._elem_type = elem_type
        self._widths = widths
        self._su_dir: Path = Path(".")
        super().__init__()  # _elem_type is set, so _default_name() works

    def _default_name(self) -> str:
        return f"{_array_utils_stem(self._elem_type)}ArrayUtilsStep"

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {
            "include": Path(_array_utils_include_path(self._elem_type)),
            "tb_include": Path(_array_utils_tb_include_path(self._elem_type)),
        }

    def generate(self, key: str, config: BuildConfig) -> str:
        hls, tb = _gen_array_utils_content(
            elem_type=self._elem_type,
            widths=self._widths,
            root_dir=config.root_dir,
            su_dir=self._su_dir,
        )
        if key == "include":
            return hls
        if key == "tb_include":
            return tb
        raise KeyError(f"Unknown ArrayUtilsStep output key: {key!r}")

    def resolve_deps(self, other_steps: list) -> None:
        from waveflow.build.streamutils import StreamUtilsStep
        from waveflow.hw.dataschema import DataSchemaStep

        self.deps = []

        su_steps = [s for s in other_steps if isinstance(s, StreamUtilsStep)]
        if not su_steps:
            raise ValueError(
                f"{self.name}: No StreamUtilsStep found. "
                "Register a StreamUtilsStep before this ArrayUtilsStep."
            )
        if len(su_steps) > 1:
            raise ValueError(
                f"{self.name}: Multiple StreamUtilsStep instances found. "
                "Only one is supported per BuildDag."
            )
        self._su_dir = su_steps[0].output_dir
        self.deps.append(su_steps[0])

        if self._elem_type.can_gen_include:
            dep_step = next(
                (
                    s for s in other_steps
                    if isinstance(s, DataSchemaStep) and s._schema is self._elem_type
                ),
                None,
            )
            if dep_step is not None:
                self.deps.append(dep_step)