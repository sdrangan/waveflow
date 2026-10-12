"""The array layout rule inside a message (plans/stream_array_alignment.md).

An array field starts on a fresh word and closes its last word, so the field after it starts on a
fresh word too; inside, ``pf = word_bw // elem_bw`` elements share a word, element 0 in the low
bits.  The Python serializer, ``nwords``, and every generated C++ path (``write_array`` /
``write_stream`` / ``write_axi4_stream`` and their readers) follow it, at every ``word_bw``.

The Python checks always run.  ``test_cpp_matches_python`` compiles the generated headers with
Vivado's MinGW (as ``tests/build/test_sw_schema.py`` does) and checks the C++ word for word; it
skips without that toolchain.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

from waveflow.hw.arrayutils import read_array, write_array
from waveflow.hw.dataschema import DataArray, DataList, FloatField, IntField

INCLUDE_DIR = "include"
F32 = FloatField.specialize(bitwidth=32, include_dir=INCLUDE_DIR)
U16 = IntField.specialize(bitwidth=16, signed=False, include_dir=INCLUDE_DIR)
I16 = IntField.specialize(bitwidth=16, signed=True, include_dir=INCLUDE_DIR)
I8 = IntField.specialize(bitwidth=8, signed=True, include_dir=INCLUDE_DIR)


def _arr(name, elem, shape):
    """A named static array, so each gets its own generated header."""
    return type(name, (DataArray,), {"element_type": elem, "max_shape": shape, "static": True,
                                     "include_dir": INCLUDE_DIR})


class AlPair(DataList):                    # 16 bits: a composite element several to a word
    include_dir = INCLUDE_DIR
    elements = {"a": {"schema": I8}, "b": {"schema": I8}}


class AlWide(DataList):                    # 48 bits: wider than a 32-bit word, one lane at 64
    include_dir = INCLUDE_DIR
    elements = {"tag": {"schema": U16}, "x": {"schema": F32}}


Coeff4 = _arr("AlCoeff4", F32, (4,))
Coeff3 = _arr("AlCoeff3", F32, (3,))
Coeff3x1 = _arr("AlCoeff3x1", F32, (3, 1))
Byte5 = _arr("AlByte5", I8, (5,))
Short3 = _arr("AlShort3", I16, (3,))
Pair3 = _arr("AlPair3", AlPair, (3,))
Wide2 = _arr("AlWide2", AlWide, (2,))
ARRAYS = [Coeff4, Coeff3, Coeff3x1, Byte5, Short3, Pair3, Wide2]


class AlPolyHdr(DataList):                 # hwdesign's poly command header: scalar, array, scalar
    include_dir = INCLUDE_DIR
    elements = {"tx_id": {"schema": U16}, "coeffs": {"schema": Coeff4}, "nsamp": {"schema": U16}}


class AlArrFirst(DataList):
    include_dir = INCLUDE_DIR
    elements = {"coeffs": {"schema": Coeff4}, "nsamp": {"schema": U16}}


class AlArrLast(DataList):                 # array last, an odd count: a half-filled last word
    include_dir = INCLUDE_DIR
    elements = {"tx_id": {"schema": U16}, "coeffs": {"schema": Coeff3}}


class AlArrLast2d(DataList):               # array last, the multi-dimensional emitter
    include_dir = INCLUDE_DIR
    elements = {"tx_id": {"schema": U16}, "c": {"schema": Coeff3x1}}


class AlSubWordMid(DataList):              # sub-word elements: wrong at 32 bits before the fix
    include_dir = INCLUDE_DIR
    elements = {"tx_id": {"schema": U16}, "v": {"schema": Byte5}, "nsamp": {"schema": U16}}


class AlSubWordFirst(DataList):
    include_dir = INCLUDE_DIR
    elements = {"v": {"schema": Short3}, "nsamp": {"schema": U16}}


class AlTwoArr(DataList):                  # two arrays back to back, the last one ending the message
    include_dir = INCLUDE_DIR
    elements = {"a": {"schema": Coeff3}, "b": {"schema": Short3}}


class AlPairArr(DataList):
    include_dir = INCLUDE_DIR
    elements = {"tx_id": {"schema": U16}, "p": {"schema": Pair3}, "nsamp": {"schema": U16}}


class AlWideArr(DataList):
    include_dir = INCLUDE_DIR
    elements = {"tx_id": {"schema": U16}, "e": {"schema": Wide2}}


def _samples():
    c4 = np.array([0.0, 1.0, 0.0, -0.5], np.float32)
    c3 = np.array([1.5, -2.25, 7.0], np.float32)
    return [
        AlPolyHdr(tx_id=0x10, coeffs=c4, nsamp=40),
        AlArrFirst(coeffs=c4, nsamp=40),
        AlArrLast(tx_id=0x10, coeffs=c3),
        AlArrLast2d(tx_id=0x10, c=c3.reshape(3, 1)),
        AlSubWordMid(tx_id=0x10, v=np.array([1, -2, 3, -4, 5], np.int8), nsamp=40),
        AlSubWordFirst(v=np.array([1, -2, 3], np.int16), nsamp=40),
        AlTwoArr(a=c3, b=np.array([-1, 2, -3], np.int16)),
        AlPairArr(tx_id=7, p=[{"a": 1, "b": -1}, {"a": 2, "b": -2}, {"a": 3, "b": -3}], nsamp=40),
        AlWideArr(tx_id=7, e=[{"tag": 0xA5, "x": 1.5}, {"tag": 0x5A, "x": -2.25}]),
    ]


SAMPLES = _samples()
IDS = [type(s).__name__ for s in SAMPLES]


# -- the layout rule, in Python ---------------------------------------------------------------

def test_poly_header_words_at_64():
    """The words the generated C++ put on the wire in hwdesign's 64-bit poly co-simulation."""
    w = [int(v) for v in SAMPLES[0].serialize(word_bw=64)]
    assert w == [0x10, 0x3F80000000000000, 0xBF00000000000000, 0x28]


