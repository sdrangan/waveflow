from pathlib import Path

import numpy as np
import pytest

from waveflow.build.build import BuildConfig
from waveflow.hw.arrayutils import gen_array_utils, get_nwords, read_uint32_file, write_array, write_uint32_file
from waveflow.hw.dataschema import FloatField, IntField


F32 = FloatField.specialize(bitwidth=32)
S16 = IntField.specialize(bitwidth=16, signed=True)


def test_write_uint32_file_roundtrip_int16(tmp_path: Path):
    data = np.array([-32768, -12345, -17, -1, 0, 1, 23, 255, 1024, 32767], dtype=np.int16)
    out_path = write_uint32_file(data, elem_type=S16, file_path=tmp_path / "arr.bin")

    words = np.fromfile(out_path, dtype="<u4")
    expected = np.asarray(write_array(data, elem_type=S16, word_bw=32), dtype="<u4")
    got = np.asarray(read_uint32_file(out_path, elem_type=S16, shape=data.size), dtype=np.int16)

    assert np.array_equal(words, expected)
    assert np.array_equal(got, data)


def test_write_uint32_file_nwrite_selects_prefix(tmp_path: Path):
    data = np.arange(7, dtype=np.int16)
    out_path = write_uint32_file(data, elem_type=S16, file_path=tmp_path / "prefix.bin", nwrite=5)

    words = np.fromfile(out_path, dtype="<u4")
    expected = np.asarray(write_array(data[:5], elem_type=S16, word_bw=32), dtype="<u4")
    got = np.asarray(read_uint32_file(out_path, elem_type=S16, shape=5), dtype=np.int16)

    assert np.array_equal(words, expected)
    assert np.array_equal(got, data[:5])


def test_write_uint32_file_write_slice_selects_subarray(tmp_path: Path):
    data = np.arange(12, dtype=np.float32).reshape(4, 3)
    out_path = write_uint32_file(
        data,
        elem_type=F32,
        file_path=tmp_path / "slice.bin",
        write_slice=np.s_[1:3, :],
    )

    words = np.fromfile(out_path, dtype="<u4")
    expected_data = data[1:3, :]
    expected = np.asarray(write_array(expected_data, elem_type=F32, word_bw=32), dtype="<u4")
    got = np.asarray(read_uint32_file(out_path, elem_type=F32, shape=expected_data.shape), dtype=np.float32)

    assert np.array_equal(words, expected)
    assert np.array_equal(got, expected_data)


def test_write_uint32_file_rejects_conflicting_selection_args(tmp_path: Path):
    data = np.arange(4, dtype=np.int16)

    try:
        write_uint32_file(
            data,
            elem_type=S16,
            file_path=tmp_path / "invalid.bin",
            write_slice=np.s_[1:3],
            nwrite=2,
        )
    except ValueError as exc:
        assert "Specify only one of write_slice or nwrite." in str(exc)
    else:
        raise AssertionError("Expected ValueError when both write_slice and nwrite are provided.")


def test_nwords_matches_serialized_length_int16() -> None:
    data = np.arange(7, dtype=np.int16)
    packed = np.asarray(write_array(data, elem_type=S16, word_bw=32))

    assert get_nwords(elem_type=S16, word_bw=32, shape=data.shape) == int(packed.shape[0])


def test_nwords_matches_serialized_length_float_matrix() -> None:
    data = np.arange(12, dtype=np.float32).reshape(3, 4)
    packed = np.asarray(write_array(data, elem_type=F32, word_bw=64))

    assert get_nwords(elem_type=F32, word_bw=64, shape=data.shape) == int(packed.shape[0])


