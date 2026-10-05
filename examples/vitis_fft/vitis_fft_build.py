"""vitis_fft_build.py — generate, synthesize, and measure ``VitisFft`` at RTL.

    python -m examples.vitis_fft.vitis_fft_build             # generate, then csynth
    python -m examples.vitis_fft.vitis_fft_build --no-synth  # generate only (Python, seconds)
    python -m examples.vitis_fft.vitis_fft_build --measure   # calibrate: RTL timing per L -> JSON

Nothing here is C++ someone maintains.  The task body is the framework's
``waveflow/build/vitis_fft_task.h``, copied into ``include/`` by ``VitisL1Step``; the vendor FFT stays
in the Vitis install and is reached by an include path; the ``ap_ctrl_none`` top comes from
``composite_top_spec`` on the module itself; the XSI harness comes from ``tb_top_spec`` on the
testbench graph in ``vitis_fft.py``.

The target is the RFSoC 4x2 (``RFSOC4X2_PART`` at ``RFSOC4X2_PERIOD_NS``), the board the RF examples
build for: cycle counts depend on the part and the clock, so they are measured where they will be used.

``include/``, ``gen/`` (with the ``.tcl``), ``xsi/`` and ``work/`` are build output, untracked: delete
them and the build writes them back (``plans/source_layout.md``).  This example has no ``src/``,
because it has no hand-written C++ of its own.  ``measured/`` is tracked: it is what ``--measure``
found, and reproducing it needs Vitis and Vivado.

**Why measuring takes two scenarios.**  The vendor core is frame-at-a-time, and its input transposer
runs a commutator on a free-running internal cycle.  A frame that arrives out of step with that cycle
waits up to one period for it, so an isolated frame's latency depends on its arrival *phase* -- which
an LT model cannot know.  So the measurement separates what is phase-independent from what is not:

* **back to back**, the core never idles, frames stay in step, and the interval is exact: ``II``;
* **a phase sweep**: isolated frames with seeded pseudo-random gaps, spread over more than one
  period, so the arrivals sample every phase.  Their latency gives a mean, which the pysim uses, and
  a min/max, which is the model's stated error.
"""
from __future__ import annotations

import argparse
import datetime
import json
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
from waveflow.build.trace_steps import AddVcdTopStep, rtl_staleness, xsi_runner_cmd
from waveflow.build.vitis_l1_step import VitisL1Step, vitis_fft_include_dir
from waveflow.simulation.simulation import Simulation
from waveflow.utils.burst_io import read_burst_bundle
from waveflow.vitis_l1.hw import VitisFft

from examples.vitis_fft.vitis_fft import (
    MEASURED,
    R,
    VitisFftTB,
    frames_from_lanes,
    golden,
    write_scenario,
)

HERE = Path(__file__).resolve().parent
TOP = "vitis_fft"
XSI_DIR = "xsi"
TB = f"{TOP}_bfm_tb"
#: Where ``measure`` builds each length.  Inside the example and short on purpose: csynth fails
#: SILENTLY (no error, "Synthesis failed") when the project path is long enough to hit the Windows
#: path limit -- measured from a deep temp directory.
WORK_DIR = "work"
#: The lengths ``measure`` calibrates by default.
MEASURE_LENGTHS = (16, 64, 256)


