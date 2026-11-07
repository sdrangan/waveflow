"""examples/dse_cg's scoring against the ground truth (plan step 4), on a small hand-made table: a
design's measured and predicted metrics, feasibility, each objective's optimum and pysim's pick, and
the pre-registered scoring of pysim.  No toolchain: the service test's stand-in design, whose
identities are cheap."""

from __future__ import annotations

import json

import pytest

from examples.dse_cg import score
from examples.dse_cg import service as sv
from tests.examples.test_dse_cg_service import Design


def _table(tmp_path, design, points):
    """*points*: ``[(params, {synth per node}, {t: t_upd_ms}, {t: pysim t_upd_ms}, pysim parts)]``."""
    t: dict[str, list] = {fn: [] for fn in ("synth", "rtlsim", "golden", "pysim")}
    seen = set()
    for params, synth, tupd, ptupd, parts in points:
        p = design.fill(params)
        for node, ident in design.identities(p).items():
            if ident not in seen:
                seen.add(ident)
                t["synth"].append({"key": ident, "row": {"status": "ok", **synth[node]}, "cost_s": 1.0})
        for tt, v in tupd.items():
            run = design.sim_identity(p, {"name": "long_term", "t": tt})["run"]
            t["rtlsim"].append({"key": run, "row": {"status": "ok", "t_upd_ms": v}, "cost_s": 1.0})
            name, w = design.workload({"name": "long_term", "t": tt})
            res = {r: sum(parts[n][r] for n in parts) for r in ("lut", "ff", "dsp", "bram")}
            t["pysim"].append({"key": sv.eval_key("pysim", p, name, w),
                               "row": {"status": "ok", "t_upd_ms": ptupd[tt], "resources": res,
                                       "parts": {n: {"integrated": v} for n, v in parts.items()}},
                               "cost_s": 1.0})
            name, w = design.workload({"name": "accuracy", "t": tt, "target": "-6_3"})
            t["golden"].append({"key": sv.eval_key("golden", p, name, w),
                                "row": {"status": "ok", "median_loss_db": 0.05 if p["W"] == 16 else 0.3},
                                "cost_s": 1.0})
    for fn, rows in t.items():
        (tmp_path / f"{fn}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return score.Truth(tmp_path, design)


def _parts(sys_lut, sys_dsp, vec_lut=5_000):
    """A design's parts: its own systolic part, the vector part of its ``W``, the shared hub and
    crossbar -- a part shared by identity has one value everywhere, as in G1."""
    return {"sys": {"lut": sys_lut, "ff": sys_lut, "dsp": sys_dsp, "bram": 2},
            "vec": {"lut": vec_lut, "ff": vec_lut, "dsp": 20, "bram": 2},
            "hub": {"lut": 500, "ff": 500, "dsp": 0, "bram": 0},
            "xbar": {"lut": 1_000, "ff": 1_000, "dsp": 0, "bram": 0}}


def test_optimum_pick_and_scoring(tmp_path):
    d = Design()
    small, big, w12 = {"R": 2, "C": 4}, {"R": 8, "C": 8}, {"R": 2, "C": 4, "W": 12}
    off = _parts(30_300, 100)                                                    # pysim 1% over on sys
    truth = _table(tmp_path, d, [
        (small, _parts(10_000, 50), {3: 6.5}, {3: 6.4}, _parts(10_000, 50)),     # small, too slow
        (big, _parts(30_000, 100), {3: 4.0}, {3: 4.1}, off),                     # fast enough
        (w12, _parts(9_000, 40, 4_000), {3: 4.5}, {3: 3.9}, _parts(9_000, 40, 4_000)),  # inaccurate
    ])
    obj = {"id": "lut", "minimize": "lut", "subject_to": [
        {"metric": "t_upd_ms", "workload": "long_term", "at": {"t": 3}, "op": "<=", "value": 5},
        {"metric": "median_loss_db", "workload": "accuracy", "at": {"t": 3, "target": "-6_3"},
         "op": "<=", "value": 0.1}]}
    designs = [d.fill(p) for p in (small, big, w12)]
    best = score.optimum(truth, obj, designs)
    assert best["params"]["R"] == 8 and best["value"] == 30_000 + 5_000 + 500 + 1_000
    pick = score.pysim_pick(truth, obj, designs)
    assert pick["params"]["R"] == 8 and pick["feasible"] and pick["value"] == best["value"]
    r = score.score_pysim(truth, designs, [obj], exclude=[small])
    assert r["parts"] == 6 and r["dsp"]["pass"] and r["bram"]["pass"]
    assert r["lut"]["mean_abs_rel"] == pytest.approx(0.01 / 6)                    # one part of six, 1% off
    assert r["cycles"]["mean_abs_rel"] == pytest.approx((0.1 / 4.0 + 0.6 / 4.5) / 2)
    assert r["picks"]["share"] == 1.0
