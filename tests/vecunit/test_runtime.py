"""``waveflow.vecunit.runtime`` (Stage 8 of ``plans/vecunit_new.md``): the simulation-speed helpers --
pysim timed and checked, the RTL bound, the fit, the profile split, the record kept across runs."""

from __future__ import annotations

import numpy as np

from waveflow.simulation.simulation import Simulation
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit import runtime as RT
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.unit import VecUnit

F = Format(12, 4, True, QMode.AP_RND, OMode.AP_SAT)


def unit() -> VecUnit:
    cfg = VecUnitConfig(group_formats=(F,) * 8, scalar_formats=(F,) * 4, in_queue_formats=(F,),
                        out_queue_formats=(F,), L=4, VLMAX=8, Kmax=8, Nmax=8)
    return VecUnit(name="vecunit", sim=Simulation(), cfg=cfg)


def test_pysim_is_timed_and_checked():
    r = RT.time_pysim(unit(), "test", 2)
    assert r["exact"] and r["events"] > 0 and r["cycles"] > 0
    assert r["build_s"] <= r["s"] and r["run_s"] <= r["s"]


def test_a_wrong_output_is_caught():
    from waveflow.hw.harness import harness

    u = unit()
    sysm = harness(u, workload="test", n_cycles=RT.PYSIM_BOUND)
    sysm.run()
    gold = u.golden("test")
    assert RT.outputs_match(sysm, gold)
    gold["s_out0"][0] = np.asarray(gold["s_out0"][0], np.uint64) ^ np.uint64(1)
    assert not RT.outputs_match(sysm, gold)


def test_bound_and_fit():
    assert RT.rtl_bound(1000) == 3200
    f = RT.fit([1, 2, 4], [0.003, 0.005, 0.009])
    assert abs(f["per_ms"] - 2.0) < 1e-9 and abs(f["setup_ms"] - 1.0) < 1e-9


def test_profile_split_adds_up():
    p = RT.profile_run(unit(), "test")
    assert p["interpreter_s"] > 0
    assert abs(p["interpreter_s"] + p["conversion_s"] + p["rest_s"] - p["run_s"]) < 1e-9


def test_a_run_without_rtl_keeps_the_previous_rtl_rows(tmp_path):
    path = tmp_path / "runtime.json"
    RT.save(path, "other", {"x": 1})
    RT.save(path, "k", {"sizes": {"1": {"rtl": {"rtl_s": 2.0}}, "2": {}}, "profile": {"run_s": 1}})
    out = {"sizes": {"1": {"pysim": {}}, "3": {}}}
    RT.keep_previous(path, "k", out)
    assert out["sizes"]["1"]["rtl"] == {"rtl_s": 2.0} and "rtl" not in out["sizes"]["3"]
    assert out["profile"] == {"run_s": 1}
    RT.save(path, "k", out)
    assert RT.load(path, "other") == {"x": 1} and RT.load(path, "k") == out
