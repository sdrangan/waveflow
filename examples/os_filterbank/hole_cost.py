"""hole_cost.py -- what an idle cycle costs SsrFft, between frames or inside one, in a cycle model of its chain
(``plans/ssr_fft_gap_throughput.md``, fix 1).

    python -m examples.os_filterbank.hole_cost            # the check against the RTL, then the hole table
    python -m examples.os_filterbank.hole_cost --L 64 256 1024

**An aid to design, not a timing model.**  The chain of a runtime-direction ``SsrFft`` (``ssr_fft.hls``'s
task list), a cycle at a time: every commutator is ``cycle_ref.CommutatorTask`` -- the C++ task's algorithm,
statement for statement -- stalled when its output FIFO is full and the tick would write; the register,
the stages and the reorder are one word a cycle; every FIFO two words deep (the HLS default), a word
written on one cycle readable on the next.  The source is a testbench driver: a frame of ``L/R`` words,
then its gap counted after the FFT took the frame's last word (the RTL driver's counting).

**Checked against the RTL** (``plans/osfb_pysim_fidelity.md`` Section 5, ``L = 64``, Vitis / xsim 2025.1):
at gaps of 3 to 24 cycles between frames it gives the RTL's interval on every frame (32, and 40 at 24) and
the RTL's input stretch (the frame accepted over 29, 28, 24, 20, 16 cycles at gaps 3, 4, 8, 12, 16).  At
gaps of 1 and 2 it is **optimistic**: it takes one frame in three (gap 1) or two (gap 2) back to back where
the RTL takes none, and accepts over 29 and 28 cycles where the RTL takes 31 and 30 -- it holds the next
frame's words closer to ``tp{S-2}`` than the RTL's pipelines do.  So a cost it reports is a lower bound.

**What it was built for: a hole inside a frame.**  ``pre`` reads a frame's tag and its ``L/R`` words off
one stream, so one cycle a frame carries no word to the FFT; the question is where to put it.  A hole
moves through each commutator by ``(R-1)*D`` words, and one that reaches a frame-sized group's boundary
(``tp{S-2}``, ``cm0``, ``rc``) costs a frame.  Measured here, a 1-cycle hole before word ``p`` of every
frame, frames otherwise back to back: ``p = 1`` and ``p = 2`` cost one cycle at ``L = 64, 256, 1024``;
``p = L/R - 1`` (holding a frame's last word for the next tag) costs a whole frame every few frames.
"""
from __future__ import annotations

import argparse
from collections import deque

from waveflow.dsp.ssr_fft import model as m
from waveflow.dsp.ssr_fft.cycle_ref import CommutatorTask


class Fifo:
    """``depth`` words; a word written on a cycle is readable from the next."""

    def __init__(self, depth: int = 2) -> None:
        self.depth, self.q, self.inc = depth, deque(), []

    def can_write(self) -> bool:                # judged on the cycle's start state
        return len(self.q) < self.depth

    def tick(self) -> None:
        self.q.extend(self.inc)
        self.inc = []


class Commutator:
    """``cycle_ref.CommutatorTask``, with the task's blocking write: a tick that would write waits for room."""

    def __init__(self, d: int, fin: Fifo, fout: Fifo) -> None:
        self.t, self.fin, self.fout = CommutatorTask(d), fin, fout

    def step(self) -> None:
        dl = self.t.out_dl[0]
        writes = dl.buf[0][1] if dl.n else True
        if writes and not self.fout.can_write():
            return
        out: list = []
        view = deque(self.fin.q)
        n0 = len(view)
        self.t.step(view, out)
        for _ in range(n0 - len(view)):
            self.fin.q.popleft()
        self.fout.inc.extend(out)


class Wire:
    """A register or a stage: one word a cycle."""

    def __init__(self, fin: Fifo, fout: Fifo) -> None:
        self.fin, self.fout = fin, fout

    def step(self) -> None:
        if self.fin.q and self.fout.can_write():
            self.fout.inc.append(self.fin.q.popleft())


