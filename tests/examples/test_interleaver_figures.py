"""tests/examples/test_interleaver_figures.py — the activity-diagram figure renders.

Toolchain-free: the interleaver figures render straight from the pysim timeline (each stage's
``fire_log``), so this exercises the whole path — sim → ActivityDiagram → PNG — without Vitis/XSI.
"""
from __future__ import annotations

from pathlib import Path


def test_save_figures_creates_png(tmp_path):
    import sys
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "examples" / "interleaver"))
    from interleaver_figures import save_figures

    saved = save_figures(tmp_path, n_jobs=2, n=64)
    assert len(saved) >= 1
    for p in saved:
        p = Path(p)
        assert p.exists() and p.stat().st_size > 0
        assert p.suffix == ".png"