def make_tb(length: int | None = None, n_frames: int | None = None, burst_gaps=(),
            untimed: bool = False) -> VitisFftTB:
    """The graph both the DUT top and the XSI harness are derived from."""
    kw = {}
    if length is not None:
        kw["length"] = length
    if n_frames is not None:
        kw["n_frames"] = n_frames
    tb = VitisFftTB(name="tb", sim=Simulation(), burst_gaps=list(burst_gaps), untimed=untimed, **kw)
    # Run past the last frame: every gap, plus a generous per-frame allowance (the measured interval
    # is 7.5 L/R at L >= 64) -- per FRAME, not per gap, or a back-to-back run is cut short.
    tb.n_cycles = int(sum(burst_gaps) + (tb.n_frames + 2) * (12 * tb.length // R + 64) + 500)
    return tb


def generate(root: Path = HERE, length: int | None = None) -> str:
    """Everything before csynth: headers, the top and its ``.tcl``, the XSI workspace and the
    default scenario.  Python only, no toolchain beyond locating the vendor headers.

    The XSI gate calls this before asking whether the RTL is stale, so a pulled change to the body,
    a generator or the framework shows up as a changed source (``trace_steps.rtl_staleness``).
    """
    dag = BuildDag()
    dag.add(SourceStep(artifact="vitis_fft_source", path=HERE / "vitis_fft.py"))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))       # the generated top includes memmgr.hpp
    dag.add(VitisL1Step(output_dir=INCLUDE_DIR))      # the body, copied verbatim
    dag.add(XsiHarnessStep(output_dir=XSI_DIR))       # framework BFMs + loader + run scripts
    dag.add(AddVcdTopStep(name="vcd_top", comp_class=VitisFft,
                          source_artifact="vitis_fft_source", output_dir=XSI_DIR))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")

    tb = make_tb(length, untimed=True)
    dut = tb.dut
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
                   part=RFSOC4X2_PART, period_ns=RFSOC4X2_PERIOD_NS), encoding="utf-8")
    xsi = root / XSI_DIR
    xsi.mkdir(parents=True, exist_ok=True)
    (xsi / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    write_tb(root, make_tb(length))
    return spec.top_name


def write_tb(root: Path, tb: VitisFftTB) -> None:
    """The XSI harness + main for *tb*, and its scenario bundles."""
    xsi = root / XSI_DIR
    spec = tb_top_spec(tb)
    (xsi / f"{TOP}_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    (xsi / f"{TB}.cpp").write_text(render_tb_main(spec, int(tb.n_cycles)), encoding="utf-8")
    write_scenario(xsi, tb.n_frames, tb.length)


def synth(root: Path = HERE) -> str:
    """csynth the top, stamp the sources it was built from, and list its RTL for ``xvlog``."""
    from waveflow.build.rtl_digest import write_stamp
    from waveflow.toolchain.toolchain import run_vitis_hls

    r = run_vitis_hls(tcl_path(root, TOP), work_dir=root)
    out = (r.stdout or "") + (r.stderr or "")
    if "WAVEFLOW_CSYNTH_OK" not in out:
        raise RuntimeError(f"csynth of {TOP} failed:\n{out[-6000:]}")
    write_stamp(root, TOP)
    (root / XSI_DIR / f"rtl_{TOP}.f").write_text(render_rtl_f(TOP, root), encoding="utf-8")
    return out


def run_xsi(root: Path = HERE, trace: bool = False) -> str:
    """Run the XSI testbench in ``<root>/xsi``; clears the previous capture first, so a stale one
    cannot pass for this run's."""
    import subprocess

    xsi = root / XSI_DIR
    for j in range(R):
        d = xsi / "vectors" / f"m_out_{j}"
        if d.exists():
            for f in d.iterdir():
                f.unlink()
    p = subprocess.run(xsi_runner_cmd(TOP, TB, trace=trace), cwd=xsi, capture_output=True,
                       text=True, timeout=3600)
    if p.returncode != 0 or "XSI_EXITCODE=0" not in p.stdout:
        raise RuntimeError(f"XSI run failed\n{p.stdout[-3000:]}\n{p.stderr[-2000:]}")
    return p.stdout


def captured_frames(root: Path, length: int):
    """What the RTL emitted, from the sinks' capture bundles, as ``[(re, im), ...]``."""
    from waveflow.simulation.simulation import Simulation as _Sim
    out_w = int(VitisFft(name="w", sim=_Sim(), L=length).out_fmt.W)
    lanes = [np.concatenate(read_burst_bundle(root / XSI_DIR / "vectors" / f"m_out_{j}"))
             for j in range(R)]
    return frames_from_lanes(lanes, out_w, length)


def port_beats(vcd_path: Path) -> dict[str, list[int]]:
    """Clock-edge index of every accepted beat (TVALID && TREADY) on each AXIS port, read with the
    framework's VCD parser -- which samples just before each edge, as the flops do."""
    from vcdvcd import VCDVCD

    from waveflow.utils.vcd import VcdParser

    parser = VcdParser(VCDVCD(str(vcd_path)))
    clk = parser.add_clock_signal("ap_clk")
    beats = {}
    for port in [f"s_in_{j}" for j in range(R)] + [f"m_out_{j}" for j in range(R)]:
        # Anchored at the top scope: the task inside has nets of the same suffix
        # (`vitis_fft.vitis_fft_task_..._U0_s_in_0_TREADY`), and the port is the one measured.
        sigs = parser.add_axiss_signals(name=f"{TOP}.{port}_T", short_name_prefix=port)
        sigs = sigs[0] if isinstance(sigs, tuple) else sigs
        bursts, _ = parser.extract_axis_bursts(clk, sigs)
        beats[port] = [int(b["start_idx"]) + i for b in bursts
                       for i, t in enumerate(b["beat_type"]) if t == 0]
    return beats


def frame_times(beats: dict[str, list[int]], length: int, n_frames: int) -> list[dict]:
    """Per frame: its first input beat and its last output beat, in cycles from frame 0's first
    input beat, which is the pysim's ``t = 0``.  ``done`` counts the last beat's own cycle, so frame
    0's ``done`` is its residence."""
    pl = length // R
    t0 = min(beats[f"s_in_{j}"][0] for j in range(R))
    rows = []
    for k in range(n_frames):
        rows.append({
            "in": min(beats[f"s_in_{j}"][pl * k] for j in range(R)) - t0,
            "done": max(beats[f"m_out_{j}"][pl * k + pl - 1] for j in range(R)) - t0 + 1,
        })
    return rows


def _check_bits(root: Path, length: int, n_frames: int, what: str) -> None:
    got, want = captured_frames(root, length), golden(n_frames, length)
    if len(got) != n_frames:
        raise RuntimeError(f"L={length} {what}: captured {len(got)} frames, expected {n_frames}")
    for k, ((g_re, g_im), (w_re, w_im)) in enumerate(zip(got, want)):
        out_w = int(VitisFft(name="w", sim=Simulation(), L=length).out_fmt.W)
        mask = (1 << out_w) - 1
        if not (np.array_equal(g_re & mask, w_re & mask) and np.array_equal(g_im & mask, w_im & mask)):
            raise RuntimeError(f"L={length} {what}: frame {k} is not bit-exact")


def sweep_gaps(length: int, n_frames: int, seed: int = 0) -> list[int]:
    """Gaps for the phase sweep: long enough that every frame finds the block idle, plus a seeded
    pseudo-random extra spread over ``4 L/R`` cycles -- more than the commutator's period (measured
    at ``2.5 L/R``), so the arrivals land on every phase of it, whatever the period turns out to be."""
    pl = length // R
    rng = np.random.default_rng(seed)
    return [int(10 * pl + 64 + rng.integers(0, 4 * pl)) for _ in range(n_frames - 1)]


def measure_length(length: int, work: Path, n_b2b: int = 6, n_sweep: int = 48,
                   seed: int = 0) -> dict:
    """Build *length* under ``work/L<length>`` (csynth only if the RTL is missing or stale), run both
    scenarios traced, check the bits, and reduce the port timing to the model's numbers."""
    root = work / f"L{length}"
    root.mkdir(parents=True, exist_ok=True)
    generate(root, length)
    if not (root / f"{TOP}_proj").is_dir() or rtl_staleness(root, TOP) is not None:
        synth(root)
    vcd = root / XSI_DIR / f"{TOP}_trace.vcd"

    write_tb(root, make_tb(length, n_b2b, untimed=True))
    run_xsi(root, trace=True)
    _check_bits(root, length, n_b2b, "back to back")
    b2b = frame_times(port_beats(vcd), length, n_b2b)

    gaps = sweep_gaps(length, n_sweep, seed)
    write_tb(root, make_tb(length, n_sweep, gaps, untimed=True))
    run_xsi(root, trace=True)
    _check_bits(root, length, n_sweep, "phase sweep")
    sweep = frame_times(port_beats(vcd), length, n_sweep)

    # Frame 0 arrives straight out of reset, in step with the commutator by construction -- a case
    # a running system never sees, so it is reported but kept out of the statistics.
    lat = np.array([f["done"] - f["in"] for f in sweep[1:]])
    overlapped = [k for k in range(1, n_sweep) if sweep[k]["in"] < sweep[k - 1]["done"]]
    if overlapped:
        raise RuntimeError(f"L={length}: sweep frames {overlapped} arrived before the previous one "
                           f"left -- the gaps are too short to isolate them")
    done = [f["done"] for f in b2b]
    intervals = np.diff(done[1:])            # from frame 1 on: past the start-up transient
    return {
        "ii_cycles": int(np.median(intervals)),
        "ii_spread": [int(intervals.min()), int(intervals.max())],
        "latency_mean": round(float(lat.mean()), 1),
        "latency_min": int(lat.min()),
        "latency_max": int(lat.max()),
        "frame0_latency": int(b2b[0]["done"]),
        "n_samples": int(lat.size),
        "b2b_done": done,
        "sweep_seed": seed,
    }


def measure(lengths=MEASURE_LENGTHS, work: Path = HERE / WORK_DIR, out: Path = MEASURED) -> dict:
    """Calibrate every length and write the timing table the pysim reads."""
    table = {"part": RFSOC4X2_PART, "period_ns": RFSOC4X2_PERIOD_NS,
             "measured": datetime.date.today().isoformat(),
             "how": "python -m examples.vitis_fft.vitis_fft_build --measure",
             "sizes": {}}
    if out.exists():
        table["sizes"] = json.loads(out.read_text(encoding="utf-8")).get("sizes", {})
    for length in lengths:
        table["sizes"][str(length)] = measure_length(length, work)
        print(f"L={length}: {table['sizes'][str(length)]}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(table, indent=1) + "\n", encoding="utf-8")
    return table


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-synth", action="store_true")
    ap.add_argument("--measure", action="store_true",
                    help="calibrate the RTL timing per L (needs Vitis + Vivado; minutes per L)")
    ap.add_argument("--lengths", type=int, nargs="*", default=list(MEASURE_LENGTHS))
    a = ap.parse_args()
    if a.measure:
        measure(a.lengths)
        print(f"wrote {MEASURED}")
        return
    top = generate()
    print(f"generated {GEN_DIR}/{top}.cpp, {GEN_DIR}/{top}.tcl and {XSI_DIR}/")
    if a.no_synth:
        return
    synth()
    print(f"csynth {top} OK")


if __name__ == "__main__":
    main()
