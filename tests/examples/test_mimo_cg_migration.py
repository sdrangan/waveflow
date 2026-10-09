"""Step 9.4a of plans/mimo_cg/mimo_cg_paper_sims.md: the re-measurement's list, its predictions and
its comparison, pre-registered before any of its builds runs (no toolchain).

* ``paper_data/migration_list.csv`` is what the code regenerates: the 132 builds of Phase 5 and the
  12 finalists, each relabelled ``mig_<old label>``, with the predicted DSP and block RAM.
* The predictions follow the counted rules: +1 DSP per CG core and +2 per systolic core (more where
  the conjugated operand changes the multiplies' binding), and a detector's block RAM moves only
  with its ``A`` channel's shape.  The three default detectors' predictions are what their step 9.2b
  csynth gave (Vitis HLS 2024.1).
* :func:`~examples.mimo_cg.hw.migration.compare`, on tables where every build came out as
  predicted and every other number unchanged, passes every criterion and does not fire the
  trigger; each criterion catches a number moved past its bound.
"""

from __future__ import annotations

import shutil

import pytest

from examples.mimo_cg.hw import migration as MG
from examples.mimo_cg.mimo_cg import read_table, write_table


def test_the_committed_list_is_what_the_code_regenerates(tmp_path):
    again = MG.write_list(tmp_path / "migration_list.csv")
    assert again.read_bytes() == MG.LIST.read_bytes()
    rows = MG.read_list()
    studied = [b for b, *_ in MG.studied()]
    assert [r["old"] for r in rows] == studied + [f["build"] for f in MG.finalists()]
    assert len(studied) == 132 and len(rows) == 144
    assert all(r["build"] == f"mig_{r['old']}" for r in rows)
    assert {r["role"] for r in rows[132:]} == {"finalist"}
    assert [b for b, *_ in MG.campaign_builds()] == [r["build"] for r in rows[:132]]


def test_predictions_follow_the_counted_rules():
    rows = MG.read_list()
    w10_form4 = []
    for r in rows:
        d_dsp = int(r["new_block_dsp"]) - int(r["old_block_dsp"])
        if r["top"] == "vec":
            assert d_dsp == 1, r["old"]
        elif r["top"] == "mm" and d_dsp != 2:
            w10_form4.append(r["old"])
        elif r["top"] == "det":
            assert d_dsp == 3, r["old"]
            assert int(r["pred_dsp"]) == int(r["old_dsp"]) + 3
            moved = int(r["new_a_bram"]) - int(r["old_a_bram"])
            assert int(r["pred_bram"]) == int(r["old_bram"]) + moved
    # the conjugated operand one bit wider changes the binding at W = 10 with four multiplies
    assert sorted(w10_form4) == [
        "mm_k16_r1_c32_m4_w10_l16",
        "mm_k8_r1_c4_m4_w10_l2",
        "mm_k8_r4_c8_m4_w10_l4",
    ]
    by = {r["old"]: r for r in rows}
    for old, csynth in (
        ("det_k4_l4_r4_c4_m4_w12g8_d64_s2_q2", (115, 20)),
        ("det_k8_l4_r8_c4_m4_w12g8_d64_s2_q2", (179, 23)),
        ("det_k16_l4_r16_c4_m4_w12g8_d64_s2_q2", (307, 55)),
    ):
        assert (int(by[old]["pred_dsp"]), int(by[old]["pred_bram"])) == csynth


