"""examples/systolic: the systolic engine and its memory wrapper in their own harnesses
(docs/examples/systolic/test.md).

* fast: the engine's pysim matches the bit-exact model on six commands (panels, keep_a, A^H with
  panels, one tile, kp = 1), and the wrapper's pysim writes C exactly, at 64- and 128-bit words;
* ``-m xsi``: the engine at RTL is bit-exact and runs 8x8x32 back to back at 171 cycles a job with
  64-bit words, 94 with 128-bit words (Vitis 2025.1); the wrapper at RTL writes a 32x32x64 product
  exactly at either width.
"""

from __future__ import annotations

import numpy as np
import pytest

from examples.systolic import engine_harness as EH
from examples.systolic import engine_mem_harness as EMH
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import read_burst_bundle


def _run(dag, root, through):
    res = dag.run(EH.build_config(root), through=through)
    bad = {k: r.message[-2000:] for k, r in res.items() if not r.success}
    assert not bad, bad


@pytest.mark.parametrize("word_bits", [64, 128])
def test_engine_pysim_matches_the_model(tmp_path, word_bits):
    _run(EH.build_dag({"word_bits": word_bits}), tmp_path, "golden")


@pytest.mark.parametrize("word_bits", [64, 128])
def test_wrapper_pysim_writes_c_exactly(tmp_path, word_bits):
    cfg = {"word_bits": word_bits}
    _run(EMH.build_dag(cfg), tmp_path, "golden")
    mem = tmp_path / EMH.WORK_DIR / EMH.TOP / "pysim_run" / "vectors" / "mem"
    words = np.asarray(read_burst_bundle(mem)[0], np.uint64)
    assert EMH.check_c(words, **cfg) == [0, 0, 0]


def _pysim_run(make, kind):
    """One pysim run in its own harness: its length in cycles, and its mismatches with the golden."""
    import tempfile
    from pathlib import Path

    from waveflow.build.system_dag import gather_recordings, golden_mismatches
    from waveflow.hw.harness import harness

    root = Path(tempfile.mkdtemp())
    s = harness(make(), workload=kind, n_cycles=90000)
    s.write_scenario(root)
    s.run()
    gather_recordings(s, root, root / "traces")
    return s.sim.env.now / s.clock().period, golden_mismatches(root / "traces", s.golden_outputs()), s


@pytest.mark.parametrize("word_bits", [64, 128])
@pytest.mark.parametrize("kind", ["test", "steady", "short", "batch"])
def test_flat_pysim_is_exact_and_times_like_the_tasks(word_bits, kind):
    """SystolicEngine(pysim='flat') -- one process, the tasks passive -- gives the model's words and
    the tasks' timing (within 3%: it reads, computes and writes an output as the tasks do, at the
    core's calibrated phase)."""
    t_tasks, bad, _ = _pysim_run(lambda: EH.engine(word_bits=word_bits), kind)
    t_flat, bad_flat, _ = _pysim_run(lambda: EH.engine(word_bits=word_bits, pysim="flat"), kind)
    assert not bad and not bad_flat, (bad, bad_flat)
    assert abs(t_flat - t_tasks) <= 0.03 * t_tasks, (t_flat, t_tasks)


@pytest.mark.parametrize("word_bits", [64, 128])
def test_flat_engine_inside_the_wrapper_writes_c_exactly(word_bits):
    cfg = {"word_bits": word_bits}
    for kind in ("test", "big"):
        t_tasks, _, _ = _pysim_run(lambda: EMH.engine_mem(**cfg), kind)
        t_flat, bad, s = _pysim_run(lambda: EMH.engine_mem(pysim="flat", **cfg), kind)
        assert not bad
        assert abs(t_flat - t_tasks) <= 0.03 * t_tasks, (kind, t_flat, t_tasks)


@pytest.mark.parametrize("word_bits", [64, 128])
@pytest.mark.parametrize("kind", ["test", "big", "square_64"])
def test_tiles_pysim_is_exact_and_times_like_the_tasks(word_bits, kind):
    """SystolicEngineMem(pysim='tiles') -- one process, one coalesced memory read a tile, one
    product a job -- writes C exactly, answers every job, and finishes within 2% of the tasks' pysim,
    with far fewer events."""
    cfg = {"word_bits": word_bits}
    t_tasks, _, s_tasks = _pysim_run(lambda: EMH.engine_mem(**cfg), kind)
    t_tiles, bad, s = _pysim_run(lambda: EMH.engine_mem(pysim="tiles", **cfg), kind)
    assert not bad
    assert abs(t_tiles - t_tasks) <= 0.02 * t_tasks, (t_tiles, t_tasks)
    assert next(s.sim.env._eid) * 4 < next(s_tasks.sim.env._eid)


