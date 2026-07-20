"""Cross-language conformance for the burst-bundle format.

The pysim side (:mod:`waveflow.utils.burst_io`) and the XSI C++ side
(``waveflow/build/xsi/xsi_bundle.h``) must read and write the *same* on-disk bundle.  This compiles a
tiny C++ round-trip with g++ (no xsim needed) and checks both directions:

    Python write -> C++ read -> C++ write -> Python read  ==  original

If the two implementations of the format ever drift, this fails without the Vitis/Vivado toolchain.
"""
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from waveflow.utils.burst_io import read_burst_bundle, write_burst_bundle

_GXX = shutil.which("g++")
pytestmark = pytest.mark.skipif(_GXX is None, reason="g++ (mingw) not on PATH")

_XSI_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"


def _roundtrip_cpp(in_dir: Path, out_dir: Path, tmp_path: Path) -> None:
    """Compile+run a C++ program that reads the bundle at *in_dir* and rewrites it at *out_dir*."""
    src = tmp_path / "rt.cpp"
    src.write_text(
        '#include "xsi_bundle.h"\n'
        "using namespace wfbfm;\n"
        "int main() {\n"
        f'    auto w = BurstBundle::read_words("{in_dir.as_posix()}");\n'
        f'    auto b = BurstBundle::read_bounds("{in_dir.as_posix()}");\n'
        f'    BurstBundle::write("{out_dir.as_posix()}", w, b);\n'
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    exe = tmp_path / "rt.exe"
    subprocess.run(
        [_GXX, "-std=c++17", f"-I{_XSI_SRC}", str(src), "-o", str(exe)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run([str(exe)], check=True, capture_output=True, text=True)


def test_multi_burst_roundtrips_python_cpp(tmp_path):
    # 64-bit words that would truncate under a 32-bit format (the bug the bundle guards against).
    bursts = [
        np.array([1, (4096 << 32) | 64, 3], dtype=np.uint64),
        np.array([10, 20], dtype=np.uint64),
    ]
    # out_dir does NOT exist: BurstBundle::write must create it (and its parent), like Python does.
    in_dir, out_dir = tmp_path / "in", tmp_path / "out" / "nested"
    write_burst_bundle(bursts, in_dir)
    _roundtrip_cpp(in_dir, out_dir, tmp_path)

    got = read_burst_bundle(out_dir)  # read_burst_bundle also validates meta.json vs the binaries
    assert len(got) == len(bursts)
    for g, e in zip(got, bursts):
        np.testing.assert_array_equal(g, e)


def test_single_burst_arena_roundtrips(tmp_path):
    # A memory arena is a single-burst bundle (one burst spanning the whole array).
    arena = (np.arange(500, dtype=np.uint64) * 2654435761 + 12345) & 0xFFFFFFFFFFFFFFFF
    in_dir, out_dir = tmp_path / "in", tmp_path / "out" / "nested"
    write_burst_bundle([arena], in_dir)
    _roundtrip_cpp(in_dir, out_dir, tmp_path)

    got = read_burst_bundle(out_dir)
    assert len(got) == 1
    np.testing.assert_array_equal(got[0], arena)
