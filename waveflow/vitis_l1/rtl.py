"""rtl.py — build ``VitisFft`` for one ``L``, run it under XSI, and read its timing off the ports.

The RTL half of the module's calibration (``waveflow/calib/fixtures/vitis_fft.py``) and of the worked
example (``examples/vitis_fft``).  Nothing here is C++ someone maintains: the task body is the
framework's ``waveflow/build/vitis_fft_task.h`` (copied by ``VitisL1Step``), the vendor FFT stays in the
Vitis install (an include path), the ``ap_ctrl_none`` top comes from ``composite_top_spec`` on the module,
and the RTL run is the **system flow** (``plans/system_model.md`` S3f): :class:`VitisFftSystem` cuts
the testbench graph at the FFT, and ``add_system_steps`` derives the system top, the harness of the
drivers' and sinks' BFM models, and the scenario.

**Build in a short directory.**  csynth fails *silently* -- "Synthesis failed", no error -- when the
project path is long enough to hit the Windows path limit (measured from a deep temp directory).
"""
from __future__ import annotations

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
from waveflow.build.vitis_l1_step import VitisL1Step, vitis_fft_include_dir
from waveflow.hw.hw_system import GraphSystem
from waveflow.simulation.simulation import Simulation
from waveflow.utils.burst_io import read_burst_bundle
from waveflow.vitis_l1.hw import VitisFft
from waveflow.vitis_l1.testbench import R, VitisFftTB, frames_from_lanes, golden, write_scenario

TOP = "vitis_fft"
#: The system top an RTL run elaborates, and where under a build root its run lives.
SYSTEM_TOP = "vitis_fft_system"
SYSTEM_DIR = "system"


def make_tb(length: int, n_frames: int = 4, burst_gaps=(), **kw) -> VitisFftTB:
    """The testbench graph, sized to run past its last frame."""
    tb = VitisFftTB(name="tb", sim=Simulation(), length=length, n_frames=n_frames,
                    burst_gaps=list(burst_gaps), capture_accepts=True, **kw)
    # Every gap, plus a generous per-frame allowance (the measured interval is 7.5 L/R at L >= 64)
    # -- per FRAME, not per gap, or a back-to-back run is cut short.
    tb.n_cycles = int(sum(burst_gaps) + (tb.n_frames + 2) * (12 * tb.length // R + 64) + 500)
    return tb


def generate(root: Path, length: int, part: str = RFSOC4X2_PART,
             period_ns: float = RFSOC4X2_PERIOD_NS, config: dict | None = None) -> str:
    """Everything before csynth for an *length*-point build at *root*: headers, the top and its
    ``.tcl``.  Python only."""
    root = Path(root).resolve()
    dag = BuildDag()
    dag.add(SourceStep(artifact="vitis_fft_hw", path=Path(__file__).with_name("hw.py")))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))       # the generated top includes memmgr.hpp
    dag.add(VitisL1Step(output_dir=INCLUDE_DIR))      # the body, copied verbatim
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")

    cfg = dict(config or {})
    dut = make_tb(length, untimed=True, **cfg).dut
    # The output word is wider than the input word (the FFT's output format grows with log2 L), so
    # each port takes its own endpoint's width rather than one design width.
    widths = {f"s_in_{j}": ep.bitwidth for j, ep in enumerate(dut.s_in)}
    widths |= {f"m_out_{j}": ep.bitwidth for j, ep in enumerate(dut.m_out)}
    spec = composite_top_spec(dut, width=int(dut.s_in[0].bitwidth), port_widths=widths)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    tcl_path(root, spec.top_name).write_text(
        render_tcl(spec.top_name, include_dirs=(str(vitis_fft_include_dir()),),
                   part=part, period_ns=period_ns), encoding="utf-8")
    return spec.top_name


@dataclass
class VitisFftSystem(GraphSystem):
    """One RTL scenario of ``VitisFft`` as a **system**: :class:`VitisFftTB` cut at the FFT, the
    lane drivers (each recording the cycle every beat was accepted) and sinks its testbench.  The
    run's bound is the graph's ``n_cycles`` (:func:`make_tb`)."""

    length: int = 16
    n_frames: int = 4
    burst_gaps: list = field(default_factory=list)
    config: dict = field(default_factory=dict)

    def graph(self) -> VitisFftTB:
        return make_tb(self.length, self.n_frames, self.burst_gaps, untimed=True, **self.config)

    def dut_modules(self, g) -> list:
        return [g.dut]

    def write_inputs(self, root) -> None:
        write_scenario(root, self.tb.n_frames, self.tb.length, **self.tb.config)


