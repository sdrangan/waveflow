"""XsiSnapshot's skip decisions, without a toolchain (plans/incremental_xsi.md D2).

The runner is faked: ``_invoke`` creates the outputs a phase would and prints the runner's success
lines.  What is under test is the decision -- which phases a build runs -- and the rule behind it:
a phase is skipped only when a stamp of the CONTENT of everything it reads matches, and its output
exists.  The real phases are exercised by ``test_xsi_snapshot_xsi.py`` (``-m xsi``).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from waveflow.build.trace_steps import xsi_runner_cmd, xsi_runner_name
from waveflow.build.xsi_snapshot import XsiRunError, XsiSnapshot

_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"


def _workspace(tmp: Path) -> Path:
    ws = tmp / "xsi"
    (ws / "inc").mkdir(parents=True)
    rtl = tmp / "rtl"
    rtl.mkdir()
    shutil.copyfile(_SRC / xsi_runner_name(), ws / xsi_runner_name())
    (rtl / "k.v").write_text("module k; endmodule\n", encoding="utf-8")
    (ws / "inc" / "defs.vh").write_text("`define W 8\n", encoding="utf-8")
    (ws / "rtl_k.f").write_text("--include inc\n../rtl/k.v\n", encoding="utf-8")
    (ws / "vcd_dumper_k.v").write_text("module vcd_dumper_k; endmodule\n", encoding="utf-8")
    (ws / "k_tb.cpp").write_text('#include "k_ports.h"\n#include <cstdio>\nint main(){}\n',
                                 encoding="utf-8")
    (ws / "k_ports.h").write_text('#include "xsi_bfm.h"\n', encoding="utf-8")
    (ws / "xsi_bfm.h").write_text('#include "xsi.h"\n', encoding="utf-8")   # xsi.h: a toolchain header
    (ws / "xsi_loader.cpp").write_text("// loader\n", encoding="utf-8")
    return ws


class _Fake(XsiSnapshot):
    """The runner, faked: each phase creates its output; ``calls`` records the verbs."""

    def __init__(self, *a, fail: str = "", **k):
        super().__init__(*a, **k)
        self.calls: list[str] = []
        self.fail = fail

    def _invoke(self, verb, timeout, vectors_dir=None):
        self.calls.append(verb)
        out = []
        if verb in ("rtl", "build"):
            if self.fail != "rtl":
                self.design_dir.mkdir(parents=True, exist_ok=True)
                self.design_lib.write_bytes(b"dll")
            out += ["xvlog errorlevel=0", f"xelab errorlevel={1 if self.fail == 'rtl' else 0}"]
        if verb in ("tb", "build"):
            if self.fail != "tb":
                self.tb_binary.write_bytes(b"exe")
            out.append(f"gpp errorlevel={1 if self.fail == 'tb' else 0}")
        if verb == "run":
            out.append("XSI_EXITCODE=0")
        return "\n".join(out)


def test_a_fresh_workspace_builds_both_phases_once(tmp_path):
    ws = _workspace(tmp_path)
    s = _Fake(ws, "k", "k_tb")
    assert s.stale() == ["rtl", "tb"]
    assert s.build() == ["rtl", "tb"] and s.calls == ["build"]
    assert s.stale() == []
    assert s.build() == [] and s.calls == ["build"]          # nothing ran the second time


def test_an_rtl_edit_rebuilds_the_design_only(tmp_path):
    ws = _workspace(tmp_path)
    s = _Fake(ws, "k", "k_tb")
    s.build()
    k = tmp_path / "rtl" / "k.v"
    k.write_text("module k; wire w; endmodule\n", encoding="utf-8")
    assert s.stale() == ["rtl"]
    assert s.build() == ["rtl"] and s.calls[-1] == "rtl"


def test_an_include_dir_edit_rebuilds_the_design(tmp_path):
    ws = _workspace(tmp_path)
    s = _Fake(ws, "k", "k_tb")
    s.build()
    (ws / "inc" / "defs.vh").write_text("`define W 16\n", encoding="utf-8")
    assert s.stale() == ["rtl"]


def test_a_header_edit_rebuilds_the_testbench_only(tmp_path):
    """Followed transitively: k_tb.cpp -> k_ports.h -> xsi_bfm.h."""
    ws = _workspace(tmp_path)
    s = _Fake(ws, "k", "k_tb")
    s.build()
    (ws / "xsi_bfm.h").write_text('#include "xsi.h"\n// changed\n', encoding="utf-8")
    assert s.stale() == ["tb"]
    assert s.build() == ["tb"] and s.calls[-1] == "tb"


def test_testbench_flags_are_an_input(tmp_path):
    ws = _workspace(tmp_path)
    _Fake(ws, "k", "k_tb").build()
    assert _Fake(ws, "k", "k_tb", tb_cxxflags="-I/x").stale() == ["tb"]


def test_the_content_decides_not_the_timestamp(tmp_path):
    """Rewriting identical bytes (a regeneration) skips; restoring an edit makes it fresh again."""
    ws = _workspace(tmp_path)
    s = _Fake(ws, "k", "k_tb")
    s.build()
    k = tmp_path / "rtl" / "k.v"
    text = k.read_text(encoding="utf-8")
    k.write_text(text, encoding="utf-8")
    os.utime(k, (1e9, 1e9))
    assert s.stale() == []
    k.write_text(text + "// edit\n", encoding="utf-8")
    assert s.stale() == ["rtl"]
    k.write_text(text, encoding="utf-8")
    assert s.stale() == []


def test_a_missing_output_or_stamp_is_stale(tmp_path):
    """The stamp vouches only for an output that is there -- the -m xsi gates delete the snapshot
    to force a clean build, and its stamp goes with it."""
    ws = _workspace(tmp_path)
    s = _Fake(ws, "k", "k_tb")
    s.build()
    shutil.rmtree(s.design_dir)
    assert s.stale() == ["rtl"]
    s.build()
    s.tb_stamp.unlink()
    assert s.stale() == ["tb"]


def test_a_missing_listed_file_cannot_be_vouched_for(tmp_path):
    ws = _workspace(tmp_path)
    s = _Fake(ws, "k", "k_tb")
    s.build()
    (ws / "rtl_k.f").write_text("--include inc\n../rtl/k.v\n../rtl/gone.v\n", encoding="utf-8")
    assert s.rtl_inputs() is None and s.stale() == ["rtl"]


def test_the_traced_snapshot_is_separate(tmp_path):
    """D3: <top>_trace has its own design and stamp, reads the dumper, and shares the testbench."""
    ws = _workspace(tmp_path)
    plain, traced = _Fake(ws, "k", "k_tb"), _Fake(ws, "k", "k_tb", trace=True)
    plain.build()
    assert traced.snapshot == "k_trace" and traced.stale() == ["rtl"]
    assert traced.build() == ["rtl"]
    assert plain.stale() == [] and traced.stale() == []
    (ws / "vcd_dumper_k.v").write_text("module vcd_dumper_k; wire x; endmodule\n", encoding="utf-8")
    assert traced.stale() == ["rtl"] and plain.stale() == []


def test_a_failed_phase_writes_no_stamp(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(XsiRunError, match="compile/elaborate failed"):
        _Fake(ws, "k", "k_tb", fail="rtl").build()
    s = _Fake(ws, "k", "k_tb")
    assert "rtl" in s.stale()
    with pytest.raises(XsiRunError, match="testbench build failed"):
        _Fake(ws, "k", "k_tb", fail="tb").build()
    assert s.stale() == ["tb"]          # the design phase succeeded and was stamped


def test_run_resolves_the_vectors_dir_and_creates_it(tmp_path):
    ws = _workspace(tmp_path)
    got = {}

    class _Run(_Fake):
        def _invoke(self, verb, timeout, vectors_dir=None):
            got[verb] = vectors_dir
            return super()._invoke(verb, timeout, vectors_dir)

    s = _Run(ws, "k", "k_tb")
    s.run(tmp_path / "runs" / "p0")
    assert (tmp_path / "runs" / "p0").is_dir()
    assert got["run"] == (tmp_path / "runs" / "p0").resolve().as_posix()


def test_runner_cmd_passes_verb_and_vectors_dir():
    assert xsi_runner_cmd("k", "tb", trace=True, verb="run", vectors_dir="v/p1",
                          os_name="posix") == ["bash", "run.sh", "k", "tb", "trace", "run", "v/p1"]
    assert xsi_runner_cmd("k", "tb", verb="build", os_name="nt")[-3:] == ["k", "tb", "build"]


@pytest.mark.parametrize("tail,ok", [
    ("Built XSI simulation shared library xsim.dir/k/xsimk.dll\n"
     "Could not remove the obj directory: boost::filesystem::remove: ... used by another process", True),
    ("ERROR: [XSIM 43-3225] Cannot find design unit work.k\n"
     "Could not remove the obj directory: ...", False),
])
def test_xelab_failing_only_its_obj_cleanup_is_a_built_design(tmp_path, tail, ok):
    """xelab exits 1 when it cannot delete its scratch obj/ after building the library (a file held
    open on Windows).  The library is complete then; a real elaboration error says ERROR:."""
    ws = _workspace(tmp_path)

    class _Elab(_Fake):
        def _invoke(self, verb, timeout, vectors_dir=None):
            return super()._invoke(verb, timeout, vectors_dir).replace(
                "xelab errorlevel=0", tail + "\nxelab errorlevel=1")

    s = _Elab(ws, "k", "k_tb")
    if ok:
        assert s.build() == ["rtl", "tb"]
    else:
        with pytest.raises(XsiRunError):
            s.build()


def test_run_params_are_flat_integers(tmp_path):
    """The C++ reader (wfbfm::run_param) is a minimal scan for integers; refuse anything else."""
    import json

    from waveflow.utils.burst_io import write_run_params

    p = write_run_params(tmp_path / "vectors", n_cycles=5000, mem_words=33344)
    assert json.loads(p.read_text(encoding="utf-8")) == {"n_cycles": 5000, "mem_words": 33344}
    for bad in (1.5, "7", True):
        with pytest.raises(TypeError):
            write_run_params(tmp_path / "v2", n_cycles=bad)
