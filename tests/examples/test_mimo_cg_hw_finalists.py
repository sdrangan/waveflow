"""Step 6.7 of plans/mimo_cg/mimo_cg_paper_sims.md: the finalists.

The twelve designs that are taken through Vivado are chosen by a rule from the model's picks and
committed before any of them is built.  These tests pin the list to the rule: for 16-QAM with 64
antennas at each K, within 0.5 dB, the cheapest design in LUTs over the whole space at the loosest
and at the second-tightest committed job-time budget, with any guard and with none.
"""

from __future__ import annotations

from collections import Counter

import pytest

from examples.mimo_cg.hw import dse, space
from examples.mimo_cg.hw import finalists as FN
from examples.mimo_cg.mimo_cg import read_table


@pytest.fixture(scope="module")
def chosen():
    return FN.select()


def test_committed_finalists_are_what_the_rule_gives(tmp_path, chosen):
    again = FN.write_finalists(tmp_path / "finalists.csv")
    assert again.read_bytes() == FN.FINALISTS.read_bytes()
    head = FN.FINALISTS.read_text(encoding="utf-8").splitlines()[0]
    assert "model_sha256=d95510d337e992d3" in head and "loss_db=0.5" in head
    assert [r["name"] for r in read_table(FN.FINALISTS)] == [r["name"] for r in chosen]


def test_twelve_designs_in_six_pairs(chosen):
    assert len(chosen) == 12 == len({r["build"] for r in chosen})
    assert Counter((r["K"], r["speed"]) for r in chosen) == dict.fromkeys(
        [(K, speed) for K in (4, 8, 16) for speed in ("loose", "tight")], 2
    )
    for r in chosen:
        c = space.HwConfig(**{k: r[k] for k in dse.KNOBS})
        assert space.is_valid(c) and r["build"] == f"fin_{space.label('det', c)}"
        assert c.cmd_depth == dse.FRONTIER_CMD_DEPTH
        assert r["loss_db"] <= FN.LOSS_DB and r["model_job"] <= r["job_budget"]
        assert (r["g_s"] == 0) == (r["guard"] == "no guard") or r["guard"] == "guard"
    pairs = {}
    for r in chosen:
        pairs.setdefault((r["K"], r["speed"]), {})[r["guard"]] = r
    for (K, speed), pair in pairs.items():
        with_g, without = pair["guard"], pair["no guard"]
        assert without["g_s"] == 0 and with_g["job_budget"] == without["job_budget"]
        # the guard never costs LUTs: with it allowed, the model's cheapest design is no dearer
        assert with_g["model_lut"] <= without["model_lut"], (K, speed)
        # without guard bits the datapath is wider, or the job runs more iterations
        assert without["W"] > with_g["W"] or without["nit"] > with_g["nit"]
    # the tight budget is the second of the eight committed ones, the loose one the last
    for K in (4, 8, 16):
        assert (
            pairs[(K, "tight")]["guard"]["job_budget"]
            < pairs[(K, "loose")]["guard"]["job_budget"]
        )
        assert (
            pairs[(K, "tight")]["guard"]["model_lut"]
            > pairs[(K, "loose")]["guard"]["model_lut"]
        )


def test_a_pick_is_the_cheapest_in_luts_over_the_whole_space(chosen):
    hw, acc = dse.hw_table(), dse.accuracy()
    hw = hw[hw.cmd_depth == dse.FRONTIER_CMD_DEPTH]
    for r in chosen[::5]:
        cands = dse.candidates(hw, acc, (r["modulation"], r["M"], r["K"]), FN.LOSS_DB)
        pool = cands[~cands.guarded & (cands.job <= r["job_budget"])]
        if r["guard"] == "no guard":
            pool = pool[pool.g_s == 0]
        assert int(pool.lut.min()) == r["model_lut"]


def test_pair_rows_compare_the_two_members():
    rows = []
    for guard, g, lut in (("guard", 4, 100), ("no guard", 0, 125)):
        row = {"K": 8, "speed": "loose", "guard": guard, "W": 12, "g_s": g, "nit": 3}
        for src in ("model", "csynth", "impl"):
            row |= {f"{src}_{k}": lut for k in FN.COUNTERS}
        rows.append(row | {"rtl_job": 1000 if g else 1500})
    (pair,) = FN.pair_rows(rows)
    assert pair["with"] == "W12g4 n3" and pair["without"] == "W12g0 n3"
    assert pair["impl_lut_pct"] == pair["model_dsp_pct"] == 25.0
    assert pair["rtl_job_pct"] == 50.0
