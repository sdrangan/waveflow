"""The commutator task, one tick at a time -- the C++ body's algorithm, written in Python.

:func:`.model.commute` says *what* a commutator computes (a block-transpose gather).  This module
says *how* the free-running task computes it, at one word per tick, with nothing but delay lines
and a lane switch -- the structure ``ssr_fft_commutator_task`` (``src/ssr_fft_tasks.h``)
implements, statement for statement.  The test that the two agree, for every block size the FFT
uses and with gaps in the input, is what ties the re-indexing in the model to the hardware.

The structure (``R`` lanes, block size ``D``, one *group* = ``R`` slots of ``D`` words):

1. **input triangle** -- lane ``j`` is delayed ``j*D`` ticks;
2. **switch** -- output lane ``l`` takes input lane ``(k - l) mod R``, where the slot
   ``k = (tick // D) mod R`` advances every ``D`` ticks;
3. **output triangle** -- lane ``l`` is delayed ``(R-1-l)*D`` ticks.

Sample ``(slot i, lane j)`` of a group leaves as ``(slot j, lane i)``, ``(R-1)*D`` ticks later.

**Ticks come in whole groups.**  The switch's slot is a function of the tick count, so a group must
occupy ``R*D`` consecutive ticks.  At each group boundary the task decides what the next group is:

* **data** if a word is waiting -- it then reads ``R*D`` words (blocking: a gap *inside* a group
  stalls every delay line in place, which is just a pause in tick time);
* **bubble** if nothing is waiting but valid samples are still inside -- ``R*D`` ticks of invalid
  samples push the tail out (it needs ``(R-1)*D < R*D``);
* **idle** if nothing is waiting and nothing is inside -- no tick at all.

Every sample carries a valid bit; only valid words are written.  An output word's lanes all come
from one group, so they are all valid or all not.  Nothing is written that was not read, which keeps
the task on the safe side of the ``hls::task`` reset trap.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np


@dataclass
class _DelayLine:
    """``n`` ticks of delay; ``n = 0`` is a wire."""
    n: int
    buf: deque = field(init=False)

    def __post_init__(self) -> None:
        self.buf = deque([(0, False)] * self.n)

    def shift(self, x: tuple) -> tuple:
        if self.n == 0:
            return x
        self.buf.append(x)
        return self.buf.popleft()


class CommutatorTask:
    """One commutator, ticked by :meth:`step` -- the C++ task's ``while(1)`` body."""

    def __init__(self, d: int, r: int = 4) -> None:
        self.d, self.r = d, r
        self.group = r * d
        self.cnt = 0                 # tick within the current group
        self.mode = "idle"           # 'data' | 'bubble' | 'idle'
        self.inside = 0              # valid words read and not yet written
        self.in_dl = [_DelayLine(j * d) for j in range(r)]
        self.out_dl = [_DelayLine((r - 1 - j) * d) for j in range(r)]

    def step(self, fifo_in: deque, out: list) -> bool:
        """One loop iteration.  Returns False if it did not tick (idle, or stalled on input)."""
        if self.cnt == 0:
            if fifo_in:
                self.mode = "data"
            elif self.inside > 0:
                self.mode = "bubble"
            else:
                self.mode = "idle"
        if self.mode == "idle":
            return False
        if self.mode == "data":
            if not fifo_in:
                return False                          # blocking read: stall, no tick
            word = fifo_in.popleft()
            x = [(word[j], True) for j in range(self.r)]
            self.inside += 1
        else:
            x = [(0, False)] * self.r

        a = [self.in_dl[j].shift(x[j]) for j in range(self.r)]
        k = (self.cnt // self.d) % self.r
        b = [a[(k - ln) % self.r] for ln in range(self.r)]
        c = [self.out_dl[ln].shift(b[ln]) for ln in range(self.r)]

        if c[0][1]:
            assert all(v for _, v in c), "an output word mixed valid and invalid lanes"
            out.append([s for s, _ in c])
            self.inside -= 1
        self.cnt = (self.cnt + 1) % self.group
        return True


def run(words: np.ndarray, d: int, r: int = 4, *, gaps: dict[int, int] | None = None,
        max_ticks: int = 10_000_000) -> np.ndarray:
    """Feed ``words`` (shape ``(n, R)``) through one commutator; return everything it writes.

    ``gaps`` maps an input word index to a number of idle cycles before that word is offered --
    the input FIFO is empty during them, so the task stalls, bubbles or idles as the rules say.
    """
    gaps = gaps or {}
    task = CommutatorTask(d, r)
    fifo: deque = deque()
    out: list = []
    pending = list(range(len(words)))
    wait = gaps.get(0, 0)
    cycles = 0
    while (pending or fifo or task.inside) and cycles < max_ticks:
        cycles += 1
        if pending:
            if wait > 0:
                wait -= 1
            else:
                fifo.append(words[pending.pop(0)])
                if pending:
                    wait = gaps.get(pending[0], 0)
        task.step(fifo, out)
    if cycles >= max_ticks:
        raise RuntimeError("commutator did not drain")
    return np.asarray(out).reshape(-1, r)
