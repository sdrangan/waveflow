"""tests/examples/test_interleaver_timing_compare.py — the RTL-vs-pysim cadence comparison logic.

Toolchain-free: the RTL *extraction* (``run_rtl_timeline``) needs a trace and is exercised by the gated
``rtl_timing`` step, but the join/format (``compare_timelines`` / ``format_compare_markdown``) is pure and
pinned here with synthetic timelines.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "examples" / "interleaver"))


def _timeline(period: int, cadences: dict) -> dict:
    """A timeline dict as ``run_timeline`` / ``run_rtl_timeline`` return; ``cadences`` maps
    ``label -> (firings_per_job, cadence_cyc)``."""
    return {"n": 256, "period_cyc": period,
            "stages": [{"label": lbl, "colour": "#000", "firings_per_job": fpj, "cadence_cyc": cad}
                       for lbl, (fpj, cad) in cadences.items()]}


def test_compare_joins_by_stage_and_scores_the_period():
    from interleaver_figures import compare_timelines

    pysim = _timeline(300, {"cmd_rx": (1, 300), "MemRStream": (2, 150), "il_compute": (1, 300)})
    rtl = _timeline(302, {"cmd_rx": (1, 302), "MemRStream": (2, 151), "il_compute": (1, 302)})
    cmp = compare_timelines(pysim, rtl)

    assert cmp["period"] == {"rtl": 302, "pysim": 300, "abs_err_pct": 0.66}
    rows = {r["stage"]: r for r in cmp["stages"]}
    assert rows["MemRStream"] == {"stage": "MemRStream", "firings_per_job": 2,
                                  "rtl_cadence": 151, "pysim_cadence": 150}


def test_format_markdown_table():
    from interleaver_figures import compare_timelines, format_compare_markdown

    pysim = _timeline(300, {"MemRStream": (2, 150)})
    rtl = _timeline(302, {"MemRStream": (2, 151)})
    md = format_compare_markdown(compare_timelines(pysim, rtl))

    assert "| `MemRStream` | 2 | 151 | 150 |" in md
    assert "**period (cyc/job)** | | **302** | **300** |" in md
    assert "99.3%" in md
