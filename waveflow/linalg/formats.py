"""formats.py — operand formats of the linear-algebra components, and how they reach C++.

A component takes each operand's fixed-point format explicitly, as a
:class:`~waveflow.utils.fixputils.Format`: width, integer bits, signedness, rounding and
saturation.  Formats are plain fields of the component, never an index into a registry.

How a format reaches the C++ body
---------------------------------
The framework names task instances and calibration keys from **integer** template arguments, so
a body cannot take a format, or a type, as one.  Each instance passes a single integer instead,
its **format id**, derived from the content of its formats (:func:`format_id`).  A build step
then renders, for every id in a design, one specialization of a traits template holding the
register typedefs and the memory element structs (:func:`render_traits_header`)::

    template <int ID> struct wf_systolic_traits;
    template <> struct wf_systolic_traits<1234567> {
        typedef ap_fixed<12, 3, AP_RND, AP_SAT> a_t;
        struct a_mem { ... };   // reads and writes a framed stream of memory elements
    };

Two instances with different formats get two ids, so they share one design.  Equal formats give
equal ids, and the same id with different content is refused.

The memory format
-----------------
An operand travels in messages as a complex element whose two parts are ``lane_bits`` wide, with
the register's integer bits (:func:`mem_format`).  Widening a ``W``-bit register to it only adds
fraction bits, so the conversion is exact both ways for ``W <= lane_bits``.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable
from dataclasses import dataclass

from waveflow.hw.arrayutils import _array_utils_filename, _array_utils_namespace
from waveflow.hw.complexfield import ComplexField
from waveflow.hw.fixpoint import FixedField
from waveflow.utils.fixputils import Format

#: The default width of each part of a memory element.
DEFAULT_LANE_BITS = 16
#: The file :func:`render_traits_header` output is written to.
TRAITS_HEADER = "wf_linalg_traits.h"


def ap_type(fmt: Format) -> str:
    """The ``ap_fixed`` (or ``ap_ufixed``) spelling of ``fmt``."""
    kind = "ap_fixed" if fmt.signed else "ap_ufixed"
    return f"{kind}<{fmt.W}, {fmt.int_bits}, {fmt.q_mode.value}, {fmt.o_mode.value}>"


def mem_format(fmt: Format, lane_bits: int = DEFAULT_LANE_BITS) -> Format:
    """The memory format of a register: ``lane_bits`` wide, with the register's integer bits.

    Both conversions are exact, so it carries no rounding or saturation mode of its own: two
    registers that differ only in their modes share one memory element type.
    """
    if fmt.W > lane_bits:
        raise ValueError(
            f"a {fmt.W}-bit register is wider than the {lane_bits}-bit lane"
        )
    return Format(int(lane_bits), fmt.int_bits, fmt.signed)


def mem_elem_type(
    fmt: Format, lane_bits: int = DEFAULT_LANE_BITS, include_dir: str | None = None
) -> type[ComplexField]:
    """``ComplexField[FixedField<mem_format(fmt)>]``, the element an operand travels as."""
    m = mem_format(fmt, lane_bits)
    kw = {} if include_dir is None else {"include_dir": include_dir}
    inner = FixedField.specialize(m.W, m.int_bits, m.signed, m.q_mode, m.o_mode, **kw)
    return ComplexField.specialize(inner, **kw)


def _canonical(fmt: Format) -> str:
    return f"{fmt.W},{fmt.int_bits},{int(fmt.signed)},{fmt.q_mode.value},{fmt.o_mode.value}"


def format_id(*parts: object) -> int:
    """A positive 31-bit integer derived from ``parts``, stable across runs and machines.

    ``Format`` parts are spelled canonically; everything else by ``str``.
    """
    text = "|".join(_canonical(p) if isinstance(p, Format) else str(p) for p in parts)
    return (zlib.crc32(text.encode()) & 0x7FFFFFFF) or 1


@dataclass(frozen=True)
class Traits:
    """One specialization ``template <> struct <family><id>`` of a component's traits.

    Parameters
    ----------
    family
        The C++ name of the traits template, e.g. ``"wf_systolic_traits"``.
    types
        ``(name, format)`` pairs rendered as ``typedef <ap_fixed> name;`` — registers and exact
        accumulators alike.
    mems
        ``(name, register format)`` pairs rendered as memory element structs ``name`` that read
        and write a framed stream of ``mem_format(format, lane_bits)`` elements.
    lane_bits
        The width of each part of a memory element.
    """

    family: str
    types: tuple[tuple[str, Format], ...]
    mems: tuple[tuple[str, Format], ...] = ()
    lane_bits: int = DEFAULT_LANE_BITS

    @property
    def id(self) -> int:
        """The format id: a hash of everything the specialization contains."""
        flat = [self.family, self.lane_bits]
        for kind, pairs in (("t", self.types), ("m", self.mems)):
            for name, fmt in pairs:
                flat += [kind, name, fmt]
        return format_id(*flat)

    def mem_elems(self, include_dir: str | None = None) -> list[type[ComplexField]]:
        """The memory element types of :attr:`mems`, without repeats, in order."""
        out: list = []
        for _name, fmt in self.mems:
            elem = mem_elem_type(fmt, self.lane_bits, include_dir)
            if elem not in out:
                out.append(elem)
        return out

    def render(self, include_dir: str | None = None) -> str:
        """The specialization's C++ text."""
        lines = [f"template <> struct {self.family}<{self.id}> {{"]
        lines += [f"    typedef {ap_type(fmt)} {name};" for name, fmt in self.types]
        for name, fmt in self.mems:
            ns = _array_utils_namespace(mem_elem_type(fmt, self.lane_bits, include_dir))
            lines.append(_MEM_STRUCT.format(name=name, ns=ns))
        lines.append("};")
        return "\n".join(lines)


