"""``vmac_cosim_sweep`` — Stage 5 calibration sweep of ``plans/vmac_mm_queue_timing.md``.

Stage 3 cosim'd a SINGLE config (4x4, PF=1) and found the loosely-timed sim is
**transaction-gated**: it mispredicts the ``ab_eq`` ordering (anorm faster) and
underestimates absolute latency ~5x (sim 630-940 ns vs RTL 3200 ns).  The fix is
**II-decoupling** — advance the timing model by the pipeline schedule
``latency_cycles ~= depth + II * trips`` instead of the sum of transfer blocks.

Fitting one latency parameter to one cosim point is tautological.  This driver runs the
**calibration sweep**: cosim several matrix sizes at PF=1 spanning distinct trip counts,
extract each RTL kernel latency in cycles, and commit the swept ``(trips -> cycles)`` dataset
as a structured artifact (``calibration/vmac_calibration.json``).  The fit + held-out
validation live in :mod:`examples.vmac.vmac_calibrate` (which reads only this artifact, no
Vitis), and the II-decoupled timing model loads the fitted ``(depth, II)`` from it.

  trips = n_rows * ceil(n_cols / PF)   (PF = 1 here, so trips = n_rows * n_cols)

One cosim per size suffices for the *cycle* calibration: Stage 3 established that ``ab_eq``
does NOT change ``transaction_cycles`` (anorm == abcorr == 347 at 4x4, fixed-II), so the
kernel latency depends on ``trips`` alone, not the read count.  Each size is run as the
worst-case **distinct-B** ``inner_prod + reduce`` (a real B read — the II the RTL fixes).

Run (use the project venv; needs Vitis HLS + Vivado xsim on Windows)::

    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/vmac/vmac_cosim_sweep.py
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import numpy as np

from examples.vmac.vmac_build import (
    BUILD_HDRS,
    HOOK_FILES,
    INCLUDE_DIR,
    MEM_WORD_BWS,
    StructCfg,
    _BUILD_DIR,
    _SOURCE_DIR,
    _cmd_schema_steps,
    _mem_words,
    _vmac_hpp,
    render_cosim_tb,
    render_top,
)
from examples.vmac.vmac_cmd import OpCode
from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.toolchain import toolchain
from waveflow.utils import complexutils as cx

# PF = 1, mirroring Stage 1/2/3 (data_bw=16, int_bits=16 -> F_in=0 integer-exact;
# mem_dwidth=32 -> element = 2*16 = 32 bits -> 1 word/element -> PF=1).
COSIM_CFG = StructCfg(out_bw=16, q_rnd=0, o_sat=0, mem_dwidth=32, data_bw=16, int_bits=16, acc_bw=48)
PF = 1
SEED = 1

# The calibration sweep: >= 4 sizes spanning distinct trip counts at PF=1.
#   (n_rows, n_cols) -> trips = n_rows * n_cols = 16, 36, 64, 128.
# Kernel acc[] capacity (render_top's MAX_COLS) is 64, so n_cols <= 64.
SWEEP = [(4, 4), (6, 6), (8, 8), (8, 16)]

CALIB_DIR = Path(__file__).resolve().parent / "calibration"
CALIB_JSON = CALIB_DIR / "vmac_calibration.json"
PROJ = "vmac_sweep_proj"

_COSIM_TCL = """# Vitis HLS csim -> csynth -> cosim (port-level trace) for one VMAC sweep point.
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


def _operands(n_rows: int, n_cols: int):
    """Distinct integer A / B in [-3, 3] (seed=1), matching the Stage-1/2/3 style."""
    rng = np.random.default_rng(SEED)
    shp = (n_rows, n_cols)
    a_re = rng.integers(-3, 4, size=shp)
    a_im = rng.integers(-3, 4, size=shp)
    b_re = rng.integers(-3, 4, size=shp)
    b_im = rng.integers(-3, 4, size=shp)
    return a_re, a_im, b_re, b_im


def _reduce_cmd(accel, n_rows, n_cols, a_addr, b_addr, y_addr):
    cmd = accel.Cmd()
    cmd.op, cmd.reduce, cmd.n_rows, cmd.n_cols = OpCode.inner_prod, 1, n_rows, n_cols
    for name, addr in (("a", a_addr), ("b", b_addr), ("y", y_addr)):
        setattr(cmd, name, {"addr": int(addr), "row_stride": n_cols})
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


def _scenario(accel, n_rows: int, n_cols: int):
    """Build (cmd, mem, mem_exp) for a distinct-B inner_prod+reduce of size n_rows x n_cols."""
    nm = n_rows * n_cols
    a_re, a_im, b_re, b_im = _operands(n_rows, n_cols)
    in_fmt = accel._in_fmt()
    A = cx.make_complex(a_re.ravel(), a_im.ravel(), in_fmt)
    B = cx.make_complex(b_re.ravel(), b_im.ravel(), in_fmt)
    a_addr, b_addr, y_addr = 0, nm, 2 * nm
    size = 2 * nm + n_cols  # A | B | Y
    mem = cx.make_complex(np.zeros(size), np.zeros(size), in_fmt)
    mem[a_addr:a_addr + nm] = A
    mem[b_addr:b_addr + nm] = B
    cmd = _reduce_cmd(accel, n_rows, n_cols, a_addr, b_addr, y_addr)
    mem_exp = mem.copy()
    accel.execute(cmd, mem_exp)  # the golden writes Y
    return cmd, mem, mem_exp


