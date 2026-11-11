"""examples/dse_cg/grid.py's re-timing of the runs G1 did not time at its condition, on synthetic rows
(plans/dse_cg.md, the row of 2026-11-11): which runs are off it, which re-timed runs count, and the
table's tool time for them.  No toolchain."""

from __future__ import annotations

import json

import pytest

from examples.dse_cg import grid


def _run(key, at, wall, *, build=True, status="ok", cycles=1000):
    phases = {"compile_rtl": 4.0, "elaborate": 100.0, "compile_tb": 4.0, "simulate": wall - 108} if build \
        else {"simulate": wall}
    return {"fn": "rtlsim", "at": at, "wall_s": wall, "sim_identity": {"build": "b", "run": key},
            "params": {}, "workload": {"name": "long_term", "t": 2},
            "row": {"status": status, "cycles": cycles, "bit_exact": True, "phases_s": phases,
                    "build_s": 108.0 if build else 0.0, "simulate_s": phases["simulate"]}}


def test_covered_counts_overlapping_partners_once():
    assert grid.covered((0, 100), [(0, 50), (25, 75)]) == pytest.approx(0.75)
    assert grid.covered((0, 100), [(-10, 200)]) == 1.0
    assert grid.covered((0, 100), [(200, 300)]) == 0.0


def test_off_condition_names_each_reason():
    files = {
        "g1_0": [_run("normal", "2026-11-10 12:10:00", 600),
                 _run("normal2", "2026-11-10 12:20:00", 600),
                 _run("job", "2026-11-07 12:40:00", 600),                 # inside the live gate
                 _run("reused", "2026-11-10 12:30:00", 600, build=False),
                 _run("alone", "2026-11-10 14:00:00", 600)],              # the partner had ended
        "g1_1": [_run("partner", "2026-11-10 12:30:00", 1800),            # 12:00-12:30, g1_0 busy
                 _run("partner2", "2026-11-07 12:41:00", 700)],
    }
    off = grid.off_condition(files)
    assert off == {"job": "live gate", "reused": "build reused", "alone": "no partner",
                   "partner2": "live gate"}


def _retimed(key, proc, start, end, *, same=True):
    row = {"status": "ok", "cycles": 1000, "bit_exact": True, "build_s": 100.0, "simulate_s": end - start - 100}
    return {"key": key, "why": "live gate", "proc": proc, "row": row, "start": start, "end": end,
            "same_result": same, "g1_cost_s": 900.0}


def test_a_retimed_run_counts_with_a_partner_and_g1s_result():
    ents = [_retimed("a", 0, 0, 600), _retimed("b", 1, 0, 400),
            {"filler": True, "proc": 1, "start": 400, "end": 1000},
            _retimed("c", 0, 600, 1200),                                  # partner gone at 1000
            _retimed("d", 1, 1000, 1100, same=False)]
    assert [e["key"] for e in grid.valid_retimes(ents)] == ["a", "b"]


def test_the_table_takes_the_retimed_tool_time(tmp_path, monkeypatch):
    monkeypatch.setattr(grid, "OUT", tmp_path)
    monkeypatch.setattr(grid, "TABLE", tmp_path / "table")
    rows = [_run("k1", "2026-11-10 12:10:00", 1000), _run("k2", "2026-11-10 12:10:00", 1000)]
    (tmp_path / "g1_0.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "retime_0.jsonl").write_text(json.dumps(_retimed("k1", 0, 0, 700)) + "\n")
    (tmp_path / "retime_1.jsonl").write_text(json.dumps({"filler": True, "proc": 1, "start": 0, "end": 800}) + "\n")
    grid.table()
    tab = {e["key"]: e for e in map(json.loads, (tmp_path / "table" / "rtlsim.jsonl").read_text().splitlines())}
    assert tab["k1"]["cost_s"] == pytest.approx(700) and tab["k1"]["retimed"]["g1_cost_s"] == pytest.approx(1000)
    assert tab["k2"]["cost_s"] == pytest.approx(1000) and "retimed" not in tab["k2"]
