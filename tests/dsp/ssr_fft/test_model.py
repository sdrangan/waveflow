"""F1 of ``plans/ssr_fft.md``: the per-stage model and its re-indexing.

Three claims, each with its own gate:

* the pipeline of stages on the wire order computes ``fft_general``'s bits exactly -- and so the
  vendor's, which ``tests/vitis_l1/fft`` pins -- at every length;
* each commutator is a block transpose, and the free-running task's algorithm
  (:mod:`waveflow.dsp.ssr_fft.cycle_ref`) computes exactly that, back to back and with gaps;
* that algorithm moves one word per tick in steady state: the interval is ``L/R``.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.dsp.ssr_fft import cycle_ref
from waveflow.dsp.ssr_fft.model import (Geometry, block_transpose_index, commute, pipeline,
                                        reorder, ssr_fft, to_words, transpose_in)
from waveflow.dsp.ssr_fft.types import edge_types
from waveflow.vitis_l1.fft import fft_general

LENGTHS = (16, 64, 256, 1024, 4096)


def _inputs(length: int, rng: np.random.Generator):
    """Random frames, plus the corners that exercise the wraps: most negative, most positive."""
    yield np.full(length, -(1 << 15)), np.full(length, (1 << 15) - 1)
    yield np.full(length, -(1 << 15)), np.full(length, -(1 << 15))
    for _ in range(3):
        yield rng.integers(-(1 << 15), 1 << 15, length), rng.integers(-(1 << 15), 1 << 15, length)


@pytest.mark.parametrize("length", LENGTHS)
def test_pipeline_is_fft_general_bit_for_bit(length):
    """Through every block on the wire order -- so the transposer, the commutators and the reorder
    are all checked, not just the arithmetic."""
    rng = np.random.default_rng(length)
    for xr, xi in _inputs(length, rng):
        a_re, a_im, a_fmt = fft_general(xr, xi, length, 16, 2)
        b_re, b_im, b_fmt = ssr_fft(xr, xi, length)
        assert a_fmt == b_fmt
        assert np.array_equal(a_re, b_re) and np.array_equal(a_im, b_im)


@pytest.mark.parametrize("length", LENGTHS)
def test_transposer_lines_up_each_butterflys_inputs(length):
    """Stage 0's butterfly ``m`` needs ``x[m + p*L/R]`` on lane ``p`` of word ``m``."""
    geo = Geometry(length)
    got = transpose_in(geo, to_words(np.arange(length)))
    m = np.arange(length // 4)[:, None]
    p = np.arange(4)[None, :]
    assert np.array_equal(got, m + p * (length // 4))


@pytest.mark.parametrize("length", LENGTHS)
def test_reorder_is_a_permutation_into_natural_order(length):
    geo = Geometry(length)
    idx = geo.natural_index.reshape(-1)
    assert np.array_equal(np.sort(idx), np.arange(length))
    assert np.array_equal(reorder(geo, geo.natural_index).reshape(-1), np.arange(length))


def test_block_sizes_match_the_vendor_build_at_L1024():
    """The csynth hierarchy of AMD's core at L=1024: transposer PF 1/4/16/64, stages 64/16/4/1."""
    geo = Geometry(1024)
    assert geo.S == 5
    assert geo.transposer_ds == [1, 4, 16, 64]
    assert [geo.stage_d(s) for s in range(geo.S - 1)] == [64, 16, 4, 1]


def test_edge_types_match_the_vendor_formats_at_L1024():
    """The stage types csynth named: complex ap_fixed 19_5, 21_7, 23_9, 25_11, then 27_13 out."""
    edges = {e.name: e for e in edge_types(Geometry(1024))}
    assert [(edges[f"st{s}"].fmt.W, edges[f"st{s}"].fmt.int_bits) for s in range(5)] == [
        (19, 5), (21, 7), (23, 9), (25, 11), (27, 13)]
    assert edges["in"].bitwidth == 128 and edges["st4"].bitwidth == 216
    for e in edges.values():
        assert e.word.nwords_per_inst(e.bitwidth) == 1, e.name


def _all_ds(length: int) -> list[int]:
    geo = Geometry(length)
    return sorted(set(geo.transposer_ds + [geo.stage_d(s) for s in range(geo.S - 1)]))


@pytest.mark.parametrize("length", (16, 64, 256, 1024))
def test_cycle_commutator_is_the_block_transpose(length):
    """The task's tick-by-tick algorithm equals the gather, three frames back to back."""
    rng = np.random.default_rng(length + 1)
    n = length // 4
    x = rng.integers(0, 1 << 30, (3 * n, 4))
    for d in _all_ds(length):
        want = np.concatenate([commute(x[f * n:(f + 1) * n], d) for f in range(3)])
        assert np.array_equal(cycle_ref.run(x, d), want), f"D={d}"


@pytest.mark.parametrize("length", (64, 1024))
def test_cycle_commutator_survives_gaps(length):
    """Input gaps inside groups, between groups and between frames: stalls, bubbles and idles all
    occur, and the output is still the gather -- including the last frame, which only a bubble
    group can push out."""
    rng = np.random.default_rng(7)
    n = length // 4
    x = rng.integers(0, 1 << 30, (4 * n, 4))
    for d in _all_ds(length):
        want = np.concatenate([commute(x[f * n:(f + 1) * n], d) for f in range(4)])
        gaps = {int(i): int(rng.integers(1, 6 * d)) for i in rng.choice(len(x), 12, replace=False)}
        gaps[2 * n] = 20 * 4 * d                                   # a long idle between frames
        assert np.array_equal(cycle_ref.run(x, d, gaps=gaps), want), f"D={d}"


@pytest.mark.parametrize("d", (1, 4, 16, 64))
def test_cycle_commutator_moves_one_word_per_tick(d):
    """Back to back, the task writes its first word ``(R-1)*D`` ticks after its first read and then
    one word every tick: the interval is the frame length, and the drain adds only the latency."""
    n = 4 * 4 * d                                                  # 4 groups
    task = cycle_ref.CommutatorTask(d)
    fifo = __import__("collections").deque(np.arange(4 * n).reshape(n, 4))
    out: list = []
    first_out, ticks = None, 0
    while fifo or task.inside:
        ticks += 1
        assert task.step(fifo, out), "a tick was lost with data available"
        if out and first_out is None:
            first_out = ticks
    assert first_out == 3 * d + 1
    # The last word leaves (R-1)*D ticks after the last read: the drain costs the latency, no more.
    assert len(out) == n and ticks == n + 3 * d


def test_block_transpose_is_an_involution():
    """Transposing the same blocks twice is the identity -- a cheap check on the index formula."""
    for n, d in ((16, 1), (16, 4), (256, 16)):
        x = np.arange(4 * n).reshape(n, 4)
        assert np.array_equal(commute(commute(x, d), d), x)
        assert not block_transpose_index(n, d).flags.writeable


def test_pipeline_digit_reversed_output_skips_only_the_reorder():
    geo = Geometry(64)
    rng = np.random.default_rng(3)
    xr, xi = (to_words(rng.integers(-(1 << 15), 1 << 15, 64)) for _ in range(2))
    nat = pipeline(geo, xr, xi)
    dr = pipeline(geo, xr, xi, natural=False)
    assert np.array_equal(reorder(geo, dr[0]), nat[0]) and np.array_equal(reorder(geo, dr[1]), nat[1])


def test_unsupported_configurations_are_refused():
    with pytest.raises(ValueError):
        Geometry(4)
    with pytest.raises(ValueError):
        Geometry(32)
    with pytest.raises(NotImplementedError):
        Geometry(64, mode="SSR_FFT_SCALE")
