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
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from waveflow.hw.fixpoint import FixedField, from_real, to_real
from waveflow.utils.fixputils import OMode, QMode

_GXX = shutil.which("g++")
pytestmark = pytest.mark.skipif(_GXX is None, reason="g++ (mingw) not on PATH")

_XSI_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"

#: (nbits, samp_per_word).  LW=1 is in here on purpose -- see the module docstring.
_FORMATS = [(8, 4), (16, 4), (16, 1), (12, 2), (16, 2), (10, 3)]

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


def _samp_type(nbits: int):
    """The element type ``Rfdc`` quantizes to — kept identical to ``rfdc.py``."""
    return FixedField.specialize(nbits, 1, signed=True, q_mode=QMode.AP_RND, o_mode=OMode.AP_SAT)


def _python_words(samples: np.ndarray, nbits: int, spw: int, full_scale: float) -> np.ndarray:
    """The AXIS words Python produces for *samples* — the real path, not a reimplementation."""
    da = from_real(np.asarray(samples, dtype=np.float64) / full_scale, _samp_type(nbits))
    return np.asarray(da.serialize(word_bw=nbits * spw), dtype=np.uint64)


def _python_stored(samples: np.ndarray, nbits: int, full_scale: float) -> np.ndarray:
    return np.asarray(from_real(np.asarray(samples, dtype=np.float64) / full_scale,
                                _samp_type(nbits)), dtype=np.int64)


# ---------------------------------------------------------------------------
# Quantization — the arithmetic contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("nbits", [8, 10, 12, 16])
def test_quantization_matches_fixedfield_on_the_hard_values(nbits, tmp_path):
    """Ties, saturation and both zeros — the values that separate rounding modes.

    A ``std::llround`` implementation passes a random sweep and fails here: it rounds half AWAY from
    zero, while AP_RND rounds half UP, so they disagree on exactly the negative ties.
    """
    f = _samp_type(nbits).get_format()
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
    RfdcFormat f; f.nbits = {nbits}; f.samp_per_word = 1; f.full_scale = 1.0;
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

@pytest.mark.parametrize("nbits,spw", _FORMATS)
def test_packed_words_match_the_schema_serializer(nbits, spw, tmp_path):
    """C++ packing == ``DataArray.serialize``, over a random block, for every width.

    The oldest sample lands in the least significant slot.  That is checked here against what the
    serializer emits rather than against the datasheet paragraph it came from.
    """
    rng = np.random.default_rng(0xADC0 + nbits * 16 + spw)
    n = spw * 8
    samples = rng.uniform(-1.0, 1.0, size=n)
    want = _python_words(samples, nbits, spw, full_scale=1.0)

    body = f"""
int main() {{
    RfdcFormat f; f.nbits = {nbits}; f.samp_per_word = {spw}; f.full_scale = 1.0;
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
        f"nbits={nbits} samp_per_word={spw}: packed words differ.\n"
        f"  python {[hex(int(x)) for x in want]}\n  c++    {[hex(int(x)) for x in got]}")


@pytest.mark.parametrize("nbits,spw", _FORMATS)
def test_unpack_inverts_python_packing(nbits, spw, tmp_path):
    """The DAC direction: words Python packed, unpacked in C++, back to the quantized reals.

    Exact equality, not a tolerance: both sides are on the quantization grid, and a tolerance here
    would hide precisely the sign-extension bug this is looking for.
    """
    rng = np.random.default_rng(0xDAC0 + nbits * 16 + spw)
    n = spw * 8
    samples = rng.uniform(-1.0, 1.0, size=n)
    words = _python_words(samples, nbits, spw, full_scale=1.0)
    want = to_real(from_real(samples, _samp_type(nbits)))

    body = f"""
int main() {{
    RfdcFormat f; f.nbits = {nbits}; f.samp_per_word = {spw}; f.full_scale = 1.0;
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
        f"nbits={nbits} samp_per_word={spw}: unpacked samples differ from Python's dequantization")


def test_full_scale_scales_both_directions(tmp_path):
    """``full_scale`` is the amplitude reference, so it divides going in and multiplies coming out.

    Checked at a power of two, where the divide and multiply are both exact in binary floating point
    — the same reason ``write_scenario`` draws its samples on the quantization grid.
    """
    nbits, spw, fs = 16, 4, 0.5
    rng = np.random.default_rng(7)
    samples = rng.uniform(-fs, fs, size=spw * 4)
    want = _python_words(samples, nbits, spw, full_scale=fs)

    body = f"""
int main() {{
    RfdcFormat f; f.nbits = {nbits}; f.samp_per_word = {spw}; f.full_scale = {fs!r};
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
