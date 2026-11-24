"""The generated C++ ``<elem>_array_utils::get_nwords<W>(n)`` counts words as Python does.

It once always returned ``ceil(n * bits / W)``, the count for densely packed elements.  The layout rule
(``docs/guide/schema/hls/serialization.md``) puts ``pf = W // bits`` WHOLE elements in a word, so
for an element width that does not divide the word width the dense count is short: five 12-bit
samples take 3 words at 32 bits, not 2.  Found by the agent-skills blind test
(``evaluation/skill_tests``).  The testbench file helpers size their reads with it, so they read
too few words and then refused the file ("unexpected trailing bytes").

``test_cpp_get_nwords_matches_python`` compiles the generated header with Vivado's MinGW (as
``test_arrayutils_datalist_elem.py`` does) and compares every count, and round-trips the
testbench file helpers against Python's ``write_uint32_file``; it skips without that toolchain.
"""
from __future__ import annotations

import os
import subprocess

import numpy as np
import pytest

from waveflow.hw.arrayutils import get_nwords, write_uint32_file
from waveflow.hw.dataschema import IntField
from waveflow.hw.fixpoint import FixedField, from_real

INCLUDE_DIR = "include"

#: Widths that divide the word (their header keeps the dense line), that do not (12, 14, 24), and
#: wider than a 32-bit word (40: pf = 0 there, two words an element).
ELEMS = {
    "s12": FixedField.specialize(12, 1, include_dir=INCLUDE_DIR),
    "u14": IntField.specialize(bitwidth=14, signed=False, include_dir=INCLUDE_DIR),
    "u16": IntField.specialize(bitwidth=16, signed=False, include_dir=INCLUDE_DIR),
    "s24": IntField.specialize(bitwidth=24, signed=True, include_dir=INCLUDE_DIR),
    "u40": IntField.specialize(bitwidth=40, signed=False, include_dir=INCLUDE_DIR),
}
WIDTHS = [32, 64]
COUNTS = list(range(0, 13))


def _gxx():
    from tests.hw.test_arrayutils_datalist_elem import _mingw
    return _mingw()


def test_python_counts_whole_elements_to_a_word():
    """The reference the C++ is checked against: the layout rule, not the dense count."""
    s12 = ELEMS["s12"]
    assert get_nwords(s12, 32, 5) == 3                       # 2 a word; dense would say 2
    assert get_nwords(s12, 64, 6) == 2                       # 5 a word; dense would say 2 too
    assert get_nwords(s12, 64, 11) == 3                      # dense would say 3 -- and 10 says 2
    assert get_nwords(ELEMS["u40"], 32, 3) == 6              # wider than a word: 2 words each
    x = from_real([0.0, 0.5, -0.75, 0.25, 0.3125], s12)
    from waveflow.hw.arrayutils import write_array
    assert len(write_array(x, word_bw=32)) == get_nwords(s12, 32, 5)


def test_headers_whose_count_was_right_are_unchanged():
    """A header whose dense count was already exact keeps its old line, byte for byte, so the RTL
    built from it does not read as stale (its source stamp hashes include/); only an element the
    dense count got wrong gets the general form."""
    from waveflow.hw.arrayutils import _gen_get_nwords

    dense = "    return (len <= 0) ? 0 : ((len * value_bitwidth + word_bw - 1) / word_bw);"
    for bits, widths in ((16, [32, 64]), (32, [32, 64, 128]), (64, [32, 64]), (8, [32])):
        assert dense in _gen_get_nwords(bits, widths), (bits, widths)
    for bits, widths in ((12, [32, 64]), (14, [32]), (24, [32, 64]), (40, [32]), (16, [24])):
        assert dense not in _gen_get_nwords(bits, widths), (bits, widths)


def test_cpp_get_nwords_matches_python(tmp_path):
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.streamutils import StreamUtilsStep
    from waveflow.hw.arrayutils import ArrayUtilsStep, _array_utils_namespace
    from waveflow.toolchain.toolchain import find_vitis_include_dir

    gxx, vitis_inc = _gxx(), find_vitis_include_dir()
    if gxx is None or vitis_inc is None:
        pytest.skip("needs Vivado's MinGW and Vitis (ap_int.h, hls_stream.h)")

    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    for elem in ELEMS.values():
        dag.add(ArrayUtilsStep(elem, WIDTHS))
    res = dag.run(BuildConfig(root_dir=tmp_path, params={}), force=True)
    assert all(r.success for r in res.values()), res

    # A file of five s12 samples, written by Python, read and rewritten by the C++ testbench helpers.
    s12 = ELEMS["s12"]
    # .val, the stored integers: from_real's array carries the cached Fixed12_1 class, not this one
    # (the same format, specialized with include_dir), and write_uint32_file refuses the pair.
    vals = from_real([0.0, 0.5, -0.75, 0.25, 0.3125], s12).val
    write_uint32_file(vals, elem_type=s12, file_path=tmp_path / "in.bin")
    ns12 = _array_utils_namespace(s12)

    includes, prints = [], []
    for name, elem in ELEMS.items():
        ns = _array_utils_namespace(elem)
        includes.append(f'#include "{ns}.h"')
        for w in WIDTHS:
            ns_counts = " ".join(f'std::printf(" %d", {ns}::get_nwords<{w}>({n}));' for n in COUNTS)
            prints.append(f'std::printf("{name}/{w}"); {ns_counts} std::printf("\\n");')
    prog = f"""
#include <cstdio>
#include "streamutils_hls.h"
{chr(10).join(includes)}
#include "{ns12}_tb.h"
int main() {{
{chr(10).join(prints)}
    {ns12}::value_type x[5];
    {ns12}::read_uint32_file_array(x, "{(tmp_path / 'in.bin').as_posix()}", 5);
    {ns12}::write_uint32_file_array(x, "{(tmp_path / 'out.bin').as_posix()}", 5);
    return 0;
}}
"""
    src = tmp_path / "nwords.cpp"
    src.write_text(prog, encoding="utf-8")
    exe = tmp_path / "nwords.exe"
    env = dict(os.environ, PATH=f"{gxx.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    r = subprocess.run([str(gxx), "-std=c++17", "-O1", f"-I{vitis_inc}",
                        f"-I{tmp_path / INCLUDE_DIR}", str(src), "-o", str(exe)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr[-4000:]
    run = subprocess.run([str(exe)], capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]

    got = {ln.split()[0]: [int(v) for v in ln.split()[1:]] for ln in run.stdout.splitlines() if ln}
    for name, elem in ELEMS.items():
        for w in WIDTHS:
            want = [get_nwords(elem, w, n) for n in COUNTS]
            assert got[f"{name}/{w}"] == want, (name, w)

    # The testbench helpers read the Python file whole and write back the same bytes.
    assert (tmp_path / "out.bin").read_bytes() == (tmp_path / "in.bin").read_bytes()
    assert len(np.fromfile(tmp_path / "in.bin", dtype="<u4")) == get_nwords(s12, 32, 5)
