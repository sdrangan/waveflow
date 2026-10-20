"""systolic_measure.py -- measure the systolic engine and its memory wrapper at RTL -> measured.json.

For each word width and workload: build the module's harness (`engine_harness`, `engine_mem_harness`),
run it in pysim and at RTL (XSI) through ``compare`` -- every output checked against the model -- and
record what the RTL did, read at the ports from the BFMs' arrival cycles, and the resources from
csynth's own XML.  The pages under docs/examples/systolic/ quote this file.

    python -m examples.systolic.systolic_measure --root C:/wfsc/sm            # everything (~1 h)
    python -m examples.systolic.systolic_measure --root C:/wfsc/sm --runs eng128_steady

Build from a short root on Windows: past 260 characters a csynth path fails silently.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

import numpy as np

from examples.systolic import engine_harness as EH
from examples.systolic import engine_mem_harness as EMH
from waveflow.utils.burst_io import read_burst_bundle

HERE = Path(__file__).resolve().parent
MEASURED = HERE / "measured.json"

#: name -> (module, word bits, workload)
RUNS = {
    f"{mod}{w}_{kind}": (mod, w, kind)
    for w in (64, 128)
    for mod, kinds in (("eng", ("test", "steady", "short")), ("mem", ("test", "big")))
    for kind in kinds
}


def _resources(proj: Path) -> dict:
    xml = (proj / "solution1" / "syn" / "report" / "csynth.xml").read_text(encoding="utf-8")
    block = xml[xml.index("<AreaEstimates>"):xml.index("</Resources>")]
    get = {k: int(re.search(rf"<{k}>(\d+)</{k}>", block).group(1))
           for k in ("BRAM_18K", "DSP", "FF", "LUT")}
    period = float(re.search(r"<EstimatedClockPeriod>([\d.]+)<", xml).group(1))
    return {"bram": get["BRAM_18K"], "dsp": get["DSP"], "ff": get["FF"], "lut": get["LUT"],
            "est_clock_ns": period}


def _cycles(vectors: Path, port: str) -> np.ndarray:
    return np.fromfile(vectors / port / "cycles.bin", dtype="<u8").astype(np.int64)


def _job_ends(golden: list, cycles: np.ndarray, per_job: int) -> list[int]:
    """The arrival cycle of each job's last beat: *golden* is the port's bursts in order, *per_job*
    of them a job."""
    beats = np.cumsum([len(b) for b in golden])
    return [int(cycles[beats[(j + 1) * per_job - 1] - 1]) for j in range(len(golden) // per_job)]


def measure(name: str, root: Path) -> dict:
    mod, w, kind = RUNS[name]
    cfg = {"word_bits": w}
    run_root = root / name
    run_root.mkdir(parents=True, exist_ok=True)
    H = EH if mod == "eng" else EMH
    config = EH.build_config(run_root)
    res = H.build_dag(cfg, workload=kind).run(config, through="compare")
    bad = {k: r.message[-500:] for k, r in res.items() if not r.success}
    work = run_root / H.WORK_DIR / H.TOP
    vec = work / "xsi_run" / "vectors"
    out = {"module": "SystolicEngine" if mod == "eng" else "SystolicEngineMem", "word_bits": w,
           "workload": kind, "tool": config.vitis_version, "exact": not bad,
           "failed": bad or None}
    if bad:
        return out
    if mod == "eng":
        m = EH.engine(**cfg)
        g = m.golden(kind)["s_out"]
        per = 2  # a Resp, then C, per output; every output of these workloads is one command's
        ends = _job_ends(g, _cycles(vec, "s_out"), per)
        out["outputs"] = len(ends)
        out["ends"] = ends
        out["interval"] = sorted({int(x) for x in np.diff(ends)})
    else:
        mem = np.asarray(read_burst_bundle(vec / "mem")[0], np.uint64)
        out["c_words_wrong"] = EMH.check_c(mem, kind, **cfg)
        out["exact"] = out["exact"] and not any(out["c_words_wrong"])
        out["done"] = [int(c) for c in _cycles(vec, "s_done")]
    out["pysim_cycles"] = json.loads((work / "pysim.json").read_text(encoding="utf-8"))["cycles"]
    out["resources"] = _resources(run_root / f"{H.KERNEL}_proj")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, help="a short build root (Windows path length)")
    ap.add_argument("--runs", nargs="+", default=list(RUNS), choices=list(RUNS))
    a = ap.parse_args()
    root = Path(a.root).resolve()
    data = json.loads(MEASURED.read_text(encoding="utf-8")) if MEASURED.exists() else {
        "target": {"part": "xczu48dr-ffvg1517-2-e", "period_ns": 4.0}, "runs": {}}
    for name in a.runs:
        r = measure(name, root)
        r["date"] = date.today().isoformat()
        data["runs"][name] = r
        print(json.dumps({k: v for k, v in r.items() if k != "ends"}), flush=True)
        data["runs"] = dict(sorted(data["runs"].items()))
        MEASURED.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
