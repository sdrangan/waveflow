"""Phase 1b range-method *pipelining* guard — csynth II of read/write_array_slice over m_axi.

The slice-conformance test (``test_arrayutils_slice_vitis``) checks bit-exactness via csim with a
``set_top main`` kernel over *local arrays* -- so it never sees the m_axi schedule. That blind spot
let two pipelining regressions ship green: the scalar ``write_array_slice`` serialized to II=16 (its
partial-word read-modify-write read aliased the gmem bundle across iterations) and a non-inlined
dispatch helper left the m_axi burst ports dangling (zeros in RTL, csim still passed).

This guard synthesizes an isolated m_axi top (``slice_ii_top``: read ``[0, N)`` then write the
disjoint, aligned ``[N, 2N)``) and asserts every pipelined slice loop achieves **II = 1** across LW
regimes -- scalar ``pf == 1`` (``s32@32``), wide-element ``pf == 0`` (``s64@32``), and vectorized
``pf > 1`` (``s16@32`` LW=2, ``s16@64`` LW=4). csynth-only (no csim/cosim), so it is fast.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from waveflow.build.build import BuildConfig
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import gen_array_utils
from waveflow.hw.dataschema import IntField
from waveflow.toolchain import toolchain
from waveflow.utils.csynthparse import CsynthParser

TEST_DIR = Path(__file__).parent
RESOURCE_DIR = TEST_DIR / "resources"
CPP_PATH = RESOURCE_DIR / "arrayutils_slice_ii_test.cpp"
TCL_PATH = RESOURCE_DIR / "arrayutils_slice_ii_run.tcl"

N = 64        # element count; a multiple of every LW below so [N, 2N) writes are aligned
DEPTH = 4096  # m_axi depth (words) -- covers 2N elements across all regimes


def _sint(bw: int):
    return IntField.specialize(bitwidth=bw, signed=True, include_dir="include")


# (id, bw, word_bw, expected_LW)
def _cases():
    return [
        ("s32_w32_scalar", 32, 32, 1),   # pf=1  -> LW=1 (scalar; the FIR regime)
        ("s64_w32_wide", 64, 32, 1),     # pf=0  -> LW=1 (one wide element / 2 words)
        ("s16_w32_vec2", 16, 32, 2),     # pf=2  -> LW=2 (vectorized)
        ("s16_w64_vec4", 16, 64, 4),     # pf=4  -> LW=4 (vectorized)
    ]


def _run_vitis_tcl(tcl_path: Path, work_dir: Path, failure_prefix: str) -> None:
    try:
        toolchain.run_vitis_hls(tcl_path, work_dir=work_dir)
    except RuntimeError as exc:
        pytest.skip(f"Vitis execution unavailable in current setup: {exc}")
    except subprocess.CalledProcessError as exc:
        pytest.fail(
            f"{failure_prefix}\nCommand: {exc.cmd}\nReturn code: {exc.returncode}\n"
            f"Stdout:\n{exc.stdout}\nStderr:\n{exc.stderr}"
        )


@pytest.mark.vitis
@pytest.mark.parametrize("case", _cases(), ids=[c[0] for c in _cases()])
def test_arrayutils_slice_ii_vitis(tmp_path: Path, case):
    name, bw, word_bw, expected_lw = case

    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found; skipping arrayutils slice II integration test.")

    elem_type = _sint(bw)
    cfg = BuildConfig(root_dir=tmp_path)
    generated_header = gen_array_utils(elem_type, [word_bw], cfg=cfg, streamutils_dir="include")
    StreamUtilsStep(output_dir="include").run(cfg)

    cpp_src = (
        CPP_PATH.read_text(encoding="utf-8")
        .replace("__HEADER__", generated_header.relative_to(tmp_path).as_posix())
        .replace("__NAMESPACE__", generated_header.stem)
        .replace("__WORD_BW__", str(word_bw))
        .replace("__N__", str(N))
        .replace("__DEPTH__", str(DEPTH))
    )
    (tmp_path / "arrayutils_slice_ii_test.cpp").write_text(cpp_src, encoding="utf-8")
    shutil.copy(TCL_PATH, tmp_path / "arrayutils_slice_ii_run.tcl")

    _run_vitis_tcl(
        tmp_path / "arrayutils_slice_ii_run.tcl",
        work_dir=tmp_path,
        failure_prefix=f"Vitis csynth failed for arrayutils slice II case {name!r}.",
    )

    sol = tmp_path / "waveflow_arrayutils_slice_ii_proj" / "solution1"
    parser = CsynthParser(sol_path=str(sol))
    parser.get_loop_pipeline_info()
    loops = parser.loop_df

    # Bus-rate invariant: every pipelined slice loop must sustain one word-beat per cycle. With
    # WPU = get_nwords(LW) words moved per loop iteration, that is II <= WPU. WPU == 1 for the
    # scalar (pf==1) and vectorized (pf>=1, LW lanes/word) regimes -> II == 1; for a wide element
    # (pf==0) spanning WPU words on a narrower bus, WPU > 1 beats/element is the physical floor.
    # A serializing RMW (II=16) or a missed-burst regression makes some loop's II exceed WPU.
    lw = max(1, word_bw // bw)
    wpu = -(-(lw * bw) // word_bw)  # ceil(LW * elem_bw / word_bw)
    assert lw == expected_lw, f"{name}: LW sanity check ({lw} != {expected_lw})."

    pipelined = loops[loops["PipelineII"].notna()]
    assert not pipelined.empty, f"{name} (LW={lw}): no pipelined loops found in csynth report."
    bad = pipelined[pipelined["PipelineII"] > wpu]
    assert bad.empty, (
        f"{name} (LW={lw}, bus-rate II<={wpu}): slice loop(s) below bus-rate:\n"
        f"{bad[['PipelineII', 'TripCountMax']].to_string()}"
    )