def test_spearman():
    assert MG.spearman([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)
    assert MG.spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert MG.spearman([5, 5, 5], [1, 1, 1]) == 1.0
    assert MG.spearman([1, 2, 3], [7, 7, 7]) == 0.0
    # one adjacent swap among four: rho 0.8 (why the trigger looks at K = 4 and not K = 8 or 16)
    assert MG.spearman([1, 2, 3, 4], [1, 3, 2, 4]) == pytest.approx(0.8)


def _as_predicted(tmp_path):
    """The old tables, and migration tables in which every build came out as predicted."""
    src = MG.PAPER_DATA
    for n in (
        "hw_builds",
        "hw_modules",
        "hw_cycles",
        "finalists_impl",
        "migration_list",
    ):
        shutil.copy(src / f"{n}.csv", tmp_path / f"{n}.csv")
    plan = {r["old"]: r for r in MG.read_list()}
    builds = []
    for r in read_table(src / "hw_builds.csv"):
        p = plan[r["build"]]
        r = r | {"build": p["build"], "role": "migration"}
        if p["top"] == "det":
            r["dsp"], r["bram"] = p["pred_dsp"], p["pred_bram"]
        builds.append(r)
    modules = []
    for r in read_table(src / "hw_modules.csv"):
        p = plan[r["build"]]
        r = r | {"build": p["build"]}
        if r["kind"] == "module" and r["name"] in MG.OLD_BLOCK.values():
            r["name"] = {"CgMm": "SystolicCore", "CgVec": "CgVectorCore"}[r["name"]]
            if p["top"] != "det":
                r["dsp"], r["bram"] = p["pred_dsp"], p["pred_bram"]
            elif r["name"] == "SystolicCore":  # the cores hold the rules' sum
                r["dsp"], r["bram"] = p["new_block_dsp"], p["new_block_bram"]
            else:
                r["dsp"], r["bram"] = "0", "0"
        modules.append(r)
    cycles = [
        r | {"build": plan[r["build"]]["build"]}
        for r in read_table(src / "hw_cycles.csv")
    ]
    for name, rows in (("builds", builds), ("modules", modules), ("cycles", cycles)):
        write_table(tmp_path / f"migration_{name}.csv", rows, "test")
    return plan


def _failed(tmp_path) -> list[tuple]:
    rows, _ = MG.compare(tmp_path)
    return [(r["build"], r["scope"], r["quantity"]) for r in rows if r["pass"] == 0]


def test_compare_passes_builds_that_came_out_as_predicted(tmp_path):
    _as_predicted(tmp_path)
    rows, summary = MG.compare(tmp_path)
    checked = [r for r in rows if r["pass"] != ""]
    assert len(checked) > 1000 and all(r["pass"] == 1 for r in checked)
    assert summary["fires"] == 0
    # the predicted A-channel changes alone move the detectors' block RAM ranks a little
    (bram,) = [
        t
        for t in summary["trigger"]
        if (t["group"], t["quantity"]) == ("detectors", "bram")
    ]
    assert 0.95 < bram["value"] < 1.0
    out = MG.write_compare(tmp_path, tmp_path)
    assert {p.name for p in out.values()} == {
        "migration_compare.csv",
        "migration_metrics.csv",
    }


def _edit(path, build, match, **values):
    rows = read_table(path)
    for r in rows:
        if r["build"] == build and all(r[k] == v for k, v in match.items()):
            r.update({k: str(v) for k, v in values.items()})
    write_table(path, rows, "test")


def test_each_criterion_catches_a_number_past_its_bound(tmp_path):
    plan = _as_predicted(tmp_path)
    det = "det_k4_l4_r4_c4_m4_w12g8_d64_s2_q2"
    mm = "mm_k8_r4_c8_m4_w12_l4"
    new_det, new_mm = plan[det]["build"], plan[mm]["build"]
    builds, cycles = (
        tmp_path / "migration_builds.csv",
        tmp_path / "migration_cycles.csv",
    )
    # each past its bound: LUT by more than 20%, a DSP the prediction does not have, the per-job
    # term by more than 100 cycles, t_iter by more than 5%, a span by more than 20 cycles
    _edit(builds, new_det, {}, lut=int(32991 * 1.21))
    _edit(builds, new_det, {}, dsp=116)
    _edit(cycles, new_det, {"quantity": "t0"}, cycles=75 + 101)
    _edit(cycles, new_det, {"quantity": "t_iter"}, cycles=1193 * 1.06)
    _edit(cycles, new_mm, {"quantity": "mm.iter"}, cycles=349 + 21)
    failed = _failed(tmp_path)
    assert (det, "total", "lut") in failed and (det, "total", "dsp") in failed
    assert (det, "cycles", "t0") in failed and (det, "cycles", "t_iter") in failed
    assert (mm, "span", "mm.iter") in failed
    assert len(failed) == 5
    # within the bounds nothing fails: 20% LUT, 100 cycles of t0, a span 20 cycles longer
    _as_predicted(tmp_path)
    _edit(builds, new_det, {}, lut=int(32991 * 1.2))
    _edit(cycles, new_det, {"quantity": "t0"}, cycles=75 + 100)
    _edit(cycles, new_mm, {"quantity": "mm.iter"}, cycles=349 + 20)
    assert _failed(tmp_path) == []


def test_every_difference_in_the_committed_comparison_has_its_cause():
    rows = read_table(MG.PAPER_DATA / "migration_compare.csv")
    changed = [
        r
        for r in rows
        if r["pass"] == "0" or (r["delta"] != "" and float(r["delta"]) != 0.0)
    ]
    assert len(changed) > 1000
    assert all(r["cause"] for r in changed)
    assert not [r for r in rows if r["cause"] == MG.CAUSES["other"]]


def test_the_migration_merges_into_its_own_tables(tmp_path):
    import json

    from examples.mimo_cg.hw import campaign as C
    from tests.examples.test_mimo_cg_hw_measure import _fake_record

    points = tmp_path / "points"
    points.mkdir()
    for build, (top, role, c) in C.split().items():
        if role == C.MIGRATION:
            rec = _fake_record(build, top, role, c)
            (points / f"{build}.json").write_text(json.dumps(rec))
    out = C.merge((C.MIGRATION,), points_dir=points, out_dir=tmp_path)
    assert sorted(out) == ["migration_builds", "migration_cycles", "migration_modules"]
    rows = read_table(out["migration_builds"])
    assert len(rows) == 132 and all(r["build"].startswith("mig_") for r in rows)
    assert "roles=migration" in out["migration_builds"].read_text().splitlines()[0]
