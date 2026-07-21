"""calibrate_platform.py — populate a calibration platform's work dir from the mem_copy measurements.

This is the *reproducible* builder behind the tracked ``zynq7020_bfm_100mhz`` platform: it writes the
platform identity, the bus-transfer law, and the writer component's control residual into a **work**
directory (untracked ``calib/work/<name>/``).  Promote it to the tracked library with::

    python -m examples.mem_copy.calibrate_platform          # -> calib/work/zynq7020_bfm_100mhz
    publish_calib calib/work/zynq7020_bfm_100mhz calib/platforms/zynq7020_bfm_100mhz --apply

Provenance of the numbers (all from the closed mem_copy calibration loop, gated for real by ``-m xsi``
— see ``plans/memcpy_timing_calibration.md`` and ``tests/examples/test_mem_copy_calibration.py``):

* **Bus law** — the m_axi per-transfer span measured off the ports (``measure_bus_span`` reproduces it
  from the committed XSI trace at 512 words): write ``nwords + 2·(num_trans-1)``, read
  ``nwords + (num_trans-1)`` cycles.  The idealised XSI BFM memory, hence the ``bfm`` in the name — a
  real DDR controller would widen it.
* **Writer residual** — the writer's ap_done-anchored RTL firing span is 183 cycles at n=128, 615 at
  n=512 (measured).  Fit against a pysim run that already charges the bus law, so the residual is the
  writer's own *control* cost (~22 cycles), not the bus term.

The pysim side is run live here (no toolchain); only the RTL spans are measured constants.
"""
from __future__ import annotations

import math
from pathlib import Path

from examples.mem_copy.mem_copy import xsi_jobs
from examples.mem_copy.mem_copy_sim import MemCopySim
from waveflow.calib.bus_model import BusCalib
from waveflow.calib.platform import Platform
from waveflow.calib.timing_model import StreamTimingModel
from waveflow.hw.clock import Clock

#: The writer task body id — the component key its firings carry and the shared-library subdir.
COMPONENT = "mem_w_stream_framed_done_task"
#: Measured writer RTL firing span (cycles), ap_done-anchored, per n_words.
RTL_SPAN = {128: 183.0, 512: 615.0}
#: Sweep sizes and the job count each was measured with (more small jobs to exercise steady state).
SWEEP = {128: 16, 512: 4}

PART = "xc7z020clg484-1"
CLK_FREQ = 100e6


def _burst(nw: int) -> int:
    return math.ceil(nw / 16)


def _bus_points(law) -> list[dict]:
    return [{"num_trans": _burst(nw), "nwords": nw, "span": law(nw, _burst(nw))} for nw in SWEEP]


def calibrate(work_root: str | Path = "calib/work", name: str = "zynq7020_bfm_100mhz") -> Path:
    """Build the platform's work dir (identity + bus law + writer residual) and return its path."""
    clk = Clock(freq=CLK_FREQ)
    plat = Platform.resolve(work_root, name, part=PART, clk_freq=CLK_FREQ)  # seeds platform.json
    work = plat.dir

    # 1) Bus law (must exist before the component runs, so pysim charges it).
    bus = BusCalib(platform_dir=work, clk_freq=CLK_FREQ)
    for nw in SWEEP:
        bus.add_run(f"n{nw}",
                    read={"num_trans": _burst(nw), "nwords": nw, "span": nw + (_burst(nw) - 1)},
                    write={"num_trans": _burst(nw), "nwords": nw, "span": nw + 2 * (_burst(nw) - 1)})
    bus.fit()

    # 2) Writer control residual — RTL span vs a pysim run that already charges the bus law.
    comp_dir = plat.component_dir(COMPONENT)
    tm = StreamTimingModel(component=COMPONENT, calib_dir=comp_dir, clk=clk)
    tm.reset(corpus=True, params=True)
    for nw, k in SWEEP.items():
        tm.collect_rtl(
            {"top": "mem_copy", "max_burst_len": 16,
             "firings": [{"component": COMPONENT, "index": 0, "nwords": nw,
                          "num_trans": _burst(nw), "span": RTL_SPAN[nw], "blocked": 0}]},
            run_id=f"n{nw}")
        dut = MemCopySim(jobs=xsi_jobs(nw, k), calib_dir=str(comp_dir),
                         platform_dir=str(work)).run()          # bus active -> residual is control only
        tm.collect_pysim(dut.wstream.firing_records, run_id=f"n{nw}")
    tm.fit()
    return work


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work-root", default="calib/work", help="untracked work root (default calib/work)")
    ap.add_argument("--name", default="zynq7020_bfm_100mhz", help="platform name")
    args = ap.parse_args(argv)
    work = calibrate(args.work_root, args.name)
    print(f"calibrated platform work dir: {work}")
    print(f"  bus law     -> {work / 'mm_bus.json'}")
    print(f"  writer resid-> {work / 'components' / COMPONENT / 'params.json'}")
    print(f"publish it:  publish_calib {work} calib/platforms/{args.name} --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
