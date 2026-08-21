# Plan: the RF lab platform

**Status: SCOPE, not started.** 2026-08-20. This is a *reframing* of the work in
`plans/rf_samp_new.md`, not a replacement: the same TX/RX machinery, aimed at a different consumer.

## What it is

**A repeat-transmit buffer plus a timed capture, driven from Python.** That pair covers most of a
wireless curriculum — synchronisation, matched filtering, channel sounding, modulation and demod,
equalisation, OFDM — for any lab that does not need to close a loop in real time.

The insight that makes it a platform rather than an example: **almost everything in wireless can be
taught offline if the transmit side repeats and the capture is time-aligned.** Students generate a
waveform in Python, load it, capture it, and process the capture offline. The hardware's only job is
to be *faithful and boring*.

## Why the first lab works without triggering

**Capture `2 × NSAMP` and at least one complete waveform is guaranteed uncut**, wherever the capture
starts. That removes triggering from lab 1 entirely:

1. Student builds a PSS / WiFi preamble in Python.
2. Loads it; it repeats forever.
3. Captures 2× the buffer.
4. Offline: matched filter, find the peak.

The peak *is* the sync offset — so the lab teaches correlation, and then hands the student the exact
measurement that motivates triggered capture in the next lab. The curriculum sequence falls out of
the design rather than being imposed on it.

Channel sounding is the same shape with a known sequence and a different offline step. So is most of
the rest.

## The product surface is a Python API, not a block design

Students will not open Vivado or read HLS. What they touch is:

```python
from waveflow.lab import RfLab

lab = RfLab(overlay="rf_lab.bit")       # or RfLab(sim=True) -- SAME SCRIPT
lab.tx.load(waveform)                   # complex baseband; DMA in, repeats forever
rx = lab.rx.capture(2 * len(waveform))  # complex baseband out
assert lab.status().clean               # no drops, no underruns -- SEE BELOW
```

Everything else — the loader, the player, the converter, the reverse channels, the counters — is
implementation. **This API is the deliverable**, and it should be designed before the RTL it sits on
is finished, because it decides what the RTL has to expose.

### `RfLab(sim=True)` is the differentiator

The same script drives the pysim model and the board. A student develops the lab in simulation, sees
the correlation peak, then runs it unchanged on hardware. When the two differ, **that difference is
the lesson** — quantisation, dropped samples, timing skew — and it is measurable rather than
anecdotal.

Very few teaching platforms can do this. It is a stronger argument for the whole Waveflow approach
than any throughput number, and it should be the thing the platform is *sold* on.

## Three properties a lab depends on

**1. Failures must be legible in Python.** A student who gets noise needs to know whether it is their
algorithm, a dropped sample, or a bad configuration. `ADC_DROPPED != 0` silently ruins a correlation
and looks *exactly* like a bug in their matched filter — which is the worst possible failure mode for
teaching, because it teaches the wrong thing.

So `lab.status()` is not diagnostic garnish. It is load-bearing, and `assert lab.status().clean`
belongs in the lab template so a student never debugs an algorithm against a corrupted capture.

**2. Captures must be contiguous.** A single dropped sample destroys a correlation. The design
already treats `ADC_DROPPED == 0` as a law; here that law is what makes the lab *possible*, not just
correct.

**3. Reproducibility.** Same waveform in, same capture out modulo noise. Any run-to-run variation
that is not the channel is a defect, because a student cannot debug against a moving target.

## `iq_mode` is required, not optional

PSS, WiFi preambles, channel sounding — all complex baseband. **The labs do not work without
`iq_mode = 1`**, which the `Rfdc` constructor currently refuses. Two things it needs, both mechanical
but neither free:

- **The RF-side bundle format** is `float64`, one real sample per 64-bit word. Complex needs a
  manifest field and an I/Q convention.
- **The quantizer's conformance twin** covers real `FixedField` only. Complex quantises I and Q
  independently with the same field, so the twin needs to cover a pair.

And one real decision hiding inside: **the I/Q slot order in a word.** That is invisible at
`samp_per_word == 1` — which is the standing trap in this repo (*"the bug hides at LW=1"*) — so it
must be pinned with a test at `samp_per_word >= 2` and stated as a convention, not discovered.

`axis_bitwidth = samp_per_word * nbits * (2 if iq_mode else 1)` already exists and is right; nothing
in the arithmetic changes.

## The labs are NOT rate-relaxed — channel sounding sets the bar

