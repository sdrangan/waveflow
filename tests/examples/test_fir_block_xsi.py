"""The block FIR's RTL gate: two flavours of declared state, bit-exact in real hardware simulation.

``examples/state_toy`` proved a ``static`` in a free-running ``hls::task`` body survives re-firing.
This proves the *design* built on that survives: coefficients held across firings, a per-block carry
that makes block-wise filtering equal filtering the whole signal, both dispatched from one compute
leaf, all through real RTL driven by the AXI-MM + AXI-Stream BFM.

**This gate earned its keep.**  csynth was clean and the first block matched on the very first run —
and the RTL was still wrong: seeding the delay line with the MAC-time invariant (``dline[k] = x[-k]``)
rather than the top-of-iteration one left the first ``SHIFT`` to slide it a slot, dropping the newest
carry sample.  Invisible to csynth, invisible in block 1 (which starts from zeros), visible only as
block 2's first samples.  Only a run against the golden could find it, which is the argument for
having the gate at all.

Needs a prior csynth (``fir_block.tcl``) plus the XSI toolchain; skips loudly rather than passing when
either is missing.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from examples.fir_block.fir_block_build import check_xsi_outputs, generate_tb

ROOT = Path(__file__).resolve().parents[2] / "examples" / "fir_block"
XSI = ROOT / "xsi"


def _require_toolchain() -> None:
    if os.name != "nt":
        pytest.skip("the XSI flow is a Windows .bat (xvlog/xelab/mingw)")
    if not (ROOT / "fir_block_proj").is_dir():
        pytest.skip(f"no csynth RTL at {ROOT / 'fir_block_proj'} — run fir_block.tcl first")
    if not (XSI / "run.bat").exists():
        pytest.skip(f"no XSI workspace at {XSI}")


def _run_xsi() -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [str(XSI / "run.bat"), "fir_block", "fir_block_bfm_tb"],
        cwd=XSI, capture_output=True, text=True, shell=False,
    )
    assert proc.returncode == 0, (
        f"XSI run failed\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}")
    return proc


@pytest.mark.xsi
def test_rtl_matches_golden_across_reload_and_carry():
    """THE gate: LOAD_TAPS -> FILTER x2 -> LOAD_TAPS -> FILTER through real RTL, every output block
    bit-exact against the stateless golden, one completion per command."""
    _require_toolchain()
    # Regenerate the scenario AND clear the previous run's dumps.  A stale bundle would let a broken
    # build "pass" on old output -- the same way a stale rtl_*.f plus a cached xsimk.dll would.
    generate_tb(ROOT)
    for name in ("out", "s_done"):
        d = XSI / "vectors" / name
        if d.exists():
            for f in d.iterdir():
                f.unlink()

    proc = _run_xsi()
    assert (XSI / "vectors" / "out").exists(), (
        f"the XSI run produced no memory dump — it did not complete\n{proc.stdout[-2000:]}")
    check_xsi_outputs(XSI)
