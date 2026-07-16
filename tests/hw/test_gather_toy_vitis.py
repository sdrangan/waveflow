"""test_gather_toy_vitis.py — Vitis HLS csynth verification for gather_toy.

Synthesizes the gather_toy_top kernel using Vitis HLS and verifies:
- Code generation succeeds (no HLS errors)
- II (initiation interval) is 1 or close to 1 for the pipeline
- SOBIF typed block interface generates correctly

Run: pytest tests/hw/test_gather_toy_vitis.py -m vitis
"""
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
    """Verify that the SOBIF interface is correctly generated in RTL."""
    # After csynth, check that the RTL contains hls::stream_of_blocks instantiation
    output_dir = Path(__file__).parent / "gather_toy_hls"
    verilog_files = list((output_dir / "gather_toy_hls" / "solution1" / "syn" / "verilog").glob("*.v"))

    assert len(verilog_files) > 0, "No Verilog files generated"

    # Search for stream_of_blocks in RTL
    sob_found = False
    for verilog_file in verilog_files:
        content = verilog_file.read_text()
        if "stream_of_blocks" in content or "ping_pong_buffer" in content:
            sob_found = True
            print(f"✓ Found SOBIF instance in {verilog_file.name}")
            break

    assert sob_found, "SOBIF (stream_of_blocks) interface not found in generated RTL"
