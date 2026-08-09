"""measure_compute_spans.py — measure ``il_compute``'s REAL per-firing gather cost from RTL.

:mod:`calibrate_compute` fits the direct loop law ``cycles = latency + ii·(n − 1)`` from an ``(n, cycles)``
sweep.  This module produces those points **measured**, not seeded: run the whole interleaver in XSI with a
VCD trace over a sweep of job sizes, then read ``il_compute``'s per-firing gather span straight off the
waveform.

The span signal is the gather's OUTPUT write-enable, ``il_compute…_y_blk_we0``.  The typed-SOB gather
``yb[i] = xb[pb[i]]`` is a pipelined element loop at II=1, so ``we0`` goes high for one contiguous window of
exactly ``n`` cycles per firing — the loop's own time.  The gate is that the window is **single and
contiguous**: a dip (``we0`` 1→0→1 inside one firing) means the gather stalled on output backpressure, so
that span is not the loop's own cost and is dropped.  This is the "full-pipeline fire-span, gated on
no-stall" method — no separate profiling fixture, the number comes from the real design running.

The VCD parser (:func:`measure_compute_spans`) is pure and toolchain-free (tested against a committed
trace); :func:`run_and_measure` drives the Vitis-HLS csynth + Vivado xsim and needs the toolchain.  Print
the resulting ``{n: cycles}`` and paste it into :data:`calibrate_compute.N_TO_CYCLES`.
"""
from __future__ import annotations

import re
from pathlib import Path

#: VCD time-units per clock cycle (100 MHz → 10 ns period, 2 edges of 5 units each in the emitted trace).
CLK_PERIOD = 20

#: The gather's per-element output write-enable; ``…_task_<MEM_DW>_<N>_U0_y_blk_we0``.  One high window per
#: firing at II=1.  ``ap_done`` segments firings so a stalled (multi-window) firing can be dropped.
_WE_RE = re.compile(r"il_compute_inband_task_\d+_\d+_U0_y_blk_we0$")
_DONE_RE = re.compile(r"il_compute_inband_task_\d+_\d+_U0_ap_done$")
_VAR_RE = re.compile(r"\$var\s+\w+\s+\d+\s+(\S+)\s+(.+?)\s+\$end")


