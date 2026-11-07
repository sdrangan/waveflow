"""examples/dse_cg's service and dselib, on a stand-in backend (plans/dse_cg.md step 3, no toolchain).

The rules of the service -- the cache by identity, what each change is charged, the budget, the arm,
the submit rule, ``results()`` ready for a data frame, and dselib's two transports -- checked on the
LTBF adaptor's space with cheap identities and a backend that answers at once with fixed costs.  The
live backend through the real tools is ``-m vitis``/``-m xsi`` (plan step 2c's points).
"""

from __future__ import annotations

import hashlib
import threading

import pytest

from examples.dse_cg import service as sv
from examples.dse_cg.ltbf_cg_mem import GRID, LtbfCgMemAdaptor

COST = {"golden": 30.0, "pysim": 2.0, "hls_estimate": 120.0, "synth": 600.0, "build": 300.0,
        "simulate": 700.0}


class Design(LtbfCgMemAdaptor):
    """The LTBF space, with identities from GRID's table (the real ones are content hashes, checked
    in test_dse_cg_ltbf.py)."""

    def _h(self, *xs) -> str:
        return hashlib.sha256(repr(xs).encode()).hexdigest()[:12]

    def identities(self, params):
        d = self.fill(params)
        return {n: self._h(n, *[d[k] for k, v in GRID.items() if v[0] == "design" and n in v[3]])
                for n in ("sys", "vec", "hub", "xbar")}

    def hls_identities(self, params):
        ids = self.identities(params)
        return {"ltbf_sys": ids["sys"], "ltbf_vec": ids["vec"], "ltbf_hub": ids["hub"]}

    def sim_identity(self, params, workload=None):
        d = self.fill(params)
        name, w = self.workload(workload)
        build = self._h(sorted(self.identities(params).items()), d["mem_rate"])
        return {"build": build, "run": self._h(build, d["cg_window"], name, sorted(w.items()))}

    def rebuilds(self):
        return {k: list(v[3]) for k, v in GRID.items() if v[0] == "design"}

    def unbuilt_resources(self, params):
        return {"bram": 456}


class Backend:
    def __init__(self):
        self.calls: list[str] = []

    def call(self, what, *args):
        self.calls.append(what)
        if what == "rtlsim":
            return ({"status": "ok", "cycles": 1_600_000, "t_upd_ms": 6.4, "bit_exact": True,
                     "build_s": COST["build"], "simulate_s": COST["simulate"]},
                    COST["build"] + COST["simulate"])
        row = {"status": "ok", "lut": 1000, "ff": 1000, "dsp": 10, "bram": 2,
               "median_loss_db": 0.05, "p90_loss_db": 0.2, "cycles": 1_590_000}
        return row, COST[what]


def _service(arm="waveflow", budget=None, objective=None, k=2):
    return sv.Service(Design(), arm, objective, budget, k, Backend(), None)


def test_a_repeated_call_is_cached_at_no_cost():
    s = _service()
    first = s.golden({}, "accuracy")
    again = s.golden({}, "accuracy")
    assert first["cost_s"] == COST["golden"] and not first["cached"]
    assert again["cost_s"] == 0.0 and again["cached"]
    assert s.budget()["by_function"]["golden"] == COST["golden"]


def test_what_each_change_is_charged():
    s = _service()
    rows = s.wait(s.synth([{}]))
    assert rows[0]["cost_s"] == 3 * COST["hls_estimate"] + 4 * COST["synth"]  # 3 kernels, 4 parts
    assert rows[0]["bram"] == 4 * 2 + 456                                        # + Shared Mem
    # a workload change: no new part, no new build, one new simulation
    r_t = s.wait(s.rtlsim([{}], "long_term"))[0]
    r_t2 = s.wait(s.rtlsim([{}], {"name": "long_term", "t": 2}))[0]
    assert r_t["cost_s"] == COST["build"] + COST["simulate"] and r_t2["cost_s"] == COST["simulate"]
    # a simulation-only change (cg_window): no part, the same build
    assert s.wait(s.rtlsim([{"cg_window": 4}], "long_term"))[0]["cost_s"] == COST["simulate"]
    # mem_rate: no part, but a new RTL build (its memory leaf)
    assert s.wait(s.rtlsim([{"mem_rate": 1}], "long_term"))[0]["cost_s"] == COST["build"] + COST["simulate"]
    # L: the vector part alone (its csynth and its synthesis)
    assert s.wait(s.synth([{"L": 2}]))[0]["cost_s"] == COST["hls_estimate"] + COST["synth"]


