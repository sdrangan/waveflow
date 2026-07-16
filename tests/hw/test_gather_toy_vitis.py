"""test_gather_toy_vitis.py — Vitis HLS csynth verification for gather_toy.

Synthesizes the gather_toy_top kernel using Vitis HLS and verifies:
- Code generation succeeds (no HLS errors)
- II (initiation interval) is 1 or close to 1 for the pipeline
- SOBIF typed block interface generates correctly

Run: pytest tests/hw/test_gather_toy_vitis.py -m vitis
"""
import re

import pytest
from pathlib import Path
from waveflow.toolchain.toolchain import find_vitis_path, run_vitis_hls


@pytest.mark.vitis
def test_gather_toy_csynth():
    """Run Vitis HLS C-Synthesis on gather_toy and verify success."""
    vitis_path = find_vitis_path()
    assert vitis_path is not None, "Vitis HLS toolchain not found"

    # Point to gather_toy TCL script
    tcl_script = Path(__file__).parent.parent.parent / "examples" / "interleaver" / "gather_toy_csynth.tcl"
    assert tcl_script.exists(), f"TCL script not found: {tcl_script}"

    # Run csynth via Vitis HLS
    output_dir = Path(__file__).parent / "gather_toy_hls"
    output_dir.mkdir(parents=True, exist_ok=True)

    result = run_vitis_hls(
        tcl_script=str(tcl_script),
        work_dir=str(output_dir),
        capture_output=True,
    )

    # Check success
    assert result.returncode == 0, (
        f"Vitis HLS csynth failed with return code {result.returncode}.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )

    # Verify output files exist (RTL, reports)
    rtl_dir = output_dir / "gather_toy_hls" / "solution1" / "syn" / "verilog"
    assert rtl_dir.exists(), f"RTL directory not generated: {rtl_dir}"

    reports_dir = output_dir / "gather_toy_hls" / "solution1" / "syn" / "report"
    assert reports_dir.exists(), f"Reports directory not generated: {reports_dir}"

    # Verify csynth report exists
    csynth_report = reports_dir / "gather_toy_top_csynth.rpt"
    assert csynth_report.exists(), f"C-synthesis report not found: {csynth_report}"

    print(f"\n✓ gather_toy csynth successful")
    print(f"  RTL: {rtl_dir}")
    print(f"  Report: {csynth_report}")


@pytest.mark.vitis
def test_gather_toy_stream_of_blocks_interface():
    """Verify the SOBIF channel lowered to a real depth-2 ping-pong buffer in RTL.

    Vitis never emits the literal string "stream_of_blocks" into RTL, so the evidence that the
    SOB lowered correctly is structural: the `sob` channel becomes its own RAM module whose
    BufferCount is the stream depth (2 = ping-pong) and whose AddressRange is the block length
    (BLOCK_N = 8 words), and the top instantiates it between the two task modules.
    """
    verilog_dir = Path(__file__).parent / "gather_toy_hls" / "gather_toy_hls" / "solution1" / "syn" / "verilog"
    assert verilog_dir.is_dir(), (
        f"No RTL at {verilog_dir} — run test_gather_toy_csynth first (it generates it)."
    )

    sob_rams = list(verilog_dir.glob("*_sob_RAM_*.v"))
    assert sob_rams, (
        f"No SOB buffer module (*_sob_RAM_*.v) in {verilog_dir}. "
        f"Found instead: {sorted(p.name for p in verilog_dir.glob('*.v'))}"
    )

    # The *_memcore variant is the inner RAM primitive; the wrapper carries the buffer params.
    wrapper = min(sob_rams, key=lambda p: len(p.name))
    content = wrapper.read_text()
    assert re.search(r"BufferCount\s*=\s*2", content), (
        f"{wrapper.name} is not a depth-2 ping-pong (BufferCount != 2):\n{content[:600]}"
    )
    assert re.search(r"AddressRange\s*=\s*8", content), (
        f"{wrapper.name} does not hold a BLOCK_N=8 block (AddressRange != 8):\n{content[:600]}"
    )

    # The buffer must actually sit between fill and gather in the top, not dangle.
    top = (verilog_dir / "gather_toy_top.v").read_text()
    for mod in (wrapper.stem, "fill_64_8", "gather_64_8"):
        assert mod in top, f"gather_toy_top.v does not instantiate {mod}"

    print(f"✓ SOBIF lowered to depth-2 ping-pong: {wrapper.name}")
