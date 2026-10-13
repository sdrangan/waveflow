"""impl_check.py — csynth estimates against implemented hardware, on a few detectors.

Step 5.9 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 5.0 decision 5).  The Phase 5 models
predict what **csynth** reports, because that is what a design-space sweep can afford to measure.
This check says how far those numbers are from placed-and-routed hardware: it takes detector builds
the campaign already synthesized and runs Vivado on them through ``export_design -flow impl``
(out-of-context synthesis, place and route at the 4 ns target), then reads the implemented
utilization and the achieved clock.

It is a reality check on a few designs, not a ground truth for the models.

::

    python -m examples.mimo_cg.hw.impl_check --run      # Vivado on every build of BUILDS, in parallel
    python -m examples.mimo_cg.hw.impl_check            # (re)write paper_data/impl_check.csv
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from examples.mimo_cg.hw import build as B
from examples.mimo_cg.mimo_cg import provenance, read_table, write_table
from waveflow.toolchain import toolchain

HERE = Path(__file__).resolve().parent
PAPER_DATA = HERE.parent / "paper_data"
TOP = B.DET_TOP
#: The detectors checked (campaign builds, synthesized already).  The first three are the Phase 4
#: default knobs at K = 4, 8 and 16 (step 5.9).  The last two were added at the M5 review, to see
#: the edges the first three share nothing with: a W = 8 design, whose plain multiplies csynth builds
#: from LUTs, and the largest held-out detector, with 16 lanes.
BUILDS = (
    "det_k4_l4_r4_c4_m4_w12g8_d64_s2_q2",
    "det_k8_l4_r8_c4_m4_w12g8_d64_s2_q2",
    "det_k16_l4_r16_c4_m4_w12g8_d64_s2_q2",
    "det_k4_l4_r4_c4_m4_w8g8_d64_s2_q2",
    "det_k4_l16_r2_c32_m3_w12g0_d64_s2_q8",
)
_TCL = f"""\
open_project {TOP}_proj
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
    "srl": r"^SRL:\s+(\d+)",
    "clb": r"^CLB:\s+(\d+)",
    "cp_required": r"^CP required:\s+([\d.]+)",
    "cp_post_synth": r"^CP achieved post-synthesis:\s+([\d.]+)",
    "cp_post_impl": r"^CP achieved post-implementation:\s+([\d.]+)",
}


def report_path(build: str) -> Path:
    sol = B.BUILD_ROOT / build / f"{TOP}_proj" / "solution1"
    return sol / "impl" / "report" / "verilog" / f"{TOP}_export.rpt"


def implemented(build: str) -> bool:
    """Whether Vivado finished on ``build``.  The export report is not the sign of it: the tool
    writes that file early and fills it in at the end.  :func:`run_impl` writes ``impl.seconds``
    and ``impl.log`` when the tool returns, and the log says whether it succeeded."""
    out = B.BUILD_ROOT / build
    log = out / "impl.log"
    return (
        (out / "impl.seconds").is_file()
        and log.is_file()
        and "IMPL_OK" in log.read_text(encoding="utf-8", errors="replace")
        and report_path(build).is_file()
    )


def run_impl(build: str) -> dict:
    """Run Vivado implementation on one synthesized build; returns ``{build, ok, seconds}``."""
    out = B.BUILD_ROOT / build
    if not (
        out / f"{TOP}_proj" / "solution1" / "syn" / "report" / "csynth.xml"
    ).is_file():
        raise FileNotFoundError(f"{build}: no csynth result; run the campaign first")
    tcl = out / "impl.tcl"
    tcl.write_text(_TCL, encoding="utf-8")
    started = time.perf_counter()
    try:
        run = toolchain.run_vitis_hls(tcl, work_dir=out, capture_output=True)
        log = (run.stdout or "") + (run.stderr or "")
    except subprocess.CalledProcessError as exc:
        # the tool exits non-zero when Vivado fails (a crash included): a failed run is a
        # result to record, not a reason to stop the other builds
        log = f"IMPL_FAILED: {exc}\n" + (exc.stdout or "") + (exc.stderr or "")
    (out / "impl.log").write_text(log, encoding="utf-8")
    seconds = round(time.perf_counter() - started, 1)
    (out / "impl.seconds").write_text(f"{seconds}\n", encoding="utf-8")
    return {
        "build": build,
        "ok": "IMPL_OK" in log and report_path(build).is_file(),
        "seconds": seconds,
    }


