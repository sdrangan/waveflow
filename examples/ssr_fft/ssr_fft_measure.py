"""ssr_fft_measure.py -- measure ``SsrFft`` at RTL and record it in ``measured.json``.

    python -m examples.ssr_fft.ssr_fft_measure                      # every length, ping-pong
    python -m examples.ssr_fft.ssr_fft_measure --lengths 64 --reorder sob

For each length: build in ``work/ssr_fft/L<L>_<reorder>`` (csynth only if the RTL is missing or
stale), then two XSI runs -- frames **back to back** (the interval, and the first frame's latency)
and **isolated** frames with random idle gaps between them (whether latency depends on arrival) --
checking every frame's bits in both, and read the resources from csynth's own XML.  The figures and
the docs tables are drawn from the file this writes; nothing in them is typed by hand.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date
from pathlib import Path

import numpy as np

from waveflow.build.composite_gen import RFSOC4X2_PART, RFSOC4X2_PERIOD_NS
from waveflow.build.trace_steps import rtl_staleness
from waveflow.dsp.ssr_fft import rtl

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MEASURED = HERE / "measured.json"
LENGTHS = (16, 64, 256, 1024, 4096)
N_B2B, N_ISO = 8, 12


def _xml_resources(root: Path) -> dict:
    xml = (root / f"{rtl.TOP}_proj" / "solution1" / "syn" / "report" / "csynth.xml").read_text(
        encoding="utf-8")
    block = xml[xml.index("<AreaEstimates>"):xml.index("</Resources>")]
    get = {k: int(re.search(rf"<{k}>(\d+)</{k}>", block).group(1))
           for k in ("BRAM_18K", "DSP", "FF", "LUT")}
    period = float(re.search(r"<EstimatedClockPeriod>([\d.]+)<", xml).group(1))
    return {"bram": get["BRAM_18K"], "dsp": get["DSP"], "ff": get["FF"], "lut": get["LUT"],
            "est_clock_ns": period}


def measure(length: int, reorder: str, lanes: bool = False) -> dict:
    root = REPO / "work" / "ssr_fft" / f"L{length}_{reorder}{'_lanes' if lanes else ''}"
    root.mkdir(parents=True, exist_ok=True)
    rtl.generate(root, length, n_frames=N_B2B, reorder=reorder, lanes=lanes)
    synth_s = None
    if not (root / f"{rtl.TOP}_proj").is_dir() or rtl_staleness(root, rtl.TOP) is not None:
        t = time.time()
        rtl.synth(root)
        synth_s = round(time.time() - t)

    t = time.time()
    rtl.run_xsi(root)
    xsi_s = round(time.time() - t)
    b2b_bits = rtl.check_bits(root, length, N_B2B, lanes=lanes)
    b2b = rtl.frame_times(root, length, lanes=lanes)

    rng = np.random.default_rng(length)
    gaps = [int(g) for g in rng.integers(2 * length // 4 + 64, 2 * length + 400, N_ISO - 1)]
    rtl.generate(root, length, n_frames=N_ISO, burst_gaps=gaps, reorder=reorder, lanes=lanes)
    rtl.run_xsi(root)
    iso_bits = rtl.check_bits(root, length, N_ISO, lanes=lanes)
    iso = rtl.frame_times(root, length, lanes=lanes)

    return {
        "L": length, "reorder": reorder, "boundary": "lanes" if lanes else "radixword",
        "bits_exact": bool(all(b2b_bits) and len(b2b_bits) == N_B2B
                           and all(iso_bits) and len(iso_bits) == N_ISO),
        "interval": sorted({int(x) for x in np.diff([f["done"] for f in b2b])}),
        "first_frame": int(b2b[0]["done"] - b2b[0]["in"]),
        "isolated_latency": sorted({int(f["done"] - f["in"]) for f in iso}),
        "isolated_span": sorted({int(f["done"] - f["last_in"]) for f in iso}),
        "resources": _xml_resources(root),
        "csynth_s": synth_s, "xsi_s": xsi_s,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lengths", type=int, nargs="+", default=list(LENGTHS))
    ap.add_argument("--reorder", default="pingpong", choices=("pingpong", "sob"))
    ap.add_argument("--lanes", action="store_true", help="VitisFft's four-lane port group")
    a = ap.parse_args()
    data = json.loads(MEASURED.read_text(encoding="utf-8")) if MEASURED.exists() else {
        "target": {"part": RFSOC4X2_PART, "period_ns": RFSOC4X2_PERIOD_NS, "board": "RFSoC 4x2"},
        "runs": {}}
    for n in a.lengths:
        r = measure(n, a.reorder, a.lanes)
        r["date"] = date.today().isoformat()
        data["runs"][f"L{n}_{a.reorder}{'_lanes' if a.lanes else ''}"] = r
        print(json.dumps(r), flush=True)
        data["runs"] = dict(sorted(data["runs"].items(),
                                   key=lambda kv: (kv[1]["reorder"], kv[1]["L"])))
        MEASURED.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
