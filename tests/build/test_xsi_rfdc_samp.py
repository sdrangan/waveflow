"""The C++ gate for the RFDC sample domain — quantize / dequantize / pack / unpack.

Compiled and run with a plain ``g++``: **no Vivado, no xsim**.  That is why ``xsi_rfdc_samp.h`` is a
separate header from the models that bind RTL pins — the bit-exactness claim is arithmetic, and a
claim that could only be checked inside a full toolchain run would in practice not be checked.

**This is a conformance twin, not a re-reading of the spec.**  Every expectation below is computed by
the *Python* path the ``Rfdc`` model actually uses — ``FixedField`` + ``from_real`` for quantization,
``DataArray.serialize`` for packing — and handed to C++ to reproduce.  So the two implementations of
"what is `ap_fixed<nbits,1>` with AP_RND/AP_SAT, packed time-ascending from the LSBs" are compared
against each other rather than each against a paragraph.  Nothing here asserts a layout someone read
off a datasheet; that is the point, because hand-rolled packing is the standing trap in this codebase
and it hides at the degenerate widths.

The widths swept below include ``samp_per_word == 1`` deliberately: a packer that "works" by treating
a word as a single sample passes every multi-sample case and fails only there.

They also include formats where the **effective** and **container** widths differ (14-in-16, the
ZU48DR's), in both justifications.  Those are the cases a `RfdcFormat` carrying one width could not
express at all, and the ones where a C++ ``>>`` on an unsigned type -- a logical shift, where the
Python is arithmetic -- turns every negative sample into a large positive one.  Every format is built
from :class:`~waveflow.hw.rfdc_samp_word.RfdcSampWord`, so the twin is compared against the type that
is now Python's source of truth rather than against a second transcription of it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from waveflow.hw.arrayutils import write_array
from waveflow.hw.fixpoint import from_real, to_real
from waveflow.hw.rfdc_samp_word import RfdcSampWord

_GXX = shutil.which("g++")
pytestmark = pytest.mark.skipif(_GXX is None, reason="g++ (mingw) not on PATH")

_XSI_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"

#: ``(bits_per_samp, bits_per_samp_pack, samp_per_word, justify)``.  LW=1 and the effective/container
#: splits are in here on purpose -- see the module docstring.  The first six rows are the formats
#: that predate the split, expressed unchanged; the last four are the split itself.
_FORMATS = [
    (8, 8, 4, "left"), (16, 16, 4, "left"), (16, 16, 1, "left"),
    (12, 12, 2, "left"), (16, 16, 2, "left"), (10, 10, 3, "left"),
    (14, 16, 4, "left"), (14, 16, 1, "left"),        # the ZU48DR's, MSB-aligned
    (14, 16, 4, "right"), (12, 16, 2, "right"),      # ...and the answer the lab might give instead
]

#: ``pytest.param`` ids that name the format rather than a tuple of integers.
_FMT_IDS = [f"{e}in{p}x{spw}_{j}" for e, p, spw, j in _FORMATS]

_PRELUDE = r"""
#include "xsi_rfdc_samp.h"
#include <cstdio>
#include <cstdlib>
#include <vector>
using namespace wfbfm;
"""


def _run_cpp(body: str, tmp_path: Path) -> str:
    src = tmp_path / "rfdc.cpp"
    src.write_text(_PRELUDE + body, encoding="utf-8")
    exe = tmp_path / "rfdc.exe"
    r = subprocess.run([_GXX, "-std=c++17", f"-I{_XSI_SRC}", str(src), "-o", str(exe)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"compile failed:\n{r.stderr}"
    r = subprocess.run([str(exe)], capture_output=True, text=True)
    assert r.returncode == 0, f"run failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout


def _word(eff: int, pack: int = 0, spw: int = 1, justify: str = "left"):
    """The word type this format is — the same class ``Rfdc`` is handed."""
    return RfdcSampWord.specialize(samp_per_word=spw, bits_per_samp=eff,
                                   bits_per_samp_pack=pack or eff, justify=justify)


def _cpp_fmt(W, full_scale: float = 1.0) -> str:
    """The five ``RfdcFormat`` assignments *W* implies, as C++ statements.

    Member assignment rather than aggregate init, because the tests below vary one field at a time
    and a positional literal would hide which.  The ORDER contract that the aggregate form depends on
    is pinned separately, by :func:`test_the_format_literal_rfdc_emits_reads_back_field_for_field`.
    """
    return (f"RfdcFormat f; f.nbits = {int(W.bits_per_samp)}; "
            f"f.samp_per_word = {int(W.samp_per_word)}; f.full_scale = {float(full_scale)!r}; "
            f"f.nbits_pack = {int(W.bits_per_samp_pack)}; "
            f"f.justify_shift = {int(W.justify_shift())};")


def _python_words(samples: np.ndarray, W, full_scale: float) -> np.ndarray:
    """The AXIS words Python produces for *samples* — the real path, not a reimplementation.

    Quantize at the EFFECTIVE width, justify into the container, then hand the container slots to
    the generated array serializer.  Exactly what ``Rfdc._pack`` does, and for the same reason: the
    shift is the word type's rule, the word<->slot layout is the serializer's.
    """
    stored = from_real(np.asarray(samples, dtype=np.float64) / full_scale, W.samp_type())
    words = write_array(W.to_slots(stored), elem_type=W.slot_type(), word_bw=W.bitwidth)
    return np.asarray(words, dtype=np.uint64).ravel()


def _python_stored(samples: np.ndarray, nbits: int, full_scale: float) -> np.ndarray:
    return np.asarray(from_real(np.asarray(samples, dtype=np.float64) / full_scale,
                                _word(nbits).samp_type()), dtype=np.int64)


# ---------------------------------------------------------------------------
# Quantization — the arithmetic contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("nbits", [8, 10, 12, 16])
def test_quantization_matches_fixedfield_on_the_hard_values(nbits, tmp_path):
    """Ties, saturation and both zeros — the values that separate rounding modes.

    A ``std::llround`` implementation passes a random sweep and fails here: it rounds half AWAY from
    zero, while AP_RND rounds half UP, so they disagree on exactly the negative ties.
    """
    f = _word(nbits).samp_type().get_format()
    lsb = 2.0 ** -f.frac_bits
    vals = np.array([
        0.0, -0.0, lsb / 2, -lsb / 2,               # the ties, both signs
        1.5 * lsb, -1.5 * lsb,
        0.5, -0.5, 0.25,
        1.0, -1.0,                                   # +1.0 saturates; -1.0 is exactly q_min
        2.0, -2.0, 1e3, -1e3,                        # far out of range: clip, never wrap
    ])
    want = _python_stored(vals, nbits, 1.0)

    body = f"""
