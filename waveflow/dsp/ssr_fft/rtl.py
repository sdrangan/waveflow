"""rtl.py -- build ``SsrFft`` for one ``L``, run it under XSI, read its bits and its port timing.

The F3 rung of ``plans/ssr_fft.md``.  Everything before csynth is Python: the headers and task
wrappers (:mod:`.hls`), the ``ap_ctrl_none`` top (``composite_top_spec`` on the module), the XSI
harness (``tb_top_spec`` on :class:`~.testbench.SsrFftTB`), the scenario.  Timing is read from the
BFMs' own capture bundles (``capture_accepts``), with no waveform.

**Build in a short directory**: csynth fails silently past the Windows path limit.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, SourceStep
from waveflow.build.composite_gen import (
    GEN_DIR,
    INCLUDE_DIR,
    RFSOC4X2_PART,
    RFSOC4X2_PERIOD_NS,
    composite_top_spec,
    render_ports_h,
    render_rtl_f,
    render_tb_harness,
    render_tb_main,
    render_tcl,
    render_top,
    tb_top_spec,
    tcl_path,
)
from waveflow.build.streamutils import MemMgrStep, XsiHarnessStep
from waveflow.build.trace_steps import AddVcdTopStep, xsi_runner_cmd
from waveflow.simulation.simulation import Simulation
from waveflow.vitis_l1.testbench import golden

from . import hls
from .hw import SsrFft
from .testbench import R, SsrFftTB, captured_frames, port_names, write_scenario

TOP = "ssr_fft"
XSI_DIR = "xsi"
TB = f"{TOP}_bfm_tb"


def make_tb(length: int, n_frames: int = 8, burst_gaps=(), **kw) -> SsrFftTB:
    """The testbench graph, sized to run past its last frame (a generous L/R + 64 a frame)."""
    tb = SsrFftTB(name="tb", sim=Simulation(), length=length, n_frames=n_frames,
                  burst_gaps=list(burst_gaps), capture_accepts=True, **kw)
    tb.n_cycles = int(sum(burst_gaps) + (tb.n_frames + 4) * (2 * tb.length // R + 64) + 3000)
    return tb


def generate(root: Path, length: int, *, n_frames: int = 8, burst_gaps=(), reorder: str = "sob",
             lanes: bool = False, part: str = RFSOC4X2_PART,
             period_ns: float = RFSOC4X2_PERIOD_NS) -> str:
    """Everything before csynth for an *length*-point build at *root*."""
    root = Path(root).resolve()
    dag = BuildDag()
    dag.add(SourceStep(artifact="ssr_fft_hw", path=Path(__file__).with_name("hw.py")))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))       # the generated top includes memmgr.hpp
    dag.add(XsiHarnessStep(output_dir=XSI_DIR))
    dag.add(AddVcdTopStep(name="vcd_top", comp_class=SsrFft, source_artifact="ssr_fft_hw",
                          output_dir=XSI_DIR))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")

    tb = make_tb(length, n_frames, burst_gaps, timed=False, reorder=reorder, lanes=lanes)
    dut = tb.dut
    hls.write_sources(dut.geo, root, INCLUDE_DIR, reorder=reorder, lanes=lanes)
    # Every boundary port at its own width: the output grows with log2 L, and the RadixWord ports
    # are R samples wide.
    widths = {name: int(ep.bitwidth) for name, ep in dut.boundary}
    spec = composite_top_spec(dut, width=next(iter(widths.values())), port_widths=widths)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    tcl_path(root, spec.top_name).write_text(
        render_tcl(spec.top_name, part=part, period_ns=period_ns,
                   solution_config=("config_rtl -reset state",)), encoding="utf-8")
    xsi = root / XSI_DIR
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    write_tb(root, tb)
    return spec.top_name


def write_tb(root: Path, tb: SsrFftTB) -> None:
    """The XSI harness + main for *tb*, and its scenario bundles."""
    xsi = Path(root) / XSI_DIR
    spec = tb_top_spec(tb)
    (xsi / f"{TOP}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{TB}.cpp").write_text(render_tb_main(spec, int(tb.n_cycles)), encoding="utf-8")
    write_scenario(xsi, tb.n_frames, tb.length, lanes=tb.lanes, **tb.config)


def synth(root: Path) -> str:
    """csynth the top, stamp the sources it was built from, and list its RTL for ``xvlog``."""
    from waveflow.build.rtl_digest import write_stamp
    from waveflow.toolchain.toolchain import run_vitis_hls

    root = Path(root).resolve()
    r = run_vitis_hls(tcl_path(root, TOP), work_dir=root)
    out = (r.stdout or "") + (r.stderr or "")
    if "WAVEFLOW_CSYNTH_OK" not in out:
        raise RuntimeError(f"csynth of {TOP} failed:\n{out[-6000:]}")
    _check_axis_ports(root)
    write_stamp(root, TOP)
    (root / XSI_DIR / f"rtl_{TOP}.f").write_text(render_rtl_f(TOP, root), encoding="utf-8")
    return out


def _check_axis_ports(root: Path) -> None:
    """Refuse a synthesis whose boundary ports did not come out as AXI-Stream.

    Twice (2026-10-06) a csynth of an unchanged, correct top produced plain ``ap_fifo`` ports -- no
    ``ap_rst_n``, so XSI later died on ``port 'ap_rst_n' not found`` -- and its log showed it had
    analyzed a different ``gen/ssr_fft.cpp`` than the one on disk (the dataflow function at another
    line).  Both times another build was running concurrently; a re-synthesis of the same files was
    correct.  The cause is not isolated, so the symptom is checked here, where it is cheap and the
    message can say what happened, rather than surfacing as a missing reset pin in the simulator.
    """
    log = (root / f"{TOP}_proj" / "solution1" / "solution1.log").read_text(encoding="utf-8",
                                                                            errors="replace")
    bad = [ln for ln in log.splitlines()
           if "Setting interface mode on port" in ln and "to 'ap_fifo'" in ln]
    if bad:
        raise RuntimeError(
            f"csynth of {TOP} in {root} produced ap_fifo boundary ports instead of axis:\n  "
            + "\n  ".join(bad)
            + "\nThe top declares them axis, so this synthesis did not compile the top on disk "
              "(seen when another build ran concurrently).  Re-run the synthesis.")


def run_xsi(root: Path, trace: bool = False) -> str:
    """Run the XSI testbench, clearing the previous capture first so a stale one cannot pass."""
    xsi = Path(root) / XSI_DIR
    ins, outs = port_names(True)
    ins2, outs2 = port_names(False)
    for name in outs + outs2 + [f"{n}_acc" for n in ins + ins2]:
        d = xsi / "vectors" / name
        if d.exists():
            for f in d.iterdir():
                f.unlink()
    p = subprocess.run(xsi_runner_cmd(TOP, TB, trace=trace), cwd=xsi, capture_output=True,
                       text=True, timeout=3600)
    if p.returncode != 0 or "XSI_EXITCODE=0" not in p.stdout:
        raise RuntimeError(f"XSI run failed\n{p.stdout[-3000:]}\n{p.stderr[-2000:]}")
    return p.stdout


def _out_w(length: int) -> int:
    from .model import Geometry
    return int(Geometry(length).out_fmt.W)


def check_bits(root: Path, length: int, n_frames: int, *, lanes: bool = False) -> list[bool]:
    """Per frame: is the RTL's output the golden?  (Fewer entries than frames = frames missing.)
    *lanes* says which boundary the build has -- the caller built it, so the caller knows."""
    got = captured_frames(Path(root) / XSI_DIR, length, lanes)
    want = golden(n_frames, length)
    mask = (1 << _out_w(length)) - 1
    return [bool(np.array_equal(g[0] & mask, w[0] & mask) and np.array_equal(g[1] & mask, w[1] & mask))
            for g, w in zip(got, want)]


def capture_beats(root: Path, *, lanes: bool = False) -> dict[str, list[int]]:
    """Each accepted input beat and each received output beat, on the BFMs' shared cycle count."""
    vec = Path(root) / XSI_DIR / "vectors"
    beats = {}
    ins, outs = port_names(lanes)
    for port, name in [(i, f"{i}_acc") for i in ins] + [(o, o) for o in outs]:
        cyc = vec / name / "cycles.bin"
        beats[port] = np.fromfile(cyc, dtype="<u8").astype(int).tolist() if cyc.exists() else []
    return beats


def frame_times(root: Path, length: int, *, lanes: bool = False) -> list[dict]:
    """Per frame: first input beat, last input beat, last output beat (cycles; the first input
    port, and every output port)."""
    b = capture_beats(root, lanes=lanes)
    n = length // R
    in_ports, out_ports = port_names(lanes)
    ins = b[in_ports[0]]
    outs = [max(b[p][k] for p in out_ports) for k in range(len(b[out_ports[0]]))]
    frames = []
    for k in range(min(len(ins), len(outs)) // n):
        frames.append({"in": ins[k * n], "last_in": ins[(k + 1) * n - 1],
                       "done": outs[(k + 1) * n - 1]})
    return frames
