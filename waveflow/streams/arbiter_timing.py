"""arbiter_timing.py -- ``StreamArbiter``'s cost per frame: the idle cycles between consecutive frames.

    python -m waveflow.streams.arbiter_timing [--n 2 4 8 16] [--policy priority round_robin]

``plans/shared_kernel_rfsoc_timing.md``.  With every input backlogged, ``m_out`` carries frame after frame;
the cycles between one frame's last word and the next one's first are what a firing's decision costs (the
``empty()`` tests, the search, the ``m_sel`` word, entering the copy loop).  Measured two ways, on the same
scenario (each input ``frames_of(k, 1, "long")``: 12 frames of 8 words):

* **RTL**: the arbiter alone as its ``ap_ctrl_none`` top (``rtl.make("arbiter", ...)``), the default part
  (``DEFAULT_PART`` at 100 MHz, as its gates), run under XSI through ``system_xsi``; ``m_out``'s trace
  holds the cycle each word left.  No ``compare``: the interleave is timing's, not pysim's.
* **pysim**: the model on its own, every input pre-loaded into a deep link and ``m_out`` read as fast as
  it comes, timed at each burst it delivers.

Gaps are read from the ``n_in``-th frame on, when every input holds frames; a priority run takes
input 0's frames back to back, so it is backlogged only if that input's driver keeps up -- ``m_sel``
says whether it did.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from waveflow.streams.calib import WORK

#: The scenario's frames per input and words per frame (``frames_of(k, 1, "long")``).
FRAMES, WORDS = 12, 8


def _gaps(starts, ends, skip: int) -> list[int]:
    """Idle cycles between consecutive frames, from frame *skip* on: next start - this end - 1."""
    return [int(starts[i + 1] - ends[i] - 1) for i in range(skip, len(starts) - 1)]


def rtl_gaps(n: int, policy: str, root: Path | None = None) -> dict:
    """The arbiter alone at RTL (default part, XSI): ``m_out``'s inter-frame gaps and ``m_sel``'s order."""
    from waveflow.build.build import BuildConfig
    from waveflow.streams import rtl

    root = Path(root or WORK / "timing" / f"{policy[:2]}{n}").resolve()
    root.mkdir(parents=True, exist_ok=True)
    dag, top = rtl.build_block_dag(root / "xw", "arbiter", n, 32, 1, policy, workload="long")
    res = dag.run(BuildConfig(root_dir=root, params={"synth": "build"}), through="system_xsi")
    bad = {k: r.message for k, r in res.items() if not r.success}
    if bad:
        raise RuntimeError(f"arbiter n={n} {policy}: {bad}")
    tr = root / "xw" / top / "traces"
    cyc = np.fromfile(tr / "m_out" / "cycles.bin", dtype="<u8")
    bounds = np.fromfile(tr / "m_out" / "bounds.bin", dtype="<u8")      # each burst's end, in words
    sel = np.fromfile(tr / "m_sel" / "words.bin", dtype="<u8")
    ends = [int(cyc[b - 1]) for b in bounds]
    starts = [int(cyc[0])] + [int(cyc[b]) for b in bounds[:-1]]
    intra = [int(d) - 1 for s, e in zip([0, *bounds[:-1]], bounds) for d in np.diff(cyc[s:e])]
    return {"frames": len(bounds), "gaps": _gaps(starts, ends, n), "intra_frame_idle": sorted(set(intra)),
            "sel": [int(x) for x in sel], "first_word": int(cyc[0]), "last_word": int(cyc[-1])}


def pysim_gaps(n: int, policy: str) -> dict:
    """The pysim model alone, every input pre-loaded: ``m_out``'s inter-frame gaps, in cycles."""
    from waveflow.hw.clock import Clock
    from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
    from waveflow.simulation.simulation import Simulation
    from waveflow.streams.arbiter import StreamArbiter, frames_of

    sim = Simulation()
    clk = Clock(freq=100e6)
    per = 1 / clk.freq
    arb = StreamArbiter(name="arb", sim=sim, n_in=n, bitwidth=32, frame_bursts=1, policy=policy, clk=clk)
    drivers = []
    for k in range(n):
        tx = StreamIFMaster(name=f"d{k}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"in{k}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=4096)
        link.bind("master", tx)
        link.bind("slave", arb.s_in[k])
        drivers.append(tx)
    rx = {}
    for port in ("m_out", "m_sel"):
        r = StreamIFSlave(name=f"r_{port}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"o_{port}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=4096)
        link.bind("master", getattr(arb, port))
        link.bind("slave", r)
        rx[port] = r
    got: dict = {"m_out": [], "m_sel": []}

    def drive(k):
        for b in frames_of(k, 1, "long"):
            yield from drivers[k].write(np.asarray(b.words, dtype=np.uint32))

    def read(port):
        while True:
            words = yield from rx[port].get()
            got[port].append((sim.env.now / per, len(words), int(words[0])))

    for k in range(n):
        sim.env.process(drive(k))
    for port in rx:
        sim.env.process(read(port))
    sim.run_sim()
    out = got["m_out"]
    # A burst is timed when the reader has it: its last word's cycle.  Its first word's is that less
    # its length less one, at one word a cycle.
    ends = [round(t) for t, _, _ in out]
    starts = [round(t) - nw + 1 for t, nw, _ in out]
    return {"frames": len(out), "gaps": _gaps(starts, ends, n), "sel": [w for _, _, w in got["m_sel"]]}


def summarize(g: dict) -> dict:
    """The gaps' range and mean -- or ``None`` for each when the run gave too few frames to measure
    (a run that stopped early: the frame count says so)."""
    gaps = g["gaps"]
    if not gaps:
        return {"frames": g["frames"], "gap_min": None, "gap_max": None, "gap_mean": None, "gaps_seen": []}
    return {"frames": g["frames"], "gap_min": min(gaps), "gap_max": max(gaps),
            "gap_mean": round(float(np.mean(gaps)), 3), "gaps_seen": sorted(set(gaps))}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, nargs="+", default=[2, 4, 8, 16])
    ap.add_argument("--policy", nargs="+", default=["priority", "round_robin"])
    ap.add_argument("--no-rtl", action="store_true", help="pysim only")
    a = ap.parse_args(argv)
    rows = []
    for policy in a.policy:
        for n in a.n:
            row = {"n_in": n, "policy": policy, "pysim": summarize(pysim_gaps(n, policy))}
            if not a.no_rtl:
                r = rtl_gaps(n, policy)
                row["rtl"] = {**summarize(r), "intra_frame_idle": r["intra_frame_idle"],
                              "sel_head": r["sel"][: 2 * n], "first_word": r["first_word"],
                              "last_word": r["last_word"]}
            rows.append(row)
            print(json.dumps(row))
    out = WORK / "timing" / "arbiter_gaps.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