def test_jobs_share_k_slots_on_the_tool_clock():
    s = _service(k=2)
    a, b, c = (s.synth([p]) for p in ({}, {"L": 2}, {"W": 12}))
    end = max(s.pool.finish_time(j) for j in (a, b, c))
    s.wait([a, b, c])
    assert s.clock_s == end and s.pool.jobs[c][0] == min(s.pool.jobs[a][1], s.pool.jobs[b][1])


def test_an_illegal_point_is_a_row_and_a_typo_is_an_error():
    s = _service()
    row = s.wait(s.synth([{"R": 2, "C": 8}]))[0]
    assert row["status"].startswith("illegal") and row["cost_s"] == 0.0
    with pytest.raises(KeyError):
        s.golden({"Wx": 12}, "accuracy")


class Breaking(Backend):
    """Fails the vector part's synthesis once (a build that breaks, a run that times out)."""

    def __init__(self):
        super().__init__()
        self.broken = True

    def call(self, what, *args):
        if what == "synth" and args[0] == "vec" and self.broken:
            self.broken = False
            return {"status": "failed: RuntimeError: vivado exited 1"}, 42.0
        return super().call(what, *args)


def test_a_failed_build_is_a_row_charged_and_not_cached():
    s = sv.Service(Design(), "waveflow", None, None, 2, Breaking(), None)
    row = s.wait(s.synth([{}]))[0]
    assert row["status"] == "failed" and row["parts"]["vec"]["status"].startswith("failed")
    assert row["cost_s"] == 3 * COST["hls_estimate"] + 3 * COST["synth"] + 42.0
    r = s.wait(s.rtlsim([{}], "long_term"))[0]            # the vector part is built again first
    assert r["status"] == "ok" and r["cost_s"] == COST["synth"] + COST["build"] + COST["simulate"]


def test_the_live_backend_turns_a_tool_failure_into_a_row():
    class Broken(Design):
        def synth(self, node, params):
            raise RuntimeError("xsi_run: timed out after 3600 seconds")

    row, cost = sv.LiveBackend(Broken()).call("synth", "sys", {})
    assert row["status"].startswith("failed: RuntimeError") and "timed out" in row["status"]
    assert cost >= 0.0


def test_the_budget_refuses_a_call_once_spent():
    s = _service(budget=100.0)
    s.wait(s.synth([{}]))                 # over budget now
    with pytest.raises(sv.Refused):
        s.golden({}, "accuracy")


def test_pysim_is_refused_in_the_traditional_arm():
    with pytest.raises(sv.Refused):
        _service(arm="traditional").pysim({}, "long_term")


def test_submit_needs_synthesis_and_the_objective_s_rtl_runs():
    obj = {"subject_to": [{"metric": "t_upd_ms", "workload": "long_term", "op": "<=", "value": 7}]}
    s = _service(objective=obj)
    assert not s.submit({}, {"evidence": []})["accepted"]       # nothing synthesized
    s.wait(s.synth([{}]))
    assert not s.submit({}, {"evidence": []})["accepted"]       # not simulated
    s.wait(s.rtlsim([{}], "long_term"))
    assert s.submit({}, {"evidence": []})["accepted"]
    with pytest.raises(sv.Refused):
        s.golden({}, "accuracy")                                  # closed


def test_results_need_no_further_work_for_a_data_frame():
    pd = pytest.importorskip("pandas")
    s = _service()
    s.pysim({}, "long_term")
    s.pysim({"L": 2}, "long_term")
    df = pd.DataFrame(s.results("pysim"))
    assert len(df) == 2 and {"R", "C", "W", "L", "cycles", "cost_s"} <= set(df.columns)


def test_dselib_in_process_and_over_http(monkeypatch):
    from examples.dse_cg.skill.dse import dselib

    s = _service()
    dselib.bind(s)
    try:
        assert dselib.golden({}, "accuracy")["status"] == "ok"
    finally:
        dselib.bind(None)
    t = threading.Thread(target=sv.serve, args=(s, "127.0.0.1:18765"), daemon=True)
    t.start()
    import time

    time.sleep(0.3)
    monkeypatch.setattr(dselib, "_SERVICE", "127.0.0.1:18765")
    assert dselib.budget()["spent_s"] == COST["golden"]
    with pytest.raises(dselib.Refused):
        dselib._call("not_a_function")
