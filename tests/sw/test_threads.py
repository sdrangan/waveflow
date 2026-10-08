"""waveflow.sw -- software threads in pysim (plans/host_runtime.md, Stage 1).

The channels between threads, interrupt waits (including the zero-time spin diagnosis), and the bus
primitives that block only for the transaction (``room_irq`` / ``push``, ``data_irq`` / ``pop``) --
exercised by the single-thread Markov driver the plan sketches, bit-exact against the golden.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from waveflow.hw.irq import IrqIF, IrqIFSink, IrqIFSource
from waveflow.simulation.simulation import Simulation
from waveflow.sw import SwEvent, SwHost, SwLock, SwQueue, SwSemaphore, wait_any


@dataclass
class _Host(SwHost):
    """A host whose main() is supplied by the test."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.log: list[tuple[float, str]] = []
        self.body = None

    def note(self, what: str) -> None:
        self.log.append((round(self.now / self.clk.period), what))

    def main(self):
        yield from self.body(self)


def _run(body):
    sim = Simulation()
    h = _Host(name="h", sim=sim)
    h.body = body
    sim.run_sim()
    return h


# ---------------------------------------------------------------------------------------------
# Channels between threads
# ---------------------------------------------------------------------------------------------

def test_compute_is_the_only_thing_that_takes_time():
    def body(h):
        h.note("a")
        yield from h.compute(0)                       # zero cycles: no time, no yield needed
        h.note("b")
        yield from h.compute(5)
        h.note("c")
    assert _run(body).log == [(0, "a"), (0, "b"), (5, "c")]


def test_event_wakes_its_waiter_and_stays_set():
    def body(h):
        ev = SwEvent(h, "ev")

        def setter():
            yield from h.compute(3)
            ev.set()
            h.note("set")
        h.start(setter)
        yield from ev.wait()
        h.note("woke")
        yield from ev.wait()                          # still set: returns at once
        h.note("again")
    assert _run(body).log == [(3, "set"), (3, "woke"), (3, "again")]


def test_semaphore_admits_at_most_its_count():
    def body(h):
        sem = SwSemaphore(h, 2, "slots")
        inside = []

        def worker(k):
            yield from sem.acquire()
            inside.append(k)
            h.note(f"in {k} ({len(inside)})")
            yield from h.compute(4)
            inside.remove(k)
            sem.release()
        ts = [h.start(worker, k) for k in range(5)]
        for t in ts:
            yield from t.join()
    log = _run(body).log
    assert max(int(w.split("(")[1][0]) for _, w in log) == 2
    assert [t for t, _ in log] == [0, 0, 4, 4, 8]


def test_lock_release_without_hold_is_refused():
    def body(h):
        lk = SwLock(h, "lk")
        yield from lk.acquire()
        lk.release()
        with pytest.raises(RuntimeError, match="not held"):
            lk.release()
    _run(body)


def test_queue_put_waits_for_room_and_get_for_a_message():
    def body(h):
        q = SwQueue(h, capacity=1, name="q")

        def producer():
            for k in range(3):
                yield from q.put(k)
                h.note(f"put {k}")

        h.start(producer)
        for _ in range(3):
            yield from h.compute(2)
            m = yield from q.get()
            h.note(f"got {m}")
    log = _run(body).log
    assert log == [(0, "put 0"), (2, "got 0"), (2, "put 1"), (4, "got 1"), (4, "put 2"), (6, "got 2")]


def test_wait_any_returns_the_first_ready_and_does_not_consume():
    def body(h):
        a, b = SwEvent(h, "a"), SwEvent(h, "b")
        q = SwQueue(h, name="q")

        def later():
            yield from h.compute(7)
            b.set()
        h.start(later)
        k = yield from wait_any(a, b, q)
        h.note(f"any={k}")
        yield from q.put("m")
        k = yield from wait_any(q, b)                 # both ready: argument order wins, at once
        h.note(f"any={k} len={len(q.items)}")
    assert _run(body).log == [(7, "any=1"), (7, "any=0 len=1")]


