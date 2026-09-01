---
title: Taking it to RTL
parent: Playing a stored shot
nav_order: 2
audience: hls
api: [FramedStreamIFSlave, FramedStreamIFMaster, RfShotTx, kernel_task, check, rtl_module]
summary: "The first boundary port in this repo with a real TLAST pin, why it has to be ap_axis and not a plain {data, last} struct, the achieved PipelineII of all six loops, and the recorded XSI cycle counts for both scenarios."
---

# Taking it to RTL

```bash
python examples/rf_shot_play/rf_shot_play_build.py --through csynth   # Vitis HLS
pytest -m xsi tests/examples/test_rf_shot_play_xsi.py                 # Vivado xsim
```

What xsim elaborates is the **wrapper** (`rf_shot_tx_top`): the kernel plus its hand-written
`bram_t2p` memory. So the testbench sees only AXI-Stream, and the converter model consumes the
playout exactly as it consumes any other design's.

## The port that grew a `TLAST` pin

Every free-running composite in this repo lowered its boundary streams to
`hls::stream<ap_uint<W>>`, which has **no TLAST pin at all** — nine designs declaring `has_tlast=True`
in Python against kernels with no such wire. That was invisible while nothing read a frame boundary.

This design reads one, and it has to. A payload word and a header word are the same 64 bits, so
without a frame boundary there is no in-band way to say *that was the end*: a host that sent fewer
words than its header declared would simply stall the buffer's counted loop, and **a hang is
indistinguishable from a deadlock.** Worse, the C++ body could not be written at all — which would
have made the SimPy twin and the hardware two different designs.

`FramedStreamIFSlave` / `FramedStreamIFMaster` are how a design asks. The pin is opt-in because it is
not free: it is a wire someone has to connect in a block diagram.

### It has to be `ap_axis`, and that is a measurement

The first attempt used `streamutils::framed_word<W>` — the plain `{data, last}` struct this repo
already uses for *internal* channels that carry a packet boundary. It compiles. And Vitis packs the
whole struct into one wide `TDATA`: at `W = 64` the port came out `[127:0] s_in_TDATA` with **no
TLAST anywhere**, and the wrapper then failed to elaborate against a pin that was never emitted.

The side channels are a property of `ap_axis`, not of having a `last` member. `framed_word` stays
right for an internal channel, where `ap_axis` is refused outright (`HLS 214-208`).

**The pragma is identical either way.** `axis` describes the protocol; the **word type** decides the
pins — which is worth knowing, because the natural guess is that a TLAST pin is something you ask for
in the pragma, and a design that changed the pragma and left the type alone gets no pin and no
diagnostic.

Here is what Vitis emitted, from the synthesis report:

| Interface | Direction | TDATA | TKEEP | TLAST | TREADY | TSTRB | TVALID |
|---|---|---|---|---|---|---|---|
| `resp_out` | out | 64 | 8 | 1 | 1 | 8 | 1 |
| `s_in` | in | 64 | 8 | 1 | 1 | 8 | 1 |
| `samp_out` | out | 64 | | | 1 | | 1 |

`samp_out` has none of them, and must not: a DAC has no packet boundary to be told about. `TKEEP` and
`TSTRB` come along with `ap_axis` whether the design reads them or not — one bit per *byte* of the
payload — so the wrapper passes them too, and the XSI driver holds them all-ones, which is what a DMA
drives for a contiguous transfer.

## The achieved II

Achieved, not target — Vitis reports both, and they differ whenever it missed:

| loop | II | what it moves |
|---|---|---|
| `take_shot` | **1** | the loader's one counted pass: forward, drain and pad |
| `drain_tail` | **1** | the residue drain on a malformed frame |
| `load_shot` | **1** | the buffer's write |
| `play_shot` | **1** | the buffer's read |
| `rf_relayout_to_slots` | **1** | dense samples → converter slots |
| `play_set_play_one` | **1** | the player's inner loop |

**The player's is the one worth stating on its own.** The streaming transmitter's player reaches II=1
while *also* maintaining an absolute slot grid, harvesting an ack channel and returning a lateness
verdict for every window. The shot player has none of those — the converter back-pressures, the
memory holds — and reaches the same II. So the simplification cost nothing in throughput.

Estimated Fmax 342 MHz against a 250 MHz target, and the kernel is 1089 FF / 1602 LUT with no DSP and
no BRAM inside it — the memory is the `bram_t2p` beside it, which is the whole point of the wrapper.

### Both loops are labelled, and that is not tidiness

Vitis names an unlabelled loop `VITIS_LOOP_<line>_1`, and nests that name into its children. So a
**comment edit above a loop renames the synthesized module** — and a gate that looks the II up by
name then *misses* and skips, which reads as a pass in a summary line. That happened here once,
between recording the names and the next edit, which is why the bodies carry labels now.

## The recorded cycle counts

Two runs of one design, because a shot buffer can accept only one load per stream (see
[Running it](run.md)). Both drive the **generated** harness; only the bundle names differ, so there is
one model of this design and not two.

| | four-verdict run | short-transfer run |
|---|---|---|
| last verdict at cycle | **292** | **76** |
| words the DAC took | **192** = 3 × 64 | **0** |
| block periods the DAC played | 14 | 14 |
| of those, zero-filled | **2** — the declared transient | **14** — all of them |
| last zero-fill at block | 2 | — |
| sample periods starved | 38 | 230 |

Exact, not bounds. A cycle count that moves is either a regression or an improvement, and both
deserve a human.

**The 38 is fully accounted for**, which is what makes it worth recording rather than tolerating: the
900-cycle run is 230 word periods long at 0.256 words/cycle, and 192 of them were fed. The other 192
were not starved at all. The block counter is the one that separates a startup transient from a
steady-state fault, because it also records *where* the last zero-fill was — and it was inside the
transient.

**Both backends measure the transient independently and get 2.** SimPy counts it on the `RFSampIF`
metronome, the RTL run counts it on the converter model, and they are checked against the same
declared number. A design whose latency changed would move both; one that moved only one would be a
twin divergence, which is this arc's most expensive failure mode.

## What the RTL run found that pysim could not

A converter-model defect, and it is worth recording because of how it presented.

`RfdcDacSlave` judged each beat by a `TREADY` it had **recomputed** from an occupancy that had
already advanced, instead of by the `TREADY` it actually drove a cycle earlier. So it captured every
word one cycle before the RTL transferred it, and its count disagreed with the handshake wherever
`TREADY` changed.

Measured here: the design put **192** beats on `samp_out` — the VCD's `TVALID && TREADY` count at
rising edges, with every internal channel agreeing — and the model counted **191**. One word in 192,
and the worst possible shape of error: the played waveform is bit-exact for 2.75 plays and then
simply stops, which reads as a design that stalls.

It surfaced here rather than in the earlier converter gates because those feed the DAC in *bursts*,
so `TREADY` is high nearly all the time. A shot player offers **continuously** at II=1, so `TREADY`
toggles on almost every beat and the disagreement has somewhere to land.

Fixing it re-timed four other gates by exactly one cycle, and every one moved toward the design:
`rf_blk_delay` now produces exactly the delay it asked for with no leftover phase, and the loopback's
first data block arrives whole instead of clipped. None of their bit-exactness claims changed, which
is what says it was a phase and not a behaviour.

## What is not measured here

**Transfer time.** Neither RFSoC DDR is calibrated in this repo, so how long a host takes to push a
shot is **uncalibrated** — stated rather than estimated. Every number above is downstream of the
stream port.
