"""Tests for waveflow.utils.burst_io — the (words, bounds, meta) burst-bundle format."""
import json

import numpy as np
import pytest

from waveflow.utils.burst_io import (
    BOUNDS_NAME,
    META_NAME,
    WORDS_NAME,
    read_burst_bundle,
    write_burst_bundle,
)
from waveflow.utils.vcd import AxisBurst


def test_roundtrip_multi_burst(tmp_path):
    bundle = tmp_path / "cmd"
    bursts = [
        np.array([1, 2, 3], dtype=np.uint32),
        np.array([10], dtype=np.uint32),
        np.array([100, 200], dtype=np.uint32),
    ]
    write_burst_bundle(bursts, bundle)
    out = read_burst_bundle(bundle)

    assert len(out) == len(bursts)
    for got, exp in zip(out, bursts):
        assert got.dtype == np.dtype("<u8")
        np.testing.assert_array_equal(got, exp)


def test_bundle_members_and_manifest(tmp_path):
    bundle = tmp_path / "cmd"
    write_burst_bundle([np.arange(3), np.arange(1), np.arange(2)], bundle)

    assert (bundle / WORDS_NAME).exists()
    assert (bundle / BOUNDS_NAME).exists()
    meta = json.loads((bundle / META_NAME).read_text())
    assert meta["word_bytes"] == 8
    assert meta["n_bursts"] == 3
    assert meta["n_words"] == 6

    # bounds are cumulative end-indices; words is the flat concatenation.
    np.testing.assert_array_equal(np.fromfile(bundle / BOUNDS_NAME, dtype="<u8"), [3, 4, 6])
    assert np.fromfile(bundle / WORDS_NAME, dtype="<u8").size == 6


def test_roundtrip_single_continuous_burst(tmp_path):
    # has_tlast=False stream => one burst, bounds is a single end entry.
    bundle = tmp_path / "cont"
    data = np.arange(9, dtype=np.uint64)
    write_burst_bundle([data], bundle)
    np.testing.assert_array_equal(np.fromfile(bundle / BOUNDS_NAME, dtype="<u8"), [9])
    out = read_burst_bundle(bundle)
    assert len(out) == 1
    np.testing.assert_array_equal(out[0], data)


def test_empty_list_roundtrips(tmp_path):
    bundle = tmp_path / "empty"
    write_burst_bundle([], bundle)
    assert np.fromfile(bundle / WORDS_NAME, dtype="<u8").size == 0
    assert np.fromfile(bundle / BOUNDS_NAME, dtype="<u8").size == 0
    assert read_burst_bundle(bundle) == []


def test_preserves_64bit_words(tmp_path):
    # A 64-bit stream word packs two 32-bit fields; uint32 storage would truncate it.
    bundle = tmp_path / "wide"
    val = (4096 << 32) | 64  # e.g. dst_off=4096, src_off=64
    write_burst_bundle([np.array([val], dtype=np.uint64)], bundle)
    out = read_burst_bundle(bundle)
    assert int(out[0][0]) == val


def test_flattens_nd(tmp_path):
    bundle = tmp_path / "nd"
    # a 2-D int64 array flattens row-major to uint64 words
    write_burst_bundle([np.array([[1, 2], [3, 4]], dtype=np.int64)], bundle)
    out = read_burst_bundle(bundle)
    assert len(out) == 1
    np.testing.assert_array_equal(out[0], [1, 2, 3, 4])


def test_corrupt_bounds_final_mismatch_raises(tmp_path):
    bundle = tmp_path / "bad"
    write_burst_bundle([np.array([1, 2, 3], dtype=np.uint64)], bundle)
    # Rewrite bounds to claim 2 words while words.bin has 3 (leave meta consistent with words).
    np.array([2], dtype="<u8").tofile(bundle / BOUNDS_NAME)
    with pytest.raises(ValueError, match="final bound"):
        read_burst_bundle(bundle)


def test_non_decreasing_bounds_enforced(tmp_path):
    bundle = tmp_path / "unsorted"
    write_burst_bundle([np.arange(5, dtype=np.uint64)], bundle)
    np.array([3, 2, 5], dtype="<u8").tofile(bundle / BOUNDS_NAME)  # 2 < 3
    with pytest.raises(ValueError, match="non-decreasing"):
        read_burst_bundle(bundle)


def test_manifest_word_count_mismatch_raises(tmp_path):
    bundle = tmp_path / "stale_meta"
    write_burst_bundle([np.arange(4, dtype=np.uint64)], bundle)
    meta = json.loads((bundle / META_NAME).read_text())
    meta["n_words"] = 99
    (bundle / META_NAME).write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="n_words"):
        read_burst_bundle(bundle)


def test_reads_without_manifest(tmp_path):
    # meta.json is optional on read: the binaries alone are sufficient.
    bundle = tmp_path / "no_meta"
    write_burst_bundle([np.arange(3, dtype=np.uint64), np.arange(2, dtype=np.uint64)], bundle)
    (bundle / META_NAME).unlink()
    out = read_burst_bundle(bundle)
    assert len(out) == 2
    np.testing.assert_array_equal(out[0], [0, 1, 2])
    np.testing.assert_array_equal(out[1], [0, 1])


def test_axis_burst_from_dict_and_derived():
    d = {
        "data": np.array([7, 8, 9], dtype=np.uint32),
        "start_idx": 4,
        "tstart": 40.0,
        "beat_type": [0, 1, 0, 2, 0],  # 3 transfers, 1 idle, 1 stall
    }
    b = AxisBurst.from_dict(d)
    assert b.start_idx == 4
    assert b.tstart == 40.0
    assert b.n_transfers == 3
    assert b.n_beats == 5
    np.testing.assert_array_equal(b.data, [7, 8, 9])
