"""Phase 1: HistAccel HwModule SimPy parity with the numpy golden.

Runs the synthesizable HistAccel over a SimPy DirectMMIF + MemModel and
asserts it reproduces HistogramAccel's histogram counts, plus the three
validation → status paths.
"""
from __future__ import annotations

import numpy as np
import pytest

from pathlib import Path

from examples.shared_mem.hist import MAX_NDATA, MAX_NBINS, HistError, run_sim
from examples.shared_mem.hist_build import HistTest

HIST_EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "shared_mem"


@pytest.mark.parametrize(
    "seed,ndata,nbins",
    [(7, 37, 6), (3, 100, 12), (5, 256, 32), (11, 1, 4)],
)
def test_histaccel_matches_numpy_golden(seed, ndata, nbins):
    """HistAccel (SimPy) reproduces HistogramAccel's counts over the same data."""
    # HistTest.simulate() runs the numpy HistogramAccel golden and records the
    # generated data/edges and its counts.
    ht = HistTest(seed=seed, ndata=ndata, nbins=nbins)
    ht.simulate()

    res = run_sim(ht.data, ht.bin_edges, nbins=nbins, tx_id=seed)

    assert res.status == HistError.NO_ERROR
    np.testing.assert_array_equal(res.counts, ht.counts)
    assert res.passed


def test_invalid_ndata_status():
    """ndata greater than max_ndata selects INVALID_NDATA before any memory op."""
    over = MAX_NDATA + 1
    data = np.zeros(over, dtype=np.float32)
    edges = np.sort(np.linspace(-1, 1, 5).astype(np.float32))
    res = run_sim(data, edges, nbins=6)
    assert res.status == HistError.INVALID_NDATA


def test_invalid_nbins_status():
    """nbins greater than max_nbins selects INVALID_NBINS."""
    over = MAX_NBINS + 1
    data, _ = np.random.default_rng(1).normal(size=20).astype(np.float32), None
    edges = np.sort(np.random.default_rng(1).uniform(-2, 2, size=over - 1).astype(np.float32))
    res = run_sim(data, edges, nbins=over)
    assert res.status == HistError.INVALID_NBINS


def test_address_error_status():
    """A misaligned data address selects ADDRESS_ERROR (mem_bw=32 → 4-byte words)."""
    data = np.random.default_rng(4).normal(size=30).astype(np.float32)
    edges = np.sort(np.random.default_rng(4).uniform(-2, 2, size=5).astype(np.float32))
    res = run_sim(data, edges, nbins=6, addr_misalign=1)   # 1 byte off a word boundary
    assert res.status == HistError.ADDRESS_ERROR


# Folded build-harness coverage (golden parity, gen_test_data, kernel bin-scan,
# _burst_to_jsonable); the mocked stage-runner orchestration tests were dropped
# in the build refactor.

@pytest.mark.parametrize("mem_dwidth", [32, 64, 128])
def test_hist_test_simulate_matches_expected_counts(mem_dwidth: int) -> None:
    hist_test = HistTest(seed=11, ndata=41, nbins=7, mem_dwidth=mem_dwidth)

    result = hist_test.simulate()

    assert hist_test.mem is not None
    assert hist_test.hist_accel is not None
    assert hist_test.cmd is not None
    assert hist_test.resp is not None
    assert hist_test.counts is not None
    assert hist_test.expected is not None

    assert hist_test.mem.word_size == mem_dwidth
    assert result.cmd is hist_test.cmd
    assert result.resp is hist_test.resp
    assert result.counts is hist_test.counts
    assert result.expected is hist_test.expected
    assert result.passed is True

    assert hist_test.resp.tx_id == hist_test.cmd.tx_id
    assert hist_test.resp.status is HistError.NO_ERROR
    assert hist_test.counts.dtype == np.uint32
    assert hist_test.expected.dtype == np.uint32
    assert np.array_equal(hist_test.counts, hist_test.expected)


def test_hist_test_gen_test_data_initializes_state_before_simulate() -> None:
    hist_test = HistTest(seed=5, ndata=13, nbins=4, mem_dwidth=64)

    hist_test.gen_test_data()

    assert hist_test.cmd is None
    assert hist_test.data is not None
    assert hist_test.bin_edges is not None
    assert hist_test.data.shape == (13,)
    assert hist_test.bin_edges.shape == (3,)

    result = hist_test.simulate()

    assert hist_test.cmd is not None
    assert result.passed is True


def test_hist_kernel_only_scans_programmed_bin_edges() -> None:
    compute_hook = (HIST_EXAMPLE_DIR / "hist_compute_impl.cpp").read_text(encoding="utf-8")

    assert "for (int b = 0; b < nbins - 1; ++b)" in compute_hook
    assert "for (int b = 0; b < max_nbins; b++)" not in compute_hook


def test_burst_to_jsonable_adds_fixed_width_hex_words() -> None:
    burst = {
        "addr": 0,
        "start_idx": 1,
        "tstart": 10.0,
        "data_start_idx": 2,
        "data_end_idx": 3,
        "data_tstart": 20.0,
        "data_tend": 30.0,
        "queue_wait_cycles": 1,
        "beat_type": [0, 1],
        "data": np.array([-1, 16], dtype=np.int32),
        "awlen": None,
        "arlen": 1,
    }

    result = HistTest._burst_to_jsonable(burst, data_bitwidth=32)

    assert result["data"] == [-1, 16]
    assert result["data_hex"] == ["0xffffffff", "0x00000010"]
    assert result["beat_type_names"] == ["transfer", "idle"]