def test_poly_header_decodes_the_recorded_wire_words():
    """The recorded words, stale bits included (the top of the last word held the last
    coefficient before the stale-bit fix); readers ignore the bits past ``nsamp``."""
    wire = [0x0000000000000010, 0x3F80000000000000, 0xBF00000000000000, 0xBF00000000000028]
    got = AlPolyHdr().deserialize(wire, word_bw=64)
    assert int(got.tx_id) == 0x10 and int(got.nsamp) == 40
    np.testing.assert_array_equal(np.asarray(got.coeffs, np.float32), [0, 1, 0, -0.5])


@pytest.mark.parametrize("word_bw", [32, 64])
def test_sub_word_array_starts_and_ends_on_a_word(word_bw):
    w = [int(v) for v in SAMPLES[4].serialize(word_bw=word_bw)]
    lanes = [1, -2, 3, -4, 5]
    packed = [0, 0]
    for k, v in enumerate(lanes):
        packed[k // (word_bw // 8)] |= (v & 0xFF) << (8 * (k % (word_bw // 8)))
    expect = [0x10] + packed[: -(-5 // (word_bw // 8))] + [40]
    assert w == expect


@pytest.mark.parametrize("word_bw", [32, 64])
@pytest.mark.parametrize("obj", SAMPLES, ids=IDS)
def test_nwords_and_round_trip(obj, word_bw):
    cls = type(obj)
    words = obj.serialize(word_bw=word_bw)
    assert len(words) == cls.nwords_per_inst(word_bw)
    assert cls().deserialize(words, word_bw=word_bw).is_close(obj)
    # the same words through a Python list of ints
    assert cls().deserialize([int(v) for v in words], word_bw=word_bw).is_close(obj)


def test_uint32_file_round_trip(tmp_path):
    obj = SAMPLES[4]
    path = obj.write_uint32_file(tmp_path / "w.bin")
    assert np.array_equal(np.fromfile(path, dtype="<u4"), obj.serialize(word_bw=32))
    assert AlSubWordMid().read_uint32_file(path).is_close(obj)


def test_stream_inband_poly_cmd_hdr_at_64():
    """examples/stream_inband's PolyCmdHdr puts coeffs last behind 33 header bits, so the first
    coefficient never fit in word 0: it was right before the fix and still is."""
    from examples.stream_inband.poly import PolyCmdHdr

    hdr = PolyCmdHdr(cmd_type=1, tx_id=0x10, nsamp=40, coeffs=np.array([1, 2, 3, 4], np.float32))
    w = [int(v) for v in hdr.serialize(word_bw=64)]
    assert PolyCmdHdr.nwords_per_inst(64) == 3
    assert w == [0x500021, 0x400000003F800000, 0x4080000040400000]


# -- the generated C++, as text -----------------------------------------------------------------

def _impl(src: str, name: str, bw: int) -> str:
    m = re.search(rf"static void {name}\(word_bw_tag<{bw}>.*?\n\n", src, re.S)
    assert m, f"{name}<{bw}> not found"
    return m.group(0)


@pytest.mark.parametrize("word_bw", [32, 64])
@pytest.mark.parametrize("cls", [type(s) for s in SAMPLES], ids=IDS)
def test_write_array_never_overruns_nwords(cls, word_bw):
    body = _impl(cls._gen_include_decl(word_bw_supported=[word_bw]), "write_array_impl", word_bw)
    idx = [int(i) for i in re.findall(r"\bx\[(\d+)\]", body)]
    assert all(i < cls.nwords_per_inst(word_bw) for i in idx), (idx, cls.nwords_per_inst(word_bw))


@pytest.mark.parametrize("word_bw", [32, 64])
def test_tlast_only_on_the_final_beat_when_the_array_is_last(word_bw):
    body = _impl(AlArrLast.gen_write(dst_type="axi4_stream", word_bw_supported=[word_bw]),
                 "write_axi4_stream_impl", word_bw)
    beats = re.findall(r"write_axi4_word<\d+>\(s, w, (.*?)\);", body)
    assert beats[0] == "false"                       # tx_id's word
    assert all(b.startswith("tlast && (") for b in beats[1:]), beats
    assert "@last" not in body


def test_tlast_on_the_trailing_scalar_when_the_array_is_not_last():
    body = _impl(AlPolyHdr.gen_write(dst_type="axi4_stream", word_bw_supported=[64]),
                 "write_axi4_stream_impl", 64)
    beats = re.findall(r"write_axi4_word<\d+>\(s, w, (.*?)\);", body)
    assert beats == ["false", "false", "tlast"]


def test_only_the_last_of_two_arrays_carries_tlast():
    body = _impl(AlTwoArr.gen_write(dst_type="axi4_stream", word_bw_supported=[64]),
                 "write_axi4_stream_impl", 64)
    beats = re.findall(r"write_axi4_word<\d+>\(s, w, (.*?)\);", body)
    assert beats[0] == "false" and beats[1].startswith("tlast && ("), beats


@pytest.mark.parametrize("dst", ["stream", "axi4_stream"])
def test_word_after_an_array_starts_clean(dst):
    """The array loop leaves its last pair in ``w``; it is cleared before ``nsamp`` is packed."""
    name = "write_stream_impl" if dst == "stream" else "write_axi4_stream_impl"
    body = _impl(AlPolyHdr.gen_write(dst_type=dst, word_bw_supported=[64]), name, 64)
    tail = body[body.rindex("out_idx++;"):]
    assert tail.index("w = 0;") < tail.index("w.range(15, 0) = self->nsamp;")


# -- packed words given as lists or signed arrays --------------------------------------------------

@pytest.mark.parametrize("word_bw", [32, 64])
@pytest.mark.parametrize("form", ["uint_array", "int_array", "uint_list", "int_list"])
def test_read_array_word_forms(word_bw, form):
    x = np.array([1.5, -2.25, -0.1, 3e-5, -1e30, 7.0, -7.0], np.float32)
    w = np.asarray(write_array(x, elem_type=F32, word_bw=word_bw))
    signed = w.view(np.int32 if word_bw == 32 else np.int64)
    packed = {"uint_array": w, "int_array": signed,
              "uint_list": [int(v) for v in w], "int_list": [int(v) for v in signed]}[form]
    got = read_array(packed, elem_type=F32, word_bw=word_bw, shape=7).val
    np.testing.assert_array_equal(np.asarray(got, np.float32), x)


@pytest.mark.parametrize("word_bw", [32, 64])
@pytest.mark.parametrize("form", ["uint_array", "int_array", "uint_list", "int_list"])
def test_from_words_numpy_word_forms(word_bw, form):
    x = np.array([1.5, -2.25, -0.1, 3e-5, -1e30, 7.0, -7.0], np.float32)
    w = np.asarray(write_array(x, elem_type=F32, word_bw=word_bw))
    signed = w.view(np.int32 if word_bw == 32 else np.int64)
    packed = {"uint_array": w, "int_array": signed,
              "uint_list": [int(v) for v in w], "int_list": [int(v) for v in signed]}[form]
    np.testing.assert_array_equal(F32.from_words_numpy(packed, 7, word_bw), x)


def test_float64_words_from_a_list():
    """One element per word: the lane-1 fast path, where a list used to become float64."""
    F64 = FloatField.specialize(bitwidth=64)
    x = np.array([1.0 + 2**-52, -3.5, 1e300, -1e-300], np.float64)
    w = np.asarray(write_array(x, elem_type=F64, word_bw=64))
    lst = [int(v) for v in w]
    np.testing.assert_array_equal(F64.from_words_numpy(lst, 4, 64), x)
    np.testing.assert_array_equal(read_array(lst, elem_type=F64, word_bw=64, shape=4).val, x)


# -- the generated C++, compiled -------------------------------------------------------------------

def _mingw62() -> Path | None:
    for root in sorted(Path("C:/Xilinx").glob("*/Vivado/tps/mingw/6.2.0/win64.o/nt/bin")):
        if (root / "g++.exe").is_file():
            return root / "g++.exe"
    return None


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _gen_headers(root: Path) -> None:
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.streamutils import StreamUtilsStep
    from waveflow.hw.arrayutils import ArrayUtilsStep
    from waveflow.hw.dataschema import DataSchemaStep

    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    for cls in [AlPair, AlWide] + ARRAYS + [type(s) for s in SAMPLES]:
        dag.add(DataSchemaStep(cls, word_bw_supported=[32, 64], include_dir=INCLUDE_DIR))
    for elem in (F32, I16, I8, AlPair, AlWide):
        dag.add(ArrayUtilsStep(elem, [32, 64]))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = {k: r for k, r in res.items() if not r.success}
    assert not bad, bad


def test_cpp_matches_python(tmp_path):
    """For every schema at 32 and 64 bits, from the Python words P: ``read_array`` into the struct,
    then every writer must reproduce P exactly -- ``write_array`` within ``nwords`` (a guard word
    after it stays untouched), ``write_stream``, and ``write_axi4_stream`` with TLAST on the last
    beat only; and every reader (``read_stream``, ``read_axi4_stream``) must decode P to the same
    struct."""
    from waveflow.toolchain.toolchain import find_vitis_include_dir

    gxx, vitis_inc = _mingw62(), find_vitis_include_dir()
    if gxx is None or vitis_inc is None:
        pytest.skip("needs Vivado's MinGW and Vitis (ap_int.h, hls_stream.h)")
    _gen_headers(tmp_path)

    includes = "\n".join(f'#include "{_snake(type(s).__name__)}.h"' for s in SAMPLES)
    calls = []
    for s in SAMPLES:
        name = type(s).__name__
        for bw in (32, 64):
            words = ", ".join(f"{int(w)}ull" for w in s.serialize(word_bw=bw))
            calls.append(f'    check<{name}, {bw}>("{name}/{bw}", {{{words}}});')
    prog = f"""
#include <cstdio>
#include <vector>
#include <cstdint>
#include "streamutils_hls.h"
{includes}
static void dump(const char* tag, const char* what, const std::vector<uint64_t>& w) {{
    std::printf("%s %s", tag, what);
    for (uint64_t x : w) std::printf(" %llu", (unsigned long long)x);
    std::printf("\\n");
}}
template <class S, int W> static std::vector<uint64_t> drain(hls::stream<ap_uint<W>>& s) {{
    std::vector<uint64_t> out; while (!s.empty()) out.push_back((uint64_t)s.read().to_uint64()); return out;
}}
template <class S, int W> static void words_of(const char* tag, const char* what, const S& v) {{
    hls::stream<ap_uint<W>> s; v.template write_stream<W>(s); dump(tag, what, drain<S, W>(s));
}}
template <class S, int W> static void check(const char* tag, std::vector<uint64_t> p) {{
    const int n = S::template nwords<W>();
    std::printf("%s nwords %d\\n", tag, n);
    ap_uint<W> in[64];
    for (int i = 0; i < 64; ++i) in[i] = i < (int)p.size() ? ap_uint<W>(p[i]) : ap_uint<W>(0);
    S v; v.template read_array<W>(in);

    ap_uint<W> out[66];
    for (int i = 0; i < 66; ++i) out[i] = ap_uint<W>(0x5A5A5A5Aull);
    v.template write_array<W>(out);
    std::vector<uint64_t> a; for (int i = 0; i < n; ++i) a.push_back((uint64_t)out[i].to_uint64());
    dump(tag, "write_array", a);
    std::printf("%s guard %d\\n", tag, (int)(out[n] == ap_uint<W>(0x5A5A5A5Aull)));

    words_of<S, W>(tag, "write_stream", v);

    hls::stream<streamutils::axi4s_word<W>> ax; v.template write_axi4_stream<W>(ax, true);
    std::vector<uint64_t> d, l;
    while (!ax.empty()) {{ auto x = ax.read(); d.push_back((uint64_t)ap_uint<W>(x.data).to_uint64()); l.push_back((uint64_t)x.last); }}
    dump(tag, "write_axi4_stream", d); dump(tag, "tlast", l);

    hls::stream<ap_uint<W>> rs; for (uint64_t x : p) rs.write(ap_uint<W>(x));
    S v2; v2.template read_stream<W>(rs);
    words_of<S, W>(tag, "read_stream", v2);

    hls::stream<streamutils::axi4s_word<W>> ra;
    for (size_t i = 0; i < p.size(); ++i) streamutils::write_axi4_word<W>(ra, ap_uint<W>(p[i]), i + 1 == p.size());
    S v3; streamutils::tlast_status tl; v3.template read_axi4_stream<W>(ra, tl);
    std::printf("%s tl %d\\n", tag, (int)tl);
    words_of<S, W>(tag, "read_axi4_stream", v3);
}}
int main() {{
{chr(10).join(calls)}
    return 0;
}}
"""
    src = tmp_path / "align.cpp"
    src.write_text(prog, encoding="utf-8")
    exe = tmp_path / "align.exe"
    env = dict(os.environ, PATH=f"{gxx.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    r = subprocess.run([str(gxx), "-std=c++14", "-O1", f"-I{vitis_inc}", f"-I{tmp_path / INCLUDE_DIR}",
                        str(src), "-o", str(exe)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr[-4000:]
    run = subprocess.run([str(exe)], capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stderr[-2000:]
    got: dict[tuple[str, str], list[int]] = {}
    for ln in run.stdout.splitlines():
        if not ln.startswith("Al"):            # hls::stream's own [SIM] chatter
            continue
        tag, what, *vals = ln.split()
        got[(tag, what)] = [int(v) for v in vals]

    for s in SAMPLES:
        name = type(s).__name__
        for bw in (32, 64):
            tag = f"{name}/{bw}"
            p = [int(w) for w in s.serialize(word_bw=bw)]
            assert got[(tag, "nwords")] == [len(p)], tag
            for what in ("write_array", "write_stream", "write_axi4_stream", "read_stream", "read_axi4_stream"):
                assert got[(tag, what)] == p, (tag, what, [hex(v) for v in got[(tag, what)]], [hex(v) for v in p])
            assert got[(tag, "guard")] == [1], tag
            assert got[(tag, "tlast")] == [0] * (len(p) - 1) + [1], tag
            assert got[(tag, "tl")] == [1], tag            # tlast_at_end
