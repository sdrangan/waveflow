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

**And, since 2026-08-22, interleaved I/Q** (``plans/adc_model.md`` stage C).  A complex sample takes
two adjacent slots and ``iq_order`` says which of I and Q takes the lower one; everything else --
the quantizer, the justification, the word layout -- is unchanged, which is exactly the claim these
rows have to hold up.  Three things make that checkable rather than merely asserted:

* the expectations come from :func:`~waveflow.hw.rfdc_samp_word.pack`, the function ``Rfdc._pack``
  calls, so the interleave is compared against the one Python implementation rather than a second
  transcription of it;
* :func:`test_the_two_slot_orders_really_differ` is the guard on the guard -- every other assertion
  compares C++ to Python for ONE declared order, and each would still agree with itself if the C++
  ignored ``iq_order`` entirely;
* :func:`test_the_word_width_agrees_with_the_python_type` catches a ``word_bits()`` that forgot to
  double, which the packing tests cannot: they size their buffers from ``slots_per_word()``.

``iq_order``'s declared default is ``i_low`` and the board bring-up log has **evidence against it**,
so nothing here hard-codes a slot order: the value is read off ``RfdcFormat`` on both sides, and a
lab correction stays the one-field change it already is.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from waveflow.hw.fixpoint import from_real, to_real
from waveflow.hw.rfdc_samp_word import RfdcSampWord, pack

_GXX = shutil.which("g++")
pytestmark = pytest.mark.skipif(_GXX is None, reason="g++ (mingw) not on PATH")

_XSI_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"

#: ``(bits_per_samp, bits_per_samp_pack, samp_per_word, justify, iq_mode, iq_order)``.  LW=1 and the
#: effective/container splits are in here on purpose -- see the module docstring.  The first six rows
#: are the formats that predate the split, expressed unchanged; the next four are the split itself;
#: the last five are interleaved I/Q.
#:
#: **The I/Q rows sit at ``samp_per_word >= 2``**, which is where ``iq_order`` is pinned on the Python
#: side too (``tests/hw/test_rfdc_samp_word.py``): a rule about *pairs* wants more than one pair
#: before a mistake in it can look like anything but a relabelling.  The one ``samp_per_word == 1``
#: I/Q row is there for the other trap, not that one -- a packer that treats a word as a single
#: sample passes every multi-sample case, and at one complex sample per beat the word still holds two
#: slots, so it separates "handles a pair" from "handles a beat".
#:
#: Every I/Q row is chosen to land at **64 bits or less**, the width a converter port can carry:
#: ``bits_per_samp_pack * samp_per_word * 2``.  That is the same arithmetic that makes an I/Q design
#: halve ``samp_per_word`` to stay on the bus, so the rows are the geometries a design would really
#: use rather than arbitrary ones.
_FORMATS = [
    (8, 8, 4, "left", False, "i_low"), (16, 16, 4, "left", False, "i_low"),
    (16, 16, 1, "left", False, "i_low"), (12, 12, 2, "left", False, "i_low"),
    (16, 16, 2, "left", False, "i_low"), (10, 10, 3, "left", False, "i_low"),
    (14, 16, 4, "left", False, "i_low"), (14, 16, 1, "left", False, "i_low"),
    (14, 16, 4, "right", False, "i_low"), (12, 16, 2, "right", False, "i_low"),
    # --- interleaved I/Q ---------------------------------------------------------------------
    # The 4x2's I/Q geometry: 2 complex samples per 64-bit beat, 14-in-16, both slot orders.  The
    # second is the one the bring-up log has evidence FOR, against the declared default.
    (14, 16, 2, "left", True, "i_low"), (14, 16, 2, "left", True, "q_low"),
    (16, 16, 2, "left", True, "q_low"),               # no split: the order rule on its own
    (12, 16, 2, "right", True, "q_low"),              # split + right-justified + q_low, all at once
    (8, 8, 4, "left", True, "i_low"),                 # 4 complex samples, 8 slots, exactly 64 bits
    (16, 16, 1, "left", True, "q_low"),               # one complex sample per beat -- see above
]

#: ``pytest.param`` ids that name the format rather than a tuple of integers.
_FMT_IDS = [f"{e}in{p}x{spw}_{j}" + (f"_iq_{o}" if iq else "")
            for e, p, spw, j, iq, o in _FORMATS]

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


def _word(eff: int, pack: int = 0, spw: int = 1, justify: str = "left",
          iq_mode: bool = False, iq_order: str = "i_low"):
    """The word type this format is — the same class ``Rfdc`` is handed."""
    return RfdcSampWord.specialize(samp_per_word=spw, bits_per_samp=eff,
                                   bits_per_samp_pack=pack or eff, justify=justify,
                                   iq_mode=iq_mode, iq_order=iq_order)


