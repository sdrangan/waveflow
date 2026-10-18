"""``ArrayUtilsStep`` with a ``DataList`` element: the lane routines move whole records.

A record is packed flat in its lane (``pack_to_uint``'s layout), ``pf = word_bw // record_bw``
records to a word, record 0 in the low bits -- the rule of ``docs/guide/schema/hls/serialization.md``.
The Python checks always run; ``test_lane_routines_match_python`` compiles a lane loop over the
generated header with Vivado's MinGW (as ``test_stream_array_alignment.py`` does) and checks it
record for record and word for word against :func:`write_array`; it skips without that toolchain.
"""
from __future__ import annotations

import os
import subprocess

import numpy as np
import pytest

from waveflow.hw.arrayutils import read_array, write_array
from waveflow.hw.dataschema import DataArray, DataList, FloatField, IntField

INCLUDE_DIR = "include"
I8 = IntField.specialize(bitwidth=8, signed=True, include_dir=INCLUDE_DIR)
U16 = IntField.specialize(bitwidth=16, signed=False, include_dir=INCLUDE_DIR)
F32 = FloatField.specialize(bitwidth=32, include_dir=INCLUDE_DIR)


class DlPair(DataList):                    # 16 bits: two records to a 32-bit word, four to 64
    include_dir = INCLUDE_DIR
    elements = {"a": {"schema": I8}, "b": {"schema": I8}}


class DlWide(DataList):                    # 48 bits: wider than 32 (pf = 0), one lane at 64
    include_dir = INCLUDE_DIR
    elements = {"tag": {"schema": U16}, "x": {"schema": F32}}


N = 7                                      # odd: a partial last word at pf = 2 and 4
PAIRS = [{"a": k + 1, "b": -(k + 1)} for k in range(N)]
WIDES = [{"tag": 0x10 + k, "x": 0.5 * k - 1.0} for k in range(N)]


def _words(elem, recs, word_bw):
    arr = DataArray.specialize(elem, max_shape=(len(recs),), static=True)(recs)
    return [int(w) for w in write_array(arr, word_bw=word_bw)]


