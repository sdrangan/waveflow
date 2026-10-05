---
title: FFT testbench
parent: A vendor FFT, frames in and out
nav_order: 1
summary: "VitisFftTB, the testbench graph that both the pysim and the XSI harness are built from: four stream drivers, the FFT, four sinks. The two scenarios -- frames back to back, and isolated frames whose gaps sweep arrival phase -- the per-burst gaps that make the second possible in both backends, how the RTL is timed at its ports from the BFMs themselves with no waveform, how every run checks every frame's bits, and the gates."
---

# The testbench

## One graph, two backends

```
StreamDriver x 4  ->  VitisFft  ->  TimedSink x 4
```

`VitisFftTB` (`waveflow/vitis_l1/testbench.py`) is a `FreeRunMod` whose `potential_targets` is
`SEQUENTIAL_XSI_TB`: it is not a kernel, it is a testbench, and the generator lowers it to an XSI
harness instead of a top. Every participant maps to a pre-written, cycle-exact C++ model:

| Python participant | XSI model | role |
|---|---|---|
| `StreamDriver` x 4 | `AxisMaster` | plays one burst bundle per lane, one burst per frame |
| `VitisFft` | the synthesized RTL | the DUT |
| `TimedSink` x 4 | `AxisSlave` | collects every word, with the cycle it arrived |

The **scenario lives in files**, not in either backend: `write_scenario` writes one burst bundle per
input lane (`vectors/s_in_<j>`), and both the pysim drivers and the XSI `AxisMaster`s play those same
bytes. The golden comes from the bit-exact model (`waveflow.vitis_l1.fft`) on the same frames.

## Two scenarios

The timing calibration needs two different kinds of run, and the testbench supports both from the
same graph:

- **Back to back.** Every frame is queued from the start. The block is saturated, which measures its
  frame interval -- and is where a correct interval and a serialized one differ, which a single frame
  cannot show.
- **Isolated frames.** A gap before each frame, long enough that it finds the block idle, and varied
  pseudo-randomly over more than one period of the core's internal commutator so the arrivals sample
  every phase of it (`waveflow.vitis_l1.rtl.sweep_gaps`; 48 frames per length). This measures the
  processing delay, and its spread.

Gaps are a property of the driver, in both backends: `StreamDriver.burst_gaps` (a per-burst list) and
`burst_gap_cycles` (a constant) are `DynParam`s, which the harness emits as member assignments on the
C++ `AxisMaster`, so pysim and RTL space the bursts identically:

```cpp
s_in_0.burst_gaps = { 117, 114, 112, 108, ... };
```

## Timing the RTL without a waveform

The calibration needs, per frame, its first and last input beat and its last output beat. The BFMs
see every beat as it happens, so they record them: each `AxisSlave` dumps its words with their arrival
cycles (`out_bundle`), and each `AxisMaster` dumps the words it handed over with their acceptance
cycles (`accept_bundle`), on the same cycle count. `rtl.capture_beats` reads those, and
`rtl.frame_times` reduces them to per-frame `in`, `last_in` and `done`.

So a calibration run is **untraced**: no waveform dump, no VCD parse. The waveform route
(`rtl.port_beats`, through Waveflow's `VcdParser`) gives identical frame times and is kept for the
cross-check and for looking inside the block -- which is how the commutator's cadence was found
(see [Timing](timing.md)).

## Every run checks every frame

`rtl.check_bits` compares every frame the RTL emitted against the golden, in every scenario of every
calibration run. It is not a formality: it caught a run at `L = 1024` that completed normally with
4 correct outputs of 1024, because XSI had not loaded the ROM that holds the twiddles there (now
staged by `render_rtl_f`). A timing measurement from that run would have been a number about wrong
hardware.

## The gates

`tests/examples/test_vitis_fft_xsi.py`, under `-m xsi`, at `L = 16` (RFSoC 4x2, 250 MHz):

- `test_vitis_fft_rtl_back_to_back_cycles`: four frames done at `[43, 84, 125, 166]` cycles from the
  first input beat, exactly. A change is a real behaviour change, in either direction.
- `test_vitis_fft_pysim_within_the_measured_spread`: the pysim, timed from the platform, against both
  scenarios -- back to back exactly, isolated frames each within the platform's measured spread (zero
  at `L = 16`).
- `test_vitis_fft_platform_measurements_are_current`: the platform's committed RTL tables still
  describe this RTL, so a change that moved the timing fails instead of quietly mistiming every design
  on the platform.

`tests/examples/test_vitis_fft.py` keeps the pysim, the platform tables (every shipped length present
and converged; an unmeasured length refused) and the generated build tree honest in the fast suite,
with no toolchain.
