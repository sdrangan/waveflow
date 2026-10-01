"""S2 gates — the build pieces that let a Waveflow design call the vendor FFT.

Two framework deliverables are checked here, both cheap and toolchain-free:

* :class:`~waveflow.build.vitis_l1_step.VitisL1Step` copies the hand-written task body, the way
  ``MemStreamStep`` does for the ``m_axi`` owners;
* ``render_tcl(include_dirs=...)`` can reach headers outside the generated ``include/``, which it
  could not before — the vendor headers stay in the Vitis install and are reached with ``-I``.

The end-to-end csynth + cosim run lives in ``verifyHwModule/`` and is gated at the bottom of this
file, marked ``vitis``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import waveflow.build.vitis_l1_step as _step_mod
from waveflow.build.composite_gen import INCLUDE_DIR, render_tcl
from waveflow.build.vitis_l1_step import VitisL1Step, vitis_fft_include_dir
from waveflow.hw.clock import Clock
from waveflow.simulation.simulation import Simulation
from waveflow.vitis_l1.hw import VitisFft

PKG = Path(__file__).resolve().parent / "verifyHwModule"
#: Where the copied bodies live, resolved from the module itself rather than
#: from the working directory.
BUILD_DIR = Path(_step_mod.__file__).resolve().parent


def test_step_copies_the_body_verbatim():
    """The *copy* path, not the extract path: nothing can derive an ``fft<>`` call from Python."""
    step = VitisL1Step(output_dir="include")
    assert step.build_outputs == {"vitis_fft_task": Path("include") / "vitis_fft_task.h"}
    body = step.generate("vitis_fft_task", None)
    src = BUILD_DIR / "vitis_fft_task.h"
    assert body == src.read_text(encoding="utf-8"), "the body must be copied, not transformed"
    assert "xf::dsp::fft::fft<P>(a_in, a_out)" in body
    with pytest.raises(KeyError, match="Unknown VitisL1Step output key"):
        step.generate("nope", None)


def test_render_tcl_is_byte_identical_without_include_dirs():
    """The default must not move. Several generated tops are gated on exact RTL cycle counts, and
    a changed TCL is a changed build — so the new parameter has to be invisible when unused."""
    assert f'set cf "-I{INCLUDE_DIR}"\n' in render_tcl("demo")
    assert render_tcl("demo") == render_tcl("demo", include_dirs=())


def test_include_dirs_adds_only_cflags():
    """Extra ``-I`` paths appear in ``$cf`` and change nothing else about the script."""
    a, b = render_tcl("demo"), render_tcl("demo", include_dirs=("/x/one", "/y/two"))
    assert f'set cf "-I{INCLUDE_DIR} -I/x/one -I/y/two"' in b
    assert [ln for ln in a.splitlines() if not ln.startswith("set cf")] == \
           [ln for ln in b.splitlines() if not ln.startswith("set cf")]


def test_the_vendor_include_resolver_prefers_an_explicit_path_and_then_the_environment(tmp_path,
                                                                                      monkeypatch):
    """Explicit beats environment beats install search — and a bad path raises rather than
    returning something that would fail later inside csynth with a confusing message."""
    good = tmp_path / "shipped"
    (good / "vitis_fft").mkdir(parents=True)
    (good / "vitis_fft" / "hls_ssr_fft.hpp").write_text("// stub\n")
    monkeypatch.delenv("WF_VITIS_LIBS", raising=False)
    assert vitis_fft_include_dir(good) == good

    monkeypatch.setenv("WF_VITIS_LIBS", str(good))
    assert vitis_fft_include_dir() == good

    monkeypatch.setenv("WF_VITIS_LIBS", str(tmp_path / "empty"))
    monkeypatch.delenv("XILINX_VITIS", raising=False)
    monkeypatch.setattr("waveflow.build.vitis_l1_step._ROOT_CANDIDATES", ())
    with pytest.raises(FileNotFoundError, match="hls_ssr_fft.hpp"):
        vitis_fft_include_dir()


def test_the_body_and_the_module_agree_on_the_template_arguments():
    """The header's parameter list and ``kernel_task``'s tuple are one contract.

    If they drift, csynth fails with a template error — but only once someone runs Vitis. This
    pins the arity and the order in the fast suite instead.
    """
    body = (BUILD_DIR / "vitis_fft_task.h").read_text(encoding="utf-8")
    decl = "template <int L, int IN_W, int IN_I, int TW_W, int TW_I, int SCALING, int ORDER, int OUT_W>"
    assert decl in body, "the body's template parameter list changed"

    m = VitisFft(name="m", sim=Simulation(), clk=Clock(freq=100e6), L=16)
    args = m.kernel_task().template_args
    assert len(args) == 9, "L, R, IN_W, IN_I, TW_W, TW_I, SCALING, ORDER, OUT_W"
    assert args == (16, 4, 16, 2, 18, 2, 0, 0, 21)
    # R is position 1 and is NOT a template argument of the body: a template cannot vary a
    # function's arity, so the body fixes R=4 and VitisFft refuses anything else.
    assert args[1] == 4 and "static const int WF_FFT_R = 4;" in body
    assert args[-1] == int(m.out_fmt.W), "the last argument is the derived output width"
    assert "static_assert(OUT_W ==" in body, (
        "the body must assert the derived width against the vendor's ssr_fft_output_type — that "
        "is what makes the Python derivation a compile-time check rather than a claim")


def test_the_committed_cosim_output_matches_the_golden():
    """The checked-in evidence from ``verifyHwModule/``, re-checked without a toolchain.

    Running Vitis is gated below; this makes the *result* a standing assertion, so a regenerated
    output that stopped matching could not land quietly.
    """
    def rows(p):
        return [tuple(int(v) for v in ln.split())
                for ln in p.read_text().splitlines() if ln.strip()]

    want = rows(PKG / "data" / "golden_output.txt")
    assert len(want) == 12 * 16, "the golden should carry 12 vectors of 16 samples"
    for tag in ("csim", "cosim"):
        got = rows(PKG / "results" / f"output_{tag}.txt")
        assert got == want, f"committed {tag} output no longer matches the vendor golden"


@pytest.mark.vitis
def test_the_generated_top_csynths_and_cosims_bit_exactly():
    """S2 + S3: build the tree, run csim -> csynth -> cosim, compare against the golden.

    Takes about a minute. Deliberately not a skip-if-convenient: ``-m vitis`` exists to run this.
    """
    if shutil.which("vitis-run") is None:
        pytest.skip("vitis-run not on PATH (source Vitis settings64.sh)")
    py = sys.executable
    subprocess.run([py, "build.py"], cwd=PKG, check=True, capture_output=True, text=True)
    run = subprocess.run(["vitis-run", "--mode", "hls", "--tcl", "run.tcl"], cwd=PKG,
                         capture_output=True, text=True, env={**os.environ})
    log = run.stdout + run.stderr
    for marker in ("WAVEFLOW_CSIM_OK", "WAVEFLOW_CSYNTH_OK", "WAVEFLOW_COSIM_OK"):
        assert marker in log, f"{marker} missing; tail:\n{log[-1500:]}"
    done = subprocess.run([py, "verify.py"], cwd=PKG, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ALL BIT-EXACT" in done.stdout, done.stdout