def system_dag(root: Path, sysm: VitisFftSystem, trace: bool = False) -> BuildDag:
    """The system flow for *sysm*, built under *root* (where :func:`generate` and :func:`synth`
    put the kernel)."""
    from waveflow.build.system_dag import add_system_steps

    root = Path(root).resolve()
    dag = BuildDag()
    add_system_steps(dag, sysm, work_dir=root / SYSTEM_DIR, top=SYSTEM_TOP,
                     tcls={TOP: tcl_path(root, TOP).relative_to(root)}, trace=trace)
    return dag


def vectors_dir(root: Path) -> Path:
    """Where the last RTL run's testbench read and recorded."""
    return Path(root).resolve() / SYSTEM_DIR / SYSTEM_TOP / "xsi_run" / "vectors"


def synth(root: Path) -> str:
    """csynth the top, stamp the sources it was built from, and list its RTL for ``xvlog``."""
    from waveflow.build.rtl_digest import write_stamp
    from waveflow.toolchain.toolchain import run_vitis_hls

    # Absolute: Vitis resolves a relative .tcl path from its work_dir, doubling the prefix.
    root = Path(root).resolve()
    r = run_vitis_hls(tcl_path(root, TOP), work_dir=root)
    out = (r.stdout or "") + (r.stderr or "")
    if "WAVEFLOW_CSYNTH_OK" not in out:
        raise RuntimeError(f"csynth of {TOP} failed:\n{out[-6000:]}")
    write_stamp(root, TOP)
    return out


def ensure_built(root: Path, length: int, config: dict | None = None) -> None:
    """Generate, and csynth only if the RTL is missing or stale."""
    from waveflow.build.trace_steps import rtl_staleness
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    generate(root, length, config=config)
    if not (root / f"{TOP}_proj").is_dir() or rtl_staleness(root, TOP) is not None:
        synth(root)


def run_xsi(root: Path, sysm: VitisFftSystem, trace: bool = False) -> Path:
    """Run *sysm* at RTL through the system flow; return the run's vectors directory.
    ``synth="check"``: the csynth'd RTL must be current (:func:`synth`), never built here.  Each run
    starts from a fresh scenario copy, so a stale capture cannot pass; compile, elaborate and the
    testbench build are skipped when their inputs are unchanged, so a repeated run costs the
    simulation alone."""
    root = Path(root).resolve()
    res = system_dag(root, sysm, trace).run(
        BuildConfig(root_dir=root, params={"synth": "check"}), through="system_xsi")
    bad = {n: r.message for n, r in res.items() if not r.success}
    if bad:
        raise RuntimeError(f"the {SYSTEM_TOP} run at {root} failed: {bad}")
    return vectors_dir(root)


def _out_w(length: int, config: dict | None = None) -> int:
    return int(VitisFft(name="w", sim=Simulation(), L=length, **(config or {})).out_fmt.W)


def captured_frames(root: Path, length: int, config: dict | None = None, vec: Path | None = None):
    """What the RTL emitted, from the sinks' capture bundles, as ``[(re, im), ...]``.  *vec* is
    the run's vectors directory (default: the last system run's, :func:`vectors_dir`)."""
    vec = Path(vec) if vec is not None else vectors_dir(root)
    lanes = [np.concatenate(read_burst_bundle(vec / f"m_out_{j}")) for j in range(R)]
    return frames_from_lanes(lanes, _out_w(length, config), length)


def check_bits(root: Path, length: int, n_frames: int, what: str = "run",
               config: dict | None = None, vec: Path | None = None) -> None:
    """Every frame the RTL emitted equals the golden; raises otherwise."""
    cfg = dict(config or {})
    got, want = captured_frames(root, length, cfg, vec), golden(n_frames, length, **cfg)
    if len(got) != n_frames:
        raise RuntimeError(f"L={length} {what}: captured {len(got)} frames, expected {n_frames}")
    mask = (1 << _out_w(length, cfg)) - 1
    for k, ((g_re, g_im), (w_re, w_im)) in enumerate(zip(got, want)):
        if not (np.array_equal(g_re & mask, w_re & mask) and np.array_equal(g_im & mask, w_im & mask)):
            raise RuntimeError(f"L={length} {what}: frame {k} is not bit-exact")


