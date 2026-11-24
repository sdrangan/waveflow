"""``wait_any`` (``waveflow.hw.interface``): block until one of several stream slaves holds a burst -- the
pysim form of an HLS body that polls its inputs with ``empty()`` (``plans/shared_kernel.md`` Stage 2)."""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave, wait_any
from waveflow.simulation.simulation import Simulation

PER = 1e-8                                    # one cycle at 100 MHz


def _link(sim, name):
    tx = StreamIFMaster(name=f"{name}_tx", sim=sim, bitwidth=32)
    rx = StreamIFSlave(name=f"{name}_rx", sim=sim, bitwidth=32)
    s = StreamIF(name=name, sim=sim, clk=Clock(freq=1 / PER), bitwidth=32, depth=64)
    s.bind("master", tx)
    s.bind("slave", rx)
    return tx, rx


def _rig(n=2):
    sim = Simulation()
    return sim, [_link(sim, f"l{i}") for i in range(n)]


def _later(sim, delay, tx, words):
    def p():
        yield sim.env.timeout(delay)
        yield from tx.write(np.asarray(words, dtype=np.uint32))
    sim.env.process(p())


def _run(sim, body):
    out = {}

    def p():
        out["v"] = yield from body()
    sim.env.process(p())
    sim.env.run()
    return out.get("v")


def test_it_wakes_on_whichever_input_is_delivered_first():
    sim, ((tx0, rx0), (tx1, rx1)) = _rig()
    _later(sim, 50 * PER, tx0, [1, 2])
    _later(sim, 10 * PER, tx1, [3])

    def body():
        i = yield from wait_any(rx0, rx1)
        return i, sim.env.now, bool(rx1.data_buffer.items), bool(rx0.data_buffer.items)

    i, t, ready1, ready0 = _run(sim, body)
    assert i == 1 and ready1 and not ready0
    assert t < 50 * PER                         # did not wait for the later input


def test_it_takes_nothing_and_returns_the_first_ready_in_argument_order():
    sim, ((tx0, rx0), (tx1, rx1)) = _rig()
    _later(sim, 0, tx0, [1])
    _later(sim, 0, tx1, [2])

    def body():
        yield sim.env.timeout(100 * PER)        # both delivered
        a = yield from wait_any(rx1, rx0)
        b = yield from wait_any(rx0, rx1)
        return a, b, len(rx0.data_buffer.items), len(rx1.data_buffer.items)

    assert _run(sim, body) == (0, 0, 1, 1)      # the first argument wins; both bursts still there


def test_a_begun_transfer_does_not_count():
    """An input whose burst has only begun is not ready: the wait ends at delivery, not at the start."""
    sim, ((tx0, rx0),) = _rig(1)
    _later(sim, 0, tx0, list(range(20)))

    def body():
        yield from wait_any(rx0)
        return sim.env.now

    assert _run(sim, body) >= 20 * PER


def _begun_at(sim, delay, tx, words, hold):
    """A writer that begins a burst at *delay* and ends it *hold* later: a frame announced before it is
    all there."""
    def p():
        yield sim.env.timeout(delay)
        x = yield from tx.begin(np.asarray(words, dtype=np.uint32))
        yield sim.env.timeout(hold)
        yield from tx.end(x)
    sim.env.process(p())


def test_with_begun_a_burst_counts_from_its_first_word():
    """``begun=True``: an input is ready once its burst's first word is here -- a cycle after the
    transfer starts -- as an HLS ``empty()`` turns false on a word (``plans/stream_dispatch.md`` Stage 8)."""
    sim, ((tx0, rx0),) = _rig(1)
    _later(sim, 0, tx0, list(range(20)))

    def body():
        i = yield from wait_any(rx0, begun=True)
        return i, sim.env.now, bool(rx0.data_buffer.items), rx0.pending_burst is not None

    i, t, delivered, begun = _run(sim, body)
    assert (i, delivered, begun) == (0, False, True)
    assert t == pytest.approx(1 * PER)


def test_with_begun_the_first_word_to_land_wins_whatever_the_burst_lengths():
    """A long burst begun first beats a short one begun later, though the short one is delivered first."""
    sim, ((tx0, rx0), (tx1, rx1)) = _rig()
    _later(sim, 10 * PER, tx0, [1])              # first word at 11, delivered at 11
    _later(sim, 0, tx1, list(range(64)))         # first word at 1, delivered at 64

    def body():
        i = yield from wait_any(rx0, rx1, begun=True)
        return i, sim.env.now

    i, t = _run(sim, body)
    assert i == 1 and t == pytest.approx(1 * PER)


def test_with_begun_two_first_words_in_one_cycle_go_in_argument_order():
    sim, ((tx0, rx0), (tx1, rx1)) = _rig()
    _later(sim, 5 * PER, tx1, list(range(8)))
    _later(sim, 5 * PER, tx0, list(range(8)))

    def body():
        a = yield from wait_any(rx0, rx1, begun=True)
        return a, sim.env.now

    a, t = _run(sim, body)
    assert a == 0 and t == pytest.approx(6 * PER)


def test_with_begun_a_burst_held_open_is_ready_long_before_it_is_delivered():
    """A writer that begins and holds its burst open: the begun wait returns at its first word, the
    default one at its delivery."""
    out = {}
    for begun in (False, True):
        sim, ((tx0, rx0),) = _rig(1)
        _begun_at(sim, 0, tx0, [1, 2, 3], hold=100 * PER)

        def body(begun=begun, rx0=rx0, sim=sim):
            yield from wait_any(rx0, begun=begun)
            return sim.env.now

        out[begun] = _run(sim, body)
    assert out[True] == pytest.approx(1 * PER)
    assert out[False] == pytest.approx(100 * PER)


def test_with_begun_a_delivered_burst_is_ready_at_once():
    sim, ((tx0, rx0),) = _rig(1)
    _later(sim, 0, tx0, [1, 2])

    def body():
        yield sim.env.timeout(50 * PER)
        yield from wait_any(rx0, begun=True)
        return sim.env.now, rx0._arrival

    t, slot = _run(sim, body)
    assert t == pytest.approx(50 * PER) and slot is None


def test_with_nothing_coming_the_run_ends_with_the_wait_pending():
    """No input ever arrives: no busy loop -- the simulation runs out of events and returns."""
    sim, ((_tx0, rx0), (_tx1, rx1)) = _rig()
    reached = []

    def body():
        yield from wait_any(rx0, rx1)
        reached.append(True)

    _run(sim, body)
    assert reached == []
    assert rx0._arrival is not None and rx1._arrival is not None    # still waiting


def test_the_arrival_slots_are_released_after_the_wait():
    sim, ((tx0, rx0), (_tx1, rx1)) = _rig()
    _later(sim, 5 * PER, tx0, [7])

    def body():
        yield from wait_any(rx0, rx1)
        return rx0._arrival, rx1._arrival

    assert _run(sim, body) == (None, None)


def test_a_second_waiter_on_the_same_input_is_refused():
    sim, ((_tx0, rx0),) = _rig(1)
    err = []

    def first():
        yield from wait_any(rx0)

    def second():
        yield sim.env.timeout(PER)
        try:
            yield from wait_any(rx0)
        except RuntimeError as e:
            err.append(str(e))

    sim.env.process(first())
    sim.env.process(second())
    sim.env.run()
    assert err and "already waiting" in err[0]


def test_it_needs_an_endpoint():
    with pytest.raises(ValueError):
        next(wait_any())
