---
title: Synthesizing the SSR FFT
parent: The SSR FFT
grand_parent: DSP Blocks
nav_order: 4
audience: hls
api: [SsrFft, write_sources, render_config, render_wrappers, task_instances, kernel_task]
summary: "How SsrFft becomes RTL: generic hand-written task bodies (a stage, a commutator, the reorder, two lane adaptors) and a generated configuration per FFT (edge adaptors over the generated array utils, per-stage formats, twiddle ROMs, one wrapper per task). The stage body is AMD's arithmetic with its quantization points spelled out; the commutator ticks in whole groups decided by a one-bit flag; the reorder's frame buffer is a stream_of_blocks pair or a single ping-pong task, measured at L/R + 4 and L/R. Two traps found only at RTL -- a blocking read at a loop head strands a burst's tail, and a sample count in the commutator's decision broke timing -- and what csynth reports."
---

# Synthesizing the SSR FFT

## Generic bodies, generated configuration

The C++ splits along what changes with the configuration:

- **Hand-written, generic** (`waveflow/dsp/ssr_fft/src/ssr_fft_tasks.h`): one body per *kind* of
  task -- a stage, a commutator, the two reorder variants, the two lane adaptors -- each a template.
- **Generated, per configuration** (`hls.py`, from the same `Geometry` the model uses):
  `ssr_fft_<config>.h` -- per edge an adaptor struct (`value_type`, width, `read`/`write` over the
  generated `<elem>_array_utils`), per stage a struct with its formats, its sub-transform length and
  its twiddle ROM -- and `ssr_fft_<config>_tasks.h`, one plain wrapper function per task.

So a format is written once, in Python, and both backends read it; and no body hand-packs a word.
The bodies are not extracted from the Python: they are written in the vocabulary the hardware needs
(`ap_fixed` temporaries at AMD's quantization points, delay lines, a lane switch), which is not one an
extractor has. The Python twin of each body is the model function for that block, and the twin of the
commutator's *algorithm* is `cycle_ref.CommutatorTask`, kept statement for statement in step with the
C++ and tested against the model.

## The stage

One butterfly per word, then the rotation -- the vendor's arithmetic, with every quantization point
explicit, because that is where the bits are decided:

```cpp
// The library's complexMultiply: partial products truncated into T1, combined in T1, then cast to TP.
template <typename T1, typename T2, typename TP>
inline std::complex<TP> ssr_cmul(const std::complex<T1>& a, const std::complex<T2>& b) {
    T1 r1 = a.real() * b.real();
    T1 r2 = a.imag() * b.imag();
    T1 rr = r1 - r2;
    ...
    return std::complex<TP>(TP(rr), TP(ii));
}
```

A "cleaner" expression -- a full-precision complex product and one cast -- computes different bits
(measured wrong in 18 of 24 real parts). The stage's word counter `m` addresses its twiddle ROM at
`m·q` for output lane `q`; the ROM holds only the entries this stage reads.

## The commutator

Two triangles of delay lines and a lane switch ([what a commutator is](architecture.md#what-a-commutator-is)),
ticked once a cycle. The switch's slot is a function of the tick count, so **ticks come in whole
groups** of `R·D`: a bubble inserted mid-group would put samples of two groups on the switch at once.
At each group boundary the task decides what the group is:

```cpp
if (cnt == 0) {
    dt = !s_in.empty();          // data, if a word is waiting
    tk = dt || flush;            // else one bubble group, if the last group was data
    flush = dt;                  // else idle: no tick at all
}
if (tk && (!dt || !s_in.empty())) { ... one tick ... }
```

One bubble group is always enough to push the previous group's tail out (a sample leaves at most
`(2R − 1)·D` ticks after its group starts), so the decision needs one flag. An earlier version kept a
**count** of samples inside, decremented on every write: that put the output path in the decision's
timing path, and the commutator missed 250 MHz by 1.29 ns; the flag gives +0.24 ns. Every sample also
carries a valid bit through the delay lines, and only valid words are written -- nothing is written
that was not read.

## The reorder: two ways

The digit reversal ends in a frame buffer: one frame written at the permuted word addresses while the
previous one is read out in order. `SsrFft(reorder=...)` builds it either way; both are bit-exact,
both run 500 frames back to back without a stall.

| `reorder` | structure | measured interval (L = 64) |
|---|---|---|
| `"sob"` (default) | writer task `→ hls::stream_of_blocks<ap_uint<W>[L/R], 2> →` reader task | 20 = `L/R` + 4 |
| `"pingpong"` | one task holding both halves, a write side and a read side running every cycle | **16 = `L/R`** |

The `stream_of_blocks` pair costs four cycles a frame because its bodies are **single-firing**: a
write or read lock is an RAII object scoped to one frame, so the reader is re-entered -- and its lock
re-acquired -- once a frame. The ping-pong task has no lock and no per-frame entry; its two halves
change hands on two flags. At `L = 1024` the difference is 1.6%; at `L = 64`, 25%.

## Two traps found at RTL

Both passed csim. Both are general to free-running HLS, not to the FFT.

- **A blocking read at a `while (1)` head strands the end of a burst.** Vitis implements these loops
  as stall-style pipelines: when the read at the head waits, the iterations already in flight freeze
  with it. So when a burst ends, its last few words sit in a stage's pipeline registers until more
  input comes; the next commutator waits mid-group for them, and the burst's tail never leaves --
  measured: 8 frames in, 5 out, the first stage having read 128 words and written 125. Vitis refuses
  `style = flp` on these loops ("more than one exit branch"). The fix is in the bodies: **every loop
  head tests `empty()` and reads only when a word is there**, so an iteration with nothing to read
  does nothing and the pipeline keeps draining. Writes stay blocking: a full output must stall the
  task -- that is back-pressure.
- **Csim is not a verdict on `hls::task` + `stream_of_blocks`.** With a commutator back-pressuring
  into the SOB writer, csim corrupted frames; the commutator alone under back-pressure, and the SOB
  pair alone behind other tasks, were each exact. The RTL was exact. The task chain without the SOB is
  gated in csim; the reorder is judged under XSI.

## What csynth reports

At `L = 64`: every loop II = 1; 24 DSPs, the vendor's `12·(S−1)` -- one twiddle rotation per lane per
stage boundary, three DSPs per complex multiply; worst slack +0.06 ns at 250 MHz on the RFSoC 4x2.
At `L = 4096` the worst slack is 0.00 ns -- met, with no margin. Resources per length, against the
vendor core, are in the [example's resource page](../../../examples/ssr_fft/resource.md).
