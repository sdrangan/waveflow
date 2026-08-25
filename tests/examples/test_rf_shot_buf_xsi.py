"""The shot buffer's RTL gate — ``plans/rf_shot_buf.md`` Stage A.

What xsim elaborates is the **wrapper** (``rf_shot_buf_top``): the kernel plus its hand-written
``bram_t2p`` memory, so the ``.f``, the snapshot and the shared library are named for it while
csynth's project keeps the kernel's name.

**This gate found something a witness could not.**  The retired ``bram_toy`` proved the ``mode=bram``
path at 16 bits and 256 of 1024 words; the first run of *this* design, at 64 bits and 256 of 1024
words, returned the second half of the shot **twice**.  The cause was the address convention: the
kernel's ``Addr_A`` is a **byte** address (``Addr_A_orig << 32'd3`` for a 64-bit array) and
``bram_t2p.v`` indexes words, so every address was scaled by 8 and everything past word 127 aliased
onto a live word — silently, and consistently enough that a design writing and reading through the
same scaled address round-trips perfectly until it wraps.  The wrapper now undoes the shift
(:func:`waveflow.build.wrapper_gen._bram_addr_shift`), and
:func:`test_the_wrapper_undoes_the_shift_vitis_actually_emits` checks that number against the RTL
Vitis produced rather than against a belief about it.

Needs a prior csynth of ``rf_shot_buf.tcl`` plus the XSI toolchain; skips **loudly** rather than
passing when either is missing.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from examples.rf_shot_buf.rf_shot_buf import NWORD, check_xsi_outputs
from examples.rf_shot_buf.rf_shot_buf_build import RTL_FILES, TOP, WRAPPER, generate_tb
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, xsi_runner_cmd

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_shot_buf"
XSI = ROOT / "xsi"
TB = f"{TOP}_bfm_tb"
VERILOG = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
REPORT = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "report"

#: Time to last completion — the cycle the 256th word landed at the sink.  Recorded 2026-08-24 on
#: the first green run.  Exact, not a bound: a cycle count that moves is either a regression or an
#: improvement, and both deserve a human.
#:
#: The shape of it: the loader takes 256 cycles for the shot plus its pipeline fill, the ``rdy``
#: token crosses, and the reader then emits 256 words back to back at one per cycle — the sink's
#: timestamps run 265..520 with no gap, which is the II=1 claim visible end to end rather than only
#: in a report.
WANT_CYCLES = 520

#: The synthesized inner-loop modules, named for the **label** on each body's counted loop rather
#: than for a source line.  That is deliberate: Vitis names an unlabelled loop ``VITIS_LOOP_<line>_1``
#: and a comment edit renames the report entry, at which point a name spelled out here stops matching
#: and — if the test skipped on a miss — would read as a pass.
_LOOPS = {
    f"{TOP}_load_task_64_1024_256_Pipeline_load_shot": "load_shot",
    f"{TOP}_read_task_64_1024_256_Pipeline_play_shot": "play_shot",
}


def _require(cond: bool, why: str) -> None:
    """Skip loudly.  A silent skip on a gate this expensive reads as 'passed' in a summary line."""
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


@pytest.mark.xsi
def test_the_shot_survives_real_rtl_byte_for_byte():
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(VERILOG.is_dir(), f"no csynth RTL at {VERILOG} — run rf_shot_buf_build.py --through csynth")
    for f in RTL_FILES:
        _require((XSI / f).is_file(), f"{XSI / f} — run rf_shot_buf_build.py --through codegen_dut")

    # 1) Regenerate the file list from the RTL actually on disk.  Never trust the committed .f: a
    # renamed module leaves it naming a file that no longer exists, and xvlog + a cached dll will
    # happily go green.
    (XSI / f"rtl_{WRAPPER}.f").write_text(render_rtl_f(TOP, ROOT, extra=RTL_FILES), encoding="utf-8")

    # 2) Force a clean elaboration of the WRAPPER, and clear the previous run's dump: a cached
    # snapshot plus a stale bundle is how a broken build passes on old output.
    shutil.rmtree(XSI / "xsim.dir" / WRAPPER, ignore_errors=True)
    for stale in (f"{TB}.exe", f"{TB}.bin", f"{TB}.o"):
        (XSI / stale).unlink(missing_ok=True)
    shutil.rmtree(XSI / "vectors" / "out", ignore_errors=True)
    generate_tb(ROOT)

    r = subprocess.run(xsi_runner_cmd(WRAPPER, TB), cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in out, f"rf_shot_buf XSI run did not complete cleanly:\n{out[-3000:]}"

    # NOTE (2026-08-25): a `read-during-write collision` assertion used to be checked here
    # against this stream.  It could never fire: `out` is run.bat's stdout/stderr, and the
    # XSI flow DISCARDS RTL text output -- `bram_t2p.v`'s $error reaches no channel this
    # test can read (measured four ways, see plans/bram_simple.md).  It was removed rather
    # than left reading as positive evidence.  The condition is to be gated from the VCD
    # trace instead; until that lands it is checked NOWHERE, which is what was already true.

    check_xsi_outputs(XSI, NWORD, WANT_CYCLES)


@pytest.mark.xsi
def test_the_kernel_really_got_bram_ports():
    """``mode=bram`` on an unsized pointer degrades to an ``ap_vld`` scalar SILENTLY.

    No warning, no error, a clean csynth, and a design elaborated against a memory that is not there.
    So "csynth OK" is not evidence; the port list is.  Checked against
    :func:`~waveflow.build.composite_gen.bram_port_signals`, which derived the names without ever
    seeing this RTL.
    """
    from waveflow.build.composite_gen import bram_port_signals

    v = VERILOG / f"{TOP}.v"
    _require(v.is_file(), f"no csynth RTL at {v}")
    text = v.read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s*(?:input|output)\s+(?:\[[^\]]+\]\s*)?(\w+);", text, re.M))
    for port in ("buf_w", "buf_r"):
        missing = sorted(set(bram_port_signals(port).values()) - declared)
        assert not missing, (
            f"{TOP}.v does not declare {missing} for the bram port {port!r}. A `mode=bram` pragma "
            f"that did not take effect degrades the port to an ap_vld scalar SILENTLY — check that "
            f"the C++ parameter is a sized array, not a pointer.")
    assert "buf_w_ap_vld" not in text and "buf_r_ap_vld" not in text


@pytest.mark.xsi
def test_the_wrapper_undoes_the_shift_vitis_actually_emits():
    """The defect this example found, pinned to the **RTL** rather than to an assumption.

    Vitis addresses a ``bram`` port in bytes: the generated task RTL contains
    ``Addr_A_local = Addr_A_orig << 32'd3`` for a 64-bit array.  The wrapper's ``>> 3`` has to be
    that same number, and the only way to know it is to read what the tool emitted.  If Vitis ever
    changes the convention, this fails with the two numbers side by side instead of a design quietly
    aliasing its memory again.
    """
    from waveflow.build.wrapper_gen import _bram_addr_shift

    _require(VERILOG.is_dir(), f"no csynth RTL at {VERILOG}")
    shifts = set()
    for p in VERILOG.glob("*.v"):
        shifts.update(int(m) for m in re.findall(
            r"Addr_A_local = \w+_Addr_A_orig << 32'd(\d+);", p.read_text(encoding="utf-8")))
    assert shifts, (
        f"no `Addr_A_local = ... << 32'dN` in {VERILOG}: either Vitis stopped scaling the bram "
        f"address (in which case the wrapper's shift must go) or this pattern moved. Do not delete "
        f"this test — re-derive the convention.")
    assert shifts == {_bram_addr_shift(64)}, (
        f"Vitis scales this design's bram address by {sorted(shifts)} bits but the wrapper undoes "
        f"{_bram_addr_shift(64)}. Every address is then wrong by a factor and high words alias onto "
        f"low ones with no tool saying a word — the failure this example was built to find.")

    wrapper = (XSI / f"{WRAPPER}.v").read_text(encoding="utf-8")
    assert wrapper.count(f">> {_bram_addr_shift(64)}") == 2, (
        "the wrapper must undo the scaling on BOTH memory ports; one of them alone is worse than "
        "neither, because the write and the read would disagree about where a word lives")


@pytest.mark.xsi
def test_both_tasks_are_free_running_with_no_pipo_gating():
    """The structure the whole ``rtl_module`` path exists to obtain.

    A shared local array between two ``hls::task`` bodies becomes a PIPO channel whose handshake
    **stalls the writer**.  This asserts the opposite: both tasks start unconditionally and continue
    unconditionally.
    """
    v = VERILOG / f"{TOP}.v"
    _require(v.is_file(), f"no csynth RTL at {v}")
    text = v.read_text(encoding="utf-8")
    for task in (f"{TOP}_load_task_64_1024_256_U0", f"{TOP}_read_task_64_1024_256_U0"):
        for pin in ("ap_start", "ap_continue"):
            assert f"assign {task}_{pin} = 1'b1;" in text, (
                f"{task}.{pin} is not tied high — the tasks are being GATED, which is the PIPO "
                f"structure this design exists to avoid.")


@pytest.mark.xsi
def test_both_shot_loops_reach_ii_1():
    """Cycles per word, **measured**: the achieved ``PipelineII`` of each body's counted loop.

    Achieved, not target — Vitis reports both and they differ whenever it missed.  ``1`` is what
    makes the shot buffer's claim over the streaming one structural rather than rhetorical: the
    capture and loader next door are pinned at 2 by an inner data-dependent spin, and a shot buffer
    has nothing to wait for mid-shot because the other side is not live.

    The loops are discovered by module rather than spelled out per report entry, and the entries
    themselves are **labels** — an unlabelled loop is named after its source line, so a comment edit
    would rename it and a hard-coded lookup would then miss and read as a pass.
    """
    from waveflow.utils.csynthparse import loop_pipeline_ii, module_loops

    _require(REPORT.is_dir(), f"no csynth report dir at {REPORT}")
    for module, loop in _LOOPS.items():
        _require((REPORT / f"{module}_csynth.xml").is_file(), f"no report for {module}")
        loops = module_loops(REPORT, module)
        assert loops == [loop], (
            f"{module} reports loops {loops}, expected exactly [{loop!r}]. A renamed entry means "
            f"the label was dropped; a second entry means the body grew a loop it did not have.")
        assert loop_pipeline_ii(REPORT, module, loop) == 1, (
            f"{module}.{loop} no longer achieves II=1. That is one cycle per WORD, and it is the "
            f"whole throughput claim of this design — do not re-record it without diagnosing why "
            f"the loop stopped flattening.")
