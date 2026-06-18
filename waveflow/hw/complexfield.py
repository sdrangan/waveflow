"""``ComplexField`` -- a complex DataSchema type generic over a scalar inner field.

A ``ComplexField`` is two inner components (real, imag) of the **same** inner format,
generic over ``FloatField`` / ``FixedField`` / ``IntField``.  Like ``FixedField``, arrays
use ``DataArray[ComplexField]`` (no parallel class) and arithmetic is **free functions**
(:func:`cadd`/:func:`csub`/:func:`cmult`/:func:`conj`), wired into the merged operator
layer as ``+``/``-``/``*``.

Value representation (``.val``) depends on the inner (see :mod:`waveflow.utils.complexutils`):

- **float**     -> native numpy complex (``complex64``/``complex128``).
- **fixed/int** -> a numpy **structured** scalar ``[('re', D), ('im', D)]`` of stored ints.

Serialization is **interleaved I/Q** -- ``inner.serialize(re)`` then ``inner.serialize(im)``,
total width ``2 x inner`` -- by *composing the inner field's own (de)serialization* (so the
float IEEE bit-view and wide-int multi-word packing are reused verbatim).  A
``DataArray[ComplexField]`` therefore maps directly onto a ``std::complex<T>`` array.

Arithmetic **composes** the inner field's arithmetic on the re/im components
(:mod:`waveflow.utils.complexutils`, which lowers fixed/int to
:mod:`waveflow.utils.fixputils` and float to numpy); result formats follow the
``FixedField`` rules and inherit the >64-bit guard.  ``cmult``/``conj`` require a signed
inner (they produce signed results); ``cadd``/``csub`` follow the inner rule.
"""
from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from waveflow.hw.dataschema import DataArray, DataField, FloatField, IntField, _elem_kind
from waveflow.hw.param import defer_if_symbolic
from waveflow.utils import complexutils as cx
from waveflow.utils import fixputils
from waveflow.utils.complexutils import complex_dtype, int_format
from waveflow.utils.fixputils import Format


def _inner_kind(inner: type[DataField]) -> str:
    """Classify a scalar inner field as ``float`` / ``fixed`` / ``int``."""
    kind = _elem_kind(inner)
    if kind == "other":
        raise TypeError(
            f"ComplexField inner must be a FloatField / FixedField / IntField, got "
            f"{inner.__name__}.")
    return kind


