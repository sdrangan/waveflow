"""Steps 4.2–4.4 of plans/mimo_cg/mimo_cg_paper_sims.md: the CG vector unit ``cg_vec``.

Step 4.2 (no markers): the Python simulation of ``CgVecUnit`` — B and the golden ``S₁ … S_nit``
in from memory, ``cg_vec`` in between, every ``P₀ … P_{nit−1}`` and the final ``X`` out — is
bit-exact against the golden sub-steps, for the frontier formats and the stress set at
K ∈ {4, 8, 16}, with ``nit`` varying across jobs.
"""

from __future__ import annotations

import pytest

from examples.mimo_cg.hw.common import HW_FORMAT_NAMES, hw_format
from examples.mimo_cg.hw.vec import CgVecUnit, CgVecUnitSim, block_type
from examples.mimo_cg.mimo_cg_conformance import CaseSetSpec, _problems
from waveflow.hw.mem_stream import MemRStream, MemWStream
from waveflow.simulation.simulation import Simulation

N = 32


def _jobs(fmt_id: int, K: int, seed: int):
    """51 problems (50 random + the zero-residual one) and a nit per job cycling 1..K."""
    probs = _problems(CaseSetSpec("vec", hw_format(fmt_id), K, N, K, False, seed))
    return [(A, B, 64.0) for A, B, _ in probs], [(j % K) + 1 for j in range(len(probs))]


@pytest.mark.parametrize("K", [4, 8, 16])
@pytest.mark.parametrize("fmt_id", range(len(HW_FORMAT_NAMES)), ids=HW_FORMAT_NAMES)
def test_vec_unit_pysim_is_bit_exact(fmt_id, K):
    problems, jobs = _jobs(fmt_id, K, seed=500 + 10 * fmt_id + K)
    assert len(problems) >= 51
    CgVecUnitSim(problems, jobs, K=K, fmt=fmt_id).run()  # raises on any mismatch


def test_vec_unit_pysim_with_32_bit_memory_words():
    problems, jobs = _jobs(1, 8, seed=590)
    CgVecUnitSim(problems[:10], jobs[:10], K=8, fmt=1, mem_dwidth=32).run()


def test_vec_unit_is_built_on_the_framework_mem_streams():
    unit = CgVecUnit(name="u", sim=Simulation(), K=4, fmt=0)
    assert isinstance(unit.rstream, MemRStream) and unit.rstream.inband
    assert isinstance(unit.wstream, MemWStream) and unit.wstream.inband
    assert unit.wstream.emit_done
    assert unit.m_in is unit.rstream.m_mem and unit.m_out is unit.wstream.m_mem
    names = [b[0] if isinstance(b, tuple) else b for b in unit.boundary]
    assert names == ["s_cmd", "m_in", "m_out", "s_done"]


def test_blocks_are_lane_groups():
    blk = block_type(12, 16, 32, 4)
    assert blk.max_shape == (16 * 32 // 4,)
    assert blk.element_type.get_bitwidth() == 2 * 12 * 4
    with pytest.raises(ValueError, match="multiple of the lane count"):
        block_type(12, 4, 30, 4)
