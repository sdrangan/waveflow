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


# --- the example page's section 9 quotes the committed tables ---------------------------------


def _pct(new, old) -> list[float]:
    return [100.0 * (n / o - 1.0) for n, o in zip(new, old, strict=True)]


def _signed(x: float, digits: int = 0) -> str:
    """As the page writes a change: a true minus sign, a plus sign otherwise."""
    text = f"{abs(x):.{digits}f}"
    return ("−" if x < 0 else "+") + text


def test_the_example_page_quotes_the_migration_tables():
    import statistics

    page = (
        MG.EXAMPLE.parents[1] / "docs" / "examples" / "mimo_cg" / "index.md"
    ).read_text(encoding="utf-8")
    section = page[page.index("## 9. The detector on Waveflow's components") :]
    old = {r["build"]: r for r in read_table(MG.PAPER_DATA / "hw_builds.csv")}
    new = {
        MG.old_label(r["build"]): r
        for r in read_table(MG.PAPER_DATA / "migration_builds.csv")
    }
    dets = [b for b, r in old.items() if r["top"] == "det"]
    assert all(r["bit_exact"] == "1" for r in new.values()) and len(new) == 132
    fin = read_table(MG.PAPER_DATA / "migration_finalists.csv")
    assert all(r["bit_exact"] == "1" and r["timing_met"] == "1" for r in fin)
    assert "132 of 132 builds, 12 of 12 finalists" in section
    cp = [float(r["cp_post_impl_ns"]) for r in fin]
    assert f"({min(cp):.2f}–{max(cp):.2f} ns)" in section
    # detector totals
    for ctr, text in (
        ("lut", "Median {m}%, from {lo}% to {hi}%"),
        ("ff", "Median {m}%, from {lo}% to {hi}%"),
    ):
        p = _pct([float(new[b][ctr]) for b in dets], [float(old[b][ctr]) for b in dets])
        digits = 0 if ctr == "lut" else 1
        want = text.format(
            m=_signed(statistics.median(p), digits),
            lo=_signed(min(p)),
            hi=_signed(max(p)),
        )
        assert want in section, (ctr, want)
    d_dsp = sorted({int(new[b]["dsp"]) - int(old[b]["dsp"]) for b in dets})
    assert (
        d_dsp == [3, 6]
        and sum(int(new[b]["dsp"]) - int(old[b]["dsp"]) == 6 for b in dets) == 1
    )
    # cycles per iteration and the start
    rows = {
        s: [
            r
            for r in read_table(MG.PAPER_DATA / f"{s}_cycles.csv")
            if r["top"] == "det"
        ]
        for s in ("hw", "migration")
    }

    def q(s: str, quantity: str) -> dict:
        return {
            MG.old_label(r["build"]): float(r["cycles"])
            for r in rows[s]
            if r["quantity"] == quantity
        }

    t_old, t_new = q("hw", "t_iter"), q("migration", "t_iter")
    p = _pct([t_new[b] for b in dets], [t_old[b] for b in dets])
    want = f"{_signed(min(p), 1)}% to {_signed(max(p), 1)}% (median {_signed(statistics.median(p), 1)}%)"
    assert want in section, want
    s_old, s_new = q("hw", "vec.init"), q("migration", "vec.init")
    by_lanes: dict = {}
    for b in dets:
        by_lanes.setdefault(int(old[b]["L"]), []).append(round(s_new[b] - s_old[b]))
    per_group = [
        d / (32 / L) for L, deltas in by_lanes.items() for d in deltas
    ]  # N = 32 columns
    lo16, hi16, lo1, hi1 = (
        min(by_lanes[16]),
        max(by_lanes[16]),
        min(by_lanes[1]),
        max(by_lanes[1]),
    )
    assert (
        f"{min(per_group):.1f}–{max(per_group):.1f} more cycles per group of L columns: "
        f"+{lo16} to +{hi16} cycles at 16 lanes, +{lo1} to +{hi1} at one lane"
    ) in section
    # the finalists, implemented
    was = {r["build"]: r for r in read_table(MG.PAPER_DATA / "finalists_impl.csv")}
    for ctr, words in (("impl_lut", "LUTs"), ("impl_ff", "flip-flops")):
        p = _pct([float(r[ctr]) for r in fin], [float(was[r["old"]][ctr]) for r in fin])
        assert f"{words} {_signed(min(p))}% to {_signed(max(p))}%" in section, ctr
    p = _pct(
        [float(r["rtl_job"]) for r in fin],
        [float(was[r["old"]]["rtl_job"]) for r in fin],
    )
    assert f"job time {_signed(min(p), 1)}% to {_signed(max(p), 1)}%" in section
    assert all(int(r["impl_bram"]) < int(was[r["old"]]["impl_bram"]) for r in fin)


def test_the_example_page_quotes_the_brute_force_on_the_components():
    page = (
        MG.EXAMPLE.parents[1] / "docs" / "examples" / "mimo_cg" / "index.md"
    ).read_text(encoding="utf-8")
    section = page[page.index("**The brute force, repeated on the components.**") :]
    section = section[: section.index("**Provenance.**")]
    new = {
        r["resource"]: float(r["right_pct"])
        for r in read_table(MG.PAPER_DATA / "migration_bruteforce_fidelity_metrics.csv")
    }
    old = {
        r["resource"]: float(r["right_pct"])
        for r in read_table(MG.PAPER_DATA / "decision_fidelity_metrics.csv")
        if r["set"] == "all"
    }
    order = ("dsp", "lut", "ff", "bram")
    row = " | ".join(f"{new[k]:.1f}%" for k in order)
    assert f"| On the components | {row} |" in section
    row = " | ".join(f"{old[k]:.1f}%" for k in order)
    assert f"| On the Phase 4 hardware (section 7) | {row} |" in section
    assert min(new[k] for k in order) >= 95.0  # "in at least 95% of the decisions"
    rows = read_table(MG.PAPER_DATA / "migration_bruteforce_decisions.csv")
    assert len(rows) == 2592 and not any(
        r["measured"] == "1" and r["met"] == "0" for r in rows
    )  # no chosen design misses its job-time budget
    err = {
        r["metric"]: float(r["value"])
        for r in read_table(MG.PAPER_DATA / "migration_bruteforce_error_metrics.csv")
    }
    for metric, text in (
        ("DSP exact (%)", "DSP is exact on {:.1f}%"),
        ("BRAM exact (%)", "block RAM on {:.1f}%"),
        ("LUT MAPE (%)", "LUT error averages {:.1f}%"),
        ("LUT MAPE (%), 1 lanes", "({:.1f}% at one lane"),
        ("FF MAPE (%)", "flip-flops {:.1f}%"),
        ("job time MAPE (%), loop-dominated", "job time {:.1f}%"),
    ):
        assert text.format(err[metric]) in " ".join(section.split()), metric
    builds = read_table(MG.PAPER_DATA / "migration_bruteforce_builds.csv")
    assert len(builds) == 1440 and all(r["bit_exact"] == "1" for r in builds)