def port_beats(vcd_path: Path, scope: str = TOP) -> dict[str, list[int]]:
    """Clock-edge index of every accepted beat (TVALID && TREADY) on each AXIS port, read with the
    framework's VCD parser -- which samples just before each edge, as the flops do.  *scope* is the
    kernel's scope in the dump: the kernel itself when it is the elaborated top, or its path inside
    a system top (``SystemTopSpec.scope_of``)."""
    from vcdvcd import VCDVCD

    from waveflow.utils.vcd import VcdParser

    parser = VcdParser(VCDVCD(str(vcd_path)))
    clk = parser.add_clock_signal("ap_clk")
    beats = {}
    for port in [f"s_in_{j}" for j in range(R)] + [f"m_out_{j}" for j in range(R)]:
        # Anchored at the top scope: the task inside has nets of the same suffix
        # (`vitis_fft.vitis_fft_task_..._U0_s_in_0_TREADY`), and the port is the one measured.
        sigs = parser.add_axiss_signals(name=f"{scope}.{port}_T", short_name_prefix=port)
        sigs = sigs[0] if isinstance(sigs, tuple) else sigs
        bursts, _ = parser.extract_axis_bursts(clk, sigs)
        beats[port] = [int(b["start_idx"]) + i for b in bursts
                       for i, t in enumerate(b["beat_type"]) if t == 0]
    return beats


def capture_beats(root: Path, vec: Path | None = None) -> dict[str, list[int]]:
    """The same beats :func:`port_beats` reads off a waveform, from the BFMs' own capture bundles:
    each ``AxisMaster``'s accepted words (``s_in_<j>_acc``) and each ``AxisSlave``'s received ones
    (``m_out_<j>``), both on the BFMs' shared 1-based cycle count.  No trace, no VCD parse -- which
    is what makes a calibration run cost the simulation and nothing more."""
    vec = Path(vec) if vec is not None else vectors_dir(root)
    beats = {}
    for j in range(R):
        for port, name in ((f"s_in_{j}", f"s_in_{j}_acc"), (f"m_out_{j}", f"m_out_{j}")):
            beats[port] = np.fromfile(vec / name / "cycles.bin", dtype="<u8").astype(int).tolist()
    return beats


def frame_times(beats: dict[str, list[int]], length: int, n_frames: int) -> list[dict]:
    """Per frame, in clock edges from frame 0's first input beat: ``in`` (first input beat),
    ``last_in`` (last input beat: the frame is in the block) and ``done`` (last output beat).

    An edge-to-edge difference is a duration: ``done - last_in`` is the processing span the pysim
    measures from the end of its read to the end of its write, and ``done`` of frame 0 counts the
    last beat's own cycle so it equals that frame's residence."""
    pl = length // R
    t0 = min(beats[f"s_in_{j}"][0] for j in range(R))
    rows = []
    for k in range(n_frames):
        rows.append({
            "in": min(beats[f"s_in_{j}"][pl * k] for j in range(R)) - t0,
            "last_in": max(beats[f"s_in_{j}"][pl * k + pl - 1] for j in range(R)) - t0 + 1,
            "done": max(beats[f"m_out_{j}"][pl * k + pl - 1] for j in range(R)) - t0 + 1,
        })
    return rows


def sweep_gaps(length: int, n_frames: int, seed: int = 0) -> list[int]:
    """Gaps for the phase sweep: long enough that every frame finds the block idle (checked by
    :func:`measure`), plus a seeded
    pseudo-random extra spread over ``4 L/R`` cycles -- more than the commutator's period (measured
    at ``2.5 L/R``), so the arrivals land on every phase of it, whatever the period turns out to be."""
    pl = length // R
    rng = np.random.default_rng(seed)
    # 16 L/R: per-frame cost rises with L (7.5 L/R at 256, 10 at 1024) and 10 L/R let two frames at
    # L=4096 queue -- which `measure` refuses rather than counting them as isolated.
    return [int(16 * pl + 64 + rng.integers(0, 4 * pl)) for _ in range(n_frames - 1)]


