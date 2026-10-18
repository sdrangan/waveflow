"""mem_copy's kernel in its own **harness** (``harness(MemCopy)``), at RTL: its two ``m_axi`` ports on
the harness's memory stand-in, which lowers to the one ``FlatMemory`` both bundles are served from.

The same 16 jobs as the system gate (``test_mem_copy_system_xsi.py``), so the same numbers: the
memory's final arena and the completions identical RTL against pysim, every destination equal to its
source, and the last completion at the kernel flow's **2908** cycles (Vitis/Vivado 2025.1).
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.mem_copy.mem_copy import XSI_JOBS, copy_arena
from examples.mem_copy.mem_copy_system_build import EXPECTED_CYCLES, HERE, UNIT_TOP, build_dag
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run
from waveflow.utils.burst_io import read_burst_bundle


@pytest.fixture(scope="module")
def unit_run():
    return system_run(build_dag(work_dir=WORK), HERE, f"unit_{UNIT_TOP}", through="unit_compare")


@pytest.mark.xsi
def test_the_memory_and_the_completions_match_pysim(unit_run):
    assert compared_clean(unit_run)
    assert recorded_names(unit_run) == ["mem", "s_done"]


@pytest.mark.xsi
def test_the_rtl_copies_and_finishes_at_the_kernel_gate(unit_run):
    vdir = unit_run / "xsi_run" / "vectors"
    out = read_burst_bundle(vdir / "mem")[0]
    _image, golden = copy_arena(XSI_JOBS)
    for j in XSI_JOBS:
        assert np.array_equal(out[j.dst_off:j.dst_off + j.n_words],
                              golden[j.dst_off:j.dst_off + j.n_words]), j
    done = read_burst_bundle(vdir / "s_done")
    assert len(np.concatenate(done)) == len(XSI_JOBS)
    cycles = np.fromfile(vdir / "s_done" / "cycles.bin", dtype="<u8")
    assert int(cycles[len(XSI_JOBS) - 1]) == EXPECTED_CYCLES