def parse_report(text: str) -> dict:
    """The implemented utilization and timing of an ``export_design -flow impl`` report."""
    out: dict = {}
    for name, pattern in _FIELDS.items():
        m = re.search(pattern, text, re.MULTILINE)
        if m is None:
            raise ValueError(f"no {name!r} in the implementation report")
        out[name] = float(m.group(1)) if name.startswith("cp_") else int(m.group(1))
    out["timing_met"] = int(re.search(r"^Timing met", text, re.MULTILINE) is not None)
    out["tool"] = (
        re.search(r"^Implementation tool:\s+(.+)$", text, re.MULTILINE).group(1).strip()
    )
    return out


def rows(builds=BUILDS) -> list[dict]:
    """One row per implemented build: csynth against implementation."""
    csynth = {r["build"]: r for r in read_table(PAPER_DATA / "hw_builds.csv")}
    out = []
    for build in builds:
        impl = parse_report(report_path(build).read_text(encoding="utf-8"))
        est = csynth[build]
        secs = B.BUILD_ROOT / build / "impl.seconds"
        row = {"build": build, "K": int(est["K"])}
        for ctr in ("lut", "ff", "dsp", "bram"):
            row |= {f"csynth_{ctr}": int(est[ctr]), f"impl_{ctr}": impl[ctr]}
        for ctr in ("lut", "ff"):
            row[f"{ctr}_ratio"] = round(int(est[ctr]) / impl[ctr], 3)
        row |= {
            "impl_srl": impl["srl"],
            "impl_clb": impl["clb"],
            "csynth_est_ns": float(est["est_ns"]),
            "cp_required_ns": impl["cp_required"],
            "cp_post_synth_ns": impl["cp_post_synth"],
            "cp_post_impl_ns": impl["cp_post_impl"],
            "timing_met": impl["timing_met"],
            "impl_seconds": float(secs.read_text()) if secs.is_file() else "",
            "tool": impl["tool"],
        }
        out.append(row)
    return out


#: The detector's task instances in the implemented netlist, by the module they are.
_TASK_OF = {
    "cg_cmd_rx_task": "CgCmdRx",
    "mem_r_stream_framed_task": "MemRStream",
    "cg_load_task": "CgLoad",
    "cg_ctrl_task": "CgCtrl",
    "cg_vec_task": "CgVec",
    "cg_mm_task": "CgMm",
    "cg_store_task": "CgStore",
    "mem_w_stream_framed_done_task": "MemWStream",
    # the detector on Waveflow's components (plan step 9.4): the two cores in the blocks' place
    "cg_vector_task": "CgVectorCore",
    "systolic_core_task": "SystolicCore",
}


def hier_path(build: str) -> Path:
    sol = B.BUILD_ROOT / build / f"{TOP}_proj" / "solution1"
    return (
        sol
        / "impl"
        / "verilog"
        / "report"
        / f"{TOP}_utilization_hierarchical_routed.rpt"
    )


