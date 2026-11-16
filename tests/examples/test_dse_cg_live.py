"""examples/dse_cg on the live backend: ``dselib`` through plan step 2c's six design points
(``plans/dse_cg.md`` step 3, ``-m xsi``; Vitis HLS and Vivado 2025.1).

The scripted client of the hand-off: a fresh service on this machine's tools, bound in process, each
point through ``golden``, ``pysim``, ``hls_estimate``, ``synth`` and ``rtlsim`` as an agent would call
them, and the service's rules checked on the rows it returns -- the arm's evaluations agree, a repeated
call is cached at no cost, the workload-only point charges no synthesis and no RTL build, the
simulation-only point no synthesis, an illegal point is a row, and ``results()`` is a data frame.

Run after ``python -m examples.dse_cg.points`` (plan step 2c), it reuses that run's builds -- csynth,
the parts' netlists and the RTL builds are cached by content -- and costs the six RTL runs; on a
fresh checkout it builds everything (hours).
"""

from __future__ import annotations

import pytest

from waveflow.toolchain import toolchain


@pytest.mark.xsi
def test_dselib_through_the_six_points(tmp_path):
    if not (toolchain.find_vitis_path() and toolchain.find_vivado_path()):
        pytest.skip("Vitis HLS and Vivado (xsim) are needed")
    pd = pytest.importorskip("pandas")
    from examples.dse_cg import service as sv
    from examples.dse_cg.ltbf_cg_mem import LtbfCgMemAdaptor
    from examples.dse_cg.points import POINTS
    from examples.dse_cg.skill.dse import dselib

    design = LtbfCgMemAdaptor()
    s = sv.Service(design, "waveflow", None, None, 2, sv.LiveBackend(design), tmp_path / "ledger.jsonl")
    dselib.bind(s)
    try:
        rows: dict[str, dict] = {}
        for name, (params, workload, _why) in POINTS.items():
            _n, w = design.workload(workload)
            g = dselib.golden(params, {"name": "accuracy", "t": w["t"], "target": w["target"]})
            p = dselib.pysim(params, workload)
            assert g["status"] == "ok" and p["status"] == "ok" and p["bit_exact"], (name, g, p)
            assert (g["median_loss_db"], g["p90_loss_db"]) == (p["median_loss_db"], p["p90_loss_db"]), name
            [h] = dselib.wait(dselib.hls_estimate([params]))
            [y] = dselib.wait(dselib.synth([params]))
            [r] = dselib.wait(dselib.rtlsim([params], workload))
            assert (h["status"], y["status"], r["status"]) == ("ok", "ok", "ok"), (name, h, y, r)
            assert r["bit_exact"], (name, r["endpoints"])
            rows[name] = {"synth": y, "rtlsim": r}

        # a repeated call is cached at no cost
        again = dselib.golden({}, {"name": "accuracy", "t": 3, "target": "-6_14"})
        assert again["cached"] and again["cost_s"] == 0.0
        [y2] = dselib.wait(dselib.synth([{}]))
        assert y2["cost_s"] == 0.0 and all(part["cached"] for part in y2["parts"].values())
        # the workload-only point: no synthesis, no RTL build (the defaults' build), a new run
        wl = rows["workload_only"]
        assert all(part["cached"] for part in wl["synth"]["parts"].values())
        sid_d = design.sim_identity({}, POINTS["defaults"][1])
        sid_w = design.sim_identity({}, POINTS["workload_only"][1])
        assert sid_w["build"] == sid_d["build"] and sid_w["run"] != sid_d["run"]
        assert not wl["rtlsim"]["cached"]
        # the simulation-only point: no synthesis, its own RTL run
        so = rows["sim_only"]
        assert all(part["cached"] for part in so["synth"]["parts"].values())
        assert not so["rtlsim"]["cached"]
        # an illegal point is a row
        [bad] = dselib.wait(dselib.synth([{"R": 2, "C": 8}]))
        assert bad["status"].startswith("illegal") and bad["cost_s"] == 0.0
        # results() needs no further work for a data frame
        df = pd.DataFrame(dselib.results("rtlsim"))
        assert len(df) == len(POINTS) and {"R", "C", "cycles", "t_upd_ms", "cost_s"} <= set(df.columns)
    finally:
        dselib.bind(None)
