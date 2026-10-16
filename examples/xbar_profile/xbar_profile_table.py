"""xbar_profile_table.py — the area / closure and latency tables, read back from the record store.

Reads the **store**, not the sweep's log: the store is where the measurements live (the sweep summary
only points at them), so a table built from it is the same table any later reader -- a resource
model, an agent -- would see::

    python -m examples.xbar_profile.xbar_profile_table            # print, and write results/
    python -m examples.xbar_profile.xbar_profile_table --platform-dir <dir>

Writes ``results/xbar_profile.md`` (the tables the docs page carries) and ``results/xbar_profile.json``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

from waveflow.calib.record_store import ModuleStore  # noqa: E402

from examples.xbar_profile.xbar_profile import (  # noqa: E402
    BEATS,
    DATA_WIDTHS,
    MODES,
    PART,
    PERIOD_NS,
    SHAPES,
    xbar_config,
)
from examples.xbar_profile.xbar_profile_build import PLATFORMS_ROOT, platform_name  # noqa: E402

LAT_COLS = ("w1", "r1", f"w{BEATS}", f"r{BEATS}", f"par_w{BEATS}", f"par_r{BEATS}",
            f"same_w{BEATS}", f"same_r{BEATS}")


def collect(platform_dir: Path, part: str = PART) -> list[dict]:
    """One row per configuration that has records: area (routed), closure, latencies."""
    store = ModuleStore(platform_dir)
    rows = []
    for dw in DATA_WIDTHS:
        for shape in SHAPES:
            for mode in MODES:
                cfg = xbar_config(shape, mode, dw, part=part)
                ident = cfg.identity()
                impl = store.read(ident.key, "resource", identity=ident, source="vivado_impl")
                synth = store.read(ident.key, "resource", identity=ident, source="vivado_synth")
                timing = store.read(ident.key, "timing", identity=ident, source="xsi")
                if not (impl or synth or timing):
                    continue
                res = (impl or synth)[-1].payload if (impl or synth) else {}
                lat = {r.payload["features"]["probe"]: r.payload["cycles"] for r in timing}
                rows.append({"shape": shape, "dw": dw, "mode": mode, "key": ident.key,
                             "routed": bool(impl),
                             "lut": res.get("lut"), "ff": res.get("ff"), "srl": res.get("srl"),
                             "lutram": res.get("lutram"), "bram": res.get("bram"),
                             "dsp": res.get("dsp"),
                             "wns_ns": res.get("wns_ns"), "met": res.get("met"),
                             "logic_levels": res.get("logic_levels"),
                             "synth_lut": synth[-1].payload.get("lut") if synth else None,
                             "synth_ff": synth[-1].payload.get("ff") if synth else None,
                             "latency": lat})
    return rows


def _f(v, fmt="{}"):
    return "--" if v is None else fmt.format(v)


def render(rows: list[dict]) -> str:
    """Markdown: an area / closure table and a latency table."""
    by = {(r["shape"], r["dw"], r["mode"]): r for r in rows}
    out = [f"### Area and timing closure at {1e3 / PERIOD_NS:g} MHz (routed, out of context)", "",
           "| shape | width | SAMD LUT | SAMD FF | SAMD WNS (ns) | SASD LUT | SASD FF | SASD WNS (ns) |",
           "|---|---|---|---|---|---|---|---|"]
    for dw in DATA_WIDTHS:
        for shape in SHAPES:
            a, b = by.get((shape, dw, "SAMD")), by.get((shape, dw, "SASD"))
            if not (a or b):
                continue
            cells = [shape, str(dw)]
            for r in (a, b):
                if r is None:
                    cells += ["--"] * 3
                    continue
                wns = _f(r["wns_ns"], "{:+.3f}") + ("" if r["met"] in (None, True) else " **fail**")
                cells += [_f(r["lut"]), _f(r["ff"]), wns]
            out.append("| " + " | ".join(cells) + " |")
    out += ["", "### Latency in cycles (XSI; first address phase to last response, inclusive)", "",
            "| shape | width | mode | " + " | ".join(LAT_COLS) + " |",
            "|---|---|---|" + "---|" * len(LAT_COLS)]
    # One row per (shape, mode) when every width measured the same latencies -- the width only
    # widens the data path, so it normally does -- and one row per width when they differ.
    for shape in SHAPES:
        for mode in MODES:
            per = {dw: by[(shape, dw, mode)]["latency"] for dw in DATA_WIDTHS
                   if (shape, dw, mode) in by and by[(shape, dw, mode)]["latency"]}
            if not per:
                continue
            if len({json.dumps(v, sort_keys=True) for v in per.values()}) == 1:
                groups = [(", ".join(map(str, per)), next(iter(per.values())))]
            else:
                groups = [(str(dw), lat) for dw, lat in per.items()]
            for width, lat in groups:
                out.append(f"| {shape} | {width} | {mode} | "
                           + " | ".join(_f(lat.get(c)) for c in LAT_COLS) + " |")
    return "\n".join(out) + "\n"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--part", default=PART)
    ap.add_argument("--platform-dir", type=Path, default=None,
                    help="the platform directory to read (default: the sweep's work-tier platform)")
    args = ap.parse_args(argv)
    pdir = args.platform_dir or (PLATFORMS_ROOT / platform_name(args.part))
    rows = collect(pdir, part=args.part)
    if not rows:
        print(f"no crossbar records under {pdir}; run xbar_profile_sweep first")
        return 1
    md = render(rows)
    res = HERE / "results"
    res.mkdir(parents=True, exist_ok=True)
    (res / "xbar_profile.md").write_text(md, encoding="utf-8")
    (res / "xbar_profile.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(md)
    print(f"{len(rows)} configuration(s) from {pdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
