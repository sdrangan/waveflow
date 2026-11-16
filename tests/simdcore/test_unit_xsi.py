"""The SIMD core at RTL (``plans/simdcore.md`` Stage 5): the core in its harness through the system
flow -- codegen, csynth at 4 ns, XSI -- and every output burst and status equal to pysim's, which
equals the interpreter's (``tests/simdcore/test_unit.py``).

The work directory is under ``C:/wfsc`` when it exists: a long build path makes csynth fail silently
on Windows (MAX_PATH).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain


def _root(tmp_path: Path, name: str) -> Path:
    short = Path("C:/wfsc")
    if short.is_dir():
        r = short / "xsi" / name
        shutil.rmtree(r, ignore_errors=True)
        r.mkdir(parents=True)
        return r
    return tmp_path


def _run(core, root, workload="test"):
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis HLS not found; the XSI gate needs it")
    if not toolchain.find_vivado_path():
        pytest.skip("Vivado (xsim) not found; the XSI gate needs it")
    from waveflow.simdcore.build import build_config, build_dag

    res = build_dag(core, workload=workload, n_cycles=400_000).run(build_config(root), through="compare")
    bad = {k: r.message[-3000:] for k, r in res.items() if not r.success}
    assert not bad, bad


@pytest.mark.xsi
@pytest.mark.parametrize("L", [2])
def test_xsi_matched_filter(L, tmp_path):
    from examples.simdcore_mf.mf_program import mf_config
    from tests.simdcore.test_unit import MfCore

    # the RTL build: e32 only (the matched filter's), a small data memory (it uses none)
    cfg = mf_config(8, L=L, sews=(32,), dm_bytes=1024)
    _run(MfCore(name="simdcore", sim=Simulation(), cfg=cfg), _root(tmp_path, f"mf{L}"))


@pytest.mark.xsi
def test_xsi_cg(tmp_path):
    """CG (two 3-iteration solves, 8 x 32, static S stimulus) at RTL: the vdiv build -- the divider
    task fed and drained by the vector task."""
    from examples.simdcore_cg.cg_program import cg_config
    from tests.simdcore.test_unit import CgCore

    cfg = cg_config(8, 32, L=2, sews=(32,))
    _run(CgCore(name="simdcore", sim=Simulation(), cfg=cfg), _root(tmp_path, "cg2"))