int main() {{
    {_cpp_fmt(_word(nbits))}
    const double v[] = {{ {", ".join(repr(float(x)) for x in vals)} }};
    for (size_t i = 0; i < sizeof(v)/sizeof(v[0]); ++i)
        std::printf("%lld\\n", (long long)rfdc_quantize(v[i], f));
    return 0;
}}
"""
    got = np.array([int(x) for x in _run_cpp(body, tmp_path).split()], dtype=np.int64)
    assert got.tolist() == want.tolist(), (
        f"nbits={nbits}: C++ quantization disagrees with FixedField.\n"
        f"  values {vals.tolist()}\n  python {want.tolist()}\n  c++    {got.tolist()}")


# ---------------------------------------------------------------------------
# Packing — the layout contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("eff,pack,spw,justify", _FORMATS, ids=_FMT_IDS)
def test_packed_words_match_the_schema_serializer(eff, pack, spw, justify, tmp_path):
    """C++ packing == what the array serializer emits, over a random block, for every format.

    The oldest sample lands in the least significant slot, justified inside its container.  That is
    checked here against what the serializer emits rather than against the datasheet paragraph it
    came from.
    """
    W = _word(eff, pack, spw, justify)
    rng = np.random.default_rng(0xADC0 + eff * 16 + spw)
    n = spw * 8
    samples = rng.uniform(-1.0, 1.0, size=n)
    want = _python_words(samples, W, full_scale=1.0)

    body = f"""