class ComplexField(DataField):
    """Complex element type generic over a scalar inner field (real, imag share a format)."""

    inner_type: ClassVar[type[DataField] | None] = None
    kind: ClassVar[str] = ""                      # "float" | "fixed" | "int"
    is_complex_field: ClassVar[bool] = True       # marker for the operator layer dispatch
    bitwidth: ClassVar[int | None] = None
    cpp_type: ClassVar[str | None] = None
    can_gen_include: ClassVar[bool] = False
    _specializations: ClassVar[dict[tuple[Any, ...], type["ComplexField"]]] = {}

    # --- specialization -------------------------------------------------------
    @classmethod
    @defer_if_symbolic
    def specialize(cls, inner: type[DataField], **kwargs: Any) -> type["ComplexField"]:
        """Return a cached ``ComplexField`` over a scalar ``inner`` field (one per inner).

        Like the other ``specialize`` methods, this defers (returns a ``LazyField``) when the
        ``inner`` is symbolic -- e.g. a ``ParamSchema`` element ``ComplexField.specialize(
        IntField.specialize(data_bw, ...))`` over a symbolic ``data_bw`` ``Param``, where the
        inner ``IntField.specialize`` is itself a deferred ``LazyField`` -- so the cascade
        resolves both layers when the schema is specialized."""
        if not isinstance(inner, type) or not issubclass(inner, DataField):
            raise TypeError("ComplexField inner must be a scalar DataField subclass.")
        kind = _inner_kind(inner)

        overrides = cls.validate_specialize_kwargs(kwargs)
        override_items = tuple(sorted(overrides.items()))
        key = (cls, inner, override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        inner_bw = inner.get_bitwidth()
        if kind == "int":
            cpp_type = f"wf_cint<{inner_bw}>"     # std::complex<ap_int> is non-standard
        else:
            cpp_type = f"std::complex<{inner.cpp_type}>"
        subclass_name = f"Complex_{inner.__name__}"
        specialized_attrs = cls.merge_specialize_attrs(
            {
                "inner_type": inner,
                "kind": kind,
                "bitwidth": 2 * inner_bw,
                "cpp_type": cpp_type,
                "__module__": cls.__module__,
                "__doc__": f"Specialized complex field: {cpp_type}.",
            },
            overrides,
        )
        specialized = type(subclass_name, (cls,), specialized_attrs)
        cls._specializations[key] = specialized
        return specialized

    # --- inner format / representation ---------------------------------------
    @classmethod
    def inner_format(cls) -> Format:
        """The fixed/int inner's :class:`Format` (raises for a float inner)."""
        inner = cls.inner_type
        if cls.kind == "fixed":
            return inner.get_format()
        if cls.kind == "int":
            return int_format(inner.get_bitwidth(), inner.signed)
        raise TypeError("a float inner has no integer Format.")

    @classmethod
    def _np_complex_type(cls) -> type:
        return np.complex128 if cls.inner_type.get_bitwidth() == 64 else np.complex64

    @classmethod
    def init_value(cls) -> Any:
        if cls.kind == "float":
            return cls._np_complex_type()(0)
        return np.zeros((), dtype=complex_dtype(cls.inner_format()))[()]

    def _convert(self, value: Any) -> Any:
        cls = type(self)
        if isinstance(value, ComplexField):
            value = value.val
        re, im = _extract_re_im(value)
        if cls.kind == "float":
            return cls._np_complex_type()(complex(float(re), float(im)))
        # fixed/int: route each component through the inner's wrapping, then pack stored ints
        inner = cls.inner_type
        rf, imf = inner(), inner()
        rf.val, imf.val = re, im
        out = np.zeros((), dtype=complex_dtype(cls.inner_format()))
        out["re"], out["im"] = rf.val, imf.val
        return out[()]

    # --- interleaved I/Q (de)serialization, composing the inner field ---------
    def _inner_fields(self) -> tuple[DataField, DataField]:
        cls = type(self)
        inner = cls.inner_type
        v = self.val
        rf, imf = inner(), inner()
        if cls.kind == "float":
            rf.val, imf.val = float(np.real(v)), float(np.imag(v))
        else:
            rf.val, imf.val = int(v["re"]), int(v["im"])
        return rf, imf

    def _serialize_recursive(
        self, word_bw: int, words: list[int], ipos0: int = 0, iword0: int = 0,
    ) -> tuple[int, int]:
        re_field, im_field = self._inner_fields()
        pos, word = re_field._serialize_recursive(word_bw, words, ipos0, iword0)
        return im_field._serialize_recursive(word_bw, words, pos, word)

    def _deserialize_recursive(
        self, word_bw: int, words: list[int], ipos0: int = 0, iword0: int = 0,
    ) -> tuple[int, int]:
        inner = type(self).inner_type
        re_field, im_field = inner(), inner()
        pos, word = re_field._deserialize_recursive(word_bw, words, ipos0, iword0)
        pos, word = im_field._deserialize_recursive(word_bw, words, pos, word)
        if type(self).kind == "float":
            self.val = complex(float(re_field.val), float(im_field.val))
        else:
            self.val = (int(re_field.val), int(im_field.val))
        return pos, word

    # --- C++ codegen protocol (mirrors the interleaved Python serialization) -------
    # Each method delegates to the *inner field's* C++ codegen for re then im, interleaved,
    # ``im`` in the high ``inner_bw`` bits -- exactly as ``_serialize_recursive`` composes the
    # inner's Python serialization.  ``array_utils`` consumes these to pack ``ComplexField``
    # like any other element (``read_array_elem_impl`` / ``write_array_elem_impl`` and the lane /
    # slice methods built on them / ``pf<>``).
    @classmethod
    def _cpp_re(cls, value_expr: str) -> str:
        """C++ accessor for the real component of a ``cpp_type`` value expression."""
        return f"({value_expr}).re" if cls.kind == "int" else f"({value_expr}).real()"

    @classmethod
    def _cpp_im(cls, value_expr: str) -> str:
        """C++ accessor for the imag component of a ``cpp_type`` value expression."""
        return f"({value_expr}).im" if cls.kind == "int" else f"({value_expr}).imag()"

    @classmethod
    def to_uint_expr(cls, value_expr: str) -> str:
        """Pack a complex value into ``2*inner_bw`` bits: ``re | (im << inner_bw)`` (delegating
        each component to the inner field's pack).

        Each component's packed bits are first truncated to ``ap_uint<inner_bw>`` so a signed
        integer inner (``wf_cint`` over ``ap_int``) is **zero-extended** -- not sign-extended --
        when widened to the ``2*inner_bw`` word (otherwise a negative ``re`` would bleed into
        the ``im`` slot).  ``fixed_to_bits`` / ``float_to_uint`` already return the raw bits, so
        the truncation is a no-op for the float / fixed inner."""
        inner = cls.inner_type
        bw = inner.get_bitwidth()
        bw2 = 2 * bw
        re = f"(ap_uint<{bw}>)({inner.to_uint_value_expr(cls._cpp_re(value_expr))})"
        im = f"(ap_uint<{bw}>)({inner.to_uint_value_expr(cls._cpp_im(value_expr))})"
        return f"((ap_uint<{bw2}>)({re}) | ((ap_uint<{bw2}>)({im}) << {bw}))"

    @classmethod
    def to_uint_value_expr(cls, value_expr: str) -> str:
        return cls.to_uint_expr(value_expr)

    @classmethod
    def from_uint_expr(cls, uint_expr: str) -> str:
        """Construct ``cpp_type(re, im)`` from the low / high ``inner_bw`` halves of the packed
        bits (the ``std::complex<…>`` / ``wf_cint`` two-arg ``(re, im)`` constructor)."""
        inner = cls.inner_type
        bw = inner.get_bitwidth()
        bw2 = 2 * bw
        w = f"(ap_uint<{bw2}>)({uint_expr})"
        re = inner.from_uint_expr(f"(ap_uint<{bw}>)({w})")
        im = inner.from_uint_expr(f"(ap_uint<{bw}>)(({w}) >> {bw})")
        return f"{cls.cpp_type}({re}, {im})"

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
        """Multi-word write: emit re then im via the inner field's ``_gen_write_recursive``."""
        if member_name is None:
            raise ValueError(f"{cls.__name__} write generation requires a member_name.")
        inner = cls.inner_type
        value_expr = f"{prefix}{member_name}"
        re_lines, ipos, iword = inner._gen_write_recursive(
            word_bw=word_bw, dst_type=dst_type, target=target, ipos0=ipos0, iword0=iword0,
            prefix="", member_name=cls._cpp_re(value_expr))
        im_lines, ipos, iword = inner._gen_write_recursive(
            word_bw=word_bw, dst_type=dst_type, target=target, ipos0=ipos, iword0=iword,
            prefix="", member_name=cls._cpp_im(value_expr))
        return re_lines + im_lines, ipos, iword

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
        """Multi-word read: read re then im into inner temps via the inner field's
        ``_gen_read_recursive``, then assign ``cpp_type(re, im)``."""
        if member_name is None:
            raise ValueError(f"{cls.__name__} read generation requires a member_name.")
        inner = cls.inner_type
        inner_cpp = inner.cpp_class_name()
        dst_expr = f"{prefix}{member_name}"
        lines = [f"    {inner_cpp} __wf_re;", f"    {inner_cpp} __wf_im;"]
        re_lines, ipos, iword = inner._gen_read_recursive(
            word_bw=word_bw, src_type=src_type, source=source, ipos0=ipos0, iword0=iword0,
            prefix="", member_name="__wf_re")
        im_lines, ipos, iword = inner._gen_read_recursive(
            word_bw=word_bw, src_type=src_type, source=source, ipos0=ipos, iword0=iword,
            prefix="", member_name="__wf_im")
        lines.extend(re_lines)
        lines.extend(im_lines)
        lines.append(f"    {dst_expr} = {cls.cpp_type}(__wf_re, __wf_im);")
        return cls._scope_local_lines(lines), ipos, iword

    @classmethod
    def get_codegen_includes(cls) -> list[str]:
        """Raw ``#include`` bodies the generated C++ needs for ``cpp_type``:
        ``<complex>`` for the ``std::complex<…>`` (float / fixed) inner, the ``wf_cint``
        header for the integer inner (``std::complex<ap_int>`` is non-standard)."""
        return ['"wf_cint.h"'] if cls.kind == "int" else ["<complex>"]


def _extract_re_im(value: Any) -> tuple[Any, Any]:
    """Pull (re, im) out of a structured scalar / complex / (re, im) pair / real number."""
    if isinstance(value, ComplexField):
        value = value.val
    if isinstance(value, (np.void, np.ndarray)) and value.dtype.names:
        return value["re"], value["im"]
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise ValueError(f"complex (re, im) pair must have length 2, got {len(value)}.")
        return value[0], value[1]
    if isinstance(value, (complex, np.complexfloating)):
        return value.real, value.imag
    return value, 0


# --- DataArray[ComplexField] arithmetic (free functions; compose complexutils) ----
def _check_same_complex_kind(a: DataArray, b: DataArray, op: str) -> str:
    ea, eb = a.element_type, b.element_type
    if not (getattr(ea, "is_complex_field", False) and getattr(eb, "is_complex_field", False)):
        raise TypeError(f"complex {op} needs both operands to be DataArray[ComplexField].")
    if ea.kind != eb.kind:
        raise TypeError(
            f"cannot apply complex {op} to {ea.kind}- and {eb.kind}-inner complex arrays "
            "(operands must share an inner kind).")
    return ea.kind


def _result_inner(kind: str, r: Format) -> type[DataField]:
    if kind == "int":
        return IntField.specialize(r.W, r.signed)
    from waveflow.hw.fixpoint import FixedField
    return FixedField.specialize(r.W, r.int_bits, r.signed, r.q_mode, r.o_mode)


def _wrap_complex(val: np.ndarray, inner: type[DataField]) -> DataArray:
    cf = ComplexField.specialize(inner)
    arr = np.asarray(val)
    shape = arr.shape if arr.shape else (1,)
    return DataArray.specialize(cf, max_shape=tuple(shape))(arr.reshape(shape))


def _wrap_float(out: np.ndarray) -> DataArray:
    bw = 64 if np.asarray(out).dtype == np.complex128 else 32
    return _wrap_complex(out, FloatField.specialize(bw))


def cadd(a: DataArray, b: DataArray) -> DataArray:
    """``a + b`` componentwise (full precision; int bits +1)."""
    kind = _check_same_complex_kind(a, b, "add")
    if kind == "float":
        return _wrap_float(cx.cadd_float(np.asarray(a.val), np.asarray(b.val)))
    out, r = cx.cadd(np.asarray(a.val), a.element_type.inner_format(),
                     np.asarray(b.val), b.element_type.inner_format())
    return _wrap_complex(out, _result_inner(kind, r))


def csub(a: DataArray, b: DataArray) -> DataArray:
    """``a - b`` componentwise (full precision; always signed)."""
    kind = _check_same_complex_kind(a, b, "sub")
    if kind == "float":
        return _wrap_float(cx.csub_float(np.asarray(a.val), np.asarray(b.val)))
    out, r = cx.csub(np.asarray(a.val), a.element_type.inner_format(),
                     np.asarray(b.val), b.element_type.inner_format())
    return _wrap_complex(out, _result_inner(kind, r))


def cmult(a: DataArray, b: DataArray) -> DataArray:
    """``a * b`` -- complex multiply; result ``(2W+1, 2I+1, signed)`` (signed inner only)."""
    kind = _check_same_complex_kind(a, b, "mul")
    if kind == "float":
        return _wrap_float(cx.cmult_float(np.asarray(a.val), np.asarray(b.val)))
    out, r = cx.cmult(np.asarray(a.val), a.element_type.inner_format(),
                      np.asarray(b.val), b.element_type.inner_format())
    return _wrap_complex(out, _result_inner(kind, r))


def conj(a: DataArray) -> DataArray:
    """``conj(a)`` -- imag negated; result ``(W+1, I+1, signed)`` (signed inner only)."""
    ea = a.element_type
    if not getattr(ea, "is_complex_field", False):
        raise TypeError("conj needs a DataArray[ComplexField].")
    if ea.kind == "float":
        return _wrap_float(cx.conj_float(np.asarray(a.val)))
    out, r = cx.conj(np.asarray(a.val), ea.inner_format())
    return _wrap_complex(out, _result_inner(ea.kind, r))


def csum(a: DataArray, axis: int = 0) -> DataArray:
    """Wide-accumulator column reduction (full precision), real **or** complex.

    Reduces over ``axis`` (default 0 -> sum the rows, one value per column), integer
    bits growing by ``ceil(log2 N)`` (``sum_format``).  Real ``FixedField`` / ``IntField``
    delegates to ``fixpoint.fixed_sum``; complex sums the re/im stored-int components with
    the same integer reduction and recombines (so re and im share the grown format).
    Vectorized -- no per-element loop."""
    ea = a.element_type
    if getattr(ea, "is_complex_field", False):
        if ea.kind == "float":
            return _wrap_float(np.asarray(a.val).sum(axis=axis))
        fmt = ea.inner_format()
        v = np.asarray(a.val)
        re, r = fixputils.fixed_sum(cx.re_of(v), fmt, axis=axis)
        im, _ = fixputils.fixed_sum(cx.im_of(v), fmt, axis=axis)
        return _wrap_complex(cx.make_complex(re, im, r), _result_inner(ea.kind, r))
    from waveflow.hw.fixpoint import fixed_sum as _fixed_sum
    return _fixed_sum(a, axis=axis)
