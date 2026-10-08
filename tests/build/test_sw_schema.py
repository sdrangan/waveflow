"""Typed host messages in C++ (``xsi_sw_schema.h``, plans/host_runtime.md S3): round trips.

A software host reads and writes the same generated DataSchema structs its kernels use.  For every
schema the two example hosts read or write, Python serializes an instance, the C++ side decodes it
with ``decode_words<T, 64>`` and prints its fields; and the C++ side encodes an instance with
``encode_words`` and Python decodes the words.  Compiled with run.bat's compiler (MinGW 6.2) against
Vitis's ``ap_int.h`` and headers generated fresh into a temp directory.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from waveflow.toolchain.toolchain import find_vitis_include_dir, find_vivado_path

XSI_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"


def _mingw62() -> Path | None:
    for root in sorted(Path("C:/Xilinx").glob("*/Vivado/tps/mingw/6.2.0/win64.o/nt/bin")):
        if (root / "g++.exe").is_file():
            return root / "g++.exe"
    return None


def _toolchain():
    gxx = _mingw62()
    vitis_inc, vivado = find_vitis_include_dir(), find_vivado_path()
    if gxx is None or vitis_inc is None or not vivado:
        pytest.skip("needs Vivado's MinGW, Vitis (ap_int.h) and Vivado (xsi.h)")
    return gxx, vitis_inc, Path(vivado).resolve().parent.parent / "data" / "xsim" / "include"


def _words(x) -> str:
    return "{" + ", ".join(f"{int(w)}ull" for w in np.asarray(x, dtype=np.uint64)) + "}"


def test_host_schemas_round_trip(tmp_path):
    gxx, vitis_inc, xsim_inc = _toolchain()
    from examples.markov import markov_build
    from examples.markov.markov import MkvCmd, MkvResp
    from examples.mm_fir import mm_fir_build
    from examples.mm_fir.mm_fir import FirCmdHdr, FirRespHdr, FirStatus

    mm_fir_build.gen_headers(tmp_path / "fir")
    markov_build.gen_headers(tmp_path / "mkv")
    resp = MkvResp(n=300, ones=257, tx_id=2)
    st = FirStatus(nsamp=200, cfg_id=2, ncfg=2)
    rh = FirRespHdr(nsamp=16, tx_id=13, cfg_id=2)
    prog = f"""
#include <cstdio>
#include "mkv_resp.h"
#include "mkv_cmd.h"
#include "fir_status.h"
#include "fir_resp_hdr.h"
#include "fir_cmd_hdr.h"
#include "xsi_sw_schema.h"
using namespace wfbfm;
static void dump(const std::vector<uint64_t>& w) {{ for (uint64_t x : w) std::printf(" %llu", (unsigned long long)x); std::printf("\\n"); }}
int main() {{
    MkvResp r = decode_words<MkvResp, 64>({_words(resp.serialize(word_bw=64))});
    std::printf("MkvResp %u %u %u\\n", (unsigned)r.n, (unsigned)r.ones, (unsigned)r.tx_id);
    FirStatus s = decode_words<FirStatus, 64>({_words(st.serialize(word_bw=64))});
    std::printf("FirStatus %u %u %u\\n", (unsigned)s.nsamp, (unsigned)s.cfg_id, (unsigned)s.ncfg);
    FirRespHdr h = decode_words<FirRespHdr, 64>({_words(rh.serialize(word_bw=64))});
    std::printf("FirRespHdr %u %u %u\\n", (unsigned)h.nsamp, (unsigned)h.tx_id, (unsigned)h.cfg_id);
    MkvCmd c; c.n = 300; c.tx_id = 3; c.x0 = 1; c.seed = 123456789u; c.p01 = 4408; c.p10 = 4314;
    c.dstaddr = 0x100600ull;
    std::printf("MkvCmd"); dump(encode_words<MkvCmd, 64>(c));
    FirCmdHdr f; f.nsamp = 16; f.tx_id = 5; f.cfg_id = 2;
    std::printf("FirCmdHdr"); dump(encode_words<FirCmdHdr, 64>(f));
    return 0;
}}
"""
    src = tmp_path / "schema_rt.cpp"
    src.write_text(prog, encoding="utf-8")
    exe = tmp_path / "schema_rt.exe"
    env = dict(os.environ, PATH=f"{gxx.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    r = subprocess.run([str(gxx), "-std=c++14", "-O1", f"-I{XSI_SRC}", f"-I{xsim_inc}", f"-I{vitis_inc}",
                        f"-I{tmp_path / 'fir' / 'include'}", f"-I{tmp_path / 'mkv' / 'include'}",
                        str(src), "-o", str(exe)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr[-4000:]
    out = subprocess.run([str(exe)], capture_output=True, text=True, env=env).stdout.splitlines()
    got = {ln.split()[0]: [int(v) for v in ln.split()[1:]] for ln in out}

    # Python -> C++
    assert got["MkvResp"] == [300, 257, 2]
    assert got["FirStatus"] == [200, 2, 2]
    assert got["FirRespHdr"] == [16, 13, 2]
    # C++ -> Python: the C++ encoding is the Python serializer's, word for word.
    cmd = MkvCmd(n=300, tx_id=3, x0=1, seed=123456789, p01=4408, p10=4314, dstaddr=0x100600)
    assert got["MkvCmd"] == [int(w) for w in cmd.serialize(word_bw=64)]
    assert got["FirCmdHdr"] == [int(w) for w in FirCmdHdr(nsamp=16, tx_id=5, cfg_id=2).serialize(word_bw=64)]