int main() {{
    {_cpp_fmt(W)}
    const double s[] = {{ {", ".join(repr(float(x)) for x in samples)} }};
    const int n = {n};
    std::vector<uint64_t> w(n / f.samp_per_word);
    rfdc_pack(s, n, f, w.data());
    for (size_t i = 0; i < w.size(); ++i) std::printf("%llu\\n", (unsigned long long)w[i]);
    return 0;
}}
"""
    got = np.array([int(x) for x in _run_cpp(body, tmp_path).split()], dtype=np.uint64)
    assert got.tolist() == want.tolist(), (
        f"{W.describe()}: packed words differ.\n"
        f"  python {[hex(int(x)) for x in want]}\n  c++    {[hex(int(x)) for x in got]}")


@pytest.mark.parametrize("eff,pack,spw,justify", _FORMATS, ids=_FMT_IDS)
def test_unpack_inverts_python_packing(eff, pack, spw, justify, tmp_path):
    """The DAC direction: words Python packed, unpacked in C++, back to the quantized reals.

    Exact equality, not a tolerance: both sides are on the quantization grid, and a tolerance here
    would hide precisely the sign-extension bug this is looking for — and, on a split format, the
    second one, where undoing the justification with a LOGICAL shift turns every negative sample
    into a large positive one.
    """
    W = _word(eff, pack, spw, justify)
    rng = np.random.default_rng(0xDAC0 + eff * 16 + spw)
    n = spw * 8
    samples = rng.uniform(-1.0, 1.0, size=n)
    words = _python_words(samples, W, full_scale=1.0)
    want = to_real(from_real(samples, W.samp_type()))

    body = f"""
int main() {{
    {_cpp_fmt(W)}
    const uint64_t w[] = {{ {", ".join(str(int(x)) + "ULL" for x in words)} }};
    const int nw = {len(words)};
    std::vector<double> s(nw * f.samp_per_word);
    rfdc_unpack(w, nw, f, s.data());
    for (size_t i = 0; i < s.size(); ++i) std::printf("%.17g\\n", s[i]);
    return 0;
}}
"""
    got = np.array([float(x) for x in _run_cpp(body, tmp_path).split()], dtype=np.float64)
    assert np.array_equal(got, want), (
        f"{W.describe()}: unpacked samples differ from Python's dequantization")


def test_full_scale_scales_both_directions(tmp_path):
    """``full_scale`` is the amplitude reference, so it divides going in and multiplies coming out.

    Checked at a power of two, where the divide and multiply are both exact in binary floating point
    — the same reason ``write_scenario`` draws its samples on the quantization grid.
    """
    fs = 0.5
    W = _word(14, 16, 4)
    rng = np.random.default_rng(7)
    samples = rng.uniform(-fs, fs, size=int(W.samp_per_word) * 4)
    want = _python_words(samples, W, full_scale=fs)

    body = f"""
int main() {{
    {_cpp_fmt(W, fs)}
    const double s[] = {{ {", ".join(repr(float(x)) for x in samples)} }};
    const int n = {len(samples)};
    std::vector<uint64_t> w(n / f.samp_per_word);
    rfdc_pack(s, n, f, w.data());
    for (size_t i = 0; i < w.size(); ++i) std::printf("%llu\\n", (unsigned long long)w[i]);
    return 0;
}}
"""
    got = np.array([int(x) for x in _run_cpp(body, tmp_path).split()], dtype=np.uint64)
    assert got.tolist() == want.tolist()


# ---------------------------------------------------------------------------
# The literal's field ORDER -- the contract aggregate initialization depends on
# ---------------------------------------------------------------------------

def test_the_format_literal_rfdc_emits_reads_back_field_for_field(tmp_path):
    """``Rfdc._fmt_literal()`` aggregate-initializes ``RfdcFormat``, so the Python string's order IS
    the struct's declaration order.

    Nothing else checks that.  Reorder the struct, or insert a field in the middle of it, and every
    value silently lands in the wrong member -- ``nbits_pack`` becoming ``samp_per_word`` would not
    even fail to compile.  This compiles the literal Python actually emits and reads the five fields
    back, so the two cannot drift apart quietly.

    That is also why ``xsi_rfdc_samp.h`` APPENDS the two new fields rather than grouping the widths
    together, which would have read better and broken every literal already in a generated TB.
    """
    from examples.rf_loopback.rfdc import Rfdc
    from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
    from waveflow.simulation.simulation import Simulation

    W = Rfsoc4x2SampWord.specialize(samp_per_word=4)
    r = Rfdc(name="fmt", sim=Simulation(), word=W, full_scale=0.5)
    literal = r._fmt_literal()

    body = f"""
int main() {{
    const RfdcFormat f = {literal};
    std::printf("%d %d %.17g %d %d\\n", f.nbits, f.samp_per_word, f.full_scale,
                f.nbits_pack, f.justify_shift);
    return 0;
}}
"""
    got = _run_cpp(body, tmp_path).split()
    assert [int(got[0]), int(got[1]), float(got[2]), int(got[3]), int(got[4])] == [
        int(W.bits_per_samp), int(W.samp_per_word), 0.5,
        int(W.bits_per_samp_pack), int(W.justify_shift()),
    ], f"{literal} did not land field-for-field: C++ read back {got}"