# ---------------------------------------------------------------------------------------------
# Interrupts
# ---------------------------------------------------------------------------------------------

def _irq_host(body):
    sim = Simulation()
    h = _Host(name="h", sim=sim)
    sink = h.add_irq("irq")
    src = IrqIFSource(name="src", sim=sim)
    line = IrqIF(name="line", sim=sim)
    line.bind("source", src)
    line.bind("sink", sink)
    h.body = lambda h: body(h, src, sink)
    return sim, h


def test_irq_wait_returns_at_once_when_high_and_on_the_rise_otherwise():
    def body(h, src, sink):
        def raiser():
            yield from h.compute(4)
            src.set(True)
        h.start(raiser)
        yield from sink.wait()
        h.note("rise")
        yield from sink.wait()                        # still high: at once
        h.note("high")
    sim, h = _irq_host(body)
    sim.run_sim()
    assert h.log == [(4, "rise"), (4, "high")]
    assert isinstance(h.irq, IrqIFSink) and h.irq in h.endpoints.values()


def test_a_handler_that_never_clears_its_interrupt_is_diagnosed():
    def body(h, src, sink):
        src.set(True)
        while True:                                   # the bug: never drains, never re-arms
            yield from sink.wait()
    sim, _h = _irq_host(body)
    with pytest.raises(RuntimeError, match="never clears the interrupt"):
        sim.run_sim()


# ---------------------------------------------------------------------------------------------
# The bus primitives, on a real system: the single-thread Markov driver
# ---------------------------------------------------------------------------------------------

def test_single_thread_markov_driver_is_bit_exact():
    """One thread that sends commands and pends on the response queue's data interrupt -- the driver
    plans/host_runtime.md sketches -- using the primitives that block only for the bus transaction."""
    from examples.markov.markov import (
        DW,
        MAX_IN_FLIGHT,
        U8,
        MarkovHost,
        MarkovSystem,
        MkvResp,
        default_jobs,
        markov_golden,
    )
    from waveflow.hw.arrayutils import read_array

    @dataclass
    class OneThread(MarkovHost):
        def main(self):
            n, sent, done = len(self.items), 0, 0
            resp_words = MkvResp.nwords_per_inst(DW)
            while done < n:
                while sent < n and sent - done < MAX_IN_FLIGHT:
                    _xaddr, _xwords, cmd = self.items[sent]
                    room = yield from self.qcmd.room_irq(len(cmd) + 1)
                    yield from room.wait()
                    yield from self.qcmd.push(cmd)
                    sent += 1
                data = yield from self.qresp.data_irq(resp_words)
                yield from data.wait()
                resp = yield from self.qresp.pop(MkvResp)
                xaddr, xwords, _cmd = self.items[int(resp.tx_id)]
                words = yield from self.mem_reader.read(xwords, xaddr)
                x = np.asarray(read_array(np.asarray(words, dtype=np.uint64), U8, word_bw=DW,
                                          shape=int(resp.n)).val, dtype=np.uint8)
                self.results[int(resp.tx_id)] = dict(n=int(resp.n), ones=int(resp.ones), x=x)
                done += 1
            self.done.succeed()

    jobs = default_jobs(4, 300)
    sysm = MarkovSystem(jobs=jobs, link="mm")
    sysm.host.__class__ = OneThread                   # same instance and wiring; only the program differs
    res = sysm.run()
    assert sorted(res) == [j["tx_id"] for j in jobs]
    for j in jobs:
        assert np.array_equal(res[j["tx_id"]]["x"], markov_golden(j))
        assert res[j["tx_id"]]["ones"] == int(markov_golden(j).sum())
    print("single-thread driver pysim cycles:", sysm.sim.env.now / sysm.clk.period)
