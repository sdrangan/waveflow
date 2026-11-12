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


def test_submit_needs_the_run_at_the_objective_s_workload_parameters():
    """A constraint stated ``at`` a workload parameter (``t`` 2) needs the run at it, not the default."""
    from examples.dse_cg.prereg import OBJECTIVES

    obj = next(o for o in OBJECTIVES if o["id"] == "dsp_tupd4_t2_loss03")
    s = _service(objective=obj)
    s.wait(s.synth([{}]))
    s.wait(s.rtlsim([{}], "long_term"))                           # t 3, the default
    assert not s.submit({}, {"evidence": []})["accepted"]
    s.wait(s.rtlsim([{}], {"name": "long_term", "t": 2}))
    assert s.submit({}, {"evidence": []})["accepted"]


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
        assert len(dselib.results("golden")) == 1 and dselib.results() is not None   # results(fn=...)
    finally:
        dselib.bind(None)
    t = threading.Thread(target=sv.serve, args=(s, "127.0.0.1:18765"), daemon=True)
    t.start()
    import time

    time.sleep(0.3)
    monkeypatch.setattr(dselib, "_SERVICE", "127.0.0.1:18765")
    assert dselib.budget()["spent_s"] == COST["golden"]
    assert len(dselib.results("golden")) == 1                         # results(fn=...) over HTTP
    with pytest.raises(dselib.Refused):
        dselib._call("not_a_function")


