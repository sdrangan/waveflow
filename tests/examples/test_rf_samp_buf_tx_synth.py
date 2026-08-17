"""The TX playout buffer through Vitis HLS — the module set, and the wider word geometry.

The mirror of ``test_rf_samp_buf_rx_synth.py``.  csynth reporting OK is not evidence: a kernel whose
argument was DCE'd still reports success and still writes a top, with nothing under it.  So the
*module set* is asserted — both task modules and the two depth-1 progress FIFOs — because with two
tasks and two channels there are four things that could vanish.

``samp_per_word > 1`` is built into its own directory, because the recorded XSI cycle count belongs to
``samp_per_word == 1`` and nothing here may quietly move it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from examples.rf_samp_buf_tx.rf_samp_buf_tx_build import (
    TOP,
    XSI_SAMP_PER_WORD,
    build_rf_samp_buf_tx_dag,
    elab_params,
    generate_dut,
)
from waveflow.build.build import BuildConfig

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_samp_buf_tx"
PROJ = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"


def _module_set(proj: Path) -> set[str]:
    assert proj.is_dir(), f"no RTL at {proj}"
    return {p.stem for p in proj.glob("*.v")}


@pytest.mark.vitis
def test_csynth_produces_both_tasks_and_both_progress_fifos():
    """The gated geometry: csynth, then assert the RTL *contains the work*."""
    dag = build_rf_samp_buf_tx_dag()
    results = dag.run(BuildConfig(root_dir=ROOT, params={}), through="csynth")
    failed = [n for n, r in results.items() if not r.success]
    assert not failed, f"build steps failed: {failed}"

    mods = _module_set(PROJ)
    assert TOP in mods, f"no top module in {sorted(mods)}"
    for task in ("loader", "player"):
        assert any(f"rf_samp_buf_{task}_task" in m for m in mods), (
            f"the {task} task module is absent from {sorted(mods)} — a DCE'd task still reports "
            f"csynth OK, and with two tasks there are two things that could vanish")
    # BOTH progress channels are declared depth 1, and they are INTERNAL channels, so unlike a
    # boundary port those declarations are honoured.  Vitis emits ONE fifo module and instantiates it
    # twice -- same width, same depth, so the module is shared -- which is why the count is checked
    # in the generated top rather than in the module set.
    assert any("fifo_w16_d1" in m for m in mods), (
        f"the depth-1 progress FIFO is not in {sorted(mods)}; a deeper one would only ever serve "
        f"stale positions")
    top_cpp = (ROOT / "gen" / f"{TOP}.cpp").read_text(encoding="utf-8")
    assert top_cpp.count("#pragma HLS STREAM") == 2, (
        f"expected two depth-1 channels in the generated top -- the loader tells the player how far "
        f"it has filled, the player tells the loader how far it has played. One would mean a "
        f"direction was lost. Got: {top_cpp}")
    assert top_cpp.count("depth=1") == 2


@pytest.mark.vitis
@pytest.mark.parametrize("samp_per_word", [2, 4])
def test_the_wider_word_geometry_synthesizes(tmp_path, samp_per_word):
    """``samp_per_word > 1`` is hardware, not a Python parameter.

    What this proves is narrow and worth being precise about: the widened bodies **compile and
    produce a datapath**.  It does not measure the throughput the widening buys — that needs an RTL
    run, which is the XSI gate, recorded for ``samp_per_word == 1`` only.
    """
    from waveflow.toolchain import toolchain

    generate_dut(tmp_path, samp_per_word=samp_per_word)
    assert (tmp_path / "gen" / f"{TOP}.cpp").is_file()

    result = toolchain.run_vitis_hls(tmp_path / f"{TOP}.tcl", work_dir=tmp_path,
                                     capture_output=True)
    out = (result.stdout or "") + (result.stderr or "")
    assert "WAVEFLOW_CSYNTH_OK" in out, (
        f"csynth of the {samp_per_word}-samples-per-word geometry failed:\n{out[-3000:]}")

    mods = _module_set(tmp_path / f"{TOP}_proj" / "solution1" / "syn" / "verilog")
    assert TOP in mods
    for task in ("loader", "player"):
        assert any(f"rf_samp_buf_{task}_task" in m for m in mods), (
            f"the {task} task vanished at samp_per_word={samp_per_word}: {sorted(mods)}")


def test_the_gated_geometry_is_one_sample_per_word():
    """The XSI cycle gate is recorded for this geometry, so it is stated rather than defaulted.

    Toolchain-free on purpose: if the gated geometry ever changes, this fails in the dev loop rather
    than only on a machine with Vitis installed.
    """
    assert XSI_SAMP_PER_WORD == 1
    assert elab_params()["samp_per_word"] == 1
    assert elab_params(4) == {"bitwidth": 64, "samp_per_word": 4, "depth": 2048,
                              "horizon_margin": 16}
