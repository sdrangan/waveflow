"""Phase 3 elem-method conformance — Python golden vs Vitis csim, bit-exact.

Exercises the random-access element primitives ``elem_read<W>`` / ``elem_write<W>`` (the word-granular
gather/scatter primitive Phase 4's Gather consumes).  Each case lays the array with the Python golden
(``arrayutils.write_array``), then in Vitis csim: (1) ``elem_read(pack(v), i) == v[i]`` for every index
(random single-element read), and (2) ``elem_write`` rebuilds the packed words element-by-element
(lane RMW); the re-emitted words must equal the Python golden bit-for-bit.

Cases keep ``N`` a multiple of ``LW`` (whole words) so the elem_write comparison has no partial-tail
lane ambiguity.  Reuses the ``tests/hw/test_arrayutils_lane_vitis.py`` harness pattern.
"""
import subprocess
from pathlib import Path

import numpy as np
import pytest

from waveflow.build.build import BuildConfig
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import gen_array_utils, read_array, write_array
from waveflow.hw.dataschema import IntField
from waveflow.toolchain import toolchain

TEST_DIR = Path(__file__).parent
RESOURCE_DIR = TEST_DIR / "resources"
ELEM_CPP_PATH = RESOURCE_DIR / "arrayutils_elem_roundtrip_test.cpp"
ROUNDTRIP_TCL_PATH = RESOURCE_DIR / "arrayutils_roundtrip_run.tcl"


def _s(bw: int):
    return IntField.specialize(bitwidth=bw, signed=True, include_dir="include")


def _int_data(bw: int, n: int) -> np.ndarray:
    lo, hi = -(1 << (bw - 1)), (1 << (bw - 1)) - 1
    step = (hi - lo) // (n - 1)
    vals = [lo + step * i for i in range(n)]
    vals[-1] = hi
    return np.array(vals, dtype=np.int64)


# (id, elem_type, word_bw, length, data). LW = pf = word_bw//elem_bw; N is a multiple of LW.
def _cases():
    return [
        ("s32_w64", _s(32), 64, 8, _int_data(32, 8)),    # pf=2 (LW=2) — the P4 gather shape
        ("s16_w64", _s(16), 64, 8, _int_data(16, 8)),    # pf=4 (LW=4)
        ("s32_w32", _s(32), 32, 5, _int_data(32, 5)),    # pf=1 (LW=1, one element per word)
    ]


def _run_vitis_tcl(tcl_path: Path, work_dir: Path, failure_prefix: str) -> None:
    try:
        toolchain.run_vitis_hls(tcl_path, work_dir=work_dir)
    except RuntimeError as exc:
        pytest.skip(f"Vitis execution unavailable in current setup: {exc}")
    except subprocess.CalledProcessError as exc:
        pytest.fail(
            f"{failure_prefix}\n"
            f"Command: {exc.cmd}\nReturn code: {exc.returncode}\n"
            f"Stdout:\n{exc.stdout}\nStderr:\n{exc.stderr}"
        )


@pytest.mark.vitis
@pytest.mark.parametrize("case", _cases(), ids=[c[0] for c in _cases()])
def test_arrayutils_elem_roundtrip_vitis(tmp_path: Path, case):
    name, elem_type, word_bw, length, data = case

    vitis_path = toolchain.find_vitis_path()
    if not vitis_path:
        pytest.skip("Vitis installation not found; skipping arrayutils elem integration test.")

    in_words = np.asarray(write_array(data, elem_type=elem_type, word_bw=word_bw))
    save_dtype = np.uint32 if word_bw <= 32 else np.uint64

    in_words_path = tmp_path / "array_words.txt"
    out_words_path = tmp_path / "array_words_out.txt"
    np.savetxt(in_words_path, in_words.astype(save_dtype), fmt="%u")

    cfg = BuildConfig(root_dir=tmp_path)
    generated_header = gen_array_utils(elem_type, [word_bw], cfg=cfg, streamutils_dir="include")
    StreamUtilsStep(output_dir="include").run(cfg)

    header_include = generated_header.relative_to(tmp_path).as_posix()
    namespace_name = generated_header.stem
    cpp_src = (
        ELEM_CPP_PATH.read_text(encoding="utf-8")
        .replace("__HEADER__", header_include)
        .replace("__NAMESPACE__", namespace_name)
        .replace("__WORD_BW__", str(word_bw))
        .replace("__ARRAY_LEN__", str(length))
        .replace("__NWORDS__", str(in_words.shape[0]))
    )
    (tmp_path / "arrayutils_elem_roundtrip_test.cpp").write_text(cpp_src, encoding="utf-8")

    tcl_src = ROUNDTRIP_TCL_PATH.read_text(encoding="utf-8").replace(
        "arrayutils_roundtrip_test.cpp", "arrayutils_elem_roundtrip_test.cpp"
    )
    (tmp_path / "arrayutils_elem_roundtrip_run.tcl").write_text(tcl_src, encoding="utf-8")

    _run_vitis_tcl(
        tmp_path / "arrayutils_elem_roundtrip_run.tcl",
        work_dir=tmp_path,
        failure_prefix=f"Vitis execution failed for arrayutils elem roundtrip case {name!r}.",
    )

    out_words = np.atleast_1d(np.loadtxt(out_words_path, dtype=save_dtype))

    # elem_write must reproduce the Python golden words bit-for-bit.
    assert np.array_equal(out_words, in_words.astype(save_dtype)), (
        f"{name}: elem_write words differ from the Python golden."
    )
    # Numeric round-trip: the rebuilt words decode back to the source elements.
    got = np.asarray(read_array(out_words, elem_type=elem_type, word_bw=word_bw, shape=length))
    assert np.array_equal(got, np.asarray(data).astype(got.dtype))