def test_tiles_pysim_writes_c_exactly(tmp_path):
    root = tmp_path
    from waveflow.hw.harness import harness

    s = harness(EMH.engine_mem(word_bits=128, pysim="tiles"), workload="test", n_cycles=10**6)
    s.write_scenario(root)
    s.run()
    words = np.asarray(read_burst_bundle(root / "vectors" / "mem")[0], np.uint64)
    assert EMH.check_c(words, "test", word_bits=128) == [0, 0, 0]


def test_square_workload_is_exact():
    """The n x 32 x n workload docs/evaluation/sim_speed/systolic.md sweeps: the flat pysim writes C
    exactly, and a size the tiles do not divide is refused."""
    from pathlib import Path
    import tempfile

    from waveflow.hw.harness import harness

    root = Path(tempfile.mkdtemp())
    s = harness(EMH.engine_mem(word_bits=128, pysim="flat"), workload="square_64", n_cycles=10**6)
    s.write_scenario(root)
    s.run()
    words = np.asarray(read_burst_bundle(root / "vectors" / "mem")[0], np.uint64)
    assert EMH.check_c(words, "square_64", word_bits=128) == [0]
    with pytest.raises(ValueError, match="multiple"):
        EMH.engine_mem().jobs("square_40")


def test_flat_pysim_refuses_an_unknown_granularity():
    with pytest.raises(ValueError, match="pysim"):
        EH.engine(pysim="coarse")
    with pytest.raises(ValueError, match="pysim"):
        EMH.engine_mem(pysim="coarse")


REPO = __import__("pathlib").Path(__file__).resolve().parents[2]


def test_measured_runs_are_exact_and_meet_the_clock():
    """examples/systolic/measured.json is the RTL evidence the pages quote: every run exact, and every
    design within its 4 ns in csynth's estimate (a design that misses it measures another target)."""
    import json

    runs = json.loads((REPO / "examples/systolic/measured.json").read_text(encoding="utf-8"))["runs"]
    for name, r in runs.items():
        assert r["exact"], name
        assert r["resources"]["est_clock_ns"] <= 4.0, (name, r["resources"]["est_clock_ns"])


def test_timing_page_quotes_the_measurements():
    """The intervals and completion times docs/examples/systolic/timing.md states are measured.json's."""
    import json

    runs = json.loads((REPO / "examples/systolic/measured.json").read_text(encoding="utf-8"))["runs"]
    page = (REPO / "docs/examples/systolic/timing.md").read_text(encoding="utf-8")
    for name in ("eng64_steady", "eng128_steady", "eng64_short", "eng128_short"):
        (iv,) = runs[name]["interval"]
        assert f"**{iv}**" in page, (name, iv)
    for name in ("mem64_big", "mem128_big"):
        (done,) = runs[name]["done"]
        assert f"**{done:,}**" in page, (name, done)


def _xsi(tmp_path_factory, name):
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis HLS not found; the XSI gate needs it")
    if not toolchain.find_vivado_path():
        pytest.skip("Vivado (xsim) not found; the XSI gate needs it")
    return tmp_path_factory.mktemp(name)


@pytest.mark.xsi
@pytest.mark.parametrize("word_bits, interval", [(64, 171), (128, 94)])
def test_xsi_engine_exact_and_interval(tmp_path_factory, word_bits, interval):
    root = _xsi(tmp_path_factory, f"eng{word_bits}")
    cfg = {"word_bits": word_bits}
    _run(EH.build_dag(cfg), root, "compare")                       # bit-exact, six commands
    _run(EH.build_dag(cfg, workload="steady"), root, "compare")    # 4 x 8x8x32 back to back
    cyc = np.fromfile(root / EH.WORK_DIR / EH.TOP / "xsi_run" / "vectors" / "s_out" / "cycles.bin",
                      dtype="<u8")
    per = 1 + 8 * 32 * 32 // word_bits                             # a Resp, then C (32-bit elements)
    ends = [int(cyc[(j + 1) * per - 1]) for j in range(4)]
    assert np.diff(ends).tolist() == [interval] * 3


@pytest.mark.xsi
@pytest.mark.parametrize("word_bits", [64, 128])
def test_xsi_wrapper_writes_a_large_product_exactly(tmp_path_factory, word_bits):
    root = _xsi(tmp_path_factory, f"engmem{word_bits}")
    cfg = {"word_bits": word_bits}
    _run(EMH.build_dag(cfg, workload="big"), root, "compare")
    mem = root / EMH.WORK_DIR / EMH.TOP / "xsi_run" / "vectors" / "mem"
    words = np.asarray(read_burst_bundle(mem)[0], np.uint64)
    assert EMH.check_c(words, "big", **cfg) == [0]
