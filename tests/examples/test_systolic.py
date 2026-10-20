"""examples/systolic: the systolic engine and its memory wrapper in their own harnesses
(docs/examples/systolic/test.md).

* fast: the engine's pysim matches the bit-exact model on six commands (panels, keep_a, A^H with
  panels, one tile, kp = 1), and the wrapper's pysim writes C exactly, at 64- and 128-bit words;
* ``-m xsi``: the engine at RTL is bit-exact and runs 8x8x32 back to back at 175 cycles a job with
  64-bit words, 94 with 128-bit words (Vitis 2025.1); the wrapper at RTL writes a 32x32x64 product
  exactly at either width.
"""

from __future__ import annotations

import numpy as np
import pytest

from examples.systolic import engine_harness as EH
from examples.systolic import engine_mem_harness as EMH
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import read_burst_bundle


def _run(dag, root, through):
    res = dag.run(EH.build_config(root), through=through)
    bad = {k: r.message[-2000:] for k, r in res.items() if not r.success}
    assert not bad, bad


@pytest.mark.parametrize("word_bits", [64, 128])
def test_engine_pysim_matches_the_model(tmp_path, word_bits):
    _run(EH.build_dag({"word_bits": word_bits}), tmp_path, "golden")


@pytest.mark.parametrize("word_bits", [64, 128])
def test_wrapper_pysim_writes_c_exactly(tmp_path, word_bits):
    cfg = {"word_bits": word_bits}
    _run(EMH.build_dag(cfg), tmp_path, "golden")
    mem = tmp_path / EMH.WORK_DIR / EMH.TOP / "pysim_run" / "vectors" / "mem"
    words = np.asarray(read_burst_bundle(mem)[0], np.uint64)
    assert EMH.check_c(words, **cfg) == [0, 0, 0]


def _xsi(tmp_path_factory, name):
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis HLS not found; the XSI gate needs it")
    if not toolchain.find_vivado_path():
        pytest.skip("Vivado (xsim) not found; the XSI gate needs it")
    return tmp_path_factory.mktemp(name)


@pytest.mark.xsi
@pytest.mark.parametrize("word_bits, interval", [(64, 175), (128, 94)])
def test_xsi_engine_exact_and_interval(tmp_path_factory, word_bits, interval):
    root = _xsi(tmp_path_factory, f"eng{word_bits}")
    cfg = {"word_bits": word_bits}
    _run(EH.build_dag(cfg), root, "compare")                       # bit-exact, six commands
    _run(EH.build_dag(cfg, workload="steady"), root, "compare")    # 4 x 8x8x32 back to back
    cyc = np.fromfile(root / EH.WORK_DIR / EH.TOP / "xsi_run" / "vectors" / "s_out" / "cycles.bin",
                      dtype="<u8")
    per = 1 + 8 * 32 * 32 // word_bits                             # a Resp, then C (32-bit elements)
    ends = [int(cyc[(j + 1) * per - 1]) for j in range(4)]
    assert np.diff(ends).tolist() == [interval] * 3


@pytest.mark.xsi
@pytest.mark.parametrize("word_bits", [64, 128])
def test_xsi_wrapper_writes_a_large_product_exactly(tmp_path_factory, word_bits):
    root = _xsi(tmp_path_factory, f"engmem{word_bits}")
    cfg = {"word_bits": word_bits}
    _run(EMH.build_dag(cfg, workload="big"), root, "compare")
    mem = root / EMH.WORK_DIR / EMH.TOP / "xsi_run" / "vectors" / "mem"
    words = np.asarray(read_burst_bundle(mem)[0], np.uint64)
    assert EMH.check_c(words, "big", **cfg) == [0]