def _cpp_fmt(W, full_scale: float = 1.0) -> str:
    """The seven ``RfdcFormat`` assignments *W* implies, as C++ statements.

    Member assignment rather than aggregate init, because the tests below vary one field at a time
    and a positional literal would hide which.  The ORDER contract that the aggregate form depends on
    is pinned separately, by :func:`test_the_format_literal_rfdc_emits_reads_back_field_for_field`.

    The I/Q pair is written with the header's **named constants**, so this file never states which
    integer ``q_low`` is — that mapping lives in one place, and a test that restated it could agree
    with itself while disagreeing with what ``Rfdc`` emits.
    """
    return (f"RfdcFormat f; f.nbits = {int(W.bits_per_samp)}; "
            f"f.samp_per_word = {int(W.samp_per_word)}; f.full_scale = {float(full_scale)!r}; "
            f"f.nbits_pack = {int(W.bits_per_samp_pack)}; "
            f"f.justify_shift = {int(W.justify_shift())}; "
            f"f.iq_mode = {'RFDC_IQ' if W.iq_mode else 'RFDC_REAL'}; "
            f"f.iq_order = {'RFDC_I_LOW' if W.iq_order == 'i_low' else 'RFDC_Q_LOW'};")


def _components(samples: np.ndarray, W) -> np.ndarray:
    """*samples* as the flat ``double`` stream C++ is handed.

    Real: itself.  I/Q: ``(re, im)`` **adjacent**, one pair per complex sample — the same layout the
    RF bundle stores a complex block in, so this is not a convention invented for the gate.

    Note this is the ARRIVAL order, not the slot order: whether I or Q lands in the lower slot is
    ``iq_order``'s business and belongs on the far side of the packer, in both languages.
    """
    arr = np.asarray(samples)
    if not W.iq_mode:
        return np.asarray(arr, dtype=np.float64).ravel()
    return np.stack([arr.real, arr.imag], axis=-1).ravel().astype(np.float64)


def _python_words(samples: np.ndarray, W, full_scale: float) -> np.ndarray:
    """The AXIS words Python produces for *samples* — the real path, not a reimplementation.

    Quantize at the EFFECTIVE width, then hand the stored integers to
    :func:`~waveflow.hw.rfdc_samp_word.pack`, which is what ``Rfdc._pack`` calls. Going through
    ``pack`` rather than re-composing ``to_slots`` + ``write_array`` here matters now that there is a
    second rule to get right: the I/Q interleave is inside ``pack``, and a gate that re-composed the
    steps would be comparing C++ against a *second* Python transcription instead of against the one
    the converter uses.

    I and Q are quantized **separately and identically** — two real values of the same converter.
    """
    x = np.asarray(samples) / full_scale
    if W.iq_mode:
        stored = (np.asarray(from_real(x.real, W.samp_type()), dtype=np.int64)
                  + 1j * np.asarray(from_real(x.imag, W.samp_type()), dtype=np.int64))
    else:
        stored = np.asarray(from_real(x, W.samp_type()), dtype=np.int64)
    return np.asarray(pack(W, stored.reshape(1, -1))[0], dtype=np.uint64).ravel()


def _python_components(samples: np.ndarray, W, full_scale: float) -> np.ndarray:
    """The dequantized components Python expects back, in the same order :func:`_components` uses."""
    x = np.asarray(samples) / full_scale
    if W.iq_mode:
        q = (to_real(from_real(x.real, W.samp_type()))
             + 1j * to_real(from_real(x.imag, W.samp_type())))
    else:
        q = to_real(from_real(x, W.samp_type()))
    return _components(q * full_scale, W)


def _samples(rng, W, n_samp: int) -> np.ndarray:
    """*n_samp* samples of the kind *W* carries — complex when it is interleaved."""
    if W.iq_mode:
        return rng.uniform(-1.0, 1.0, size=n_samp) + 1j * rng.uniform(-1.0, 1.0, size=n_samp)
    return rng.uniform(-1.0, 1.0, size=n_samp)


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

