---
title: Running it
parent: Playing a stored shot
nav_order: 1
audience: python
api: [RfShotTx, ShotTxLoad, ShotTxPlay, ShotTxHdr, ShotTxResp, RFSampIF, StreamDriver]
summary: "The two scenarios and why they cannot be one, the five verdicts and what each one is a repair for, and the number a DMA cannot produce. Everything on this page is measured in SimPy and re-measured identically at RTL."
---

# Running it

```bash
pytest tests/examples/test_rf_shot_play.py                  # no toolchain
python examples/rf_shot_play/rf_shot_play_build.py --through pysim
```

Both run the same graph: a file-driven driver pushing frames, the transmitter, a real converter
model, and two sinks.

## A frame

The header rides ahead of the samples on the same stream, and `TLAST` marks the end:

```
[ ShotTxHdr | w0 w1 w2 ... w63 ]
     1 word        64 words     TLAST on the last one
```

`ShotTxHdr` is four fields in one 64-bit word — an opcode, a transaction id, the number of samples
the host *believes* it is sending, and how many times to play what arrives. `ShotTxResp` is three
fields in one word: the id echoed back, a status, and **the number of samples that actually landed.**

**There is no length-of-shot field**, and that is deliberate. How many words a shot is, is build-time
structure declared once on the module; a command that restated it would be a second source that could
disagree. `nsamp` is here because it is what the host believes, and catching that belief disagreeing
with what arrived is the verdict's whole job.

## Two scenarios, and why they cannot be one

Once a shot is accepted the buffer is **busy** until its play-set finishes. A file-driven driver
pushes every frame back to back, so at most one load per stream can succeed and every later one is
`SHOT_BUSY`.

That is not a limitation of the testbench — it is what `SHOT_BUSY` *is*. A host that wanted two loads
would read its verdicts and wait between them, which a vector file cannot do. So the successful load
has to be the first frame of a stream, and the truncated-transfer case needs a stream of its own.

### Scenario 1 — four verdicts

| `tid` | the frame | verdict |
|---|---|---|
| 1 | a whole shot, three plays | `SHOT_LOADED`, `nsamp_loaded = 256` |
| 2 | a whole shot, arriving mid-play | `SHOT_BUSY` |
| 3 | `nsamp` the buffer was not built for | `SHOT_WRONG_LEN` |
| 4 | `nsamp == 0`, and no payload at all | `SHOT_ZERO_LEN` |
| 5 | `SHOT_END` — the fence | `SHOT_LOADED` |

`tid` 3 is the interesting one: it is malformed **and** badly timed, and the design promises the
fault the host can fix. A build that tested "busy" first would answer `SHOT_BUSY` here — true, and
useless, because the host would retry forever against a length the buffer was never built for. So
malformed is tested before transient, and this scenario is what says so.

`tid` 4 carries no payload at all, so its `TLAST` lands on the header beat itself. That is a distinct
branch in both twins — the loop below it reads nothing and pads everything — and no other frame in
either scenario reaches it.

`tid` 5 is a **fence**, not a halt. An `hls::task` has no loop to break: the runtime re-fires the
body forever and an `ap_ctrl_none` design has no `return` to reach. What `END` is worth is what its
*response* proves — headers are answered strictly in order, so this one says everything ahead of it
has been processed. A testbench that ended by timing out instead could not tell a finished run from
a deadlocked one.

### Scenario 2 — the short transfer

One frame, declaring a whole shot and carrying half of one, and a fence behind it.

## The five verdicts, and what each is a repair for

Collapsing any two would report one fault as another:

| status | when | what the host does about it |
|---|---|---|
| `SHOT_LOADED` | the shot is in the memory and is playable | nothing |
| `SHOT_SHORT` | `TLAST` before the shot was full | send the rest of the buffer |
| `SHOT_WRONG_LEN` | `nsamp` is not the shot the buffer holds | rebuild, or send a different waveform |
| `SHOT_BUSY` | a load arrived while a shot was playing | **retry** — the only one a retry fixes |
| `SHOT_ZERO_LEN` | `nsamp == 0` | fix the caller |

## `nsamp_loaded` — the number a DMA cannot produce

`sendchannel.transfer()` blocks until the DMA has pushed its bytes, and a completion interrupt serves
a second thread. On timing alone a host is fully covered. What the DMA does **not** know is whether
the buffer considered those bytes a valid waveform:

- **a short transfer completes cleanly.** Send fewer beats than the buffer expects and the DMA
  reports success while the buffer sits half-loaded — a block of the right shape carrying half a
  signal, and invisible from the host side;
- **a refused load** — arrived while playing, wrong length — is indistinguishable from one that
  worked;
- **"is it playable now?"** is a property of the buffer, not of the transfer.

So the verdict is the answer, and `nsamp_loaded` is *how much*: the difference between it and the
header's `nsamp` **is** the diagnosis. In the short scenario the header declares 1024 samples and the
response says **128**, which is the 32 words that actually arrived.

## What a short shot does to the memory, and what it does not do to the air

The memory really does end up holding half a waveform. That is what a short transfer physically
leaves behind, and the design does not un-write it: `RfShotBufLoad`'s inner loop is **counted** —
`nword` words, no early exit — which is exactly why it reaches II=1 where the streaming buffer cannot,
and it is not this stage's to change. So the frame is completed with zeros, the buffer fills, and it
emits its one token.

What the design refuses to do is **play** it. A shot that is not `SHOT_LOADED` is handed a repeat
count of **zero**: the token still has to be consumed, because nothing else will take it, but half a
waveform must not reach the converter. Measured, in both backends: the short run puts **zero** words
on the converter's port and every one of its 14 block periods is a zero-fill.

That is why the verdict and the repeat count travel together in the same decision.

## What the run measures that a byte comparison does not

The playout is bit-exact — 3 × 256 samples, in converter **codes**, which is what the host wrote — but
three claims sit beside it:

**The startup transient is declared, not tolerated.** A converter fed through a pipeline *must*
underrun until the first shot has been loaded, so `assert_clean(2)` demands exactly two blocks of
zero-fill and none after. An underrun past the transient fails; so does a *shortfall*, because a
design that declares two blocks of latency and exhibits one is a failure too. Measured: the ramp
first appears at played sample **128**, which is block 2 exactly, on both backends.

**The plays are whole.** A playout that stopped mid-shot has the right samples in the right order for
as far as it got — this repo's recurring failure — so the word count is checked against
`n_plays * nword` rather than assumed from it.

**The two phases never overlapped.** A read while the writer is live would return plausible samples
rather than an error, so `ShotPhase` refuses it during the run and `assert_phases_separated` then
checks the guard actually *ran*: a guard that never fired is evidence that something ran, not that the
invariant held.

## The pysim modelling accommodations, named

Two knobs on the player exist for SimPy and reach no hardware:

- **`blk_words`** — the converter's process consumes a whole `blksize` burst per event and refuses a
  partial one, so the twin hands it one block per write. The RTL body writes one word per beat and
  knows nothing about blocks.
- **`dac_word_rate`** — in RTL this task is paced by `TREADY`, and that back-pressure *is* the whole
  scheduling story of the shot design, which is why the body has no grid arithmetic in it. pysim does
  not back-pressure a burst write, so the metronome has to be handed over instead. Left unset the
  playout runs at the fabric's rate, the converter is never the bottleneck, and the one property this
  design claims — that it keeps a DAC fed — is not being tested at all.

The rate is charged on an **absolute grid**, never as a relative timeout: a relative wait restarts
from wherever the body finished, so everything it yielded for is added to the period and never given
back — the defect that once made a sibling design's player slip a whole block every fourth firing.
