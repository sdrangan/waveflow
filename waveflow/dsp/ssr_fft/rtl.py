"""rtl.py -- build ``SsrFft`` for one ``L``, run it under XSI, read its bits and its port timing.

The F3 rung of ``plans/ssr_fft.md``.  Everything before csynth is Python: the headers and task
wrappers (:mod:`.hls`), the ``ap_ctrl_none`` top (``composite_top_spec`` on the module).  The RTL
run is the **system flow** (``plans/system_model.md`` S3f): :class:`SsrFftSystem` cuts
:class:`~.testbench.SsrFftTB` at the FFT, and ``add_system_steps`` derives the system top, the
harness of the drivers' and sinks' BFM models, and the scenario, then runs it.  Timing is read from
the models' own capture bundles (``capture_accepts``), with no waveform.

**Build in a short directory**: csynth fails silently past the Windows path limit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, SourceStep
from waveflow.build.composite_gen import (
    GEN_DIR,
    INCLUDE_DIR,
    RFSOC4X2_PART,
    RFSOC4X2_PERIOD_NS,
    composite_top_spec,
    render_tcl,
    render_top,
    tcl_path,
)
from waveflow.build.streamutils import MemMgrStep
from waveflow.hw.hw_system import GraphSystem
from waveflow.simulation.simulation import Simulation

from . import hls
from .testbench import R, SsrFftTB, captured_frames, golden, port_names, write_scenario

TOP = "ssr_fft"
#: The system top an RTL run elaborates, and where under a build root its run lives.
SYSTEM_TOP = "ssr_fft_system"
SYSTEM_DIR = "system"
#: What :func:`generate` records for :func:`run_xsi`: the scenario the build was generated for.
SCENARIO_JSON = "ssr_fft_scenario.json"


def make_tb(length: int, n_frames: int = 8, burst_gaps=(), **kw) -> SsrFftTB:
    """The testbench graph, sized to run past its last frame (a generous L/R + 64 a frame)."""
    tb = SsrFftTB(name="tb", sim=Simulation(), length=length, n_frames=n_frames,
                  burst_gaps=list(burst_gaps), capture_accepts=True, **kw)
    tb.n_cycles = int(sum(burst_gaps) + (tb.n_frames + 4) * (2 * tb.length // R + 64) + 3000)
    return tb


def generate(root: Path, length: int, *, n_frames: int = 8, burst_gaps=(), reorder: str = "sob",
             lanes: bool = False, inverse: bool = False, part: str = RFSOC4X2_PART,
             period_ns: float = RFSOC4X2_PERIOD_NS) -> str:
    """Everything before csynth for an *length*-point build at *root*."""
    root = Path(root).resolve()
    dag = BuildDag()
    dag.add(SourceStep(artifact="ssr_fft_hw", path=Path(__file__).with_name("hw.py")))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))       # the generated top includes memmgr.hpp
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")

    scenario = {"length": int(length), "n_frames": int(n_frames),
                "burst_gaps": [int(g) for g in burst_gaps], "reorder": reorder,
                "lanes": bool(lanes), "inverse": bool(inverse)}
    (root / SCENARIO_JSON).write_text(json.dumps(scenario, indent=1) + "\n", encoding="utf-8")
    dut = SsrFftSystem(**scenario).tb.dut
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
    return spec.top_name


@dataclass
class SsrFftSystem(GraphSystem):
    """One RTL scenario of ``SsrFft`` as a **system**: :class:`~.testbench.SsrFftTB` cut at the FFT,
    the drivers (each recording the cycle every beat was accepted) and the timed sinks its
    testbench.  The run's bound is the graph's ``n_cycles`` (:func:`make_tb`)."""

    length: int = 64
    n_frames: int = 8
    burst_gaps: list = field(default_factory=list)
    reorder: str = "sob"
    lanes: bool = False
    inverse: bool = False

    def graph(self) -> SsrFftTB:
        return make_tb(self.length, self.n_frames, self.burst_gaps, timed=False,
                       reorder=self.reorder, lanes=self.lanes, inverse=self.inverse)

    def dut_modules(self, g) -> list:
        return [g.dut]

    def write_inputs(self, root) -> None:
        g = self.tb
        write_scenario(root, g.n_frames, g.length, lanes=g.lanes, **g.config)


def system_dag(root: Path, trace: bool = False) -> BuildDag:
    """The system flow for the scenario :func:`generate` recorded at *root*."""
    from waveflow.build.system_dag import add_system_steps

    root = Path(root).resolve()
    scenario = json.loads((root / SCENARIO_JSON).read_text(encoding="utf-8"))
    dag = BuildDag()
    add_system_steps(dag, SsrFftSystem(**scenario), work_dir=root / SYSTEM_DIR, top=SYSTEM_TOP,
                     tcls={TOP: tcl_path(root, TOP).relative_to(root)}, trace=trace)
    return dag


def vectors_dir(root: Path) -> Path:
    """Where the last RTL run's testbench read and recorded."""
    return Path(root).resolve() / SYSTEM_DIR / SYSTEM_TOP / "xsi_run" / "vectors"


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


def run_xsi(root: Path, trace: bool = False) -> Path:
    """Run the scenario :func:`generate` recorded, through the system flow at RTL; return the
    run's vectors directory (:func:`vectors_dir`).  ``synth="check"``: the csynth'd RTL must be
    current (:func:`synth`), never built here.  Each run starts from a fresh scenario copy, so a
    stale capture cannot pass; compile, elaborate and the testbench build are skipped when their
    inputs are unchanged, so a repeated run costs the simulation alone."""
    root = Path(root).resolve()
    res = system_dag(root, trace).run(BuildConfig(root_dir=root, params={"synth": "check"}),
                                      through="system_xsi")
    bad = {n: r.message for n, r in res.items() if not r.success}
    if bad:
        raise RuntimeError(f"the {SYSTEM_TOP} run at {root} failed: {bad}")
    return vectors_dir(root)


def _out_w(length: int) -> int:
    from .model import Geometry
    return int(Geometry(length).out_fmt.W)


def check_bits(root: Path, length: int, n_frames: int, *, lanes: bool = False,
               inverse: bool = False) -> list[bool]:
    """Per frame: is the RTL's output the golden?  (Fewer entries than frames = frames missing.)
    *lanes* and *inverse* say what the build is -- the caller built it, so the caller knows."""
    got = captured_frames(vectors_dir(root).parent, length, lanes, inverse=inverse)
    want = golden(n_frames, length, inverse=inverse)
    mask = (1 << _out_w(length)) - 1
    return [bool(np.array_equal(g[0] & mask, w[0] & mask) and np.array_equal(g[1] & mask, w[1] & mask))
            for g, w in zip(got, want)]


def capture_beats(root: Path, *, lanes: bool = False) -> dict[str, list[int]]:
    """Each accepted input beat and each received output beat, on the BFMs' shared cycle count."""
    vec = vectors_dir(root)
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
