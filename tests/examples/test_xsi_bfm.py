"""The XSI RTL gates — the four free-running kernels driven through real RTL by the BFM library.

**Why this file exists.**  These four cycle counts are the only evidence that the generated
``ap_ctrl_none`` tops, the generated ``mem_seq_task.h`` body, and the BFM library are correct, and
until now they were checked *by hand*.  A refactor that broke them would have gone unnoticed until
someone happened to re-run ``run.bat``.  ``plans/xsi_tb_codegen.md`` records them as Stage 1's gate;
this makes the gate real.

Run: ``pytest tests/examples/test_xsi_bfm.py -m xsi``  (needs Vivado xsim + mingw g++, and a prior
csynth of each top — see the module's skip messages).

**On the numbers.**  They are exact, not bounds.  A cycle count that moves is either a real
regression or a real improvement, and both are worth a human look — an inequality would silently
absorb the first kind.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from waveflow.build.composite_gen import render_rtl_f

HERE = Path(__file__).resolve().parents[2] / "examples" / "interleaver"
XSI = HERE / "xsi"

#: (top, tb basename, expected cycles, a substring proving the golden actually ran).
#: Recorded 2026-07-16 from a clean rebuild; see plans/xsi_tb_codegen.md.
GATES = [
    ("mem_r_stream", "mem_r_bfm_tb", 414, "collected=128"),
    ("mem_w_stream", "mem_w_bfm_tb", 432, "w_count=128"),
    ("mem_copy", "mem_copy_bfm_tb", 3347, "done=16 w_count=2048"),
    ("interleaver_canon", "interleaver_canon_bfm_tb", 3469, "done=8/8"),
]


#: The committed .f files, captured AT IMPORT — i.e. before any gate below can regenerate one.
#: Without this snapshot the drift check is self-defeating: the gate rewrites rtl_<top>.f, so a
#: check that read the file afterwards would compare the gate's own output against itself and could
#: never fail.  (It ran green that way; the flaw only showed when the drift test was run alone.)
_COMMITTED_F = {
    top: (XSI / f"rtl_{top}.f").read_text(encoding="utf-8").replace("\r\n", "\n")
    for top, _tb, _c, _m in GATES
    if (XSI / f"rtl_{top}.f").exists()
}


def _require(cond: bool, why: str) -> None:
    """Skip loudly.  A silent skip on a gate this expensive is worse than a failure: it reads as
    'passed' in a summary line."""
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


@pytest.mark.xsi
@pytest.mark.parametrize("top,tb,want_cycles,want_marker", GATES,
                         ids=[g[0] for g in GATES])
def test_xsi_bfm_gate(top: str, tb: str, want_cycles: int, want_marker: str):
    _require(os.name == "nt", "the XSI flow is a Windows .bat (xvlog/xelab/mingw)")
    _require((XSI / "run.bat").exists(), f"{XSI / 'run.bat'}")
    proj = HERE / f"{top}_proj" / "solution1" / "syn" / "verilog"
    _require(proj.is_dir(), f"no csynth RTL at {proj} — run {top}.tcl first")

    # 1) Regenerate the xvlog file list from the RTL that is actually on disk.  Never trust the
    # committed .f: it is the half of this flow that silently drifts (a renamed module leaves it
    # naming a file that no longer exists, and xvlog + a cached dll will happily go green).
    (XSI / f"rtl_{top}.f").write_text(render_rtl_f(top, HERE), encoding="utf-8")

    # 2) Force a clean build.  A cached xsimk.dll is the other half: xelab would reuse RTL elaborated
    # from a previous design and the run would prove nothing about the current one.
    shutil.rmtree(XSI / "xsim.dir" / top, ignore_errors=True)
    for stale in (f"{tb}.exe", f"{tb}.o"):
        (XSI / stale).unlink(missing_ok=True)

    # ".\\run.bat", not "run.bat": cmd does not resolve a bare name from cwd, and the bare form
    # fails with "not recognized as an internal or external command" rather than anything useful.
    r = subprocess.run(["cmd", "/c", ".\\run.bat", top, tb], cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")

    assert "PASSED test" in out, f"{top} XSI BFM did not pass:\n{out[-3000:]}"
    assert want_marker in out, (
        f"{top} printed PASSED but not '{want_marker}' — the golden may not have run:\n{out[-2000:]}"
    )
    assert f"cycles={want_cycles}" in out, (
        f"{top} cycle count moved (want {want_cycles}).  That is a real behaviour change: either a\n"
        f"regression, or an improvement worth re-recording in plans/xsi_tb_codegen.md.\n{out[-2000:]}"
    )


@pytest.mark.xsi
@pytest.mark.parametrize("top", [g[0] for g in GATES])
def test_committed_rtl_f_matches_the_rtl_on_disk(top: str):
    """The COMMITTED .f must equal what the RTL implies.

    If this fails, the checked-in list has drifted from the design, and anyone driving `run.bat` by
    hand is running against a file set that no longer matches — the classic fake PASS.  The gates
    above defend themselves by regenerating; this defends the manual path.

    Compares against ``_COMMITTED_F`` (snapshotted at import), NOT the file on disk, because the
    gates rewrite it — reading it here would compare the gate's output against itself.
    """
    proj = HERE / f"{top}_proj" / "solution1" / "syn" / "verilog"
    _require(proj.is_dir(), f"no csynth RTL at {proj}")
    _require(top in _COMMITTED_F, f"no committed rtl_{top}.f")

    assert render_rtl_f(top, HERE) == _COMMITTED_F[top], (
        f"rtl_{top}.f has drifted from the elaborated RTL -- regenerate it (render_rtl_f). "
        f"A renamed RTL module leaves the committed list naming a file that no longer exists."
    )


def test_render_rtl_f_refuses_when_there_is_no_rtl(tmp_path):
    """Fail loudly rather than emit an empty file list: an empty .f makes xvlog a no-op, and the
    stale-dll path then turns that into a green run.  (No xsi marker: pure logic, no toolchain.)"""
    with pytest.raises(FileNotFoundError, match="run csynth"):
        render_rtl_f("nope", tmp_path)
