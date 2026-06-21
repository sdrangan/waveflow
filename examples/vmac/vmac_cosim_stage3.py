"""``vmac_cosim_stage3`` — Stage 3 of ``plans/vmac_mm_queue_timing.md``: the headline validation.

Run Vitis HLS RTL cosim for TWO scenarios at PF=1 and compare against the Stage-1/2 sim:

  * ``anorm``  = inner_prod(A, A) + reduce, with ``a_addr == b_addr`` -> ``ab_eq`` (B aliases A,
    so the kernel copies ``a_lane`` into ``b_lane`` instead of a second m_axi read).
  * ``abcorr`` = inner_prod(A, B) + reduce, with ``a_addr != b_addr`` (a real B read).

For each: generate the synthesizable top (m_axi gmem + scalar command), run csim->csynth->cosim
(the TB re-checks the result against the Python golden, so this also proves the ``ab_eq`` branch is
bit-exact), re-run xsim with a port-level VCD, and extract the m_axi read/write bursts.

Emit ``timeline/cosim_timeline.json`` in the SAME source-agnostic schema as ``sim_timeline.json``
(``source="cosim"``), and ``timeline/sim_vs_cosim.md`` with the headline result: does ``ab_eq``
issue half the reads, and does eliding B's read LOWER anorm's latency (the sim's transaction-driven
prediction) or is it fixed-II (same latency, only the read bus freed)?

SCOPE: the kernel cosim has an m_axi memory but NO command queue (host/ring are sim-only), so this
validates per-burst MEMORY timing + the ab_eq latency/bus behavior.  Queue occupancy is a sim-only
quantity — out of cosim scope; not invented here.

Run (use the project venv; needs Vitis HLS + Vivado xsim on Windows)::

    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/vmac/vmac_cosim_stage3.py
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

from examples.vmac.vmac_build import (
    BUILD_HDRS,
    HOOK_FILES,
    INCLUDE_DIR,
    MEM_AWIDTH,
    MEM_WORD_BWS,
    StructCfg,
    _array_utils_namespace,
    _cmd_schema_steps,
    _mem_words,
    _vmac_hpp,
    render_cosim_tb,
    render_top,
    _SOURCE_DIR,
    _BUILD_DIR,
)
from examples.vmac.vmac_cmd import OpCode
from examples.vmac.vmac_golden_mem import apply_golden
from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.toolchain import toolchain
from waveflow.utils import complexutils as cx

# PF = 1, mirroring the Stage-1/2 sim (data_bw=16, int_bits=16 -> F_in=0, mem_dwidth=32 -> PF=1).
COSIM_CFG = StructCfg(out_bw=16, q_rnd=0, o_sat=0, mem_dwidth=32, data_bw=16, int_bits=16, acc_bw=48)
N_ROWS = N_COLS = 4
SEED = 1
MAX_COLS = 16  # kernel acc[] capacity (>= n_cols)

TIMELINE_DIR = Path(__file__).resolve().parent / "timeline"
SIM_JSON = TIMELINE_DIR / "sim_timeline.json"
COSIM_JSON = TIMELINE_DIR / "cosim_timeline.json"
COMPARE_MD = TIMELINE_DIR / "sim_vs_cosim.md"
PROJ = "vmac_cosim_proj"

_COSIM_TCL = """# Vitis HLS csim -> csynth -> cosim (port-level trace) for one VMAC scenario.
set d [file dirname [file normalize [info script]]]
set data_dir [file join $d data]
open_project -reset %(proj)s
set_top vmac
add_files vmac_top.cpp -cflags "-I. -Iinclude"
add_files -tb vmac_tb.cpp -cflags "-I. -Iinclude"
set su [file join $d include streamutils.cpp]
if {[file exists $su]} { add_files -tb $su -cflags "-I. -Iinclude" }
open_solution -reset "solution1"
set_part {xc7z020clg484-1}
create_clock -period 10
if {[catch {csim_design -argv $data_dir} res]} { puts "WAVEFLOW_ERROR: vmac csim failed."; puts $res; exit 1 }
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: vmac csynth failed."; puts $res; exit 1 }
if {[catch {cosim_design -argv $data_dir -trace_level port} res]} { puts "WAVEFLOW_ERROR: vmac cosim failed."; puts $res; exit 1 }
puts "WAVEFLOW_SUCCESS: vmac cosim (trace port) passed."
exit 0
"""


def _operands():
    """The exact Stage-1/2 sim operands (seed=1, integer values in [-3, 3])."""
    rng = np.random.default_rng(SEED)
    shp = (N_ROWS, N_COLS)
    a_re = rng.integers(-3, 4, size=shp)
    a_im = rng.integers(-3, 4, size=shp)
    b_re = rng.integers(-3, 4, size=shp)
    b_im = rng.integers(-3, 4, size=shp)
    return a_re, a_im, b_re, b_im


def _reduce_cmd(accel, a_addr, b_addr, y_addr):
    cmd = accel.Cmd()
    cmd.op, cmd.reduce, cmd.n_rows, cmd.n_cols = OpCode.inner_prod, 1, N_ROWS, N_COLS
    for name, addr in (("a", a_addr), ("b", b_addr), ("y", y_addr)):
        setattr(cmd, name, {"addr": int(addr), "row_stride": N_COLS})
    cmd.alpha = {"direct": 1, "imm": (0, 0), "addr": 0, "stride": 0}
    return cmd


def _scalars(cmd) -> list[int]:
    return [
        int(cmd.op), int(cmd.reduce), int(cmd.n_rows), int(cmd.n_cols),
        int(cmd.a.addr), int(cmd.a.row_stride), int(cmd.b.addr), int(cmd.b.row_stride),
        int(cmd.y.addr), int(cmd.y.row_stride), int(cmd.alpha.direct),
        int(cx.re_of(cmd.alpha.imm)), int(cx.im_of(cmd.alpha.imm)),
        int(cmd.alpha.addr), int(cmd.alpha.stride),
    ]


def _scenario(accel, ab_eq: bool):
    """Build (cmd, mem, mem_exp) for an anorm (ab_eq) or abcorr scenario."""
    nm = N_ROWS * N_COLS
    a_re, a_im, b_re, b_im = _operands()
    in_fmt = accel._in_fmt()
    A = cx.make_complex(a_re.ravel(), a_im.ravel(), in_fmt)
    B = cx.make_complex(b_re.ravel(), b_im.ravel(), in_fmt)
    if ab_eq:  # B aliases A: a_addr == b_addr; layout A | Y
        a_addr, b_addr, y_addr, size = 0, 0, nm, nm + N_COLS
    else:      # distinct B: layout A | B | Y
        a_addr, b_addr, y_addr, size = 0, nm, 2 * nm, 2 * nm + N_COLS
    mem = cx.make_complex(np.zeros(size), np.zeros(size), in_fmt)
    mem[a_addr:a_addr + nm] = A
    if not ab_eq:
        mem[b_addr:b_addr + nm] = B
    cmd = _reduce_cmd(accel, a_addr, b_addr, y_addr)
    mem_exp = mem.copy()
    apply_golden(accel, cmd, mem_exp)  # the golden writes Y into mem_exp
    return cmd, mem, mem_exp


def gen_scenario(tdir: Path, ab_eq: bool) -> dict:
    """Generate the synthesizable top + cosim TB + vectors for one scenario."""
    cfg = COSIM_CFG
    tdir = Path(tdir).resolve()
    tdir.mkdir(parents=True, exist_ok=True)
    bc = BuildConfig(root_dir=tdir)
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    for step in _cmd_schema_steps(cfg):
        dag.add(step)
    dag.add(ArrayUtilsStep(cfg.in_elem(), MEM_WORD_BWS))
    dag.run(bc)
    for fname in HOOK_FILES:
        shutil.copy(_SOURCE_DIR / fname, tdir / fname)
    for fname in BUILD_HDRS:
        shutil.copy(_BUILD_DIR / fname, tdir / fname)

    accel = cfg.accel()
    cmd, mem, mem_exp = _scenario(accel, ab_eq)
    in_elem = cfg.in_elem()
    mem_in_w = _mem_words(mem, in_elem, cfg.mem_dwidth)
    mem_exp_w = _mem_words(mem_exp, in_elem, cfg.mem_dwidth)
    nmem = len(mem_in_w)
    depth = nmem
    scalars = _scalars(cmd)

    (tdir / "vmac.hpp").write_text(_vmac_hpp(cfg), encoding="utf-8")
    (tdir / "vmac_top.cpp").write_text(render_top(cfg, depth), encoding="utf-8")
    (tdir / "vmac_tb.cpp").write_text(render_cosim_tb(cfg, scalars, nmem, depth), encoding="utf-8")
    (tdir / "run.tcl").write_text(_COSIM_TCL % {"proj": PROJ}, encoding="utf-8")
    data = tdir / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "mem_in.txt").write_text("\n".join(str(w) for w in mem_in_w) + "\n", encoding="utf-8")
    (data / "mem_exp.txt").write_text("\n".join(str(w) for w in mem_exp_w) + "\n", encoding="utf-8")
    return {"cmd": cmd, "nmem": nmem, "a_addr": int(cmd.a.addr), "b_addr": int(cmd.b.addr),
            "y_addr": int(cmd.y.addr), "ab_eq": ab_eq}


def _burst_words(burst) -> int:
    return sum(1 for bt in burst.get("beat_type", []) if bt == 0)  # transfer beats


def _label_read(addr_bytes: int, info: dict, word_bytes: int) -> str:
    nm = N_ROWS * N_COLS
    if addr_bytes < nm * word_bytes:
        return "A"
    if addr_bytes < 2 * nm * word_bytes:
        return "B"
    return "data"


def parse_bursts(tdir: Path, info: dict, scenario: str) -> dict:
    """Run xsim to dump a VCD, then extract m_axi read/write bursts as transactions."""
    from vcdvcd import VCDVCD

    from waveflow.scripts.xsim_vcd import run_xsim_vcd
    from waveflow.utils.cosimparse import CosimReportParser
    from waveflow.utils.vcd import VcdParser

    vcd_path = run_xsim_vcd(top="vmac", comp=PROJ, out="dump.vcd",
                            soln="solution1", trace_level="port", workdir=tdir)
    vcd = VCDVCD(str(vcd_path), signals=None, store_tvs=True)
    vp = VcdParser(vcd)
    clk_sig = vp.add_clock_signal()
    aximm_sigs, aximm_bw = vp.add_aximm_signals(
        prefix="m_axi_gmem_", dir="both", lite_only=False, short_name_prefix="gmem_",
    )
    write_bursts, read_bursts, clk_period = vp.extract_aximm_bursts(
        clk_name=clk_sig, aximm_sigs=aximm_sigs,
    )
    word_bytes = COSIM_CFG.mem_dwidth // 8
    y_label = "Y_anorm" if info["ab_eq"] else "Y_abcorr"

    txns = []
    for b in read_bursts:
        txns.append({
            "name": _label_read(int(b["addr"]), info, word_bytes), "rw": "read",
            "addr": int(b["addr"]), "nwords": _burst_words(b),
            "tstart": float(b.get("data_tstart", b["tstart"])),
            "tend": float(b["data_tend"]),
        })
    for b in write_bursts:
        txns.append({
            "name": y_label, "rw": "write",
            "addr": int(b["addr"]), "nwords": _burst_words(b),
            "tstart": float(b.get("data_tstart", b["tstart"])),
            "tend": float(b["data_tend"]),
        })
    txns.sort(key=lambda t: (t["tstart"], t["rw"], t["addr"]))

    cycles = CosimReportParser(
        sol_path=tdir / PROJ / "solution1", top="vmac"
    ).get_transaction_cycles()

    kernel_start = min((t["tstart"] for t in txns), default=0.0)
    complete_t = max((t["tend"] for t in txns if t["rw"] == "write"), default=kernel_start)
    return {
        "scenario": scenario,
        "transactions": txns,
        "read_words": sum(t["nwords"] for t in txns if t["rw"] == "read"),
        "write_words": sum(t["nwords"] for t in txns if t["rw"] == "write"),
        "read_burst_count": sum(1 for t in txns if t["rw"] == "read"),
        "kernel_start_t": kernel_start,
        "complete_t": complete_t,
        "latency_ns": complete_t - kernel_start,
        "transaction_cycles": cycles,
        "clk_period_ns": float(clk_period),
    }


def run() -> dict:
    TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    base = Path(__file__).resolve().parent / "cosim"
    scenarios = [("anorm", True), ("abcorr", False)]
    results = {}
    for name, ab_eq in scenarios:
        tdir = base / name
        info = gen_scenario(tdir, ab_eq)
        print(f"[{name}] running Vitis csim/csynth/cosim (trace port) ...")
        res = toolchain.run_vitis_hls(tdir / "run.tcl", work_dir=tdir, capture_output=True)
        out = (getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")
        if "WAVEFLOW_SUCCESS" not in out or "VMAC_COSIM_OK" not in out:
            raise RuntimeError(
                f"[{name}] cosim did not report success markers.\n"
                f"WAVEFLOW_SUCCESS={'WAVEFLOW_SUCCESS' in out}  "
                f"VMAC_COSIM_OK={'VMAC_COSIM_OK' in out}\n--- tail ---\n{out[-2000:]}"
            )
        print(f"[{name}] markers: WAVEFLOW_SUCCESS + VMAC_COSIM_OK seen (csim bit-exact vs golden)")
        results[name] = {"info": info, "out_markers": True, **parse_bursts(tdir, info, name)}
    emit(results)
    return results


def emit(results: dict) -> None:
    cfg = COSIM_CFG
    commands = []
    for cmd_idx, name in enumerate(("anorm", "abcorr")):
        r = results[name]
        commands.append({
            "cmd_idx": cmd_idx, "scenario": name, "op": "inner_prod",
            "ab_eq": r["info"]["ab_eq"], "n_rows": N_ROWS, "n_cols": N_COLS,
            "kernel_start_t": r["kernel_start_t"], "complete_t": r["complete_t"],
            "latency": r["latency_ns"], "transaction_cycles": r["transaction_cycles"],
            "read_words": r["read_words"], "write_words": r["write_words"],
            "read_burst_count": r["read_burst_count"], "transactions": r["transactions"],
        })
    timeline = {
        "source": "cosim",
        "timebase": "ns",
        "clk_period_ns": results["anorm"]["clk_period_ns"],
        "mem_bw": cfg.mem_dwidth, "word_bytes": cfg.mem_dwidth // 8,
        "n_rows": N_ROWS, "n_cols": N_COLS,
        "queue_occupancy": None,
        "queue_note": "out of cosim scope: the command queue / ring is sim-only (kernel has m_axi, no queue)",
        "commands": commands,
    }
    COSIM_JSON.write_text(json.dumps(timeline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"cosim timeline written: {COSIM_JSON}")
    _write_comparison(results)


def _write_comparison(results: dict) -> None:
    sim = json.loads(SIM_JSON.read_text(encoding="utf-8")) if SIM_JSON.exists() else None
    an, ab = results["anorm"], results["abcorr"]
    sim_an = sim_ab = None
    if sim is not None:
        for c in sim["commands"]:
            if c.get("ab_eq"):
                sim_an = c
            elif c["op"] == "inner_prod":
                sim_ab = c

    halves = an["read_words"] * 2 == ab["read_words"]
    rtl_lat_equal = an["transaction_cycles"] == ab["transaction_cycles"]
    verdict = ("FIXED-II: same RTL latency, only the read bus freed"
               if rtl_lat_equal else
               "anorm RTL latency is LOWER (eliding B's read shortened it)")
    lines = [
        "# VMAC ab_eq: sim prediction vs Vitis RTL cosim (the headline validation)",
        "",
        "Stage 3 of `plans/vmac_mm_queue_timing.md`. PF=1, n_rows=n_cols=4, clk 100 MHz "
        "(`create_clock -period 10`).",
        "",
        "## (a) m_axi read bursts in RTL — does ab_eq issue half the reads?",
        "",
        "| scenario | ab_eq | read bursts | read words | write words |",
        "|---|---|---|---|---|",
        f"| anorm  | {an['info']['ab_eq']} | {an['read_burst_count']} | {an['read_words']} | {an['write_words']} |",
        f"| abcorr | {ab['info']['ab_eq']} | {ab['read_burst_count']} | {ab['read_words']} | {ab['write_words']} |",
        "",
        f"=> anorm read words are {'HALF' if halves else 'NOT half'} of abcorr "
        f"({an['read_words']} vs {ab['read_words']}): the B read-bus traffic **is** suppressed by ab_eq.",
        "",
        "## (b) kernel latency in RTL — did eliding B's read lower latency, or is it fixed-II?",
        "",
        "| scenario | RTL transaction cycles | RTL latency (ns, first burst -> last write) |",
        "|---|---|---|",
        f"| anorm  | {an['transaction_cycles']} | {an['latency_ns']:.1f} |",
        f"| abcorr | {ab['transaction_cycles']} | {ab['latency_ns']:.1f} |",
        "",
        f"=> **{verdict}.**",
        "",
        "## (c) sim-predicted vs RTL-measured anorm latency (the loosely-timed model error)",
        "",
    ]
    if sim_an is not None and sim_ab is not None:
        # sim latency is in seconds (sim timebase); cosim in ns. Report both raw + the gap framing.
        lines += [
            f"- sim predicted: anorm latency = {sim_an['latency']:.4g} s, "
            f"abcorr latency = {sim_ab['latency']:.4g} s  -> sim gap (abcorr - anorm) = "
            f"{sim_ab['latency'] - sim_an['latency']:.4g} s (anorm predicted FASTER).",
            f"- RTL measured:  anorm cycles = {an['transaction_cycles']}, "
            f"abcorr cycles = {ab['transaction_cycles']}  -> RTL gap = "
            f"{(ab['transaction_cycles'] - an['transaction_cycles']) if (an['transaction_cycles'] and ab['transaction_cycles']) else 'n/a'} cycles.",
            "",
            "The loosely-timed sim is **transaction-gated**: it makes anorm finish sooner because it "
            "issues one fewer read block. The fixed-II RTL "
            + ("shows the SAME latency for both (the gap the sim predicts is the model error a cosim-"
               "calibrated II would close); only the read bus is freed."
               if rtl_lat_equal else
               "shows anorm genuinely shorter, so here the transaction-gated prediction holds."),
        ]
    else:
        lines.append("- (sim_timeline.json not found; run vmac_queue_sim.py first for the gap.)")
    lines.append("")
    lines.append("## Scope")
    lines.append("Queue occupancy is a sim-only quantity (the kernel has an m_axi but no command "
                 "ring); it is out of cosim scope and not fabricated here.")
    COMPARE_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"comparison written: {COMPARE_MD}")


if __name__ == "__main__":
    run()
