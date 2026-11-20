# Skill test (modules) 1: three blocks, described by what they talk to

Write three hardware blocks with Waveflow, as Python modules simulated in pysim. **Nothing is
synthesized or simulated in Vitis**, and no HLS C++ is asked for. Everything you write must run in
Python without Vitis, and should be run.

Each block below is described the way its user described it: by what it receives, what it sends and
who it talks to. Turning that into ports and protocols is your job. Where a description leaves a
choice open, make it.

All streams in this design are 32 bits wide.

## Block A: contrast stretch

An image-processing block. Another block upstream gives it grayscale image rows to process, and a
block downstream takes the processed rows.

For each row the block is given an offset `lo` (0..255), a gain `g` (1..255) and the row's pixels
(8-bit unsigned, 1 to 4096 of them), and it produces the row's processed pixels,

```text
out[i] = clamp(((p[i] - lo) * g) >> 4, 0, 255)      (the subtraction and product signed, exact)
```

A gain of 0 is a bad request. Downstream also needs to know, for each row, whether it was processed
correctly.

## Block B: burst energy monitor

A radio receiver's monitor. It runs from power-up; nothing starts it.

- **Samples** come from the ADC block in frames of exactly 256 complex samples, each frame ending with
  TLAST. A sample is a signed 16-bit I and a signed 16-bit Q in one 32-bit word, I in the low half.
- For each frame the monitor computes `E = (sum of I*I + Q*Q over the frame) >> shift` and, if
  `E >= threshold`, sends a **report**: the frame's number (0, 1, 2, ... since power-up) and `E`.
- The **host processor**, over the AXI bus:
  - sets `threshold` (32-bit) and `shift` (0..31) at any time. New values apply from the next frame
    that starts after they are written; a frame is never processed with a mix of old and new values.
    The monitor must not process any frame before the host has set them once.
  - reads the reports. It must be woken when a report is waiting; it must not poll.
  - can read, at any time, how many frames the monitor has processed and the last fault it saw.
- A frame shorter than 256 samples is a fault. After a fault the monitor processes nothing more until
  it is reset.
- Our firmware engineer wants the usual interrupt registers too: an interrupt-enable register, and an
  interrupt-status register the host clears by writing 1 to it, as on AMD's IP blocks.

## Block C: correlator

A correlator, fed by a controller block on the same chip (no bus).

- The controller sends **templates**: up to 32 signed 16-bit taps each. Templates change rarely.
- It sends **jobs**: a block of `n` signed 16-bit samples (1 to 2048) to correlate against a template,
  `y[k] = sum_j h[j] * x[k + j]` for `k = 0 .. n - L` (`L` the template's length; a job with `n < L`
  is a bad request). The results go to a block downstream as signed 32-bit values (`>> 8`, then
  saturated to 32 bits).
- **Templates and jobs must be on two separate streams**: the templates come from a different part of
  the controller than the jobs.
- Each job must be correlated against the template the controller meant for it, even when a new
  template is sent while earlier jobs are still waiting.

## What to write

In this folder:

1. **`block_a.py`, `block_b.py`, `block_c.py`**: each block's message schemas, a golden model of its
   arithmetic (plain Python / numpy), and the block as a Waveflow module: its ports, its behaviour in
   pysim, the scenarios it is tested with, and anything a bus needs to reach it. No HLS body.
2. **`test_blocks.py`**: pytest tests that run each block in simulation on its scenarios and check
   what it sent against the golden, including each block's error or fault case. They must pass.
3. **`DESIGN.md`**: for each block, a table with one row per interface its user described (a stream,
   a setting, a report, a notification, a register, ...): what you declared for it -- port, direction,
   the message it carries -- and what connects it to the outside, if anything; then every choice you
   made that the description left open, with the reason. Say if anything asked for cannot be done with
   Waveflow, and what you did instead.
