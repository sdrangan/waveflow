"""AC5 / AC5b of plans/mimo_cg/ltbf_cg_dse.md: the LTBF engine bit-exact at RTL.

Each gate runs the engine's system flow (``examples/ltbf_cg/hw/system_build.py``) through XSI: a
sector solved jointly, then UE by UE with ``H`` kept on chip, then a second sector, N_rx = 64.  The
sink's recording must equal pysim's word for word, and every X word the fixed-point model's.  Needs
the configuration's csynth (``python -m examples.ltbf_cg.hw.build ...``, local) plus Vivado.
"""

from __future__ import annotations

import pytest

from examples.ltbf_cg.hw import system_build as sb
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run


@pytest.mark.xsi
@pytest.mark.parametrize("form", ["factored", "explicit"])
def test_engine_rtl_is_bit_exact_joint_and_per_ue(form):
    work = system_run(sb.build_dag(form, work_dir=WORK), sb.root(form), sb.top(form))
    assert compared_clean(work) and recorded_names(work) == ["m_out"]
    got = sb.check_xsi(work, form)
    assert got.size == sum(e.size for e in sb.system(form).expected())
