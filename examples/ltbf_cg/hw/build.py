"""build.py -- one engine configuration: generate, csynth, harvest the reports.

``plans/mimo_cg/ltbf_cg_dse.md`` steps 4.1-4.6.  A configuration is :class:`~examples.ltbf_cg.hw.
engine.LtbfEngine`'s knobs; its build tree is ``hw/build/<name>/`` (local, gitignored).  The harvest
keeps what the cost models need -- total resources, the estimated clock, and every loop's II, depth
and trip count -- as ``report.json`` beside the tree, so the tree itself can be removed.

    python -m examples.ltbf_cg.hw.build --nmax 64 --cmax 64 --mmax 16 --R 2 --C 2 --W 16
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from examples.ltbf_cg.hw import engine as eg

BUILD = eg.HERE / "build"
TOP = "ltbf_engine"
KNOBS = ("nmax", "cmax", "mmax", "R", "C", "explicit", "W")


def name(knobs: dict) -> str:
    k = knobs
    return (
        f"{'e' if k['explicit'] else 'f'}_n{k['nmax']}_c{k['cmax']}_m{k['mmax']}"
        f"_r{k['R']}x{k['C']}_w{k['W']}"
    )


def loops(report_dir: Path) -> dict[str, dict]:
    """Every loop of every module report: II, depth, trip count and latency."""
    out: dict[str, dict] = {}
    for xml in sorted(Path(report_dir).glob("*.xml")):
        try:
            root = ET.parse(xml).getroot()
        except ET.ParseError:
            continue
        for lat in root.iter("SummaryOfLoopLatency"):
            for loop in lat.iter():
                if (
                    loop is lat
                    or loop.find("PipelineII") is None
                    and loop.find("Latency") is None
                ):
                    continue

                def num(tag, node=loop):
                    try:
                        return int(node.findtext(tag))
                    except (TypeError, ValueError):
                        return None

                out[loop.tag] = {
                    "ii": num("PipelineII"), "depth": num("PipelineDepth"),
                    "trip_max": num("TripCount/range/max") or num("TripCount"),
                    "latency": num("Latency/range/max") or num("Latency"),
                    "iteration_latency": num("IterationLatency"),
                }  # fmt: skip
    return out


def harvest(out_dir: Path) -> dict:
    """What a finished csynth leaves for the models."""
    from waveflow.utils.csynthparse import CsynthParser, synth_target

    rep = Path(out_dir) / f"{TOP}_proj" / "solution1" / "syn" / "report"
    p = CsynthParser(report_path=str(rep))
    p.get_total_resources()
    return {
        "resources": p.total_resources,
        "available": p.available_resources,
        "target": synth_target(rep),
        "loops": loops(rep),
    }


def csynth(knobs: dict, *, keep_tree: bool = True, log=print) -> dict:
    """Generate and csynth one configuration; write and return its ``report.json``."""
    from waveflow.build.rtl_digest import write_stamp
    from waveflow.toolchain import toolchain

    knobs = {k: knobs[k] for k in KNOBS}
    out = BUILD / name(knobs)
    t0 = time.perf_counter()
    tcl = eg.generate(out, knobs)
    res = toolchain.run_vitis_hls(tcl, work_dir=out, capture_output=True)
    sec = time.perf_counter() - t0
    (out / "csynth.log").write_text((res.stdout or "") + (res.stderr or ""))
    ok = "WAVEFLOW_CSYNTH_OK" in (res.stdout or "")
    rec: dict = {
        "name": name(knobs),
        "knobs": knobs,
        "ok": ok,
        "csynth_s": round(sec, 1),
    }
    if ok:
        write_stamp(out, TOP)
        rec.update(harvest(out))
    (out / "report.json").write_text(json.dumps(rec, indent=1))
    log(f"{rec['name']}: {'ok' if ok else 'FAILED'} in {sec:.0f} s")
    if ok and not keep_tree:
        shutil.rmtree(out / f"{TOP}_proj", ignore_errors=True)
    return rec


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for k in ("nmax", "cmax", "mmax", "R", "C", "W"):
        p.add_argument(f"--{k}", type=int, required=True)
    p.add_argument("--explicit", action="store_true")
    a = p.parse_args(argv)
    rec = csynth({k: getattr(a, k) for k in KNOBS})
    if rec["ok"]:
        print(json.dumps({k: rec[k] for k in ("resources", "target")}, indent=1))
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
