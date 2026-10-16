"""Option B of the Stage 1 open question, measured: every plain get of a burst that was already
waiting is charged its beats -- returns at max(delivery, L.first + (n-1) cycles), L.first = when it
asked.  Counts SimPy events and the simulated end time with and without, in fresh processes."""
import subprocess
import sys

CHILD = r'''
import sys, time
sys.path.insert(0, r"plans/witness/pipelined_ops")
import waveflow.hw.interface as itf
CHARGE = sys.argv[1] == "1"
stamps, charged = {}, [0]
_transfer, _take = itf.QueuedTransferIF._transfer, itf.QueuedTransferIFSlave._take_burst

def transfer(self, ep, words, cycles, tstart):
    yield from _transfer(self, ep, words, cycles, tstart)
    stamps[id(words)] = (self.env.now, self.clk.period if self.clk else 0.0)

def take(self):
    asked = self.env.now
    words = yield from _take(self)
    s = stamps.pop(id(words), None)
    if CHARGE and s is not None:
        delivered, per = s
        target = asked + (int(words.shape[0]) - 1) * per
        if asked > delivered and target > self.env.now:      # it sat in the buffer: read it at II=1
            charged[0] += 1
            yield self.env.timeout(target - self.env.now)
    return words

itf.QueuedTransferIF._transfer = transfer
itf.QueuedTransferIFSlave._take_burst = take
which = sys.argv[2]
if which.startswith("poly"):
    from pathlib import Path
    from examples.stream_inband.poly import PolyAccel, PolyTB, connect
    from waveflow.hw.clock import Clock
    from waveflow.simulation.simulation import Simulation
    from waveflow.utils.burst_io import read_bursts
    d = Path("examples/stream_inband/data/w32") / which.split(":")[1]
    sim = Simulation(); clk = Clock(freq=100e6)
    a = PolyAccel(name="poly_accel", sim=sim, clk=clk); tb = PolyTB(name="poly_tb", sim=sim,
        stimulus=d / "in", word_bw=32, n_out=len(read_bursts(d / "expected")))
    connect(sim, tb, a, clk); sim.run_sim(); env, per = sim.env, clk.period
else:
    from examples.markov.markov import MarkovSystem, default_jobs
    s = MarkovSystem(jobs=default_jobs(4, 300), link=which.split(":")[1]); s.run()
    env, per = s.sim.env, s.clk.period
print(next(env._eid), round(env.now / per), charged[0])
'''

if __name__ == "__main__":
    print(f"{'run':24s} {'events A':>9s} {'events B':>9s} {'added':>6s} {'charged':>8s}"
          f" {'cycles A':>9s} {'cycles B':>9s}")
    for which in ("poly:timing", "poly:multi_data", "markov:direct", "markov:mm"):
        r = {}
        for charge in ("0", "1"):
            out = subprocess.run([sys.executable, "-c", CHILD, charge, which], capture_output=True,
                                 text=True, check=True).stdout.split()
            r[charge] = [int(x) for x in out]
        a, b = r["0"], r["1"]
        print(f"{which:24s} {a[0]:9d} {b[0]:9d} {b[0] - a[0]:+6d} {b[2]:8d} {a[1]:9d} {b[1]:9d}")
