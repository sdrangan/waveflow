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


def test_a_failed_vivado_run_is_recorded_and_not_taken_for_done(tmp_path, monkeypatch):
    """The tool exits non-zero when Vivado fails or crashes (it did once, out of memory), and it
    writes the export report early.  Neither may pass for a finished run."""
    import subprocess
    from types import SimpleNamespace

    from examples.mimo_cg.hw import build as B
    from examples.mimo_cg.hw import impl_check

    monkeypatch.setattr(B, "BUILD_ROOT", tmp_path)
    sol = tmp_path / "x" / f"{impl_check.TOP}_proj" / "solution1"
    (sol / "syn" / "report").mkdir(parents=True)
    (sol / "syn" / "report" / "csynth.xml").write_text("<x/>")
    report = impl_check.report_path("x")
    report.parent.mkdir(parents=True)
    report.write_text("early, not final")  # the tool writes this before it is done
    assert not impl_check.implemented("x")

    def crash(*a, **k):
        raise subprocess.CalledProcessError(
            1, ["vitis-run"], output="Segmentation fault"
        )

    monkeypatch.setattr(impl_check.toolchain, "run_vitis_hls", crash)
    res = impl_check.run_impl("x")
    assert res["ok"] is False
    assert "IMPL_FAILED" in (tmp_path / "x" / "impl.log").read_text()
    assert not impl_check.implemented("x")  # so a re-run builds it again

    ok = SimpleNamespace(stdout="... IMPL_OK\n", stderr="")
    monkeypatch.setattr(impl_check.toolchain, "run_vitis_hls", lambda *a, **k: ok)
    assert impl_check.run_impl("x")["ok"] is True and impl_check.implemented("x")


# --- the finalists as implemented (step 6.7) ---------------------------------------------------


def _impl() -> list[dict]:
    return read_table(FN.PAPER_DATA / "finalists_impl.csv")


def test_every_finalist_is_implemented_meets_timing_and_is_bit_exact(chosen):
    rows = _impl()
    assert [r["name"] for r in rows] == [r["name"] for r in chosen]
    head = (FN.PAPER_DATA / "finalists_impl.csv").read_text().splitlines()[0]
    assert "Vivado v.2024.1" in head and "hls=vitis_hls 2024.1" in head
    for r, c in zip(rows, chosen, strict=True):
        assert r["timing_met"] == "1" and float(r["cp_post_impl_ns"]) <= 4.0
        assert r["bit_exact"] == "1"
        assert all(int(r[f"model_{k}"]) == c[f"model_{k}"] for k in FN.COUNTERS)
    clocks = [float(r["cp_post_impl_ns"]) for r in rows]
    assert (
        min(clocks) == 2.7 and max(clocks) == 3.913
    )  # 0.09 ns of slack at the tightest


def test_the_models_hold_on_the_finalists():
    """None of the twelve is a calibration or brute-force build (they come from the whole space,
    with 8 lanes and block depth 3 among them).  Against csynth: DSP and block RAM exact, LUT
    within 2.1%, FF within 5.1%; against the RTL run: job time within 2.4%."""
    for r in _impl():
        assert int(r["model_dsp"]) == int(r["csynth_dsp"])
        assert int(r["model_bram"]) == int(r["csynth_bram"])
        assert abs(int(r["model_lut"]) / int(r["csynth_lut"]) - 1) < 0.021, r["name"]
        assert abs(int(r["model_ff"]) / int(r["csynth_ff"]) - 1) < 0.052, r["name"]
        assert abs(float(r["model_job"]) / float(r["rtl_job"]) - 1) < 0.024, r["name"]


def test_what_place_and_route_changes():
    rows = _impl()
    lut = [int(r["csynth_lut"]) / int(r["impl_lut"]) for r in rows]
    ff = [int(r["csynth_ff"]) / int(r["impl_ff"]) for r in rows]
    assert 2.7 < min(lut) and max(lut) < 3.9  # csynth over-counts LUTs about threefold
    assert 1.1 < min(ff) and max(ff) < 1.8
    # the order of the twelve by LUTs, and by flip-flops, is the model's order exactly
    for k in ("lut", "ff"):
        by_model = sorted(rows, key=lambda r: int(r[f"model_{k}"]))
        by_impl = sorted(rows, key=lambda r: int(r[f"impl_{k}"]))
        assert [r["name"] for r in by_model] == [r["name"] for r in by_impl], k
    # DSPs: four more per lane than csynth counts from 12 bits up ...
    for r in rows:
        if int(r["W"]) >= 12:
            assert int(r["impl_dsp"]) - int(r["csynth_dsp"]) == 4 * int(
                r["build"].split("_l")[1].split("_")[0]
            )
    # ... and at 10 bits Vivado puts back in DSPs the multiplies csynth built from LUTs, so the
    # 10-bit design has as many DSPs as its 14-bit counterpart
    by = {r["name"]: r for r in rows}
    narrow, wide = by["k8_loose_guard"], by["k8_loose_no_guard"]
    assert (narrow["W"], narrow["csynth_dsp"], wide["csynth_dsp"]) == ("10", "17", "24")
    assert narrow["impl_dsp"] == wide["impl_dsp"] == "28"


def test_what_guard_bits_are_worth_as_implemented():
    """Six pairs.  With guard bits the implemented design is smaller in LUTs and in flip-flops in
    every pair; the DSP saving csynth shows at 10 bits is not there."""
    pairs = read_table(FN.PAPER_DATA / "finalists_pairs.csv")
    assert pairs == read_table(FN.PAPER_DATA / "finalists_pairs.csv")
    again = FN.pair_rows(
        [
            {
                k: (v if k in ("name", "build", "speed", "guard", "tool") else float(v))
                for k, v in r.items()
                if v != ""
            }
            | {
                "K": int(r["K"]),
                "W": int(r["W"]),
                "g_s": int(r["g_s"]),
                "nit": int(r["nit"]),
            }
            for r in _impl()
        ]
    )
    assert len(pairs) == len(again) == 6
    for a, b in zip(pairs, again, strict=True):
        assert (a["K"], a["speed"], a["with"], a["without"]) == (
            str(b["K"]),
            b["speed"],
            b["with"],
            b["without"],
        )
        for k in FN.COUNTERS:
            for src in ("model", "csynth", "impl"):
                assert float(a[f"{src}_{k}_pct"]) == pytest.approx(
                    b[f"{src}_{k}_pct"], abs=0.01
                )
    lut = sorted(float(p["impl_lut_pct"]) for p in pairs)
    ff = sorted(float(p["impl_ff_pct"]) for p in pairs)
    assert lut[0] > 1.0 and lut[-1] < 18.0 and 7.0 < (lut[2] + lut[3]) / 2 < 11.0
    assert ff[0] > 3.0 and ff[-1] < 18.5 and 14.0 < (ff[2] + ff[3]) / 2 < 16.0
    k8 = next(p for p in pairs if (p["K"], p["speed"]) == ("8", "loose"))
    assert float(k8["csynth_dsp_pct"]) > 40 and float(k8["impl_dsp_pct"]) == 0
    # where the guard is left out and the width is not raised enough, the job runs longer
    slow = {(p["K"], p["speed"]): float(p["rtl_job_pct"]) for p in pairs}
    assert slow[("4", "loose")] > 80 and slow[("8", "loose")] > 20
    assert all(
        abs(v) < 1 for k, v in slow.items() if k not in (("4", "loose"), ("8", "loose"))
    )