An earlier draft of this file claimed the labs could live at 10–100 MSa/s and were therefore
decoupled from the throughput arc. **That is wrong, and the counter-example is channel sounding.**

A sounder's delay resolution is roughly `1/B`:

| sample rate | delay resolution | path-length resolution |
|---|---|---|
| 100 MSa/s | ~10 ns | ~3 m |
| 500 MSa/s | ~2 ns | ~60 cm |
| 1 GSa/s | ~1 ns | **~30 cm** |

Indoor multipath needs sub-metre, so the sounding labs run at **500–1000 MSa/s**. Sync and matched
filtering are relaxed; sounding is not, and it is the lab that actually gets used.

### What 1 GSa/s costs

Design capacity is `samp_per_word × f_axis / cycles_per_word`. At 250 MHz:

| `samp_per_word` | word width | `cycles_per_word = 3` | `cycles_per_word = 1` |
|---|---|---|---|
| 4 | 64 bits | 333 MSa/s | 1000 MSa/s |
| 8 | 128 bits | 666 MSa/s | 2000 MSa/s |
| 16 | 256 bits | 1333 MSa/s | 4000 MSa/s |

**Two independent levers, and they multiply.** II=1 at 64 bits reaches 1 GSa/s exactly; so does
`cycles_per_word = 3` at 256 bits. Neither is optional if the target is 1 GSa/s with margin — and
the wider path needs the `>64` audit (`unpack_samples`, the `Rfdc` guard, the bundle round trip),
while the II path needs `while (1)` to actually schedule at 1.

**Shipping a 3-cycle player is not acceptable.** If a hand-written streaming body cannot reach II=1,
that is a finding about the platform, not a parameter to design around.

## What this changes about `rf_samp_new.md`'s stages

- **Stage 1's circular player becomes framework**, not `examples/rf_repeat_play` — the same move
  `RfSampBuf` made. Stable interface, documented, versioned.
- **Runtime `nsamp` moves onto the critical path.** A student changing waveform length is the normal
  case. Build-time `MAX_NSAMP`, runtime `nsamp`, refusal above it — the pattern `RxCmd.nsamp` already
  uses against buffer depth.
- **`stat_out` stops being optional.** It is how `lab.status()` is implemented, and property 1 above
  makes it required.
- **Stage 2 (timed capture) is the second lab**, so its "constant measured delay" gate is not just a
  correctness check — it is the number a student needs in order to interpret a capture.

## Future: long capture, periodic drain to Ethernet

A sounding run wants far more capture than fits on-chip, and the natural sink is a host over
Ethernet: capture a long window, drain it periodically, repeat.

**That is a peripheral in the exact sense `plans/rf_example_restructure.md` defines** — a boundary
whose far side you refuse to model at RTL. Nobody is going to simulate a TCP stack cycle-accurately,
so the design gets captured at the Ethernet boundary in pysim and replayed at RTL from vectors. It is
the second user of that flow after the converter, and the first one where the "refuse to model the
far side" argument is self-evident rather than argued.

Not scoped here. Recorded so the peripheral flow has a known second customer, which is what tells you
whether its abstraction is right.

## Open questions

- **Where does the host library live?** `waveflow/lab/` is the obvious home, but it imports PYNQ,
  which only exists on the board. Needs a split: a backend-neutral API, a PYNQ backend, and a pysim
  backend — with the pysim one importable anywhere.
- **What is the capture depth limit?** Streaming straight to PS DRAM makes it effectively unbounded,
  which is better than a BRAM-based capture — but the DMA has to sustain the sample rate. At lab
  rates that is comfortable; the crossover point should be measured and documented rather than
  discovered by a student.
- **How much of a lab is a template?** A notebook that already does load / capture / assert-clean, so
  a student writes only the offline processing. Probably yes, and it is where `assert
  lab.status().clean` gets into every student's script by default.
- **Which board(s)?** RFSoC 4x2 first. The AUP-ZU3 has no converters, so the labs that need RF do not
  run there — but `RfLab(sim=True)` does, on any machine, which may matter more for a class than the
  hardware does.

## Relationship to other plans

- `plans/rf_samp_new.md` — the machinery this sits on. That plan's stages are the implementation;
  this one is what they are *for*.
- `plans/adc_model.md` — the converter model, `iq_mode`, and what the real RFDC does that the model
  does not.
- `plans/rf_example_restructure.md` — capture-replay, whose limit (*a reactive host costs you a
  second model*) is why the repeat scheduler lives in fabric.