def gen_point(tdir: Path, n_rows: int, n_cols: int) -> dict:
    """Generate the synthesizable top + cosim TB + vectors for one sweep size."""
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
    cmd, mem, mem_exp = _scenario(accel, n_rows, n_cols)
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
    return {"n_rows": n_rows, "n_cols": n_cols, "nmem": nmem}


def _burst_words(burst) -> int:
    return sum(1 for bt in burst.get("beat_type", []) if bt == 0)  # transfer beats


def extract_timing(tdir: Path, n_rows: int, n_cols: int) -> dict:
    """RTL kernel latency: transaction cycles (cosim report) + VCD first-burst->last-write ns."""
    from vcdvcd import VCDVCD

    from waveflow.scripts.xsim_vcd import run_xsim_vcd
    from waveflow.utils.cosimparse import CosimReportParser
    from waveflow.utils.vcd import VcdParser

    cycles = CosimReportParser(
        sol_path=tdir / PROJ / "solution1", top="vmac"
    ).get_transaction_cycles()

    vcd_path = run_xsim_vcd(top="vmac", comp=PROJ, out="dump.vcd",
                            soln="solution1", trace_level="port", workdir=tdir)
    vcd = VCDVCD(str(vcd_path), signals=None, store_tvs=True)
    vp = VcdParser(vcd)
    clk_sig = vp.add_clock_signal()
    aximm_sigs, _ = vp.add_aximm_signals(
        prefix="m_axi_gmem_", dir="both", lite_only=False, short_name_prefix="gmem_",
    )
    write_bursts, read_bursts, clk_period = vp.extract_aximm_bursts(
        clk_name=clk_sig, aximm_sigs=aximm_sigs,
    )
    read_words = sum(_burst_words(b) for b in read_bursts)
    write_words = sum(_burst_words(b) for b in write_bursts)
    tstarts = [float(b.get("data_tstart", b["tstart"])) for b in read_bursts + write_bursts]
    wends = [float(b["data_tend"]) for b in write_bursts]
    kernel_start = min(tstarts, default=0.0)
    complete_t = max(wends, default=kernel_start)
    return {
        "transaction_cycles": cycles,
        "read_words": read_words,
        "write_words": write_words,
        "read_burst_count": len(read_bursts),
        "vcd_latency_ns": complete_t - kernel_start,
        "clk_period_ns": float(clk_period),
    }


def run() -> dict:
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    base = Path(__file__).resolve().parent / "sweep"
    points = []
    for n_rows, n_cols in SWEEP:
        tag = f"{n_rows}x{n_cols}"
        tdir = base / tag
        gen_point(tdir, n_rows, n_cols)
        print(f"[{tag}] running Vitis csim/csynth/cosim (trace port) ...")
        res = toolchain.run_vitis_hls(tdir / "run.tcl", work_dir=tdir, capture_output=True)
        out = (getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")
        if "WAVEFLOW_SUCCESS" not in out or "VMAC_COSIM_OK" not in out:
            raise RuntimeError(
                f"[{tag}] cosim did not report success markers.\n"
                f"WAVEFLOW_SUCCESS={'WAVEFLOW_SUCCESS' in out}  "
                f"VMAC_COSIM_OK={'VMAC_COSIM_OK' in out}\n--- tail ---\n{out[-2000:]}"
            )
        print(f"[{tag}] markers: WAVEFLOW_SUCCESS + VMAC_COSIM_OK seen (csim bit-exact vs golden)")
        timing = extract_timing(tdir, n_rows, n_cols)
        trips = n_rows * math.ceil(n_cols / PF)
        point = {"n_rows": n_rows, "n_cols": n_cols, "pf": PF, "trips": trips, **timing}
        print(f"[{tag}] trips={trips}  rtl_cycles={timing['transaction_cycles']}  "
              f"read_words={timing['read_words']}  vcd_latency_ns={timing['vcd_latency_ns']:.1f}")
        points.append(point)
    emit(points)
    return {"points": points}


def emit(points: list[dict]) -> None:
    artifact = {
        "source": "cosim",
        "tool": "Vitis HLS 2025.1 cosim (xsim), xc7z020clg484-1, create_clock -period 10",
        "clk_period_ns": points[0]["clk_period_ns"] if points else 10.0,
        "pf": PF,
        "mem_dwidth": COSIM_CFG.mem_dwidth,
        "op": "inner_prod+reduce (distinct B, worst-case II)",
        "trips_formula": "trips = n_rows * ceil(n_cols / PF)",
        "latency_model": "latency_cycles ~= depth + II * trips",
        "note": ("RTL transaction_cycles is independent of ab_eq (Stage 3: anorm==abcorr==347 "
                 "at 4x4), so one distinct-B cosim per size calibrates the cycle model; "
                 "the II-decoupled sim reproduces anorm==abcorr by construction (latency = f(trips))."),
        "sweep": points,
    }
    CALIB_JSON.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"calibration artifact written: {CALIB_JSON}")


if __name__ == "__main__":
    run()