@pytest.mark.parametrize("eff,packw,spw,justify,iq_mode,iq_order", _FORMATS, ids=_FMT_IDS)
def test_packed_words_match_the_schema_serializer(eff, packw, spw, justify, iq_mode, iq_order,
                                                  tmp_path):
    """C++ packing == what Python's ``pack`` emits, over a random block, for every format.

    The oldest sample lands in the least significant slot, justified inside its container; with
    ``iq_mode`` each sample occupies two adjacent slots and ``iq_order`` says which of I and Q takes
    the lower.  Both are checked against what Python produces rather than against the datasheet
    paragraph they came from.
    """
    W = _word(eff, packw, spw, justify, iq_mode, iq_order)
    rng = np.random.default_rng(0xADC0 + eff * 16 + spw + (0x100 if iq_mode else 0))
    n_samp = spw * 8
    samples = _samples(rng, W, n_samp)
    want = _python_words(samples, W, full_scale=1.0)
    comps = _components(samples, W)

    body = f"""
int main() {{
    {_cpp_fmt(W)}
    const double s[] = {{ {", ".join(repr(float(x)) for x in comps)} }};
    const int n = {len(comps)};
    std::vector<uint64_t> w(n / f.slots_per_word());
    rfdc_pack(s, n, f, w.data());
    for (size_t i = 0; i < w.size(); ++i) std::printf("%llu\\n", (unsigned long long)w[i]);
    return 0;
}}
"""
    got = np.array([int(x) for x in _run_cpp(body, tmp_path).split()], dtype=np.uint64)
    assert got.tolist() == want.tolist(), (
        f"{W.describe()}: packed words differ.\n"
        f"  python {[hex(int(x)) for x in want]}\n  c++    {[hex(int(x)) for x in got]}")


@pytest.mark.parametrize("eff,packw,spw,justify,iq_mode,iq_order", _FORMATS, ids=_FMT_IDS)
def test_unpack_inverts_python_packing(eff, packw, spw, justify, iq_mode, iq_order, tmp_path):
    """The DAC direction: words Python packed, unpacked in C++, back to the quantized components.

    Exact equality, not a tolerance: both sides are on the quantization grid, and a tolerance here
    would hide precisely the sign-extension bug this is looking for — and, on a split format, the
    second one, where undoing the justification with a LOGICAL shift turns every negative sample
    into a large positive one.

    It is also the only check that can catch an I/Q **de**-interleave that disagrees with the
    interleave: the words come from Python, so a C++ pack and unpack that were wrong in the same way
    would still fail here.
    """
    W = _word(eff, packw, spw, justify, iq_mode, iq_order)
    rng = np.random.default_rng(0xDAC0 + eff * 16 + spw + (0x100 if iq_mode else 0))
    samples = _samples(rng, W, spw * 8)
    words = _python_words(samples, W, full_scale=1.0)
    want = _python_components(samples, W, full_scale=1.0)

    body = f"""
int main() {{
    {_cpp_fmt(W)}
    const uint64_t w[] = {{ {", ".join(str(int(x)) + "ULL" for x in words)} }};
    const int nw = {len(words)};
    std::vector<double> s(nw * f.slots_per_word());
    rfdc_unpack(w, nw, f, s.data());
    for (size_t i = 0; i < s.size(); ++i) std::printf("%.17g\\n", s[i]);
    return 0;
}}
"""
    got = np.array([float(x) for x in _run_cpp(body, tmp_path).split()], dtype=np.float64)
    assert np.array_equal(got, want), (
        f"{W.describe()}: unpacked components differ from Python's dequantization")


@pytest.mark.parametrize("eff,packw,spw,justify,iq_mode,iq_order", _FORMATS, ids=_FMT_IDS)
def test_the_word_width_agrees_with_the_python_type(eff, packw, spw, justify, iq_mode, iq_order,
                                                    tmp_path):
    """``word_bits()`` == ``RfdcSampWord.bitwidth`` — the one number both sides derive separately.

    Cheap, and it is the check that would have caught an I/Q ``word_bits()`` that forgot to double:
    the pack tests above size their buffer from ``slots_per_word()``, so a wrong ``word_bits()``
    would not show up there at all. It is also what the AXIS port's width is set from on the Python
    side, so a disagreement is a bus that does not fit its own beats.
    """
    W = _word(eff, packw, spw, justify, iq_mode, iq_order)
    body = f"""
int main() {{
    {_cpp_fmt(W)}
    std::printf("%d %d\\n", f.word_bits(), f.slots_per_word());
    return 0;
}}
"""
    got = [int(x) for x in _run_cpp(body, tmp_path).split()]
    assert got == [int(W.bitwidth), int(W.slots_per_word())], (
        f"{W.describe()}: C++ says word_bits/slots_per_word = {got}, Python says "
        f"{[int(W.bitwidth), int(W.slots_per_word())]}")


