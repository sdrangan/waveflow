"""markov's memory priced as the system holds it (``plans/ip_packaging_routes.md`` Stage 2): its
idle port B is part of its identity (``RtlInstance.ties``), so its out-of-context synthesis ties the
port off and measures one RAMB36, not two -- and the composed estimate's BRAM equals the measured.

Runs markov's ``system_synth`` and ``resource_report`` (with ``measure_leaves``) into a scratch
platform under the gates' work directory: minutes cold (eleven leaf configurations and the crossbar
out of context, one global synthesis), seconds once every result is cached.  Needs Vivado and
markov's csynth'd kernels.
"""
from __future__ import annotations

import pytest

from examples.markov.markov_build import HERE, build_dag
from tests.examples._system_run import WORK
from waveflow.build.build import BuildConfig
from waveflow.build.system_estimate import estimate_system
from waveflow.toolchain.toolchain import find_vivado_path

pytestmark = pytest.mark.xsi


@pytest.fixture(scope="module")
def run():
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado")
    cfg = BuildConfig(root_dir=HERE, params={"synth": "check", "measure_leaves": True},
                      platform="tieoffs", platforms_root=WORK / "platforms",
                      part="xc7z020clg484-1", clk_freq=100e6)
    dag = build_dag(work_dir=WORK)
    for through in ("system_synth", "resource_report"):
        res = dag.run(cfg, through=through)
        bad = {n: r.message for n, r in res.items() if not r.success}
        if bad:
            pytest.fail(f"markov's {through} failed: {bad}")
    rtl = next(s for s in dag.steps() if s.name == "system_rtl")
    composed = estimate_system(rtl.sysm, cfg.platform_info, spec=rtl.spec, root=HERE,
                               use_in_context=False)
    return rtl.spec, composed


def test_the_memory_is_one_ramb36_out_of_context(run):
    _spec, composed = run
    mem = next(p for r in composed.rows for p in r.parts if p.inst == "u_mem_mem")
    assert (mem.counters["bram"], mem.context) == (2, "ooc")      # BRAM18 halves: one RAMB36


def test_the_composed_bram_is_the_measured(run):
    _spec, composed = run
    assert composed.measured["bram"] == 4
    assert composed.total["bram"] == composed.measured["bram"]