_MEM_STRUCT = """\
    struct {name} {{
        typedef {ns}::value_type value_type;
        template <int WBW> static constexpr int lane_capacity() {{
            return {ns}::lane_capacity<WBW>();
        }}
        template <int WBW> static void read_framed_stream_lane(
            hls::stream<streamutils::framed_word<WBW> >& s, value_type* dst, int n,
            streamutils::tlast_status& tl) {{
#pragma HLS INLINE
            {ns}::read_framed_stream_lane<WBW>(s, dst, n, tl);
        }}
        template <int WBW> static void write_framed_stream_lane(
            const value_type* src, hls::stream<streamutils::framed_word<WBW> >& s, bool tlast,
            int n) {{
#pragma HLS INLINE
            {ns}::write_framed_stream_lane<WBW>(src, s, tlast, n);
        }}
    }};"""


def unique_traits(traits: Iterable[Traits]) -> list[Traits]:
    """``traits`` without repeats, in order; raises if two differ but share a family and an id."""
    seen: dict[tuple[str, int], Traits] = {}
    for t in traits:
        key = (t.family, t.id)
        if key in seen and seen[key] != t:
            raise ValueError(
                f"format id collision in {t.family}: {t.id} names two different format sets"
            )
        seen.setdefault(key, t)
    return list(seen.values())


def render_traits_header(
    traits: Iterable[Traits], include_dir: str | None = None
) -> str:
    """:data:`TRAITS_HEADER`: the primary template of every family and one specialization per id."""
    traits = unique_traits(traits)
    includes = []
    for t in traits:
        for elem in t.mem_elems(include_dir):
            inc = f'#include "{_array_utils_filename(elem)}"'
            if inc not in includes:
                includes.append(inc)
    families = list(dict.fromkeys(t.family for t in traits))
    body = [f"template <int ID> struct {fam};" for fam in families]
    body += [t.render(include_dir) for t in traits]
    return (
        "// GENERATED by waveflow.linalg.build -- do not edit.\n"
        "// The register, accumulator and memory element types of each linear-algebra component\n"
        "// instance, keyed by its format id (waveflow/linalg/formats.py).\n"
        "#ifndef WF_LINALG_TRAITS_H\n"
        "#define WF_LINALG_TRAITS_H\n"
        "#include <ap_fixed.h>\n"
        '#include "hls_stream.h"\n'
        '#include "streamutils_hls.h"\n'
        + "".join(f"{inc}\n" for inc in includes)
        + "\n"
        + "\n\n".join(body)
        + "\n\n#endif  // WF_LINALG_TRAITS_H\n"
    )
