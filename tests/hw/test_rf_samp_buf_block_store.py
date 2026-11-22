"""The RX sample buffer's pysim twin moves blocks, not words -- and leaves exactly what the words did.

Both tasks used to touch the memory one word at a time: the ingress stored each word through the port
and moved the write pointer per word, the capture read and emitted one word per burst.  That per-word
Python was most of a long run's time (``plans/rf_samp_buf_pysim_speed.md``).  Now the ingress stores a
burst as one numpy assignment per contiguous run (:meth:`RfSampBufIngress._store`), and the capture
reads and emits up to :attr:`RfSampBufCapture.pysim_burst_words` words at a time.

What must not change is what the loop left behind, so the ingress is checked here against the loop
itself, word for word, over the cases a block write could get wrong: a burst that wraps past the end
of the buffer, one longer than the buffer, the sample counter wrapping, a counter that wraps before
the buffer does, and a value too wide for the word.  The capture is checked on a window read across
the end of the buffer, which none of the example's own commands do.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.rf_samp_buf_rx.rf_samp_buf_rx import (
    captured_samples,
    ramp_samples,
    responses,
    run_pysim,
)
from waveflow.hw.rf_samp_buf import IDX_BW, RF_SAMP_BUF_OK, RfSampBufRx
from waveflow.simulation.simulation import Simulation

WRAP = 1 << IDX_BW


def _reference_store(storage: np.ndarray, wr: int, raw: np.ndarray, spw: int, depth: int) -> int:
    """The body this replaced, verbatim in its arithmetic: one word, one address, one pointer step."""
    mask = depth - 1
    for x in raw:
        storage[(wr // spw) & mask] = int(x)
        wr = (wr + spw) % WRAP
    return wr


def _buffer(bitwidth: int, spw: int, depth: int) -> RfSampBufRx:
    return RfSampBufRx(name="rx", sim=Simulation(), bitwidth=bitwidth, samp_per_word=spw,
                       depth=depth)


#: (bitwidth, samp_per_word, depth): the gated geometry, the two wider words the example runs, and
#: one where ``depth * spw > 2**IDX_BW`` -- the counter wraps before the buffer does, so the top half
#: of the buffer is never written and the runs wrap at ``2**IDX_BW // spw`` rather than at ``depth``.
GEOMETRIES = [(16, 1, 1024), (32, 2, 1024), (64, 4, 1024), (32, 2, 65536)]
#: Burst lengths: shorter than, equal to, one past, and well past the buffer.
LENGTHS = [1, 37, 256, 1023, 1024, 1025, 2500]


@pytest.mark.parametrize("bitwidth,spw,depth", GEOMETRIES)
@pytest.mark.parametrize("n", LENGTHS)
def test_a_stored_burst_leaves_what_the_word_loop_left(bitwidth, spw, depth, n):
    rng = np.random.default_rng(1000 * n + depth + spw)
    rx = _buffer(bitwidth, spw, depth)
    ing, storage = rx.ingress, rx.mem.storage
    # Three write positions: the start, just before the buffer's end, just before the counter wraps.
    for wr0 in (0, spw * (min(depth, WRAP // spw) - 5), WRAP - 3 * spw):
        storage[:] = rng.integers(0, 1 << min(bitwidth, 63), size=storage.size, dtype=np.uint64)
        want = storage.copy()
        raw = rng.integers(0, 1 << min(bitwidth, 63), size=n, dtype=np.uint64)
        want_wr = _reference_store(want, wr0, raw, spw, depth)

        ing.wr = wr0
        ing._store(raw)
        assert ing.wr == want_wr, f"wr0={wr0}: pointer {ing.wr}, the word loop leaves {want_wr}"
        bad = np.flatnonzero(storage != want)
        assert bad.size == 0, (
            f"wr0={wr0}, n={n}: {bad.size} words differ from the word loop's, first at address "
            f"{int(bad[0])}: {int(storage[bad[0]])} against {int(want[bad[0]])}")


def test_an_empty_burst_changes_nothing():
    rx = _buffer(16, 1, 1024)
    rx.ingress.wr = 77
    before = rx.mem.storage.copy()
    rx.ingress._store(np.zeros(0, dtype=np.uint64))
    assert rx.ingress.wr == 77 and np.array_equal(rx.mem.storage, before)


def test_a_word_too_wide_for_the_buffer_is_refused_not_wrapped():
    """The per-word store refused it (numpy raises on an out-of-range scalar); a block assignment
    into the 16-bit storage would wrap 70000 to 4464 without a word.  Refused before anything lands."""
    rx = _buffer(16, 1, 1024)
    before = rx.mem.storage.copy()
    with pytest.raises(ValueError, match="outside 0..2\\*\\*16-1"):
        rx.ingress._store(np.array([1, 70000, 3], dtype=np.uint64))
    assert rx.ingress.wr == 0 and np.array_equal(rx.mem.storage, before)


def test_a_window_read_across_the_end_of_the_buffer_is_the_right_samples():
    """The first command waits until sample 3308 is written; the second then asks for 3000..3199,
    already in the buffer, so the capture serves it in full bursts -- and the one covering samples
    3064..3079 sits at buffer addresses 1016..1023 then 0..7.  None of the gate's commands cross the
    end of the buffer, so without this the read's wrap would be untested."""
    cmds = [(1, 3300, 8), (2, 3000, 200)]
    tb = run_pysim(cmds=cmds)
    ramp = ramp_samples()
    want = np.concatenate([ramp[3300:3308], ramp[3000:3200]])
    got = captured_samples(tb)
    assert got.size == want.size and np.array_equal(got, want), (
        f"captured {got.size} samples, want {want.size}; first difference at "
        f"{int(np.flatnonzero(got[:want.size] != want[:got.size])[0]) if got.size else 0}")
    assert responses(tb) == [(1, RF_SAMP_BUF_OK, 8), (2, RF_SAMP_BUF_OK, 200)]
    # The test is only evidence if some burst really did cross the end of the buffer.
    crossed = [b for b in tb.out_sink.words
               if {int(ramp[3071]), int(ramp[3072])} <= {int(v) for v in np.asarray(b).ravel()}]
    assert crossed, "no single output burst held samples 3071 and 3072: the read never wrapped"
