"""Integer-backed, Vitis-bit-exact fixed-point DataSchema type + vector arithmetic.

``FixedField`` is the element type carrying the **format** (``W, I, signed, q, o``)
on its cached ``specialize`` class; **arrays use ``DataArray[FixedField]``** (a
numpy-backed wrapper — no parallel class).  An ``ap_fixed<W,I>`` is a scaled W-bit
integer, so ``FixedField`` subclasses :class:`IntField` and reuses its W-bit word
serialization verbatim — and because storage is now the **stored integer** (not a
float), value↔bits needs no override at all.  ``.val`` holds the stored int;
``to_real``/``.real`` derives ``stored · 2^-F``.

Arithmetic is **free functions** (:func:`mult`/:func:`add`/:func:`sub`/:func:`div`/
:func:`abs`/:func:`shift`/:func:`quantize`/:func:`fixed_sum`), not methods — each reads the operands' formats,
runs the vectorized integer op on the stored ``.val`` arrays via
:mod:`waveflow.utils.fixputils`, derives the result format, and returns a
``DataArray[FixedField<derived>]``.  Full-precision intermediates; :func:`quantize`
(to a declared format) is the only lossy step.  :func:`from_real` is the other
quantizing entry point (loading real data into a format).

Mixed signed/unsigned operands raise in the arithmetic (a v1 limitation; promote to a common
signed format first).  Any derived format wider than 64 bits raises at derivation.  The
comparisons (:func:`lt` ... :func:`ne`, and ``<`` ... ``>=`` on a ``DataArray``) take any two
formats, signed or not, and compare exactly, as ``ap_fixed`` does.
"""
from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from waveflow.hw.dataschema import DataArray, IntField
from waveflow.utils import fixputils
from waveflow.utils.fixputils import Format, OMode, QMode