def _rising(lines: "list[str]", sig_id: str) -> "list[int]":
    """Cycles at which ``sig_id`` rises 0→1 (VCD scalar), earliest first."""
    out: "list[int]" = []
    t = 0
    cur = "x"
    for L in lines:
        if L.startswith("#"):
            t = int(L[1:])
        elif len(L) >= 2 and L[0] in "01" and L[1:] == sig_id:
            if cur != "1" and L[0] == "1":
                out.append(t // CLK_PERIOD)
            cur = L[0]
    return out


def _windows(lines: "list[str]", sig_id: str) -> "list[tuple[int, int]]":
    """Contiguous ``(start, end)`` high windows of a scalar signal, in cycles (span = end − start)."""
    wins: "list[tuple[int, int]]" = []
    t = 0
    cur = "x"
    start: "int | None" = None
    for L in lines:
        if L.startswith("#"):
            t = int(L[1:])
        elif len(L) >= 2 and L[0] in "01" and L[1:] == sig_id:
            c = t // CLK_PERIOD
            if L[0] == "1" and cur != "1" and start is None:
                start = c
            elif L[0] == "0" and cur == "1" and start is not None:
                wins.append((start, c))
                start = None
            cur = L[0]
    return wins


def measure_compute_spans(vcd_path: "str | Path", sizes: "tuple[int, ...]") -> "dict[int, list[int]]":
    """Read ``il_compute``'s per-firing gather span from a full-pipeline trace, gated on no-stall.

    ``sizes`` is the job-size sequence in firing order (firing *k* processed ``sizes[k]`` elements). Returns
    ``{n: [span, …]}`` — the contiguous ``y_blk`` write-burst span of every **clean** firing at that size. A
    firing is clean iff its ``we0`` is a single contiguous window between consecutive ``ap_done`` marks (a
    split window == output backpressure → dropped, not recorded)."""
    lines = Path(vcd_path).read_text(errors="ignore").splitlines()
    id2name = {m.group(1): m.group(2).strip() for L in lines if (m := _VAR_RE.match(L))}
    we_id = next(i for i, n in id2name.items() if _WE_RE.search(n))
    done_id = next(i for i, n in id2name.items() if _DONE_RE.search(n))

    done = _rising(lines, done_id)
    wins = _windows(lines, we_id)
    if len(done) != len(sizes):
        raise ValueError(f"{len(done)} il_compute firings (ap_done) but {len(sizes)} job sizes given")

    spans: "dict[int, list[int]]" = {}
    prev = -1
    for k, end_c in enumerate(done):
        # windows attributed to firing k: their fall lands in (prev_done, this_done].
        firing_wins = [(s, e) for (s, e) in wins if prev < e <= end_c]
        n = sizes[k]
        if len(firing_wins) == 1:  # single contiguous burst -> no output stall -> clean
            s, e = firing_wins[0]
            spans.setdefault(n, []).append(e - s)
        prev = end_c
    return spans


def n_to_cycles(vcd_path: "str | Path", sizes: "tuple[int, ...]") -> "dict[int, float]":
    """Collapse :func:`measure_compute_spans` to one representative cycle count per size (the min clean
    span — the loop's own time with the least residual stall)."""
    spans = measure_compute_spans(vcd_path, sizes)
    return {n: float(min(vals)) for n, vals in sorted(spans.items()) if vals}


def build_rtl_trace(
    *,
    sizes: "tuple[int, ...]",
    n_max: int = 512,
    width: int = 64,
    n_cycles: int = 12000,
    work_root: "str | Path | None" = None,
) -> Path:
    """Build the interleaver RTL (template capacity ``n_max``), run the ``sizes`` sweep in XSI **with a
    trace**, and return the trace VCD path.  Needs Vitis HLS + Vivado xsim; ``sizes`` must all be ≤
    ``n_max``.  Shared by the compute-span measurement and the RTL timing step."""
    import shutil
    import subprocess
    import tempfile

    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.composite_gen import render_rtl_f
    from waveflow.build.streamutils import XsiHarnessStep
    from waveflow.build.trace_steps import _DUMPER_TEMPLATE, XSI_RUNNER, xsi_runner_cmd
    from waveflow.toolchain.toolchain import run_vitis_hls

    from examples.interleaver.interleaver_inband import (
        generate_inband,
        generate_tb,
        write_xsi_bundles,
    )

    if any(n > n_max for n in sizes):
        raise ValueError(f"every job size must be <= n_max={n_max}; got {sizes}")

    d = Path(tempfile.mkdtemp(prefix="il_trace_", dir=work_root))
    print(f"workdir: {d}", flush=True)
    generate_inband(out_dir=d, mem_dwidth=width, n=n_max)
    generate_tb(out_dir=d, width=width, sizes=sizes, n_cycles=n_cycles)
    (d / "xsi" / "vcd_dumper_interleaver_inband.v").write_text(
        _DUMPER_TEMPLATE.format(top="interleaver_inband"), encoding="utf-8"
    )
    cfg = BuildConfig(root_dir=d)
    dag = BuildDag()
    dag.add(XsiHarnessStep(output_dir="xsi"))
    dag.run(cfg, force=True)

    print("--- csynth ---", flush=True)
    res = run_vitis_hls(d / "interleaver_inband.tcl", work_dir=d)
    if "WAVEFLOW_CSYNTH_OK" not in ((res.stdout or "") + (res.stderr or "")):
        raise RuntimeError("csynth failed")
    (d / "xsi" / "rtl_interleaver_inband.f").write_text(
        render_rtl_f("interleaver_inband", d), encoding="utf-8"
    )
    write_xsi_bundles(d / "xsi", width=width, sizes=sizes)
    shutil.rmtree(d / "xsi" / "xsim.dir" / "interleaver_inband", ignore_errors=True)

    print(f"--- {XSI_RUNNER} trace ---", flush=True)
    r = subprocess.run(
        xsi_runner_cmd("interleaver_inband", "interleaver_inband_bfm_tb", trace=True),
        cwd=str(d / "xsi"), capture_output=True, text=True,
    )
    print(f"{XSI_RUNNER} returncode: {r.returncode}", flush=True)
    vcd = d / "xsi" / "interleaver_inband_trace.vcd"
    if not vcd.exists():
        print((r.stdout or "")[-3000:], flush=True)
        raise RuntimeError(f"no trace VCD at {vcd}")
    return vcd


def run_and_measure(
    sizes: "tuple[int, ...]" = (128, 128, 256, 256, 512, 512),
    n_max: int = 512,
    width: int = 64,
    n_cycles: int = 12000,
    work_root: "str | Path | None" = None,
) -> "dict[int, float]":
    """Build + trace the ``sizes`` sweep and return the measured ``{n: cycles}`` gather spans."""
    vcd = build_rtl_trace(sizes=sizes, n_max=n_max, width=width, n_cycles=n_cycles, work_root=work_root)
    spans = measure_compute_spans(vcd, sizes)
    print("\nclean gather spans per size (cycles):", flush=True)
    for n in sorted(spans):
        print(f"   n={n:4d}: {spans[n]}", flush=True)
    return n_to_cycles(vcd, sizes)


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sizes", default="128,128,256,256,512,512",
                    help="comma job-size sweep in firing order (all <= n-max)")
    ap.add_argument("--n-max", type=int, default=512, help="RTL block capacity to synthesize")
    ap.add_argument("--width", type=int, default=64, help="mem data width (bits)")
    args = ap.parse_args(argv)
    sizes = tuple(int(x) for x in args.sizes.split(","))
    measured = run_and_measure(sizes=sizes, n_max=args.n_max, width=args.width)
    print("\nN_TO_CYCLES = {", flush=True)
    for n, c in sorted(measured.items()):
        print(f"    {n}: {c},", flush=True)
    print("}  # measured il_compute gather spans (no-stall XSI fire-span)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