def chain(L: int):
    """``(geometry, the input FIFO, the tasks, the output FIFO)``: ``in_reg``, the transposer, the stages
    with their commutators, ``rc`` (the ping-pong reorder after it delays every frame alike: left out)."""
    g = m.Geometry(L, runtime=True)
    first = Fifo()
    tasks, a = [], first

    def hop(make, *args):
        nonlocal a
        b = Fifo()
        tasks.append(make(*args, a, b))
        a = b
    hop(Wire)                                                   # in_reg
    for d in g.transposer_ds:
        hop(Commutator, d)
    for s in range(g.S):
        hop(Wire)                                               # stage s
        if s < g.S - 1:
            hop(Commutator, g.stage_d(s))
    hop(Commutator, m.reorder_d(g))                             # rc
    return g, first, tasks, a


class Driver:
    """Frames of ``nw`` words: ``gaps[k]`` idle cycles after frame ``k``'s last word is taken, and a hole of
    ``holes[p]`` idle cycles before word ``p`` of every frame."""

    def __init__(self, nw: int, gaps: list[int], holes: dict[int, int] | None = None) -> None:
        self.nw, self.gaps, self.holes = nw, list(gaps), holes or {}
        self.k = self.i = self.wait = 0
        self.hole: int | None = None

    def __call__(self):
        if self.k >= len(self.gaps):
            return None
        if self.wait > 0:
            self.wait -= 1
            return None
        if self.hole is None:
            self.hole = self.holes.get(self.i, 0)
        if self.hole > 0:
            self.hole -= 1
            return None
        word = [self.k * self.nw + self.i] * 4
        self.i += 1
        self.hole = None
        if self.i == self.nw:
            self.i = 0
            self.wait = self.gaps[self.k]
            self.k += 1
        return word


def run(L: int, driver: Driver, ncyc: int) -> tuple[list[int], list[int]]:
    """The cycles each input word is taken and each output word leaves ``rc``."""
    _g, first, tasks, out = chain(L)
    fifos = [first] + [t.fout for t in tasks]
    taken, left = [], []
    for c in range(ncyc):
        while out.q:
            out.q.popleft()
            left.append(c)
        for t in reversed(tasks):
            t.step()
        if first.can_write():
            w = driver()
            if w is not None:
                first.inc.append(w)
                taken.append(c)
        for f in fifos:
            f.tick()
    return taken, left


def intervals(left: list[int], nw: int) -> list[int]:
    starts = left[::nw]
    return [b - a for a, b in zip(starts, starts[1:])]


def check_gaps(L: int = 64, nframes: int = 12) -> list[tuple[int, list[int], list[int]]]:
    """Per constant gap: the output intervals and how long each input frame took to be accepted."""
    nw = L // 4
    out = []
    for g in (0, 1, 2, 3, 4, 8, 12, 16, 24):
        taken, left = run(L, Driver(nw, [g] * nframes), 60 * nw * 4)
        span = [taken[i * nw + nw - 1] - taken[i * nw] + 1 for i in range(len(taken) // nw)]
        out.append((g, sorted(set(intervals(left, nw)[1:])), sorted(set(span[2:]))))
    return out


def hole_cost(L: int, positions, nframes: int = 10) -> dict[int, list[int]]:
    """Per position ``p``: the output intervals with a 1-cycle hole before word ``p`` of every frame."""
    nw = L // 4
    return {p: intervals(run(L, Driver(nw, [0] * nframes, {p: 1}), 4 * nframes * nw + 40 * nw)[1], nw)
            for p in positions}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--L", type=int, nargs="+", default=[64, 256, 1024])
    a = ap.parse_args()
    print("L = 64, constant gaps -- gap: output intervals / input frame accepted over (cycles)")
    print("  RTL: 0: 16/16  1: 32/31  2: 32/30  3: 32/29  4: 32/28  8: 32/24  12: 32/20  16: 32/16  24: 40/16")
    print("  here: " + "  ".join(f"{g}: {iv}/{sp}" for g, iv, sp in check_gaps()))
    for L in a.L:
        nw = L // 4
        print(f"L = {L}, a 1-cycle hole before word p of every frame -- p: output intervals")
        for p, iv in hole_cost(L, (1, 2, nw // 2, nw - 1)).items():
            print(f"  p = {p}: {iv}")