def test_gen_array_utils_writes_companion_tb_header(tmp_path: Path):
    Int16Inc = IntField.specialize(bitwidth=16, signed=True, include_dir="include")

    out_path = gen_array_utils(Int16Inc, [32], cfg=BuildConfig(root_dir=tmp_path), streamutils_dir="common")
    tb_path = tmp_path / "include" / "int16_array_utils_tb.h"

    content = out_path.read_text(encoding="utf-8")
    tb_content = tb_path.read_text(encoding="utf-8")

    assert out_path == tmp_path / "include" / "int16_array_utils.h"
    assert tb_path.exists()
    assert "#ifndef INCLUDE_INT16_ARRAY_UTILS_TB_H" in tb_content
    assert '#include "../common/streamutils_tb.h"' in tb_content
    assert '#include "int16_array_utils.h"' in tb_content
    assert '#include "../common/streamutils_hls.h"' in content
    assert '#include <hls_stream.h>' in content
    assert '#if __has_include(<hls_axi_stream.h>)' in content
    assert f"namespace {out_path.stem} {{" in content
    assert f"namespace {out_path.stem} {{" in tb_content
    assert "static constexpr int value_bitwidth = 16;" in content
    assert "static constexpr int pf() {" in content
    assert "return word_bw / 16;" in content
    assert "static constexpr int get_nwords(int len) {" in content
    assert "return (len <= 0) ? 0 : ((len * value_bitwidth + word_bw - 1) / word_bw);" in content
    # The per-element *_elem_impl<W> structs are kept (lane methods + bulk loops delegate to them).
    assert "struct read_array_elem_impl {" in content
    assert "struct write_array_elem_impl {" in content
    assert "struct read_stream_elem_impl {" in content
    assert "struct read_axi4_stream_elem_impl {" in content
    assert "struct read_axi4_stream_elem_impl<32> {" in content
    assert "static void run(hls::stream<streamutils::axi4s_word<32>>& s, value_type* out, streamutils::tlast_status& tl, int n) {" in content
    assert "struct write_stream_elem_impl {" in content
    assert "struct write_axi4_stream_elem_impl {" in content
    # The public *_elem wrappers and the bulk memory read_array/write_array are retired (phase 2b).
    assert "inline void read_array_elem(" not in content
    assert "inline void write_array_elem(" not in content
    assert "inline void read_stream_elem(" not in content
    assert "inline void write_stream_elem(" not in content
    assert "inline void read_axi4_stream_elem(" not in content
    assert "inline void write_axi4_stream_elem(" not in content
    assert "inline void read_array<" not in content and "inline void write_array<" not in content
    assert "inline void read_array(const ap_uint<word_bw>* src, value_type* dst, int len)" not in content
    assert "inline void write_array(const value_type* src, ap_uint<word_bw>* dst, int len)" not in content
    # The bulk stream helpers are kept (TB push_array/pop_array lower to them).
    assert "inline void read_stream(hls::stream<ap_uint<word_bw>>& s, value_type* dst, int len) {" in content
    assert "inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, streamutils::tlast_status& tl, int len) {" in content
    assert "inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, streamutils::tlast_status& tl, int& nread, int len) {" in content
    assert "inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, int& nread, int len) {" in content
    assert "inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, int len) {" in content
    assert "inline void write_stream(hls::stream<ap_uint<word_bw>>& s, const value_type* src, int len) {" in content
    assert "inline void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, const value_type* src, bool tlast = true, int len = pf<word_bw>()) {" in content
    # ...and they delegate to the kept *_elem_impl<W>::run structs (not the retired wrappers).
    assert "read_stream_elem_impl<word_bw>::run(s, dst + i, len - i);" in content
    assert "read_axi4_stream_elem_impl<word_bw>::run(s, dst + i, lane_tl, len - i);" in content
    assert "write_stream_elem_impl<word_bw>::run(s, src + i, len - i);" in content
    assert "const bool lane_tlast = (i + pf<word_bw>() >= len) ? tlast : false;" in content
    assert "write_axi4_stream_elem_impl<word_bw>::run(s, src + i, lane_tlast, len - i);" in content
    assert "ap_uint<32> w = src[0];" in content
    assert "dst[0] = w;" in content
    assert "tl = streamutils::tlast_status::no_tlast;" in content
    assert "nread = 0;" in content
    assert "bool stop = false;" in content
    assert "const int lane_count = ((len - i) < pf<word_bw>()) ? (len - i) : pf<word_bw>();" in content
    assert "nread += lane_count;" in content
    assert "auto axis_word = s.read();" in content
    assert "ap_uint<32> w = axis_word.data;" in content
    assert "if (axis_word.last) {" in content
    assert "tl = (i + pf<word_bw>() >= len) ? streamutils::tlast_status::tlast_at_end : streamutils::tlast_status::tlast_early;" in content
    assert "streamutils::write_axi4_word<32>(s, w, tlast);" in content
    assert "inline void read_uint32_file_array(value_type* dst, const char* file_path, int n0) {" in tb_content
    assert "inline void write_uint32_file_array(const value_type* src, const char* file_path, int n0) {" in tb_content
    assert "const int nwords = get_nwords<32>(n0);" in tb_content
    assert "words.push_back(streamutils::read_le_uint32(ifs));" in tb_content
    assert "streamutils::write_le_uint32(ofs, static_cast<uint32_t>(word));" in tb_content


def test_gen_array_utils_tb_header_uses_local_streamutils_path(tmp_path: Path):
    out_path = gen_array_utils(F32, [32], cfg=BuildConfig(root_dir=tmp_path))
    tb_path = tmp_path / "float32_array_utils_tb.h"
    content = out_path.read_text(encoding="utf-8")
    tb_content = tb_path.read_text(encoding="utf-8")

    assert out_path == tmp_path / "float32_array_utils.h"
    assert '#include "streamutils_hls.h"' in content
    assert '#include "streamutils_tb.h"' in tb_content
    assert '#include "float32_array_utils.h"' in tb_content

# ---------------------------------------------------------------------------
# Phase 4: array() factory
# ---------------------------------------------------------------------------

def test_array_factory_returns_dataarray_instance():
    from waveflow.hw.arrayutils import array
    from waveflow.hw.dataschema import DataArray
    inst = array(F32, [1.0, 2.0, 3.0])
    assert isinstance(inst, DataArray)
    assert type(inst).element_type is F32


def test_array_factory_round_trip():
    from waveflow.hw.arrayutils import array, read_array, write_array
    data = [1.0, 2.0, 3.0, 4.0]
    inst = array(F32, data)
    packed = write_array(inst, word_bw=32)
    result = read_array(packed, elem_type=F32, word_bw=32, shape=4)
    np.testing.assert_array_almost_equal(result.val, np.array(data, dtype=np.float32))


def test_write_array_accepts_dataarray_instance():
    from waveflow.hw.arrayutils import array, write_array
    inst = array(F32, [1.0, 2.0])
    packed = write_array(inst, word_bw=32)
    assert len(packed) > 0


def test_write_array_dataarray_elem_type_mismatch_raises():
    from waveflow.hw.arrayutils import array, write_array
    inst = array(F32, [1.0, 2.0])
    from waveflow.hw.dataschema import IntField
    S16 = IntField.specialize(bitwidth=16, signed=True)
    with pytest.raises(TypeError, match="elem_type mismatch"):
        write_array(inst, elem_type=S16, word_bw=32)
