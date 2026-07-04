# Plan — RFSoC 4x2 bring-up: Waveflow for SDR / wireless experiments

**Status:** direction captured (2026-06-20), not started. Deferred until the VMAC timing
stages wrap. Drafted from a design discussion; treat as a starting frame, not a committed spec.

## Why
Make Waveflow useful to a wireless lab doing SDR experiments (channel sounding, array
processing) on **RFSoC 4x2** (Zynq UltraScale+ RFSoC gen3, 4 ADC / 2 DAC) and ZCU-series
boards. The applied motivation for the paper positioning ([[reference-paper-positioning]]).

## The key fit (already reasoned through)
The standard RFDC simulation methodology IS the Waveflow component model. You **cannot**
simulate the analog converter; "data from an ADC" in sim always means injecting digital samples
at the **AXIS boundary**. The pragmatic, widely-used approach (often a literal `SIM`/`SYNTH`
generate): in SIM, drive the ADC-side AXIS from a file/waveform/channel source and capture the
DAC-side AXIS; in SYNTH, the real RFDC IP. That "signal-source-replaces-converter in sim,
real-RFDC in hardware" pattern is exactly Waveflow's sim/synth duality — make it first-class.

## The block-LT simulation architecture (the user's design)
Same loosely-timed block pattern as VMAC ([[project-vmac-timing-example]]): **block = the
transaction, numpy = the function, block-duration = the timing.** Process samples in blocks
(`blksize` e.g. 1024), NOT per-IQ-sample — one SimPy event per block, timed at `blksize/fs`,
with the channel computed by numpy. Avoids a SimPy event per sample.

- **CDC fits naturally at LT:** each clock domain is a SimObj at its own block cadence; blocks
  cross between them. LT captures rate-match + crossing latency + backpressure; abstracts the
  FIFO internals/metastability (a verified-IP / RTL-sizing concern). Backpressure (fabric can't
  keep the converter rate) → the SAME queue/occupancy machinery as VMAC's mm-queue.
- **`blksize` is the sim-grain knob** (fast/coarse): make it compatible with the DSP's natural
  block (FFT size, AXIS samples-per-beat × int) to avoid re-blocking. Block→AXIS packing is the
  same duality as VMAC (one block event in sim, N-samples/beat in hardware).

## Components to model
- `RfdcAdc` — AXIS master; params = sample rate, decimation, NCO/mixer freq; `run_proc` emits
  sample blocks from a signal/channel model. DDC (NCO mix + decimate) modeled functionally.
- `RfdcDac` — AXIS slave sink (capture to file / compare). DUC functionally.
- `Channel` — sparse FIR (multipath taps) + Doppler (time-varying phase), applied to blocks.
- The fabric DSP between them = generated HLS kernels (the existing Waveflow path).

## The one real subtlety: inter-block state (overlap)
Channels and stateful DSP (filters, DDC) have memory spanning block boundaries → the channel /
filter SimObjs MUST carry state across blocks (overlap-save / keep last `L-1` samples; Doppler
phase accumulator carried across blocks). Discontinuities at block edges if state resets per
block — bake the overlap discipline in from day one.

## Fidelity boundary (name it, like the VMAC LT work)
Feedforward DSP (filters, FFT, channelizers, mixers, matched filters) is **block-perfect**.
**Sample-level feedback loops** (carrier recovery, timing recovery, AGC) have dynamics block
granularity can't resolve — model those functionally or at finer grain. Most SDR receivers have
at least one.

## (b) Vivado TCL autogen — bounded but real
RFDC is hardened IP instantiated via IPI TCL (`create_bd_cell` + a large pile of
`set_property CONFIG.*`: tile/converter map, sample rate, PLL/clocking, mixer, decimation). If
the RFDC config is the component's params, emitting that TCL is mechanical codegen. Intricate
and **device/board-specific** (4x2 gen3 vs ZCU111 gen1 differ) — template from a known-good
reference BD and parameterize. Clocking is the fiddly part.

## (c) RTL sim path — hardest, not Waveflow-specific
The same "decouple at AXIS, drive with vectors" pattern; the ADC model's generated sample
vectors drive an RTL/cosim testbench at the AXIS boundary (exactly today's HLS cosim stimulus,
scaled to the converter boundary). System-level RTL sim through the whole chain (vs per-kernel
cosim) is new integration work.

## Staging (first milestone first)
1. **Loopback** — DAC → modeled `Channel` → ADC with a trivial DSP (decimating FIR / DDC),
   modeled end-to-end with a file-driven ADC source; prove the sim/synth duality on the 4x2.
2. **Channel sounder** — transmit a known sequence (Zadoff-Chu / PN), pass through the
   sparse-FIR+Doppler channel, correlate at RX to estimate the CIR. Real experiment; the golden
   (the known CIR) is trivially checkable against the estimate. Exercises overlap/state.

## Ecosystem
Complementary to **RFSoC-PYNQ** (runtime: Python drives the deployed bitstream), not competing —
Waveflow is design-time (model + generate the overlay logic). A lab uses Waveflow to build, PYNQ
to run.
