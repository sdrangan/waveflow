"""Step 6.8 of plans/mimo_cg/mimo_cg_paper_sims.md: what the exploration says about the design.

The guard comparison and the shape table are read off the DSE (measured accuracy, predicted csynth
cost, the whole space).  These tests pin the committed tables to the code and state the finding's
numbers, so a change of model or of rule shows up here.
"""

from __future__ import annotations

import pytest

from examples.mimo_cg.hw import finding as FD
from examples.mimo_cg.mimo_cg import read_table


def _table(name: str) -> list[dict]:
    return read_table(FD.PAPER_DATA / f"{name}.csv")


def test_committed_tables_are_what_the_code_regenerates(tmp_path):
    out = FD.write(tmp_path)
    assert sorted(out) == ["dse_guard", "dse_guard_pairs", "dse_shape"]
    for name, path in out.items():
        assert path.read_bytes() == (FD.PAPER_DATA / f"{name}.csv").read_bytes(), name
        head = path.read_text().splitlines()[0]
        assert (
            "model_sha256=d95510d337e992d3" in head and "cost=csynth, predicted" in head
        )


def test_guard_pairs_ask_216_questions():
    """27 scenarios × the eight committed job-time budgets, at 0.5 dB."""
    pairs = _table("dse_guard_pairs")
    assert len(pairs) == 27 * 8
    for r in pairs:
        assert r["with_W"] != ""  # with any guard there is always a design
        assert float(r["with_job"]) <= float(r["job_budget"])
        if r["without_W"] == "":
            assert r["lut_pct"] == ""
            continue
        assert r["without_g_s"] == "0" and float(r["without_job"]) <= float(
            r["job_budget"]
        )
        # allowing a guard can only make the cheapest design cheaper
        assert int(r["with_lut"]) <= int(r["without_lut"]) and float(r["lut_pct"]) >= 0
        # without one the datapath is wider, or the job runs longer, or nothing changes (QPSK)
        assert int(r["without_W"]) >= int(r["with_W"]) or int(r["without_nit"]) > int(
            r["with_nit"]
        )


def test_what_guard_bits_are_worth():
    """The finding, in csynth numbers: leaving the guard bits out costs a median 8% in LUTs,
    15% in flip-flops and 24% in block RAM, nothing in DSPs — and for a quarter of the questions
    there is then no design at all within the 16-bit datapath."""
    g = {r["group"]: r for r in _table("dse_guard")}
    a = g["all"]
    assert (a["questions"], a["no_design_without_guard"]) == ("216", "52")
    assert float(a["lut_median_pct"]) == pytest.approx(8.13)
    assert float(a["ff_median_pct"]) == pytest.approx(15.06)
    assert float(a["bram_median_pct"]) == pytest.approx(23.87)
    assert float(a["dsp_median_pct"]) == 0.0
    assert float(a["wider_bits_median"]) == 4 and a["wider_by_4_or_more"] == "114"
    assert a["more_iterations"] == "28"
    # the higher the modulation, the more the guard matters
    none = {m: int(g[m]["no_design_without_guard"]) for m in ("qpsk", "16qam", "64qam")}
    assert none == {"qpsk": 0, "16qam": 9, "64qam": 43}
    lut = [float(g[m]["lut_median_pct"]) for m in ("qpsk", "16qam", "64qam")]
    assert lut == sorted(lut) and lut[0] < 4 and lut[-1] > 12
    assert sum(int(g[f"K = {K}"]["questions"]) for K in (4, 8, 16)) == 216


def test_speed_comes_from_lanes_first():
    """Along the job-time budget the cheapest design goes from 1 lane and 4 array elements to 16
    lanes, with the array growing behind; the vector unit stays about two thirds of an
    iteration, so both blocks are scaled together."""
    shape = {int(r["budget"]): r for r in _table("dse_shape")}
    assert sorted(shape) == list(range(8)) and all(
        r["scenarios"] == "27" for r in shape.values()
    )
    lanes = [float(shape[i]["lanes_median"]) for i in range(8)]
    assert lanes == sorted(lanes, reverse=True) and (lanes[0], lanes[-1]) == (16, 1)
    assert (shape[0]["lanes_min"], shape[7]["lanes_max"]) == ("16", "1")
    assert (
        float(shape[7]["array_pes_max"]) == 4 and float(shape[0]["array_pes_min"]) == 64
    )
    share = [float(r["vec_share_of_iteration_median"]) for r in shape.values()]
    assert 0.45 < min(share) and max(share) < 0.75
    # the 3-multiply form and 32-bit memory words are the cheap choices in csynth LUTs
    assert shape[7]["three_multiply_form"] == shape[0]["three_multiply_form"] == "27"
    assert all(int(r["words_32_bit"]) >= 26 for r in shape.values())
    # from the loosest budget to the tightest: 17 times faster for 5 times the LUTs
    speed = float(shape[7]["job_us_median"]) / float(shape[0]["job_us_median"])
    area = float(shape[0]["lut_median"]) / float(shape[7]["lut_median"])
    assert 16 < speed < 18 and 5 < area < 5.5
    assert float(shape[0]["dsp_median"]) / float(shape[7]["dsp_median"]) == 24


def test_frontier_points_are_the_two_fronts():
    hw, acc = FD._context()
    pts = FD.frontier_points(hw, acc, FD.FIGURE_SCENARIOS[1])
    for side in ("with", "without"):
        xs, ys = zip(*pts[side], strict=True)
        assert list(xs) == sorted(xs) and list(ys) == sorted(ys, reverse=True)
        assert len(pts[side]) > 20
    # with a guard allowed the front is never above the front without one
    for x, y in pts["without"]:
        best = min(yy for xx, yy in pts["with"] if xx <= x)
        assert best <= y
