"""Shared stimulus: the burst bundle with TLAST flags, and its C++ testbench helper.

Python writes a scenario's stream once (``write_bursts``); a hand-written C++ testbench plays
it into the kernel and records what comes out (``bundle_tb.h``); Python reads the recording
(``read_bursts``).  plans/hook_first_flow.md Stage 2.

The ``-m vitis`` test round-trips bundles through Vitis csim: play into an ``hls::stream``,
record from it, compare.  Two cases:

- a missing TLAST on the LAST burst survives the round trip exactly;
- a missing TLAST on a middle burst merges it with the next one on recording -- TLAST is the
  only boundary on the wire, so that is what any kernel would see, and the helper reports it
  rather than inventing a boundary.
"""
from __future__ import annotations

import subprocess

import numpy as np
import pytest

from waveflow.build.build import BuildConfig
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import (
    StreamBurst,
    read_burst_tlast,
    read_bursts,
    write_burst_bundle,
    write_bursts,
)


def _same(a: list[StreamBurst], b: list[StreamBurst]) -> bool:
    return len(a) == len(b) and all(
        x.tlast == y.tlast and np.array_equal(np.asarray(x.words, np.uint64),
                                              np.asarray(y.words, np.uint64))
        for x, y in zip(a, b))


def test_bursts_round_trip_with_their_tlast_flags(tmp_path) -> None:
    bursts = [StreamBurst(np.arange(3)), StreamBurst(np.array([2**40 + 5, 7]), tlast=False)]
    write_bursts(bursts, tmp_path)
    assert _same(read_bursts(tmp_path), bursts)


def test_a_bundle_without_flags_reads_as_all_tlast(tmp_path) -> None:
    write_burst_bundle([np.arange(2), np.arange(4)], tmp_path)
    assert not (tmp_path / "tlast.bin").exists()       # existing bundles: unchanged bytes
    assert read_burst_tlast(tmp_path) == [True, True]


def test_rewriting_without_flags_drops_a_stale_flag_file(tmp_path) -> None:
    write_bursts([StreamBurst(np.arange(2), tlast=False)], tmp_path)
    write_burst_bundle([np.arange(2)], tmp_path)
    assert read_burst_tlast(tmp_path) == [True]


def test_flag_count_must_match_burst_count(tmp_path) -> None:
    with pytest.raises(ValueError, match="TLAST flags for"):
        write_burst_bundle([np.arange(2)], tmp_path, tlast=[True, False])


def test_streamutils_step_ships_the_helper(tmp_path) -> None:
    res = StreamUtilsStep("include").run(BuildConfig(root_dir=tmp_path))
    assert res.success, res.message
    text = (tmp_path / "include" / "bundle_tb.h").read_text(encoding="utf-8")
    assert "play_stream" in text and "record_stream" in text


_TB = r"""
#include "include/bundle_tb.h"
#include <cstdio>
void nop(int* x) { *x = 0; }   // csim needs a top; the testbench is what is tested
int main(int argc, char** argv) {
    const std::string root = argv[1];
    for (const char* tag : {"last", "middle"}) {
        hls::stream<streamutils::axi4s_word<32>> s32;
        wf::play_stream<32>(root + "/in32_" + tag, s32);
        wf::record_stream<32>(s32, root + "/out32_" + tag);
        hls::stream<streamutils::axi4s_word<64>> s64;
        wf::play_stream<64>(root + "/in64_" + tag, s64);
        wf::record_stream<64>(s64, root + "/out64_" + tag);
    }
    std::printf("BUNDLE_TB_DONE\n");
    return 0;
}
"""


@pytest.mark.vitis
def test_vitis_csim_plays_and_records_bundles(tmp_path) -> None:
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found")
    assert StreamUtilsStep("include").run(BuildConfig(root_dir=tmp_path)).success
    hdr, samples, footer = np.array([1, 2, 3]), np.arange(10, 15), np.array([99])
    big = np.array([2**40 + 1, 2**63 + 2, 3], dtype=np.uint64)
    cases = {
        # a missing TLAST on the last burst: the recording is identical
        "last": [StreamBurst(hdr), StreamBurst(samples), StreamBurst(footer, tlast=False)],
        # a missing TLAST mid-stream: the samples and the footer arrive as one burst
        "middle": [StreamBurst(hdr), StreamBurst(samples, tlast=False), StreamBurst(footer)],
    }
    for tag, bursts in cases.items():
        write_bursts(bursts, tmp_path / f"in32_{tag}")
        write_bursts(bursts[:2] + [StreamBurst(big, bursts[2].tlast)], tmp_path / f"in64_{tag}")
        (tmp_path / f"out32_{tag}").mkdir()
        (tmp_path / f"out64_{tag}").mkdir()
    (tmp_path / "tb.cpp").write_text(_TB, encoding="utf-8")
    (tmp_path / "run.tcl").write_text(
        "open_project -reset bundle_proj\nset_top nop\n"
        'add_files -tb tb.cpp -cflags "-I."\n'
        'open_solution -reset "solution1"\nset_part {xc7z020clg484-1}\ncreate_clock -period 10\n'
        f'csim_design -argv "{tmp_path.as_posix()}"\nexit 0\n', encoding="utf-8")
    try:
        toolchain.run_vitis_hls(tmp_path / "run.tcl", work_dir=tmp_path)
    except subprocess.CalledProcessError as e:
        pytest.fail(((e.stdout or "") + (e.stderr or ""))[-3000:])

    for w in ("32", "64"):
        last = read_bursts(tmp_path / f"in{w}_last")
        assert _same(read_bursts(tmp_path / f"out{w}_last"), last), f"{w}-bit, missing TLAST last"
        mid_in = read_bursts(tmp_path / f"in{w}_middle")
        merged = [mid_in[0], StreamBurst(np.concatenate([mid_in[1].words, mid_in[2].words]))]
        assert _same(read_bursts(tmp_path / f"out{w}_middle"), merged), f"{w}-bit, missing TLAST mid"
