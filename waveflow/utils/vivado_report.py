"""vivado_report.py — read a Vivado ``report_utilization`` table into resource counters.

The sibling of :mod:`waveflow.utils.csynthparse` for **Vivado** synthesis and implementation, where
the numbers are measured on a netlist rather than estimated by HLS.  ``sv_synth`` / ``sv_impl`` write
``util.rpt`` and :func:`waveflow.build.axi_xbar.synth_axi_xbar` writes one per run; this is what turns
it into the counters a resource record stores (``waveflow.calib.record_store``).

The report is a set of ASCII tables.  Only the ``Site Type | Used | ...`` rows are read, by name, so
the parser is indifferent to the column layout (which moves between Vivado releases) and to the
families' different spellings:

==========================  =========================  ============
UltraScale+                 7-series                   counter
==========================  =========================  ============
``CLB LUTs``                ``Slice LUTs``             ``lut``
``CLB Registers``           ``Slice Registers``        ``ff``
``LUT as Shift Register``   ``LUT as Shift Register``  ``srl``
``LUT as Distributed RAM``  ``LUT as Distributed RAM`` ``lutram``
``Block RAM Tile``          ``Block RAM Tile``         ``bram``
``URAM``                    --                         ``uram``
``DSPs``                    ``DSPs``                   ``dsp``
==========================  =========================  ============

**BRAM is counted in 18K halves**, not in the 36K tiles Vivado reports, so a Vivado record and an
``hls_estimate`` record (``BRAM_18K``) of the same module can be compared counter for counter.

A counter that appears twice (``LUT as Shift Register`` sits under both "LUT as Memory" and a
summary in some releases) keeps its **first** value: the first table is the top-level summary.
"""
from __future__ import annotations

import re

#: Site-type row name (``*`` and surrounding space stripped) -> canonical counter.
UTIL_ROWS: dict[str, str] = {
    "CLB LUTs": "lut", "Slice LUTs": "lut",
    "CLB Registers": "ff", "Slice Registers": "ff",
    "LUT as Shift Register": "srl",
    "LUT as Distributed RAM": "lutram",
    "Block RAM Tile": "bram",
    "URAM": "uram",
    "DSPs": "dsp",
}

_ROW = re.compile(r"^\|\s*(?P<name>[^|]+?)\s*\|\s*(?P<used>[0-9.]+)\s*\|")


def parse_utilization(text: str) -> dict[str, int]:
    """Counters from a ``report_utilization`` text report.

    Returns ``{lut, ff, srl, lutram, bram, uram, dsp}`` for whichever rows the report has; a family
    without a resource (no URAM on a 7-series) simply lacks the key.  Raises ``ValueError`` if the
    text has no LUT row at all -- a report that cannot be read must not become a record of zeros.
    """
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = _ROW.match(line.strip())
        if m is None:
            continue
        name = m.group("name").rstrip("*").strip()
        key = UTIL_ROWS.get(name)
        if key is None or key in out:
            continue
        used = float(m.group("used"))
        out[key] = int(round(used * 2)) if key == "bram" else int(round(used))
    if "lut" not in out:
        raise ValueError("no LUT row in the utilization report; is this a report_utilization file?")
    return out


#: ``report_utilization -hierarchical`` column -> canonical counter, and its weight (RAMB36 is two
#: 18K halves, as in :func:`parse_utilization`).  Spellings differ between families and releases.
HIER_COLUMNS: dict[str, tuple[str, int]] = {
    "Total LUTs": ("lut", 1), "FFs": ("ff", 1), "SRLs": ("srl", 1), "LUTRAMs": ("lutram", 1),
    "RAMB36": ("bram", 2), "RAMB18": ("bram", 1), "URAM": ("uram", 1),
    "DSP Blocks": ("dsp", 1), "DSP48 Blocks": ("dsp", 1), "DSPs": ("dsp", 1),
}


def parse_utilization_hierarchical(text: str) -> list[dict]:
    """Rows of a ``report_utilization -hierarchical`` table, top first.

    Each row is ``{"instance", "module", "depth", "counters"}``: *depth* 0 is the top, 1 its child
    instances, and so on (Vivado indents each level by two spaces); *counters* are the canonical
    names of :data:`HIER_COLUMNS`, BRAM in 18K halves.  A parent's figures **include** its children's,
    as in the csynth report.  Raises ``ValueError`` when no table is found.
    """
    header: list[str] | None = None
    rows: list[dict] = []
    base: int | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.startswith("|"):
            continue
        cells = line.split("|")[1:-1]
        if header is None:
            names = [c.strip() for c in cells]
            if names and names[0] == "Instance" and "Module" in names:
                header = names
            continue
        if len(cells) != len(header):
            continue
        name_cell = cells[0]
        indent = len(name_cell) - len(name_cell.lstrip(" "))
        if base is None:
            base = indent
        counters: dict[str, int] = {}
        for col, cell in zip(header[2:], cells[2:]):
            if col not in HIER_COLUMNS:
                continue
            key, weight = HIER_COLUMNS[col]
            try:
                v = float(cell.strip())
            except ValueError:
                continue
            counters[key] = counters.get(key, 0) + int(round(v * weight))
        rows.append({"instance": name_cell.strip(), "module": cells[1].strip(),
                     "depth": (indent - base) // 2, "counters": counters})
    if header is None or not rows:
        raise ValueError("no hierarchical utilization table found; is this "
                         "report_utilization -hierarchical output?")
    return rows


def parse_markers(text: str, prefix: str = "WF_") -> dict[str, str]:
    """``{NAME: value}`` from ``WF_NAME value`` lines a TCL script printed with ``puts``.

    Values Vivado computes directly (slack, logic levels, the tool version) are printed by the
    script rather than parsed out of a timing report, which is both shorter and immune to the
    report's layout.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            name, _sep, value = line[len(prefix):].partition(" ")
            out[name] = value.strip()
    return out