def run_scenario(root: Path, length: int, n_frames: int, gaps=(),
                 config: dict | None = None, trace: bool = False) -> list[dict]:
    """One XSI run of *n_frames* (bits checked), reduced to :func:`frame_times`.

    Untraced by default: the BFMs timestamp every port beat themselves (:func:`capture_beats`).
    ``trace=True`` reads the same beats off a waveform instead (:func:`port_beats`) -- the
    cross-check, and the route when something inside the block needs looking at."""
    cfg = dict(config or {})
    sysm = VitisFftSystem(length=length, n_frames=n_frames, burst_gaps=list(gaps), config=cfg)
    run_xsi(root, sysm, trace=trace)
    check_bits(root, length, n_frames, "back to back" if not gaps else "phase sweep", cfg)
    if trace:
        from waveflow.build.system_top import SystemTopSpec

        spec = SystemTopSpec.from_system(sysm, top=SYSTEM_TOP)
        work = Path(root).resolve() / SYSTEM_DIR / SYSTEM_TOP
        beats = port_beats(work / f"{SYSTEM_TOP}_trace.vcd", spec.scope_of(spec.kernels[0]))
    else:
        beats = capture_beats(root)
    return frame_times(beats, length, n_frames)


def measure(root: Path, length: int, n_b2b: int = 6, n_sweep: int = 48, seed: int = 0,
            config: dict | None = None) -> dict:
    """The two calibration scenarios at *length*, built under *root*:

    * ``b2b`` -- *n_b2b* frames back to back: the frame interval;
    * ``sweep`` -- *n_sweep* isolated frames on :func:`sweep_gaps`: the processing span over arrival
      phases.  Every gap must isolate its frame (it arrives after the previous one left)."""
    ensure_built(root, length, config)
    b2b = run_scenario(root, length, n_b2b, config=config)
    gaps = sweep_gaps(length, n_sweep, seed)
    sweep = run_scenario(root, length, n_sweep, gaps, config=config)
    overlapped = [k for k in range(1, n_sweep) if sweep[k]["in"] < sweep[k - 1]["done"]]
    if overlapped:
        raise RuntimeError(f"L={length}: sweep frames {overlapped} arrived before the previous one "
                           f"left -- the gaps are too short to isolate them")
    return {"length": length, "b2b": b2b, "sweep": sweep, "gaps": gaps, "seed": seed}


def record_resources(root: Path, length: int, platform_dir: Path, *, synth_seconds: float = 0.0,
                     part: str = RFSOC4X2_PART, period_ns: float = RFSOC4X2_PERIOD_NS,
                     config: dict | None = None):
    """File this build's csynth resource figures onto the platform's module store.

    The synthesis is the expensive part and has already happened; reading its report is free, so it
    is filed on every build rather than only when someone remembers -- the same rule as
    :class:`~waveflow.build.resource_steps.InspectSynthStep`.  Records are keyed by the module's
    elaborated identity (class + ``HwParam`` values: ``L`` and the word/twiddle widths), so each
    configuration is its own entry: a lookup, exact where synthesized.  Returns the attribution.
    """
    from waveflow.calib.module_key import walk_modules
    from waveflow.calib.record_store import ModuleStore
    from waveflow.calib.resource_model import InterfaceResourceModel
    from waveflow.calib.synth_report import report_from_solution, store_report

    root = Path(root).resolve()
    dut = make_tb(length, untimed=True, **(config or {})).dut
    sol = root / f"{TOP}_proj" / "solution1"
    report = report_from_solution(dut, sol, top_name=TOP)
    mods = list(walk_modules(dut))
    identities = {i.key: i for _, _, i in mods}
    top_ident = next((i for _p, c, i in mods if c is dut), None)
    store_report(report, ModuleStore(platform_dir), identities, source="hls_estimate",
                 part=part, period_ns=float(period_ns), tool="vitis_hls",
                 cost_seconds=float(synth_seconds), top_identity=top_ident,
                 boundary=InterfaceResourceModel().get_params(dut))
    return report