def test_the_replay_backend_answers_from_the_table_at_its_recorded_cost(tmp_path):
    """ReplayBackend over a table (``grid.py table`` / ``finish``): each row as measured, at the tool
    time it took; a point outside the table is ``not_in_space``; the service charges as live."""
    import json

    d = Design()
    p = d.fill({})
    sid = d.sim_identity(p, {"name": "long_term", "t": 3})
    rows = {
        "synth": [{"key": i, "row": {"status": "ok", "lut": 10, "dsp": 1, "bram": 2, "seconds": 600.0},
                   "cost_s": 600.0} for i in d.identities(p).values()],
        "hls": [{"key": i, "row": {"status": "ok", "lut": 20, "seconds": 120.0}, "cost_s": 120.0}
                for i in d.hls_identities(p).values()],
        "rtlsim": [{"key": sid["run"], "row": {"status": "ok", "cycles": 1_611_880, "build_s": 180.0,
                                               "simulate_s": 797.0}, "cost_s": 977.0}],
        "pysim": [{"key": sv.eval_key("pysim", p, "long_term", {"t": 3, "target": "-6_14"}),
                   "row": {"status": "ok", "cycles": 1_620_803, "seconds": 1.1}, "cost_s": 1.1}],
    }
    for fn, rs in rows.items():
        (tmp_path / f"{fn}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rs))
    s = sv.Service(d, "waveflow", None, None, 2, sv.ReplayBackend(d, tmp_path), None)
    assert s.pysim({}, "long_term")["cycles"] == 1_620_803
    r = s.wait(s.rtlsim([{}], "long_term"))[0]
    assert r["cycles"] == 1_611_880 and r["cost_s"] == 3 * 120.0 + 4 * 600.0 + 180.0 + 797.0
    assert s.pysim({"L": 2}, "long_term")["status"] == "not_in_space"


def test_golden_is_charged_once_per_accuracy_table():
    """golden_identity: two points that read one accuracy table (same W, form, target) share its
    computation; another target is a new table."""
    s = _service()
    a = s.golden({}, "accuracy")
    b = s.golden({"R": 8, "C": 8, "L": 2}, "accuracy")               # another design, the same table
    c = s.golden({}, {"name": "accuracy", "target": "-6_3"})          # a new table
    assert a["cost_s"] == COST["golden"] and not a["cached"]
    assert b["cost_s"] == 0.0 and not b["cached"]
    assert c["cost_s"] == COST["golden"]


class Synthetic(Backend):
    """Metrics that follow the parameters, enough for the scripted policies to have something to find:
    each part's synthesis follows its own parameters (as G1's rows do: a part shared by identity has
    one row), the kernels' csynth leaves out the crossbar (as ``hls_estimate`` does), and pysim is 1%
    over on LUTs and 2% on T_upd."""

    @staticmethod
    def part(node, p):
        if node == "sys":
            lut = p["R"] * p["C"] * 1000 * p["W"] // 16
            return {"lut": lut, "ff": lut, "dsp": p["R"] * p["C"] * 5, "bram": 2}
        if node == "vec":
            return {"lut": 5000 * p["L"], "ff": 5000 * p["L"], "dsp": 20 * p["L"], "bram": 2}
        if node == "xbar":
            return {"lut": 500 * p["banks"], "ff": 100, "dsp": 0, "bram": 0}
        return {"lut": 300, "ff": 300, "dsp": 0, "bram": 0}                 # hub

    @classmethod
    def total(cls, p):
        return {r: sum(cls.part(n, p)[r] for n in ("sys", "vec", "hub", "xbar")) + (456 if r == "bram" else 0)
                for r in ("lut", "ff", "dsp", "bram")}

    @staticmethod
    def tupd(p, t=3):
        return (6.4 if p["mem_rate"] == 2 else 4.7) * (0.73 if t == 2 else 1.0)

    def call(self, what, *args):
        self.calls.append(what)
        if what == "golden":
            p, w = args
            return {"status": "ok", "median_loss_db": 0.02 if p["W"] == 16 else 0.25,
                    "p90_loss_db": 0.5}, COST["golden"]
        if what == "pysim":
            p, w = args
            res = self.total(p)
            res["lut"] = round(res["lut"] * 1.01)
            return {"status": "ok", "t_upd_ms": self.tupd(p, w.get("t", 3)) * 1.02, "cycles": 1,
                    "resources": res}, COST["pysim"]
        if what == "synth":
            node, p = args
            return {"status": "ok", **self.part(node, p)}, COST["synth"]
        if what == "hls_estimate":
            kernel, p = args
            node = {"ltbf_sys": "sys", "ltbf_vec": "vec", "ltbf_hub": "hub"}[kernel]
            est = {r: round(v * 0.8) for r, v in self.part(node, p).items()}
            return {"kernel": kernel, "status": "ok", **est, "latency": None}, COST["hls_estimate"]
        if what == "rtlsim":
            p, w = args
            return ({"status": "ok", "t_upd_ms": self.tupd(p, w.get("t", 3)), "bit_exact": True,
                     "build_s": COST["build"], "simulate_s": COST["simulate"]},
                    COST["build"] + COST["simulate"])
        return super().call(what, *args)


#: The first objective's optimum on Synthetic: W 16 (accuracy), mem_rate 1 (T_upd), the smallest
#: array and lanes, one bank (its crossbar is 500 LUTs smaller, which only synthesis sees).
OPT0 = {"R": 2, "C": 4, "W": 16, "L": 2, "banks": 1, "mem_rate": 1}
OPT0_LUT = 2 * 4 * 1000 + 2 * 5000 + 300 + 500


@pytest.mark.parametrize("arm", ["traditional", "waveflow"])
def test_the_scripted_policies_find_the_optimum(arm):
    """Both policies submit a design of the first objective within 5% of its optimum -- the Waveflow
    one the optimum, the traditional one a design the kernels' estimate cannot tell from it (two
    banks: its crossbar is not a kernel) -- and the Waveflow one verifies fewer."""
    from examples.dse_cg import policies
    from examples.dse_cg.prereg import OBJECTIVES
    from examples.dse_cg.skill.dse import dselib

    obj = OBJECTIVES[0]                             # min LUT s.t. T_upd <= 5 ms (t 3), loss <= 0.1 dB
    be = Synthetic()
    s = sv.Service(Design(), arm, obj, None, 2, be, None)
    dselib.bind(s)
    try:
        out = policies.traditional(dselib, obj) if arm == "traditional" else policies.waveflow(dselib, obj)
    finally:
        dselib.bind(None)
    sub = out["submitted"]
    keys = ("R", "C", "W", "L", "mem_rate")
    assert out["accepted"] and {k: sub[k] for k in keys} == {k: OPT0[k] for k in keys}
    if arm == "waveflow":
        assert sub["banks"] == 1 and out["value"] == OPT0_LUT
    else:
        assert out["value"] <= OPT0_LUT * 1.05
    verified = be.calls.count("rtlsim")
    assert verified <= (4 if arm == "waveflow" else 8), (arm, verified)


class Pessimistic(Synthetic):
    """``hls_estimate`` over its kernels' synthesis by 1.9x, as csynth's LUT estimate of the LTBF
    kernels is (plan row of 2026-11-12)."""

    def call(self, what, *args):
        if what == "hls_estimate":
            kernel, p = args
            self.calls.append(what)
            node = {"ltbf_sys": "sys", "ltbf_vec": "vec", "ltbf_hub": "hub"}[kernel]
            est = {r: round(v * 1.9) for r, v in self.part(node, p).items()}
            return {"kernel": kernel, "status": "ok", **est, "latency": None}, COST["hls_estimate"]
        return super().call(what, *args)


@pytest.mark.parametrize("calibrate", [True, False])
def test_the_traditional_rule_corrects_its_estimate_on_one_synthesis(calibrate):
    """Minimize T_upd under a LUT limit that only the smallest designs meet (20,000; the smallest
    estimate is over 30,000): corrected by one synthesis, the traditional policy finds the fastest design
    under the limit; uncorrected, it removes every design and submits nothing (step 6's ablation)."""
    from examples.dse_cg import policies
    from examples.dse_cg.prereg import OBJECTIVES
    from examples.dse_cg.skill.dse import dselib

    obj = {**OBJECTIVES[1], "id": "tupd_lut20k",
           "subject_to": [{**c, "value": 20_000} if c["metric"] == "lut" else c for c in OBJECTIVES[1]["subject_to"]]}
    be = Pessimistic()
    s = sv.Service(Design(), "traditional", obj, None, 2, be, None)
    dselib.bind(s)
    try:
        out = policies.traditional(dselib, obj, lambda lib, o, c: policies.rank_traditional(lib, o, c, calibrate))
    finally:
        dselib.bind(None)
    if not calibrate:
        assert out["submitted"] is None and "synth" not in be.calls
        return
    sub = out["submitted"]
    assert out["accepted"] and sub["mem_rate"] == 1 and out["value"] == pytest.approx(4.7)
    assert Synthetic.total(Design().fill(sub))["lut"] <= 20_000


def synthetic_table(tmp_path, design):
    """G1's table for Synthetic over every legal design of the space, in ``grid.py``'s layout."""
    import itertools
    import json

    be = Synthetic()
    names = [k for k, v in GRID.items() if v[0] == "design"]
    t: dict[str, dict] = {fn: {} for fn in ("synth", "hls", "rtlsim", "golden", "pysim")}
    pts = []
    for combo in itertools.product(*[GRID[k][1] for k in names]):
        p = design.fill(dict(zip(names, combo, strict=True)))
        if design.legal(p):
            continue
        pts.append(p)
        for node, ident in design.identities(p).items():
            t["synth"].setdefault(ident, be.call("synth", node, p))
        for kernel, ident in design.hls_identities(p).items():
            t["hls"].setdefault(ident, be.call("hls_estimate", kernel, p))
        for tt in GRID["t"][1]:
            name, w = design.workload({"name": "long_term", "t": tt})
            row, _ = be.call("rtlsim", p, {"name": name, **w})
            t["rtlsim"][design.sim_identity(p, {"name": name, **w})["run"]] = (row, 0.0)
            t["pysim"][sv.eval_key("pysim", p, name, w)] = be.call("pysim", p, w)
            for g in GRID["target"][1]:
                name, w = design.workload({"name": "accuracy", "t": tt, "target": g})
                t["golden"][sv.eval_key("golden", p, name, w)] = be.call("golden", p, w)
    for fn, rows in t.items():
        (tmp_path / f"{fn}.jsonl").write_text("".join(json.dumps({"key": k, "row": r, "cost_s": c}) + "\n"
                                                      for k, (r, c) in rows.items()))
    return pts


def test_scripted_runs_on_replay_are_scored(tmp_path):
    """Plan step 6 end to end on a synthetic G1: both policies on the replay backend, recorded, and
    scored by ``score.summarize`` -- each reaches the target, the Waveflow one with fewer RTL runs, and
    every number either report states is backed by its ledger."""
    from examples.dse_cg import policies, score
    from examples.dse_cg.prereg import OBJECTIVES

    d = Design()
    table = tmp_path / "table"
    table.mkdir()
    pts = synthetic_table(table, d)
    obj = OBJECTIVES[0]
    recs = [policies.run_scripted(arm, obj, table, tmp_path / "runs" / arm, design=d)
            for arm in ("traditional", "waveflow")]
    truth = score.Truth(table, d)
    out = score.summarize(recs, truth, [obj], pts, epsilon=0.05, calibration_s=3600.0)
    assert out["optimum"][obj["id"]]["value"] == OPT0_LUT
    by = {r["arm"]: r for r in out["runs"]}
    for arm, r in by.items():
        assert r["time_to_target"] is not None, arm
        assert r["submitted"]["feasible"] and r["report_errors"] == [], (arm, r["report_errors"])
    assert by["waveflow"]["quality"] == 0.0
    assert by["waveflow"]["spend"]["rtl_runs"] < by["traditional"]["spend"]["rtl_runs"]
    g = out["groups"][f"waveflow/{obj['id']}"]
    assert g["time_to_target_s_upfront"]["median"] == g["time_to_target_s"]["median"] + 3600.0
