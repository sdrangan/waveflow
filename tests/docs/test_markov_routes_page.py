"""markov's route comparison quotes its record, not typed-in numbers
(``plans/ip_packaging_routes.md`` Stage 6).  The record is
``docs/examples/markov/images/routes.json`` (committed past the ``*.json`` ignore), written by
``markov_routes`` and promoted by ``sync_routes_figure``; ``tests/examples/test_markov_routes.py``
holds its area to a fresh Vivado run.  Here, with no toolchain: every cell of ``synth.md``'s two
route tables, and the figures ``flows/bitstream_ipi.md`` repeats, are the record's.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RECORD = REPO / "docs" / "examples" / "markov" / "images" / "routes.json"


def _n(v: int) -> str:
    return f"{v:,}".replace("-", "−")


def _rows(text: str) -> dict[str, list[str]]:
    """``{first cell: [other cells]}`` of every table row, bold stripped."""
    out = {}
    for line in text.splitlines():
        if line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip().replace("**", "") for c in line.strip().strip("|").split("|")]
            out[cells[0]] = cells[1:]
    return out


def _section(text: str, anchor: str) -> str:
    i = text.index(anchor)
    j = text.find("\n## ", i + 1)
    return text[i:] if j < 0 else text[i:j]


def test_the_area_table_is_the_record():
    rec = json.loads(RECORD.read_text(encoding="utf-8"))
    page = (REPO / "docs" / "examples" / "markov" / "synth.md").read_text(encoding="utf-8")
    rows = _rows(_section(page, "{#routes}"))
    pd = rec["per_device"]
    want = {"generator device, alone": pd["parts"]["gen"],
            "chain device, alone": pd["parts"]["chain"],
            "memory, alone": pd["parts"]["mem"], "crossbar and glue, alone": pd["top"],
            "link term": pd["link_term"], "per device, linked": pd["linked"],
            "global": rec["global"]["opt"]}
    for label, c in want.items():
        assert rows[label] == [_n(c["lut"]), _n(c["ff"]), _n(c["bram"])], label
    assert f"{rec['gap']['lut']} LUT (+{rec['gap_pct']['lut']:.2f}%)" in page
    assert f"and {rec['gap']['ff']} FF" in page


def test_the_time_table_is_the_record():
    rec = json.loads(RECORD.read_text(encoding="utf-8"))
    s = rec["seconds"]
    page = (REPO / "docs" / "examples" / "markov" / "synth.md").read_text(encoding="utf-8")
    rows = _rows(_section(page, "{#routes}"))
    one = sorted(s["per_device_one_changed"].values())
    assert rows["cold"] == [f"{s['global']:.0f} s", f"{s['per_device_cold']:.0f} s"]
    assert rows["one device changed"] == [f"{s['global']:.0f} s",
                                          f"{one[0]:.0f}-{one[-1]:.0f} s"]
    assert rows["nothing changed"] == ["none", f"{s['per_device_unchanged']:.0f} s (the link)"]


def test_the_flows_page_repeats_the_record():
    rec = json.loads(RECORD.read_text(encoding="utf-8"))
    s = rec["seconds"]
    page = (REPO / "docs" / "guide" / "flows" / "bitstream_ipi.md").read_text(encoding="utf-8")
    rows = _rows(page)
    worst = max(s["per_device_one_changed"].values())
    assert rows["global + `opt_design`"] == [_n(rec["global"]["opt"]["lut"]),
                                             f"{s['global']:.0f} s", f"{s['global']:.0f} s"]
    assert rows["per device, linked"] == [
        f"{_n(rec['per_device']['linked']['lut'])} (+{rec['gap_pct']['lut']:.2f}%)",
        f"{s['per_device_cold']:.0f} s", f"{worst:.0f} s"]
    assert re.search(rf"\b{rec['gap_pct']['lut']:.2f}% LUT", page)
