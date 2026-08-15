"""The XSI gate for a design whose memory lives OUTSIDE the kernel — ``plans/rtl_module.md`` S3.

**What makes this gate different from the four in ``test_xsi_bfm.py``**: the module xsim elaborates
is not the kernel.  ``bram_toy_top`` is the generated wrapper — the kernel plus its hand-written
``bram_t2p`` memory — so the ``.f``, the snapshot and the shared library are named for it, while
csynth's project and generated Verilog keep the kernel's name.

**The check is values, not plumbing, and the values are not ours.**  ``plans/witness/t2p_bram/`` ran
before any of this infrastructure existed: write ``buf[i] = i + 100`` for 256 samples, then read
addresses ``0, 1, 7, 255, 128``.  It returned ``100, 101, 107, 355, 228``, and so must this.

A **ramp rather than a constant**, deliberately: the likeliest failure here is a read-latency
mismatch between the kernel's ``latency=`` pragma and the memory's published ``READ_LATENCY``, which
shifts every value by one position and would pass a constant check without a murmur.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.bram_toy.bram_toy import ADDRS, EXPECTED, check_xsi_outputs, write_scenario
from examples.bram_toy.bram_toy_build import RTL_FILES, TOP, WRAPPER
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, xsi_runner_cmd

ROOT = Path(__file__).resolve().parents[2] / "examples" / "bram_toy"
TB = f"{TOP}_bfm_tb"

#: Time to last completion — the cycle the fifth (last) address's answer landed at the sink.
#: Recorded 2026-08-14 on the first green run.  Exact, not a bound: a cycle count that moves is
#: either a regression or an improvement, and both deserve a human.  The bulk of it is the design's
#: own sequencing — the reader waits for the writer's "buffer ready" token, which cannot arrive
#: before all 256 words have been written.
WANT_CYCLES = 529


def _require(cond: bool, why: str) -> None:
    """Skip loudly.  A silent skip on a gate this expensive reads as 'passed' in a summary line."""
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


@pytest.mark.xsi
def test_bram_toy_xsi_reproduces_the_witness():
    xsi = ROOT / "xsi"
    _require((xsi / XSI_RUNNER).exists(), f"{xsi / XSI_RUNNER}")
    proj = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
    _require(proj.is_dir(), f"no csynth RTL at {proj} — run bram_toy_build.py --through csynth first")
    for f in RTL_FILES:
        _require((xsi / f).is_file(), f"{xsi / f} — run bram_toy_build.py --through codegen_dut")

    # 1) Regenerate the file list from the RTL actually on disk, and name the wrapper's own sources
    # after it.  Never trust the committed .f: a renamed module leaves it naming a file that no
    # longer exists, and xvlog + a cached dll will happily go green.
    (xsi / f"rtl_{WRAPPER}.f").write_text(render_rtl_f(TOP, ROOT, extra=RTL_FILES), encoding="utf-8")

    # 2) Force a clean elaboration of the WRAPPER: a cached snapshot would prove nothing about the
    # current design, and here it could also be a snapshot of the KERNEL from a previous flow.
    shutil.rmtree(xsi / "xsim.dir" / WRAPPER, ignore_errors=True)
    for stale in (f"{TB}.exe", f"{TB}.bin", f"{TB}.o"):
        (xsi / stale).unlink(missing_ok=True)
    shutil.rmtree(xsi / "vectors" / "out", ignore_errors=True)
    write_scenario(xsi)

    r = subprocess.run(xsi_runner_cmd(WRAPPER, TB), cwd=str(xsi),
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in out, f"bram_toy XSI run did not complete cleanly:\n{out[-3000:]}"

    # The memory's own assertion. `bram_t2p.v` $errors when the reader touches the address the
    # writer is writing that cycle, so a clean log is positive evidence that "rd trails wr" held --
    # the design invariant, checked by the hand-written RTL and by nothing else.
    assert "read-during-write collision" not in out, (
        f"the memory's read-during-write assertion fired: the reader is not trailing the writer.\n"
        f"{out[-3000:]}")

    check_xsi_outputs(xsi, WANT_CYCLES)


@pytest.mark.xsi
def test_the_kernel_really_got_bram_ports():
    """THE GATE S1 HAD TO DEFER — and it is read off the RTL, not off an exit code.

    ``mode=bram`` on an *unsized* pointer silently produces an ``ap_vld`` scalar port: no warning, no
    error, a clean csynth, and a design elaborated against a memory that is not there.  So "csynth
    OK" is not evidence of anything; the port list is.

    Checks both interfaces, all seven signals, both halves — 28 ports — and that they are exactly
    what :func:`~waveflow.build.composite_gen.bram_port_signals` derived without ever seeing this
    RTL.
    """
    from waveflow.build.composite_gen import bram_port_signals

    v = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog" / f"{TOP}.v"
    _require(v.is_file(), f"no csynth RTL at {v}")
    text = v.read_text(encoding="utf-8")

    import re
    declared = set(re.findall(r"^\s*(?:input|output)\s+(?:\[[^\]]+\]\s*)?(\w+);", text, re.M))
    for port in ("buf_w", "buf_r"):
        want = set(bram_port_signals(port).values())
        missing = sorted(want - declared)
        assert not missing, (
            f"{TOP}.v does not declare {missing} for the bram port {port!r}. A `mode=bram` pragma "
            f"that did not take effect degrades the port to an ap_vld scalar SILENTLY — check that "
            f"the C++ parameter is a sized array, not a pointer.")
    assert f"{TOP}_ap_vld" not in text and "buf_w_ap_vld" not in text, (
        "an ap_vld port on a bram interface is the silent-degradation failure mode")


@pytest.mark.xsi
def test_both_tasks_are_free_running_with_no_pipo_gating():
    """The structure the whole plan exists to obtain.

    Experiment 1 in ``plans/rtl_module.md`` csynths and is *wrong*: a shared local array becomes a
    PIPO channel, the reader is gated on ``buf_r_t_empty_n`` and — the part that matters — the WRITER
    stalls on ``full_n``.  A converter-facing stage may never stall, so this asserts the opposite:
    both tasks start unconditionally and continue unconditionally.
    """
    v = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog" / f"{TOP}.v"
    _require(v.is_file(), f"no csynth RTL at {v}")
    text = v.read_text(encoding="utf-8")

    for task in ("bram_write_task_16_1024_256_U0", "bram_read_task_16_1024_U0"):
        for pin in ("ap_start", "ap_continue"):
            assert f"assign {task}_{pin} = 1'b1;" in text, (
                f"{task}.{pin} is not tied high — the tasks are being GATED, which is the PIPO "
                f"structure this design exists to avoid (the writer would stall on a full buffer).")


def test_the_expected_values_are_the_witness_s(tmp_path):
    """No toolchain: the numbers this gate demands are the witness's, transcribed once.

    Cheap, and it closes the one way a gate like this rots into meaninglessness — someone adjusting
    the expected values to match a run instead of diagnosing why the run moved.
    """
    tb = (Path(__file__).resolve().parents[2] / "plans" / "witness" / "t2p_bram" / "tb.v"
          ).read_text(encoding="utf-8")
    for addr, want in zip(ADDRS, EXPECTED):
        assert str(want) in tb, f"the witness's tb.v does not mention {want} (address {addr})"
    assert np.array_equal(np.array(EXPECTED), np.array(ADDRS) + 100)
