"""``examples/ssr_fft`` without a toolchain: the pysim, the committed measurements, the docs tables.

The RTL rung is ``tests/dsp/ssr_fft/test_xsi.py`` (``-m xsi``); ``measured.json`` is what
``ssr_fft_measure`` recorded from RTL at every length.  Here: the example's pysim is bit-exact at its
own configuration with the interval the RTL measured, the measurements say what the docs say they
say, and the docs' tables are the measurements -- so re-measuring cannot leave a stale page behind.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from examples.ssr_fft.ssr_fft import L, N_FRAMES, R, golden, pysim_frame_cycles, pysim_output, run_pysim
from examples.ssr_fft.ssr_fft_figures import vitis_intervals, vitis_resources
from waveflow.vitis_l1.testbench import default_platform_dir

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs" / "examples" / "ssr_fft"
RUNS = json.loads((REPO / "examples" / "ssr_fft" / "measured.json").read_text(encoding="utf-8"))["runs"]
PP = {r["L"]: r for r in RUNS.values() if r["reorder"] == "pingpong"}


def _rows(page: str, first_col: str = "L") -> dict[int, list[str]]:
    """The first markdown table on *page* whose header starts with *first_col*, keyed by its first cell."""
    text = (DOCS / page).read_text(encoding="utf-8")
    out, on = {}, False
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else None
        if cells and cells[0] == first_col:
            on = True
            continue
        if on and cells:
            if set(cells[0]) <= set("-"):
                continue
            out[int(cells[0])] = cells[1:]
        elif on and out:
            break
    return out


def _n(cell: str) -> int:
    return int(re.sub(r"[,*]", "", cell))


def test_example_pysim_is_bit_exact_at_the_measured_interval(tmp_path):
    tb = run_pysim(tmp_path)
    for (g_re, g_im), (w_re, w_im) in zip(pysim_output(tb), golden(N_FRAMES, L)):
        assert np.array_equal(g_re, w_re) and np.array_equal(g_im, w_im)
    assert set(np.round(np.diff(pysim_frame_cycles(tb))).astype(int).tolist()) == {PP[L]["interval"][0]} == {L // R}


def test_every_measured_run_is_bit_exact_at_l_over_r():
    """What the docs claim of the RTL: bit-exact everywhere; ping-pong at L/R; SOB at L/R + 4; an
    isolated frame's latency a single value, equal to the first frame's."""
    for r in RUNS.values():
        assert r["bits_exact"], r
        want = r["L"] // 4 + (4 if r["reorder"] == "sob" else 0)
        assert r["interval"] == [want], r
        assert r["isolated_latency"] == [r["first_frame"]], r
        assert len(r["isolated_span"]) == 1, r
        assert r["resources"]["dsp"] == 12 * (round(np.log(r["L"]) / np.log(4)) - 1), r


def test_timing_table_is_the_measurements():
    vit = vitis_intervals(default_platform_dir())
    rows = _rows("timing.md")
    assert sorted(rows) == sorted(PP)
    for n, cells in rows.items():
        r = PP[n]
        assert [_n(c) for c in cells] == [r["interval"][0], r["first_frame"], r["isolated_latency"][0],
                                          r["isolated_span"][0], round(vit[n])], n


def test_resource_table_is_the_measurements():
    vit = vitis_resources(default_platform_dir())
    rows = _rows("resource.md")
    assert sorted(rows) == sorted(PP)
    for n, cells in rows.items():
        res = PP[n]["resources"]
        assert [_n(c) for c in cells[:4]] == [res["dsp"], res["bram"], res["lut"], res["ff"]], n
        v = [_n(c) for c in cells[4].split("/")]
        assert v == [vit[n]["dsp"], vit[n]["bram"], vit[n]["lut"], vit[n]["ff"]], n


def test_guide_table_is_the_measurements():
    """The guide's overview table quotes the same intervals."""
    text = (REPO / "docs" / "guide" / "dsp" / "ssr_fft" / "index.md").read_text(encoding="utf-8")
    vit = vitis_intervals(default_platform_dir())
    for n, r in PP.items():
        row = re.search(rf"^\| {n} \| \*\*([\d,]+)\*\* \| ([\d,]+) \| (\d+) \|$", text, re.M)
        assert row, n
        assert (_n(row.group(1)), _n(row.group(2)), int(row.group(3))) == (
            r["interval"][0], round(vit[n]), r["resources"]["dsp"]), n
