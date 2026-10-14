"""finalists.py -- Vivado implementation of the DSE's finalists (step 5.4, AC10).

Each finalist (an engine build picked from ``paper_data/dse_decisions.csv``) is synthesized into a
kept tree if needed, then implemented with ``export_design -flow impl`` (Vivado 2024.1, the 4 ns
clock); the export report's utilization and post-implementation critical path are set beside
csynth's and the model's in ``paper_data/finalists_impl.csv``.  AC10: timing met (WNS >= 0, i.e.
the achieved period <= 4 ns).

    python -m examples.ltbf_cg.hw.finalists --builds f_n256_c240_m8_r4x4_w12 ...
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from examples.ltbf_cg.hw import build as hb
from examples.ltbf_cg.hw import models as md
from examples.ltbf_cg.hw import space as sp
from waveflow.toolchain import toolchain

PAPER = md.eg.HERE.parent / "paper_data"
_TCL = f"""\
open_project {hb.TOP}_proj
open_solution solution1
if {{[catch {{export_design -flow impl -rtl verilog}} res]}} {{ puts "IMPL_ERROR: $res"; exit 2 }}
puts "IMPL_OK"
exit 0
"""
_FIELDS = {
    "lut": r"^LUT:\s+(\d+)",
    "ff": r"^FF:\s+(\d+)",
    "dsp": r"^DSP:\s+(\d+)",
    "bram": r"^BRAM:\s+(\d+)",
    "cp_required": r"^CP required:\s+([\d.]+)",
    "cp_post_synth": r"^CP achieved post-synthesis:\s+([\d.]+)",
    "cp_post_impl": r"^CP achieved post-implementation:\s+([\d.]+)",
}


def report_path(name: str) -> Path:
    sol = hb.BUILD / name / f"{hb.TOP}_proj" / "solution1"
    return sol / "impl" / "report" / "verilog" / f"{hb.TOP}_export.rpt"


def parse_report(text: str) -> dict:
    out: dict = {}
    for k, pat in _FIELDS.items():
        m = re.search(pat, text, re.MULTILINE)
        if m is None:
            raise ValueError(f"no {k!r} in the implementation report")
        out[k] = float(m.group(1)) if k.startswith("cp_") else int(m.group(1))
    out["timing_met"] = int(re.search(r"^Timing met", text, re.MULTILINE) is not None)
    return out


def run_impl(k: sp.Knobs) -> dict:
    out = hb.BUILD / k.name
    if not (
        out / f"{hb.TOP}_proj" / "solution1" / "syn" / "report" / "csynth.xml"
    ).is_file():
        hb.csynth(k.as_dict(), keep_tree=True)
    (out / "impl.tcl").write_text(_TCL)
    t0 = time.perf_counter()
    try:
        run = toolchain.run_vitis_hls(
            out / "impl.tcl", work_dir=out, capture_output=True
        )
        log = (run.stdout or "") + (run.stderr or "")
    except subprocess.CalledProcessError as exc:
        log = f"IMPL_FAILED: {exc}\n" + (exc.stdout or "") + (exc.stderr or "")
    sec = round(time.perf_counter() - t0, 1)
    (out / "impl.log").write_text(log)
    ok = "IMPL_OK" in log and report_path(k.name).is_file()
    rec = {"build": k.name, "ok": ok, "impl_s": sec}
    if ok:
        rec.update(parse_report(report_path(k.name).read_text()))
    (out / "impl.json").write_text(json.dumps(rec, indent=1))
    return rec


def knobs_of(name: str) -> sp.Knobs:
    m = re.fullmatch(r"([fe])_n(\d+)_c(\d+)_m(\d+)_r(\d+)x(\d+)_w(\d+)", name)
    if m is None:
        raise ValueError(name)
    e, n, c, mm, r, cc, w = m.groups()
    return sp.Knobs(e == "e", int(n), int(c), int(mm), int(r), int(cc), int(w))


def table(names: list[str]) -> Path:
    params = md.load()
    rows = []
    for name in names:
        p = hb.BUILD / name / "impl.json"
        if not p.is_file():
            continue
        impl = json.loads(p.read_text())
        k = knobs_of(name)
        syn = json.loads((hb.BUILD / name / "report.json").read_text())
        pred = md.predict_resources(params, k)
        row = {"build": name, "ok": impl["ok"], "impl_s": impl["impl_s"]}
        if impl["ok"]:
            row.update({
                "cp_post_impl_ns": impl["cp_post_impl"], "timing_met": impl["timing_met"],
                "csynth_est_ns": syn["target"]["estimated_period_ns"],
            })  # fmt: skip
            for res, key in (
                ("DSP", "dsp"),
                ("LUT", "lut"),
                ("FF", "ff"),
                ("BRAM_18K", "bram"),
            ):
                row[f"{res}_impl"] = impl[key]
                row[f"{res}_csynth"] = syn["resources"][res]
                row[f"{res}_model"] = round(pred[res], 1)
        rows.append(row)
    out = PAPER / "finalists_impl.csv"
    PAPER.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        keys = sorted({k for r in rows for k in r}, key=lambda x: (x != "build", x))
        w = csv.DictWriter(fh, fieldnames=keys, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--builds", nargs="+", required=True)
    ap.add_argument("--table-only", action="store_true")
    a = ap.parse_args(argv)
    if not a.table_only:
        for name in a.builds:
            rec = run_impl(knobs_of(name))
            print(json.dumps(rec), flush=True)
    print(f"wrote {table(a.builds)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