class FixedField(IntField):
    """Fixed-point element type, bit-exact with ``ap_fixed<W, I, Q, O>``.

    Class attributes (set by :meth:`specialize`): ``bitwidth`` (= W), ``int_bits``
    (= I), ``signed``, ``q_mode`` (:class:`QMode`), ``o_mode`` (:class:`OMode`), and
    the ``ap_fixed``/``ap_ufixed`` ``cpp_type``.  ``.val`` is the stored W-bit
    integer; assign stored integers (use :func:`from_real` to quantize reals)."""

    bitwidth: ClassVar[int] = 32
    int_bits: ClassVar[int] = 16
    signed: ClassVar[bool] = True
    q_mode: ClassVar[QMode] = QMode.AP_TRN
    o_mode: ClassVar[OMode] = OMode.AP_WRAP
    cpp_type: ClassVar[str] = "ap_fixed<32, 16, AP_TRN, AP_WRAP>"
    can_gen_include: ClassVar[bool] = False
    _specializations: ClassVar[dict] = {}

    @classmethod
    def specialize(  # type: ignore[override]
        cls,
        W: int,
        I: int,  # noqa: E741 — ap_fixed integer-bit count
        signed: bool = True,
        q_mode: QMode = QMode.AP_TRN,
        o_mode: OMode = OMode.AP_WRAP,
        **kwargs: Any,
    ) -> type[FixedField]:
        """Return a cached ``FixedField`` for ``ap_fixed<W, I, q, o>`` (one per format)."""
        if not kwargs and type(W) is int and type(I) is int:    # a format made before: validated
            cached = cls._specializations.get(
                (cls, int(W), int(I), bool(signed), q_mode, o_mode, ()))
            if cached is not None:
                return cached
        Format(W, I, signed, q_mode, o_mode)        # validates + fires the >64-bit guard

        overrides = cls.validate_specialize_kwargs(kwargs)
        override_items = tuple(sorted(overrides.items()))
        key = (cls, int(W), int(I), bool(signed), q_mode, o_mode, override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        base = "ap_fixed" if signed else "ap_ufixed"
        cpp_type = f"{base}<{W}, {I}, {q_mode.value}, {o_mode.value}>"
        subclass_name = f"{'Fixed' if signed else 'UFixed'}{W}_{I}"
        specialized_attrs = cls.merge_specialize_attrs(
            {
                "bitwidth": int(W), "int_bits": int(I), "signed": bool(signed),
                "q_mode": q_mode, "o_mode": o_mode, "cpp_type": cpp_type,
                "__module__": cls.__module__,
                "__doc__": f"Specialized fixed-point field: {cpp_type}.",
            },
            overrides,
        )
        specialized = type(subclass_name, (cls,), specialized_attrs)
        cls._specializations[key] = specialized
        return specialized

    @classmethod
    def get_format(cls) -> Format:
        return Format(cls.bitwidth, cls.int_bits, cls.signed, cls.q_mode, cls.o_mode)

    @classmethod
    def init_value(cls) -> Any:
        return np.int64(0) if cls.signed else np.uint64(0)

    def _convert(self, value: Any) -> Any:
        """Store the W-bit integer (wrap to W bits). Use :func:`from_real` for reals."""
        if isinstance(value, (float, np.floating)) and not float(value).is_integer():
            raise ValueError(
                f"{type(self).__name__} holds stored integers; assign a stored int or use "
                f"from_real() to quantize a real value (got {value!r}).")
        W = type(self).bitwidth
        wrapped = int(value) & ((1 << W) - 1)
        if type(self).signed and (wrapped >> (W - 1)) & 1:
            wrapped -= 1 << W
        return type(self).init_value().dtype.type(wrapped)

    @property
    def real(self) -> float:
        """The real value ``stored · 2^-F`` (scalar view)."""
        return fixputils.to_float(self.val, type(self).get_format())

    # --- C++ codegen: ap_fixed <-> ap_uint<W> word payload is a bit-reinterpret
    # (.range()), not a value cast — routed through the streamutils helpers (same
    # shape FloatField uses). The stored W-bit payload is identical to IntField.
    @classmethod
    def to_uint_expr(cls, value_expr: str) -> str:
        return f"streamutils::fixed_to_bits<{cls.cpp_type}>({value_expr})"

    @classmethod
    def to_uint_value_expr(cls, value_expr: str) -> str:
        return f"streamutils::fixed_to_bits<{cls.cpp_type}>({value_expr})"

    @classmethod
    def from_uint_expr(cls, uint_expr: str) -> str:
        return f"streamutils::bits_to_fixed<{cls.cpp_type}>({uint_expr})"


# --- helpers ------------------------------------------------------------------
def _fmt(da: DataArray) -> Format:
    return da.element_type.get_format()


def _wrap(stored: np.ndarray, fmt: Format, cls: type[FixedField] | None = None) -> DataArray:
    """Wrap a stored-int array + format into a DataArray[FixedField<fmt>].

    *cls* is the class the caller declared, when the result has its format (:func:`from_real`,
    :func:`quantize`): the array keeps it, and with it what the class carries beyond the format
    (``include_dir``, say).  Without it -- an op's derived format -- the cached class of the format.
    """
    if cls is None:
        cls = FixedField.specialize(fmt.W, fmt.int_bits, fmt.signed, fmt.q_mode, fmt.o_mode)
    elif cls.get_format() != fmt:
        raise ValueError(f"{cls.__name__} is {cls.cpp_type}, not the result's format {fmt}")
    arr = np.asarray(stored)
    shape = arr.shape if arr.shape else (1,)
    return DataArray.specialize(cls, max_shape=shape)(arr.reshape(shape))


# --- real <-> fixed entry points ----------------------------------------------
def from_real(values: Any, fixed_cls: type[FixedField]) -> DataArray:
    """Quantize real value(s) into ``DataArray[fixed_cls]`` (the load-data path).

    The array's element type is *fixed_cls* itself, not another class of the same format.
    """
    stored = fixputils.quantize_real(np.asarray(values, dtype=np.float64), fixed_cls.get_format())
    return _wrap(stored, fixed_cls.get_format(), fixed_cls)


def to_real(da: DataArray) -> np.ndarray:
    """The real-valued view of a ``DataArray[FixedField]``."""
    return fixputils.to_float(np.asarray(da), _fmt(da))


# --- vector arithmetic (free functions; result format derived per ap_fixed) ---
def mult(a: DataArray, b: DataArray) -> DataArray:
    stored, fmt = fixputils.mult(np.asarray(a), _fmt(a), np.asarray(b), _fmt(b))
    return _wrap(stored, fmt)


def add(a: DataArray, b: DataArray) -> DataArray:
    stored, fmt = fixputils.add(np.asarray(a), _fmt(a), np.asarray(b), _fmt(b))
    return _wrap(stored, fmt)


def sub(a: DataArray, b: DataArray) -> DataArray:
    stored, fmt = fixputils.sub(np.asarray(a), _fmt(a), np.asarray(b), _fmt(b))
    return _wrap(stored, fmt)


def div(a: DataArray, b: DataArray) -> DataArray:
    """``ap_fixed`` division ``a / b`` with Waveflow's zero guard (``x / 0 = 0``).

    Truncates toward zero and keeps the dividend's fraction bits
    (:func:`~waveflow.utils.fixputils.div_format`).  For a more precise quotient,
    :func:`quantize` the dividend to more fraction bits first.  The matching C++ is
    ``(b == 0) ? 0 : a / b``.
    """
    stored, fmt = fixputils.div(np.asarray(a), _fmt(a), np.asarray(b), _fmt(b))
    return _wrap(stored, fmt)


def shift(a: DataArray, n: int) -> DataArray:
    """Lossless rescale by ``2^n`` (point-move): stored bits unchanged."""
    stored, fmt = fixputils.shift(np.asarray(a), _fmt(a), n)
    return _wrap(stored, fmt)


def abs(a: DataArray) -> DataArray:  # noqa: A001 -- the fixed-point op, beside mult/add/sub
    """Full-precision magnitude ``|a|``: a signed ``<W, I>`` gives ``<W+1, I+1>``.

    Lossless, like the other ops: the extra bit holds ``|min|`` (``ap_fixed`` unary minus
    has the same type).  An unsigned format is returned unchanged.  The matching C++ is
    ``ap_fixed<W+1, I+1> y = (a < 0) ? ap_fixed<W+1, I+1>(-a) : ap_fixed<W+1, I+1>(a);``;
    :func:`quantize` the result to keep the input width (with ``AP_SAT``, ``|min|``
    saturates to the maximum).  Vitis ``hls::abs`` on ``ap_fixed<W, I>`` keeps the width
    and returns 0 for ``min``, so it is not this op.  Also spelled ``abs(a)`` on a
    ``DataArray`` (Python's builtin calls ``__abs__``).
    """
    stored, fmt = fixputils.abs_(np.asarray(a), _fmt(a))
    return _wrap(stored, fmt)


def fixed_sum(a: DataArray, axis: int | None = None) -> DataArray:
    """Full-precision reduction (integer bits grow by ``ceil(log2 N)``)."""
    stored, fmt = fixputils.fixed_sum(np.asarray(a), _fmt(a), axis=axis)
    return _wrap(stored, fmt)


def quantize(a: DataArray, target: type[FixedField]) -> DataArray:
    """Requantize to a declared ``target`` format — the only lossy operation.

    The result's element type is *target* itself, not another class of the same format.
    """
    stored = fixputils.quantize(np.asarray(a), _fmt(a), target.get_format())
    return _wrap(stored, target.get_format(), target)


# --- comparisons ------------------------------------------------------------------
# ap_fixed compares exactly: the two binary points are aligned and the values compared, whatever
# the two formats -- widths, integer bits, signedness.  So does this: both operands' stored
# integers are shifted to the finer of the two fraction lengths, in a signed integer wide enough
# for either (int64 when that fits, Python ints otherwise), and compared.  No rounding, no floats,
# and a signed operand compares with an unsigned one -- which the arithmetic above refuses, because
# there a result format must be derived and numpy would mix int64 with uint64 into float64.

_COMPARE = {"lt": np.less, "le": np.less_equal, "gt": np.greater, "ge": np.greater_equal,
            "eq": np.equal, "ne": np.not_equal}


def _cmp_operand(x: Any, side: str) -> tuple[np.ndarray, Format]:
    if isinstance(x, DataArray) and isinstance(x.element_type, type) \
            and issubclass(x.element_type, FixedField):
        return np.asarray(x.val), x.element_type.get_format()
    if isinstance(x, FixedField):
        return np.asarray(x.val), type(x).get_format()
    raise TypeError(
        f"a fixed-point comparison needs a FixedField array or value on the {side}, got "
        f"{type(x).__name__}.  A plain number is ambiguous (a stored integer, or a real value?): "
        "wrap a stored integer as T(stored), or quantize a real with from_real(value, T).")


def _compare(a: Any, b: Any, op: str) -> np.ndarray:
    va, fa = _cmp_operand(a, "left")
    vb, fb = _cmp_operand(b, "right")
    frac = max(fa.frac_bits, fb.frac_bits)
    sa, sb = frac - fa.frac_bits, frac - fb.frac_bits
    # Bits a signed container needs for each aligned operand (an unsigned one, one more for the sign).
    need = max(fa.W + sa + (not fa.signed), fb.W + sb + (not fb.signed))
    dtype: Any = np.int64 if need <= 64 else object
    xa = va.astype(dtype) << sa
    xb = vb.astype(dtype) << sb
    return _COMPARE[op](xa, xb)


def lt(a: Any, b: Any) -> np.ndarray:
    """``a < b``, element-wise and exact, as ``ap_fixed`` compares: any two formats, signed or not.

    Operands are ``DataArray[FixedField]`` (broadcast as numpy does) or scalar ``FixedField``
    values.  Also spelled ``a < b`` on a ``DataArray``; ``==`` / ``!=`` are only the functions
    :func:`eq` / :func:`ne`, because a ``DataArray``'s ``==`` stays identity (it is hashable).
    """
    return _compare(a, b, "lt")


def le(a: Any, b: Any) -> np.ndarray:
    """``a <= b``, exact (see :func:`lt`)."""
    return _compare(a, b, "le")


def gt(a: Any, b: Any) -> np.ndarray:
    """``a > b``, exact (see :func:`lt`)."""
    return _compare(a, b, "gt")


def ge(a: Any, b: Any) -> np.ndarray:
    """``a >= b``, exact (see :func:`lt`)."""
    return _compare(a, b, "ge")


def eq(a: Any, b: Any) -> np.ndarray:
    """Equal in value, exact: ``0.5`` in ``fixed<12, 1>`` equals ``0.5`` in ``ufixed<16, 4>``."""
    return _compare(a, b, "eq")


def ne(a: Any, b: Any) -> np.ndarray:
    """Not equal in value, exact (see :func:`eq`)."""
    return _compare(a, b, "ne")