def parse_hierarchy(text: str) -> dict:
    """Implemented utilization per module of the detector, from Vivado's hierarchical report.

    Returns ``{module: {lut, ff, dsp, bram}}`` with ``bram`` in BRAM18 equivalents (a RAMB36 is
    two).  Everything in the top that is not one of the eight tasks — the stream-of-blocks
    memories, the FIFOs, the bus adapters and the top's own logic — is summed as ``integration``.
    Vivado counts a LUT shared by two instances in both, so the modules' LUTs add up to a few more
    than the design's total.
    """
    rows = []
    for line in text.splitlines():
        cells = line.strip("|").split("|") if line.startswith("|") else []
        if len(cells) >= 11 and cells[2].strip().isdigit():
            depth = len(cells[0]) - len(cells[0].lstrip())
            lut, ff, r36, r18, dsp = (int(cells[i]) for i in (2, 6, 7, 8, 10))
            rows.append(
                (
                    depth,
                    cells[0].strip(),
                    {"lut": lut, "ff": ff, "dsp": dsp, "bram": 2 * r36 + r18},
                )
            )
    task_depth = sorted({d for d, _n, _r in rows})[
        4
    ]  # wrapper, bd, hls_inst, inst, then the tasks
    out: dict = {"integration": {"lut": 0, "ff": 0, "dsp": 0, "bram": 0}}
    for depth, name, res in rows:
        if depth != task_depth:
            continue
        base = re.sub(r"(_\d+)*_U0?$", "", name)
        module = _TASK_OF.get(base, "integration")
        if module == "integration":
            for k, v in res.items():
                out["integration"][k] += v
        else:
            out[module] = res
    return out


def module_rows(builds=BUILDS) -> list[dict]:
    """One row per build and module: the csynth row against the implemented instance."""
    modules = [
        r for r in read_table(PAPER_DATA / "hw_modules.csv") if r["kind"] != "subblock"
    ]
    out = []
    for build in builds:
        impl = parse_hierarchy(hier_path(build).read_text(encoding="utf-8"))
        mine = [r for r in modules if r["build"] == build]
        csynth: dict = {}
        for r in mine:
            name = r["name"] if r["kind"] == "module" else "integration"
            row = csynth.setdefault(name, {"lut": 0, "ff": 0, "dsp": 0, "bram": 0})
            for k in row:
                row[k] += int(r[k])
        for name in [*(m for m in _TASK_OF.values() if m in impl), "integration"]:
            row = {"build": build, "module": name}
            for k in ("lut", "ff", "dsp", "bram"):
                row |= {f"csynth_{k}": csynth[name][k], f"impl_{k}": impl[name][k]}
            row["lut_ratio"] = (
                round(csynth[name]["lut"] / impl[name]["lut"], 2)
                if impl[name]["lut"]
                else ""
            )
            out.append(row)
    return out


def _hls_tool() -> str:
    """The Vitis HLS version the csynth side was measured with (``hw_builds.csv``'s header)."""
    head = (PAPER_DATA / "hw_builds.csv").read_text(encoding="utf-8").splitlines()[0]
    return re.search(r"tool=([^,]+)", head).group(1)


def write(builds=BUILDS) -> Path:
    path = PAPER_DATA / "impl_check.csv"
    tools = sorted({r["tool"] for r in rows(builds)})
    note = provenance(
        "impl_check",
        tool="+".join(tools),
        hls=_hls_tool(),
        part=B.PART,
        period_ns=B.PERIOD_NS,
        flow="export_design -flow impl",
    )
    write_table(path, rows(builds), note)
    write_table(PAPER_DATA / "impl_check_modules.csv", module_rows(builds), note)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--run", action="store_true", help="run Vivado implementation first (long)"
    )
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument(
        "--force", action="store_true", help="re-run builds already implemented"
    )
    args = ap.parse_args(argv)
    if args.run:
        todo = [b for b in BUILDS if args.force or not implemented(b)]
        with ThreadPoolExecutor(args.jobs) as pool:
            for res in pool.map(run_impl, todo):
                print(res)
    path = write()
    print("wrote", path)
    for r in rows():
        print(
            f"  K = {r['K']:2d}: LUT {r['csynth_lut']} -> {r['impl_lut']} (x{r['lut_ratio']}), "
            f"FF {r['csynth_ff']} -> {r['impl_ff']} (x{r['ff_ratio']}), DSP {r['csynth_dsp']} -> "
            f"{r['impl_dsp']}, BRAM {r['csynth_bram']} -> {r['impl_bram']}, "
            f"clock {r['cp_post_impl_ns']} ns ({'met' if r['timing_met'] else 'NOT met'})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
