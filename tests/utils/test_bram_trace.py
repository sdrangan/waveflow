"""The pure half of :mod:`waveflow.utils.bram_trace` — no VCD, no toolchain.

The VCD-reading half is exercised by ``tests/examples/test_bram_access_xsi.py``, which is the only
place it *can* be exercised honestly: a scan needs a real waveform with a real collision in it.  What
is here is the arithmetic those gates rest on, pinned where it costs nothing to run.
"""
from __future__ import annotations

import numpy as np

from waveflow.utils.bram_trace import Hazard, PortSamples, describe, measured_read_latency


def _port(addr, dout, en=None) -> PortSamples:
    n = len(addr)
    return PortSamples(inst="mem",
                       en=np.array([1] * n if en is None else en),
                       we=np.zeros(n, dtype=int),
                       addr=np.asarray(addr),
                       dout=np.asarray(dout))


def test_a_ramp_pins_the_latency_to_one_offset():
    """The measurement objective 4 rests on: the offset at which the answer appears.

    ``dout`` here lags ``addr`` by two, and the payload is a ramp — so exactly one offset explains
    every read.
    """
    addr = [0, 1, 2, 3, 4, 5]
    dout = [999, 999, 100, 101, 102, 103]     # value = 100 + addr, two cycles late
    assert measured_read_latency(_port(addr, dout), lambda a: 100 + a, max_latency=3) == {2}


def test_a_constant_payload_cannot_tell_the_offsets_apart():
    """Why the scenario is a ramp, stated as a property rather than as a comment.

    With a constant payload every offset fits, and a gate asserting ``fits == {1}`` fails loudly
    instead of confirming a number the data never distinguished.
    """
    addr = [0, 1, 2, 3]
    dout = [7, 7, 7, 7]
    assert measured_read_latency(_port(addr, dout), lambda _a: 7, max_latency=2) == {0, 1, 2}


def test_an_answer_that_never_appears_leaves_the_set_empty():
    """A real defect — the data is not what the address asked for at any offset — is not silence."""
    addr = [0, 1, 2, 3]
    dout = [500, 501, 502, 503]
    assert measured_read_latency(_port(addr, dout), lambda a: 100 + a, max_latency=3) == set()


def test_cycles_whose_contents_are_unknown_are_skipped_not_guessed():
    """``expected(addr) -> None`` is how a rewritten or never-written word stays out of the fit.

    Without it the scan would demand a value for memory that holds ``X`` at RTL and ``0`` in pysim —
    which is the same care the sentinel exists for in the scenario.
    """
    addr = [0, 1, 900, 2]
    dout = [999, 100, 101, 12345]             # index 3 belongs to addr 900, whose value is unknown
    fits = measured_read_latency(_port(addr, dout), lambda a: None if a == 900 else 100 + a,
                                 max_latency=2)
    assert fits == {1}


def test_disabled_cycles_are_not_reads():
    """``en`` low is not a read, and counting one would drag a stale ``dout`` into the fit."""
    addr = [5, 0, 1]
    dout = [0, 111, 100, 101]
    port = _port(addr, dout[:3], en=[0, 1, 1])
    port = PortSamples(inst=port.inst, en=port.en, we=port.we, addr=port.addr,
                       dout=np.asarray(dout))
    assert measured_read_latency(port, lambda a: 100 + a, max_latency=2) == {1}


def test_describe_says_nothing_happened_and_says_what_did():
    assert describe([]) == "no read-during-write collisions"
    msg = describe([Hazard("mem", 10, 64), Hazard("mem", 11, 65)])
    assert "2 read-during-write collision(s) on mem" in msg
    assert "cycle 10 addr 64" in msg and "cycle 11 addr 65" in msg


def test_describe_truncates_rather_than_dumping_hundreds():
    msg = describe([Hazard("mem", c, 64) for c in range(50)], limit=3)
    assert "+47 more" in msg
