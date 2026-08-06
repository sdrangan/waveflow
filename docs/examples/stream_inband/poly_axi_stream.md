---
title: AXI4-Stream Timing Analysis
parent: Streaming polynomial
nav_order: 6
has_children: false
---

# Polynomial AXI4-Stream Timing Analysis

The polynomial demo includes functions to demonstrate how to perform the 
timing analysis and waveform viewing of the AXI4-signals.  
This guide explains how to analyze the AXI4-Stream timing of the `poly`
Vitis HLS kernel from an existing VCD file — **without** rerunning RTL
co-simulation.  

The source code for everything on this page lives in
`examples/stream_inband/timing_analysis.py`. The demo uses
Waveflow's [AXI4-stream VCD analysis tools](../../guide/timing/axistream.md). 

## Overview

The `poly` kernel communicates over two AXI4-Stream interfaces plus an AXI-Lite control/status block. The VCD timing analysis covers only the streams; the AXI-Lite register map is observed end-of-simulation via `regmap_status.json`.

- **Input stream**: a DATA command header followed by input sample data, then an END command header that terminates the kernel
- **Output stream**: per-DATA-transaction response header followed by output sample data

The timing analysis API:
1. loads a VCD file
2. discovers the clock and AXI4-Stream signals
3. extracts individual bursts from each stream
4. decodes the bursts into `PolyCmdHdr`, `PolyRespHdr`, and NumPy sample arrays
5. returns everything in a `PolyTimingResult` instance

## The `PolyTimingResult` class

```python
class PolyTimingResult:
    clk_name: str        # full VCD name of the clock signal
    clk_period: float    # estimated clock period in nanoseconds
    in_signals: dict     # AXI4-Stream signal names for the input stream
    out_signals: dict    # AXI4-Stream signal names for the output stream
    bursts_in: list      # raw burst dicts from the input stream
    bursts_out: list     # raw burst dicts from the output stream
    cmd_hdr: PolyCmdHdr  # decoded DATA command header
    x: np.ndarray        # input sample array
    resp_hdr: PolyRespHdr  # decoded response header
    y: np.ndarray          # output sample array
```

Each `burst` dict has keys `data`, `beat_type`, `start_idx`, and `tstart`.

## Analyzing an existing VCD

```python
import sys
sys.path.insert(0, "examples/stream_inband")   # make the sibling poly / timing_analysis modules importable
from timing_analysis import analyze_poly_vcd

result = analyze_poly_vcd("vcd/dump.vcd")

# Decoded command header
print(f"cmd_type = {result.cmd_hdr.val['cmd_type']}")
print(f"tx_id    = {result.cmd_hdr.val['tx_id']}")
print(f"nsamp    = {result.cmd_hdr.val['nsamp']}")

# Input / output sample arrays
print(f"x = {result.x}")
print(f"y = {result.y}")

# Timing information
print(f"clk_period = {result.clk_period} ns")
print(f"Input bursts:  {len(result.bursts_in)}")
print(f"Output bursts: {len(result.bursts_out)}")
```

Expected output for a standard run with `nsamp=100`:

```
cmd_type = 0
tx_id    = 42
nsamp    = 100
x = [0.  0.010101 0.020202 ... 1.]
y = [ 1.  0.9697... ... 0.]
clk_period = 10.0 ns
Input bursts:  3
Output bursts: 2
```

## Burst structure

| Burst           | Stream | Contents |
|-----------------|--------|----------|
| `bursts_in[0]`  | input  | `PolyCmdHdr` (DATA) — cmd_type, tx_id, nsamp |
| `bursts_in[1]`  | input  | `nsamp` input samples (float32) |
| `bursts_in[2]`  | input  | `PolyCmdHdr` (END) — terminates the kernel loop |
| `bursts_out[0]` | output | `PolyRespHdr` — tx_id echo |
| `bursts_out[1]` | output | `nsamp` output samples (float32) |

Coefficients no longer appear on the stream — they are configured via the AXI-Lite register map before launch. Halt status (`halted` / `error` / `tx_id`) is read from the regmap after the kernel returns, not from a streamed footer.

## Plotting the timing diagram

```python
import matplotlib
matplotlib.use("Agg")   # omit for interactive display

from timing_analysis import analyze_poly_vcd, plot_poly_timing

result = analyze_poly_vcd("vcd/dump.vcd")

# Full-range timing diagram with color-coded bursts
ax = plot_poly_timing(result, show=True)
```

The plot colors:

- 🟠 **Orange** — header bursts (DATA cmd_hdr, END cmd_hdr, resp_hdr)
- 🟢 **Green** — data bursts (input samples, output samples)

To zoom into a specific time range, pass `trange=(t_start_ns, t_end_ns)`:

```python
ax = plot_poly_timing(result, trange=(0, 500), show=True)
```

## Stable test fixture

A minimal VCD fixture is committed to the repository for use in tests and
documentation examples:

```
tests/fixtures/poly/timing/poly_timing_fixture.vcd
```

This fixture contains a 3-sample transaction (`nsamp=3`) and is **never
overwritten** by regular demo runs.  To load it:

```python
from pathlib import Path
from timing_analysis import analyze_poly_vcd

fixture = Path("tests/fixtures/poly/timing/poly_timing_fixture.vcd")
result = analyze_poly_vcd(fixture)
```

## Generating a VCD

See [RTL cosim timing verification](./05_cosim_timing.md) for how to
capture a fresh VCD using the Python-callable `run_xsim_vcd` API or the
CLI.
