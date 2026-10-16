"""profile_xsi.py — where an XSI run's time goes, next to the pysim's for the same scenario.

    python -m waveflow.vitis_l1.profile_xsi <build root> <L> [--frames N] [--sweep]

Times each phase of the XSI flow separately -- ``xvlog``, ``xelab``, the harness ``g++``, the
simulation itself (untraced, ``-debug typical`` and ``-debug off``) -- plus the traced variant's extra
cost (dumping and parsing the waveform), and the pysim for the identical scenario.  csynth is read from
the build's own log (it already ran).  The point is a fair comparison: an untraced run is what a
designer who knows what they need would pay; tracing is reported separately, as the debugging cost.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

VIV = Path(os.environ.get("XILINX_VIVADO", r"C:\Xilinx\2025.1\Vivado"))
MINGW = VIV / "tps" / "mingw" / "6.2.0" / "win64.o" / "nt"


def _env(xsi: Path, top: str) -> dict:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(xsi / "xsim.dir" / top), str(MINGW / "bin"),
                                   str(VIV / "lib" / "win64.o"), str(VIV / "bin"), env["PATH"]])
    return env


def _timed(cmd, cwd, env, shell=False) -> tuple[float, str]:
    t = time.perf_counter()
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, shell=shell)
    dt = time.perf_counter() - t
    if p.returncode != 0:
        raise RuntimeError(f"{cmd} failed:\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
    return dt, p.stdout


def _csynth_seconds(root: Path) -> float | None:
    log = root / "logs" / "hls_run_tcl.log"
    if not log.exists():
        return None
    m = re.findall(r"Total elapsed time: ([\d.]+) seconds", log.read_text(errors="replace"))
    return float(m[-1]) if m else None


#: The profiler's own XSI workspace under the build root.  It times the kernel flow's phases one by
#: one, so it drives the KERNEL as the elaborated top with a harness of its own -- not the system
#: flow the gates run (``rtl.run_xsi``), whose steps hide those phases behind one DAG step.
XSI_DIR = "xsi_profile"


def _kernel_workspace(root: Path, length: int, tb) -> tuple[Path, str]:
    """Write the kernel-as-top XSI workspace for *tb* under ``<root>/xsi_profile``: the BFM library
    and run scripts, the VCD dumper, the kernel's port map and RTL file list, the harness and its
    main, and the scenario.  Returns ``(the workspace, the testbench name)``."""
    from waveflow.build.build import BuildConfig, BuildDag, SourceStep
    from waveflow.build.composite_gen import (composite_top_spec, render_ports_h, render_rtl_f,
                                              render_tb_harness, render_tb_main, tb_top_spec)
    from waveflow.build.streamutils import XsiHarnessStep
    from waveflow.build.trace_steps import AddVcdTopStep
    from waveflow.vitis_l1 import rtl
    from waveflow.vitis_l1.hw import VitisFft
    from waveflow.vitis_l1.testbench import write_scenario

    dag = BuildDag()
    dag.add(SourceStep(artifact="vitis_fft_hw", path=Path(rtl.__file__).with_name("hw.py")))
    dag.add(XsiHarnessStep(output_dir=XSI_DIR))
    dag.add(AddVcdTopStep(name="vcd_top", comp_class=VitisFft, source_artifact="vitis_fft_hw",
                          output_dir=XSI_DIR))
    dag.run(BuildConfig(root_dir=root, params={}), force=True)
    xsi = root / XSI_DIR
    dut = tb.dut
    widths = {f"s_in_{j}": ep.bitwidth for j, ep in enumerate(dut.s_in)}
    widths |= {f"m_out_{j}": ep.bitwidth for j, ep in enumerate(dut.m_out)}
    kspec = composite_top_spec(dut, width=int(dut.s_in[0].bitwidth), port_widths=widths)
    (xsi / f"{rtl.TOP}_ports.h").write_text(render_ports_h(kspec), encoding="utf-8")
    # The list names the csynth'd RTL relative to a workspace one level below the build root --
    # which this one is, as the kernel flow's xsi/ was.
    (xsi / f"rtl_{rtl.TOP}.f").write_text(render_rtl_f(rtl.TOP, root), encoding="utf-8")
    tb_name = f"{rtl.TOP}_bfm_tb"
    spec = tb_top_spec(tb)
    (xsi / f"{rtl.TOP}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{tb_name}.cpp").write_text(render_tb_main(spec, int(tb.n_cycles)), encoding="utf-8")
    write_scenario(xsi, tb.n_frames, tb.length, **tb.config)
    return xsi, tb_name


def profile(root: Path, length: int, n_frames: int = 6, sweep: bool = False) -> dict:
    from waveflow.simulation.simulation import Simulation
    from waveflow.vitis_l1 import rtl
    from waveflow.vitis_l1.testbench import VitisFftTB, write_scenario

    root = Path(root).resolve()
    rtl.ensure_built(root, length)
    top = rtl.TOP
    gaps = rtl.sweep_gaps(length, n_frames) if sweep else ()
    tb = rtl.make_tb(length, n_frames, gaps, untimed=True)
    xsi, tb_name = _kernel_workspace(root, length, tb)
    env = _env(xsi, top)
    bat = lambda exe: str(VIV / "bin" / f"{exe}.bat")   # noqa: E731
    out: dict = {"L": length, "n_frames": n_frames, "sweep": sweep, "sim_cycles": int(tb.n_cycles),
                 "csynth_s": _csynth_seconds(root)}

    out["xvlog_s"], _ = _timed(["cmd", "/c", bat("xvlog"), "-f", f"rtl_{top}.f"], xsi, env)
    gpp = str(MINGW / "bin" / "g++.exe")
    inc = f"-I{VIV / 'data' / 'xsim' / 'include'}"

    def build_and_run(debug: str, trace: bool) -> tuple[float, float, float]:
        tops = [f"work.{top}"] + ([f"work.vcd_dumper_{top}"] if trace else [])
        if trace:
            _timed(["cmd", "/c", bat("xvlog"), f"vcd_dumper_{top}.v"], xsi, env)
        t_elab, _ = _timed(["cmd", "/c", bat("xelab"), *tops, "-dll", "-s", top, "-debug", debug],
                           xsi, env)
        t_gpp = 0.0
        for c in ([gpp, inc, "-O3", "-c", "-o", "xsi_loader.o", "xsi_loader.cpp"],
                  [gpp, inc, "-O3", "-c", "-o", f"{tb_name}.o", f"{tb_name}.cpp"],
                  [gpp, "-o", f"{tb_name}.exe", f"{tb_name}.o", "xsi_loader.o"]):
            dt, _ = _timed(c, xsi, env)
            t_gpp += dt
        t_run, _ = _timed([str(xsi / f"{tb_name}.exe")], xsi, env)
        return t_elab, t_gpp, t_run

    out["xelab_s"], out["gpp_s"], out["sim_s"] = build_and_run("typical", False)
    rtl.check_bits(root, length, n_frames, "profile", vec=xsi / "vectors")
    ref = rtl.frame_times(rtl.capture_beats(root, xsi / "vectors"), length, n_frames)
    out["xelab_debug_off_s"], _, out["sim_debug_off_s"] = build_and_run("off", False)
    same = rtl.frame_times(rtl.capture_beats(root, xsi / "vectors"), length, n_frames) == ref
    out["debug_off_same_timing"] = same
    out["xelab_traced_s"], _, out["sim_traced_s"] = build_and_run("typical", True)
    t = time.perf_counter()
    rtl.port_beats(xsi / f"{top}_trace.vcd")
    out["vcd_parse_s"] = time.perf_counter() - t
    out["vcd_mb"] = round((xsi / f"{top}_trace.vcd").stat().st_size / 1e6, 1)
    out["sim_cycles_per_s"] = round(out["sim_cycles"] / out["sim_s"])
    out["sim_cycles_per_s_debug_off"] = round(out["sim_cycles"] / out["sim_debug_off_s"])

    # The pysim for the identical scenario, timed from the platform.
    with tempfile.TemporaryDirectory() as d:
        write_scenario(d, n_frames, length)
        t = time.perf_counter()
        ptb = VitisFftTB(name="tb", sim=Simulation(), length=length, n_frames=n_frames,
                         burst_gaps=list(gaps), root=Path(d))
        ptb.sim.run_sim()
        out["pysim_s"] = time.perf_counter() - t
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in out.items()}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("length", type=int)
    ap.add_argument("--frames", type=int, default=6)
    ap.add_argument("--sweep", action="store_true", help="isolated frames on the phase-sweep gaps")
    a = ap.parse_args(argv)
    print(json.dumps(profile(a.root, a.length, a.frames, a.sweep), indent=1))


if __name__ == "__main__":
    main()