def test_the_two_slot_orders_really_differ(tmp_path):
    """The guard on the I/Q gate itself: ``i_low`` and ``q_low`` must not pack the same.

    Every assertion above compares C++ against Python for **one** declared order. If the C++ ignored
    ``iq_order`` entirely and Python's rows happened to be compared independently, each would still
    agree with itself. This is the check that says the field is load-bearing — and it is written in
    C++ alone, because what is under test is that *this header* reads the field.

    The two orders are a swap of adjacent slots, so they differ on any sample where I != Q.
    """
    W_i = _word(14, 16, 2, "left", True, "i_low")
    W_q = _word(14, 16, 2, "left", True, "q_low")
    comps = [0.25, -0.5, 0.75, 0.125]          # (re, im) adjacent, all four distinct

    body = f"""
int main() {{
    {{ {_cpp_fmt(W_i)}
      const double s[] = {{ {", ".join(repr(x) for x in comps)} }};
      uint64_t w = 0; rfdc_pack(s, 4, f, &w);
      std::printf("%llu\\n", (unsigned long long)w); }}
    {{ {_cpp_fmt(W_q)}
      const double s[] = {{ {", ".join(repr(x) for x in comps)} }};
      uint64_t w = 0; rfdc_pack(s, 4, f, &w);
      std::printf("%llu\\n", (unsigned long long)w); }}
    return 0;
}}
"""
    i_low, q_low = (int(x) for x in _run_cpp(body, tmp_path).split())
    assert i_low != q_low, (
        "i_low and q_low packed the same word, so the C++ is not reading iq_order at all")
    # ...and each is what Python says, so "they differ" is not "they are both wrong differently".
    for W, got in ((W_i, i_low), (W_q, q_low)):
        x = np.asarray(comps[0::2]) + 1j * np.asarray(comps[1::2])
        assert got == int(_python_words(x, W, 1.0)[0]), f"{W.describe()}"


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
    std::printf("%d %d %.17g %d %d %d %d\\n", f.nbits, f.samp_per_word, f.full_scale,
                f.nbits_pack, f.justify_shift, f.iq_mode, f.iq_order);
    return 0;
}}
"""
    got = _run_cpp(body, tmp_path).split()
    assert [int(got[0]), int(got[1]), float(got[2]), int(got[3]), int(got[4]),
            int(got[5]), int(got[6])] == [
        int(W.bits_per_samp), int(W.samp_per_word), 0.5,
        int(W.bits_per_samp_pack), int(W.justify_shift()),
        0, 0,                                # RFDC_REAL, RFDC_I_LOW -- this word is real
    ], f"{literal} did not land field-for-field: C++ read back {got}"


def test_the_format_literal_carries_the_iq_rules_by_name(tmp_path):
    """The I/Q half of the same contract, at the geometry stage D's gate uses.

    ``_fmt_literal`` emits ``RFDC_IQ`` / ``RFDC_Q_LOW`` rather than ``1`` / ``1``. That is not
    decoration: a bare integer in the sixth or seventh position of an aggregate initializer can
    be transposed without failing to compile, and ``iq_order`` is the field this project most
    expects the lab to correct -- a generated line that says ``RFDC_Q_LOW`` says what was
    assumed.

    The names are resolved by the C++ compiler here, so this also pins that they exist and mean
    what the Python side thinks. The whole literal is still not an identifier, so the harness's
    promote-bare-identifiers rule does not see them.
    """
    from examples.rf_loopback.rfdc import Rfdc
    from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord

    W = Rfsoc4x2SampWord.specialize(samp_per_word=2, iq_mode=True, iq_order="q_low")
    assert int(W.bitwidth) == 64, "the 4x2 I/Q geometry: 2 complex samples on a 64-bit bus"
    # The CONVERTER still refuses this word (stage D). The literal is a property of the word
    # type and the amplitude reference, so it is read off the same method without standing up a
    # converter that cannot exist yet -- which is also the honest statement of what is left:
    # the twin is ready before the thing that will use it.
    literal = Rfdc._fmt_literal(type("_", (), {"word": W, "full_scale": 1.0})())
    assert "RFDC_IQ" in literal and "RFDC_Q_LOW" in literal, literal
    assert not literal.isidentifier()

    body = f"""
int main() {{
    const RfdcFormat f = {literal};
    std::printf("%d %d %d %d\\n", f.iq_mode, f.iq_order, f.slots_per_word(), f.word_bits());
    return 0;
}}
"""
    got = [int(x) for x in _run_cpp(body, tmp_path).split()]
    assert got == [1, 1, int(W.slots_per_word()), int(W.bitwidth)], (
        f"{literal} did not land field-for-field: C++ read back {got}")
