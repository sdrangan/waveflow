"""Stage 1 baseline (plans/pipelined_ops.md): SimPy events per stream transfer and per message,
today, on poly and markov.  Counts env._eid (SimPy's per-scheduled-event counter) after a run, and
the stream transfers by wrapping the write/read primitives in this process only."""
import time
from pathlib import Path

import waveflow.hw.interface as itf

counts = {"push": 0, "take": 0}
_push, _take = itf.QueuedTransferIF._push_to_endpoint, itf.QueuedTransferIFSlave._take_burst


def push(self, *a, **k):
    counts["push"] += 1
    return (yield from _push(self, *a, **k))


def take(self, *a, **k):
    counts["take"] += 1
    return (yield from _take(self, *a, **k))


itf.QueuedTransferIF._push_to_endpoint = push
itf.QueuedTransferIFSlave._take_burst = take


def report(name, env, msgs, secs):
    ev = next(env._eid)
    print(f"{name:28s} events={ev:6d}  stream writes={counts['push']:5d} reads={counts['take']:5d}"
          f"  events/write={ev / max(counts['push'], 1):6.1f}  messages={msgs:4d}"
          f"  events/message={ev / max(msgs, 1):6.1f}  wall={secs * 1e3:7.1f} ms")


def reset():
    counts["push"] = counts["take"] = 0


def poly(scenario: str, bw: int = 32):
    from examples.stream_inband.poly import PolyAccel, PolyTB, connect
    from waveflow.hw.clock import Clock
    from waveflow.simulation.simulation import Simulation
    from waveflow.utils.burst_io import read_bursts

    d = Path("examples/stream_inband/data") / f"w{bw}" / scenario
    reset()
    sim = Simulation()
    clk = Clock(freq=100e6)
    accel = PolyAccel(name="poly_accel", sim=sim, clk=clk, in_bw=bw, out_bw=bw)
    tb = PolyTB(name="poly_tb", sim=sim, stimulus=d / "in", word_bw=bw,
                n_out=len(read_bursts(d / "expected")))
    connect(sim, tb, accel, clk)
    t0 = time.perf_counter()
    sim.run_sim()
    ncmd = len(read_bursts(d / "in"))
    report(f"poly {scenario} w{bw}", sim.env, ncmd, time.perf_counter() - t0)


def markov(link: str):
    from examples.markov.markov import MarkovSystem, default_jobs

    reset()
    s = MarkovSystem(jobs=default_jobs(4, 300), link=link)
    t0 = time.perf_counter()
    s.run()
    report(f"markov {link} (4 jobs x 300)", s.sim.env, 4, time.perf_counter() - t0)


if __name__ == "__main__":
    for sc in ("timing", "multi_data", "coeff_change"):
        poly(sc)
    for link in ("direct", "mm"):
        markov(link)
