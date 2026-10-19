"""The systolic engine and its memory wrapper (``plans/systolic_characterization.md``, Part B).

* fast: the engine's harness in pysim matches the bit-exact model on six commands (panels, keep_a,
  A^H with panels, kp = 1), and the wrapper's pysim writes C exactly; the top declares the banked
  blocks; refused commands and jobs are answered;
* ``-m xsi``: the engine at RTL is bit-exact and runs 8x8x32 back to back at 175 cycles a job,
  stream-fed (Vitis 2025.1); the wrapper at RTL writes a 32x32x64 product exactly.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.linalg import _engine_harness as EH
from tests.linalg import _engine_mem_harness as EMH
from tests.linalg._hls import require_vitis
from tests.linalg._systolic_harness import build_config
from waveflow.build.composite_gen import composite_top_spec, render_top
from waveflow.linalg.message import Status
from waveflow.linalg.systolic import MatmulOp
from waveflow.linalg.systolic_engine import command, command_status
from waveflow.linalg.systolic_engine_mem import EngineJobDesc, job_status
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import read_burst_bundle


def _run(dag, root, through):
    res = dag.run(build_config(root), through=through)
    bad = {k: r.message[-2000:] for k, r in res.items() if not r.success}
    assert not bad, bad


def test_engine_pysim_matches_the_model(tmp_path):
    _run(EH.build_dag(), tmp_path, "golden")


def test_engine_top_declares_banked_blocks():
    top = render_top(composite_top_spec(EH.engine(), width=64))
    assert "hls::stream_of_blocks<ap_uint<24>[8][8], 2> a;" in top
    assert "#pragma HLS ARRAY_PARTITION variable=a dim=1 complete" in top
    assert "hls::stream_of_blocks<ap_uint<24>[8][4][4][2], 2> b;" in top
    assert "#pragma HLS ARRAY_PARTITION variable=b dim=3 complete" in top
    assert "#pragma HLS ARRAY_PARTITION variable=b dim=4 complete" in top
    assert "#pragma HLS STREAM variable=paya depth=32" in top   # a whole A: the receiver moves on


def test_command_rules():
    lim = dict(Mmax=8, Kmax=8, Nmax=32, Pmax=4, R=4, C=8)
    assert command_status(command(1, MatmulOp.MUL, 8, 5, 16, npanels=3), **lim) == Status.OK
    assert command_status(command(1, 9, 8, 8, 32), **lim) == Status.BAD_OP
    assert command_status(command(1, MatmulOp.MUL, 6, 8, 32), **lim) == Status.BAD_DIMS
    assert command_status(command(1, MatmulOp.MUL, 8, 8, 32, npanels=5), **lim) == Status.BAD_DIMS
    assert command_status(command(1, MatmulOp.MUL, 8, 8, 32, npanels=2, keep_a=True),
                          **lim) == Status.BAD_DIMS
    d = EngineJobDesc()
    d.op, d.M, d.K, d.N = MatmulOp.MUL, 16, 40, 32
    assert job_status(d, Mmax=8, Kmax=8, Nmax=32, Pmax=4) == Status.BAD_DIMS     # 5 panels
    d.K = 32
    assert job_status(d, Mmax=8, Kmax=8, Nmax=32, Pmax=4) == Status.OK


def test_wrapper_pysim_writes_c_exactly(tmp_path):
    _run(EMH.build_dag(), tmp_path, "golden")
    mem = tmp_path / EMH.WORK_DIR / EMH.TOP / "pysim_run" / "vectors" / "mem"
    words = np.asarray(read_burst_bundle(mem)[0], np.uint64)
    assert EMH.check_c(words) == [0, 0, 0]


def _xsi(tmp_path_factory, name):
    require_vitis()
    if not toolchain.find_vivado_path():
        pytest.skip("Vivado (xsim) not found; the XSI gate needs it")
    return tmp_path_factory.mktemp(name)


@pytest.mark.xsi
def test_xsi_engine_exact_and_175_cycles(tmp_path_factory):
    root = _xsi(tmp_path_factory, "eng")
    _run(EH.build_dag(), root, "compare")                          # bit-exact, six commands
    _run(EH.build_dag(workload="steady"), root, "compare")         # 4 x 8x8x32 back to back
    cyc = np.fromfile(root / EH.WORK_DIR / EH.TOP / "xsi_run" / "vectors" / "s_out" / "cycles.bin",
                      dtype="<u8")
    per = 1 + 128                                                  # a Resp, then C
    ends = [int(cyc[(j + 1) * per - 1]) for j in range(4)]
    assert np.diff(ends).tolist() == [175, 175, 175]


@pytest.mark.xsi
def test_xsi_wrapper_writes_a_large_product_exactly(tmp_path_factory):
    root = _xsi(tmp_path_factory, "engmem")
    _run(EMH.build_dag(workload="big"), root, "compare")
    mem = root / EMH.WORK_DIR / EMH.TOP / "xsi_run" / "vectors" / "mem"
    words = np.asarray(read_burst_bundle(mem)[0], np.uint64)
    assert EMH.check_c(words, "big") == [0]
