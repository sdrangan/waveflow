"""run_vitis_hls re-runs a csynth once when Vitis dropped a declared m_axi bundle (HLS 214-450), and
only then: a correct csynth, and a script that runs no csynth, run once."""
import subprocess

import pytest

import waveflow.build.composite_gen as composite_gen
import waveflow.toolchain.toolchain as toolchain

CSYNTH = "open_project -reset k_proj\nset_top k\nadd_files gen/k.cpp\ncsynth_design\nexit 0\n"
CSIM = "open_project -reset k_proj\nset_top k\ncsim_design\nexit 0\n"


@pytest.fixture
def runs(monkeypatch):
    calls = []
    monkeypatch.setattr(toolchain, "_build_vitis_hls_cmd", lambda tcl_script, args: (["vitis-run"], "."))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(1) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    return calls


@pytest.mark.parametrize("script, dropped, want", [
    (CSYNTH, [["gmem0"], []], 2),        # dropped, then lowered: one re-run
    (CSYNTH, [[], []], 1),               # lowered: no re-run
    (CSIM, [["gmem0"], []], 1),          # no csynth in the script: never asked
])
def test_a_dropped_bundle_reruns_csynth_once(tmp_path, monkeypatch, runs, script, dropped, want):
    tcl = tmp_path / "k.tcl"
    tcl.write_text(script, encoding="utf-8")
    answers = iter(dropped)
    monkeypatch.setattr(composite_gen, "unlowered_maxi_bundles", lambda root, top: next(answers))
    toolchain.run_vitis_hls(tcl, work_dir=tmp_path)
    assert len(runs) == want