@pytest.mark.parametrize("word_bw", [32, 64])
def test_records_pack_flat_in_lanes_and_round_trip(word_bw):
    w = _words(DlPair, PAIRS, word_bw)
    pf = word_bw // 16
    assert len(w) == -(-N // pf)
    lane0 = w[0] & 0xFFFF                                     # record 0, low bits: a then b
    assert lane0 == (1 & 0xFF) | ((-1 & 0xFF) << 8)
    back = read_array(w, elem_type=DlPair, word_bw=word_bw, shape=N).val
    assert [{k: int(v) for k, v in r.items()} for r in back] == PAIRS


def test_a_wide_record_without_framed_methods_is_a_clear_error(tmp_path):
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.streamutils import StreamUtilsStep
    from waveflow.hw.arrayutils import ArrayUtilsStep
    from waveflow.hw.dataschema import DataSchemaStep

    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(DataSchemaStep(DlWide, word_bw_supported=[32], include_dir=INCLUDE_DIR))
    with pytest.raises(ValueError, match="framed=True"):       # at add, before any C++
        dag.add(ArrayUtilsStep(DlWide, [32]))
    dag = BuildDag()                                            # at 64 bits one record fits a word
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(DataSchemaStep(DlWide, word_bw_supported=[64], include_dir=INCLUDE_DIR))
    dag.add(ArrayUtilsStep(DlWide, [64]))
    res = dag.run(BuildConfig(root_dir=tmp_path, params={}), force=True)
    assert all(r.success for r in res.values()), res


# -- the generated C++, compiled -------------------------------------------------------------------

def _mingw() -> "os.PathLike | None":
    """A C++17 compiler (the headers use ``if constexpr``): Vivado's MinGW 10, else ``g++``."""
    import shutil
    from pathlib import Path
    for root in sorted(Path("C:/Xilinx").glob("*/Vivado/tps/mingw/10.0.0/win64.o/nt/bin")):
        if (root / "g++.exe").is_file():
            return root / "g++.exe"
    found = shutil.which("g++")
    return Path(found) if found else None


def _lane_check(elem: str, ns: str, word_bw: int, words: list[int], fields: list[str]) -> str:
    """One C++ check: read the words lane by lane (memory and AXI4-Stream), print each record's
    fields, write them back lane by lane, print the words."""
    init = ", ".join(f"{w}ull" for w in words)
    prints = " ".join(f'std::printf(" %g", (double)r[k].{f});' for f in fields)
    return f"""
    {{
        using namespace {ns};
        constexpr int W = {word_bw};
        constexpr int LW = lane_capacity<W>();
        constexpr int STEP = get_nwords<W>(LW);
        const uint64_t p[] = {{{init}}};
        const int nw = sizeof(p) / sizeof(p[0]);
        ap_uint<W> in[64], out[64];
        for (int i = 0; i < 64; ++i) {{ in[i] = i < nw ? ap_uint<W>(p[i]) : ap_uint<W>(0); out[i] = 0; }}
        hls::stream<streamutils::axi4s_word<W>> ax;
        for (int i = 0; i < nw; ++i) streamutils::write_axi4_word<W>(ax, in[i], i + 1 == nw);
        std::printf("{elem}/%d rec", W);
        int iw = 0;
        for (int i = 0; i < {N}; i += LW, iw += STEP) {{
            const int n = ({N} - i) < LW ? ({N} - i) : LW;
            value_type r[LW], s[LW];
            read_array_lane<W>(&in[iw], r, n);
            read_axi4_stream_lane<W>(ax, s, n);
            for (int k = 0; k < n; ++k) {{
                {prints}
                if (!(pack_to_uint_eq(r[k], s[k]))) std::printf(" AXIS_MISMATCH");
            }}
            write_array_lane<W>(r, &out[iw], n);
        }}
        std::printf("\\n{elem}/%d words", W);
        for (int i = 0; i < nw; ++i) std::printf(" %llu", (unsigned long long)out[i].to_uint64());
        std::printf("\\n");
    }}"""


def test_lane_routines_match_python(tmp_path):
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.streamutils import StreamUtilsStep
    from waveflow.hw.arrayutils import ArrayUtilsStep
    from waveflow.hw.dataschema import DataSchemaStep
    from waveflow.toolchain.toolchain import find_vitis_include_dir

    gxx, vitis_inc = _mingw(), find_vitis_include_dir()
    if gxx is None or vitis_inc is None:
        pytest.skip("needs Vivado's MinGW and Vitis (ap_int.h, hls_stream.h)")

    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    for cls in (DlPair, DlWide):
        # DlWide is wider than a 32-bit word: its lane routines need the framed record methods
        dag.add(DataSchemaStep(cls, word_bw_supported=[32, 64], include_dir=INCLUDE_DIR,
                               framed=cls is DlWide))
        dag.add(ArrayUtilsStep(cls, [32, 64]))
    res = dag.run(BuildConfig(root_dir=tmp_path, params={}), force=True)
    assert all(r.success for r in res.values()), res

    checks, expect = [], {}
    for elem, recs, fields in (("DlPair", PAIRS, ["a", "b"]), ("DlWide", WIDES, ["tag", "x"])):
        ns = f"{'dl_pair' if elem == 'DlPair' else 'dl_wide'}_array_utils"
        for bw in (32, 64):
            words = _words(globals()[elem], recs, bw)
            checks.append(_lane_check(elem, ns, bw, words, fields))
            expect[(f"{elem}/{bw}", "rec")] = [float(r[f]) for r in recs for f in fields]
            expect[(f"{elem}/{bw}", "words")] = [float(w) for w in words]
    prog = f"""
#include <cstdio>
#include <cstdint>
#include "streamutils_hls.h"
#include "dl_pair_array_utils.h"
#include "dl_wide_array_utils.h"
template <class T> static bool pack_to_uint_eq(const T& x, const T& y) {{
    return T::pack_to_uint(x) == T::pack_to_uint(y);
}}
int main() {{
{"".join(checks)}
    return 0;
}}
"""
    src = tmp_path / "lanes.cpp"
    src.write_text(prog, encoding="utf-8")
    exe = tmp_path / "lanes.exe"
    env = dict(os.environ, PATH=f"{gxx.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    r = subprocess.run([str(gxx), "-std=c++17", "-O1", f"-I{vitis_inc}",
                        f"-I{tmp_path / INCLUDE_DIR}", str(src), "-o", str(exe)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr[-4000:]
    run = subprocess.run([str(exe)], capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stderr[-2000:]
    assert "MISMATCH" not in run.stdout, run.stdout
    got = {}
    for ln in run.stdout.splitlines():
        if ln.startswith("Dl"):
            tag, what, *vals = ln.split()
            got[(tag, what)] = [float(v) for v in vals]
    for key, want in expect.items():
        np.testing.assert_array_equal(got[key], want, err_msg=str(key))
