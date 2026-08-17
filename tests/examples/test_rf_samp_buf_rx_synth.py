"""The RX sample buffer through Vitis HLS — the module set, and the wider word geometry.

Two claims, and they fail differently.

* **The gated geometry synthesizes and contains a datapath.**  csynth reporting OK is not evidence:
  a kernel whose argument was DCE'd still reports success and still writes a top, with nothing under
  it.  So the *module set* is asserted — both task modules, the memory ports, and the depth-1
  progress FIFO — because with two tasks there are two things that could vanish.

* **``samp_per_word > 1`` is real hardware, not a Python parameter.**  Widening the word is the
  throughput lever (``samp_per_word / fire_cycles`` samples per cycle), and a generalization that
  only ever ran in pysim would be a claim about the model rather than about the design.  It is built
  into its own directory, because the recorded XSI cycle count belongs to ``samp_per_word == 1`` and
  nothing here may quietly move it.

There was no ``-m vitis`` test for this example before this file: ``pytest -m vitis -k rf_capture``
collected nothing, which reads as a pass in a summary line.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from examples.rf_samp_buf_rx.rf_samp_buf_rx_build import (
    TOP,
    XSI_SAMP_PER_WORD,
    build_rf_samp_buf_rx_dag,
    elab_params,
    generate_dut,
)
from waveflow.build.build import BuildConfig

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_samp_buf_rx"
PROJ = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"


def _module_set(proj: Path) -> set[str]:
    assert proj.is_dir(), f"no RTL at {proj}"
    return {p.stem for p in proj.glob("*.v")}


@pytest.mark.vitis
def test_csynth_produces_both_tasks_and_the_progress_fifo():
    """The gated geometry: csynth, then assert the RTL *contains the work*."""
    dag = build_rf_samp_buf_rx_dag()
    results = dag.run(BuildConfig(root_dir=ROOT, params={}), through="csynth")
    failed = [n for n, r in results.items() if not r.success]
    assert not failed, f"build steps failed: {failed}"

    mods = _module_set(PROJ)
    assert TOP in mods, f"no top module in {sorted(mods)}"
    for task in ("ingress", "capture"):
        assert any(f"rf_samp_buf_{task}_task" in m for m in mods), (
            f"the {task} task module is absent from {sorted(mods)} — a DCE'd task still reports "
            f"csynth OK, and with two tasks there are two things that could vanish")
    # The progress channel is declared depth 1 and it is an INTERNAL channel, so unlike a boundary
    # port that declaration is honoured and shows up as a named FIFO.
    assert f"{TOP}_fifo_w16_d1_S" in mods, (
        f"the depth-1 progress FIFO is not in {sorted(mods)}; a deeper one would only ever serve "
        f"stale positions")


@pytest.mark.vitis
@pytest.mark.parametrize("samp_per_word", [2, 4])
def test_the_wider_word_geometry_synthesizes(tmp_path, samp_per_word):
    """``samp_per_word > 1`` is hardware.

    Built into ``tmp_path``, so the gated project and its recorded cycle count are untouched.  What
    this proves is narrow and worth being precise about: the widened bodies **compile and produce a
    datapath**.  It does not measure the throughput the widening buys — that needs an RTL run with a
    converter attached, which is the XSI gate, and which is recorded for ``samp_per_word == 1`` only.
    """
    from waveflow.toolchain import toolchain

    top = TOP
    generate_dut(tmp_path, samp_per_word=samp_per_word)
    assert (tmp_path / "gen" / f"{top}.cpp").is_file()

    result = toolchain.run_vitis_hls(tmp_path / f"{top}.tcl", work_dir=tmp_path,
                                     capture_output=True)
    out = (result.stdout or "") + (result.stderr or "")
    assert "WAVEFLOW_CSYNTH_OK" in out, (
        f"csynth of the {samp_per_word}-samples-per-word geometry failed:\n{out[-3000:]}")

    mods = _module_set(tmp_path / f"{top}_proj" / "solution1" / "syn" / "verilog")
    assert top in mods
    for task in ("ingress", "capture"):
        assert any(f"rf_samp_buf_{task}_task" in m for m in mods), (
            f"the {task} task vanished at samp_per_word={samp_per_word}: {sorted(mods)}")


def test_the_gated_geometry_is_one_sample_per_word():
    """The XSI cycle gate is recorded for this geometry, so it is stated rather than defaulted.

    Toolchain-free on purpose: if the gated geometry ever changes, this fails in the dev loop rather
    than only on a machine with Vitis installed.
    """
    assert XSI_SAMP_PER_WORD == 1
    assert elab_params()["samp_per_word"] == 1
    assert elab_params(4) == {"bitwidth": 64, "samp_per_word": 4, "depth": 1024,
                              "horizon_margin": 16}
