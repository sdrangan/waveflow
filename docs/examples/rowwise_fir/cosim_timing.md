---
title: Extracting timing from cosim
parent: Rowwise FIR
nav_order: 7
has_children: false
audience: python
api: [VcdParser, AximmBeatType, CalibDataFrame]
summary: "Measuring the FIR timing from the RTL co-simulation VCD: extract the X-read and Y-write bursts, count transfer beats for true channel occupancy, read off the per-stage spans and the whole-kernel, and sweep over a (n_row, n_col) grid to build the calibration corpus that the fit consumes."
---

# Extracting timing from cosim

The co-simulation produces a VCD; calibration turns it into **datapoints**. This is the FIR-specific
instance of the general [calibration playbook](../../guide/calib/) and the
[AXI-MM burst extraction](../../guide/timing/aximm.md) tools — driven by
[`fir_calibrate.py`](../../../examples/rowwise_fir/fir_calibrate.py) (`measure_cosim`).

## What to measure, and where

The FIR command has two bus-visible operations, each a top-level burst: the **X-read** and the
**Y-write**. From the VCD those are the read and write bursts on the `m_axi` port. The
[`begin`/`end` events](./timing_model.md#where-the-events-are-logged) bracket exactly these, so the sim
and the cosim measure the *same* spans.

```python
from waveflow.utils.vcd import VcdParser, AximmBeatType

def transfer_beats(burst):
    return sum(1 for b in burst["beat_type"] if b == AximmBeatType.TRANSFER)

vp = VcdParser(vcd); clk = vp.add_clock_signal()
sigs, _ = vp.add_aximm_signals(prefix=gmem_prefix, dir="both")
writes, reads, clk_ns = vp.extract_aximm_bursts(clk_name=clk, aximm_sigs=sigs)

read_words  = sum(transfer_beats(b) for b in reads)    # X (+ taps) — true occupancy
write_words = sum(transfer_beats(b) for b in writes)   # Y
```

The important discipline: occupancy is the **`transfer`-beat count**, not the wall-clock span. The idle
beats inside a burst are the *upstream stall* (the channel waiting on compute), which belongs to the
compute block, not the bus — see
[transfer-beat occupancy](../../guide/timing/aximm.md#true-channel-occupancy-count-transfer-beats). That
separation is what makes the FIR primitives physical: the read/write occupancy comes out **exactly**
`n_row·(n_col+T)` and `n_row·(n_col−T+1)` words (the kernel re-reads the `T` taps per row), with slope 1.

## Sweep into a corpus

One cosim run is one datapoint; a sweep over sizes gives the corpus. FIR sweeps
**`n_row ∈ {1, 2, 4, 8} × n_col ∈ {64, 256, 1024}`** — 12 points — recording for each the X-read span,
the Y-write span, the fill, the whole-kernel, and the transfer-beat counts:

```python
db = CalibDataFrame(columns=["n_row", "n_col", "x_read_span_cyc", "y_write_span_cyc",
                             "whole_kernel_cyc", "read_words", "write_words"])
for n_row in [1, 2, 4, 8]:
    for n_col in [64, 256, 1024]:
        db.add_datapoint(measure_cosim(n_row, n_col))   # one RTL cosim -> one row
db.save("results/cosim_grid.json")                       # the committed corpus
```

The result is the committed grid [`results/cosim_grid.json`](../../../examples/rowwise_fir/results/cosim_grid.json)
— 12 rows of ground-truth measurements. Choosing the grid deliberately (≥ 3 values per feature, an
*interior* held-out point) is what lets the [fit](./fit.md) prove it *generalizes* rather than memorizes.

## Next

The corpus is the input to the model. The [next page](./fit.md) fits the one calibrated term from it,
composes the whole-kernel, and shows the result.
