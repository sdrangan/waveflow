# Plan — a pysim burst write should stall when its consumer is full

**Status: SCOPED HERE, NOTHING BUILT.** Started 2026-09-04. Owns the back-pressure semantics of
`QueuedTransferIF._push_to_endpoint`, and the metronome parameters that exist only because it is
missing. Does **not** own `offer()`, which is correct as it stands.

---

## Next session starts here — S1

```
claude "Read plans/pysim_burst_backpressure.md, section 'S1', and build it.
        S1 is the MEASUREMENT, not the change: find every channel whose burst
        exceeds its depth before touching _push_to_endpoint."
```

---

## The finding

**A pysim burst write is back-pressured for exactly one word, then absorbed.**
`QueuedTransferIF._push_to_endpoint` (`waveflow/hw/interface.py:622-635`):

```python
if nwords_rem > 0:
    yield ep.nrx.put(1)                                   # blocks -- for ONE word
    nwords_rem -= 1
nwords_rx = min(nwords_rem, ep.nrx.capacity - ep.nrx.level)
if nwords_rx > 0:
    yield ep.nrx.put(nwords_rx)                           # fills what happens to be free
if nwords_rem > 0:
    yield ep.ntx.put(nwords_rem)                          # the rest goes HERE
yield ep.data_buffer.put(words)                           # all of it lands regardless
```

and the two containers are not alike (`:441-442`):

```python
self.nrx = simpy.Container(env, init=0, capacity=capacity)   # bounded -- bind() gives it the depth
self.ntx = simpy.Container(env, init=0)                      # UNBOUNDED
```

`nrx` *is* correctly bounded: `bind()` hands an endpoint the channel's `depth` when it declared no
`queue_size` of its own (`:997-998`). The bound is simply not enforced past the first word.

### The consequence, stated plainly

**`write()` and `offer()` are supposed to be the two answers to *who may wait*, and today they barely
differ.** The comment at `:868-880` makes the case well — *"the difference is a property of the
PRODUCER, not of the wire… an ordinary module keeps calling `write()` while a converter calls
`offer()`"* — but `_push_to_endpoint` never waits past word one, so a module behaves almost exactly
like a converter that cannot wait. `StreamIF._admit` (`:957`) is the `offer()` twin and its docstring
says outright that it uses *"the same split `_push_to_endpoint` uses"*.

**The paths should diverge, not share.** `offer()`'s split is right: a converter presents a beat
whether or not the fabric is ready, and what the fabric does not take is gone — counted in `dropped`.
`write()`'s should block.

### And this is why the metronomes exist

`ShotTxPlayer.dac_word_rate` is not a modelling preference. It is a **workaround for a producer that
cannot be paced by its consumer**: the example computes `samp_rate / samp_per_word` by hand, from the
same `samp_rate` it already used to build the converter's sample clock, because the queue will not do
the pacing. The same accommodation appears on `RfSampBufPlayer` and is documented there at length.

Remove the cause and the parameter deletes itself, along with the units trap it carries (words/s, not
samples/s) and `blk_words`'s second meaning.

## What the fix is

**Not a loop.** `simpy.Container.put(n)` already blocks on the whole amount in a single event.
Measured, not assumed:

| | result |
|---|---|
| `put(3)` into `capacity=4` | **one yield, completes** — whole-burst back-pressure, one event |
| `put(8)` into `capacity=2`, with an active drainer | **never completes** — deadlocks |

So the blocking path becomes a deletion, not an addition:

```python
yield ep.nrx.put(nwords)      # blocks until room for ALL of them -- ONE event
yield ep.data_buffer.put(words)
```

**The existing split exists precisely because of that second row.** A burst larger than the queue
would hang on a bare `put(N)`, so the code trades correctness for not deadlocking. Any fix must
handle `N > capacity` deliberately: chunk into at most `capacity` at a time, so the producer stalls
once per queue-full's worth.

## The event cost, which is the question that decides the shape

| | events per burst |
|---|---|
| burst **fits** the queue (`N <= depth`) | **1** — same as today, but now actually paced |
| burst **exceeds** the queue | `ceil(N / depth)`, **not** `N` |

Those extra events are not overhead. Pushing 16 words into a 2-deep FIFO really does stall the
producer 8 times while the consumer drains; **the events are the stalls.** Modelling that in one
event is the optimism this plan removes.

**So there is a clean design lever: size the pysim channel depth ≥ the burst.** Then every burst fits
and it is permanently one event per block. That claims nothing false about hardware — `StreamIF.depth`
at a **boundary** port is already meaningless at RTL (Vitis ignores it, silently; see
`reference-fifo-depth-is-physical`, which was the calibration blocker), so choosing a pysim depth that
matches the block quantum is a modelling decision, not a hardware claim.

## What it costs, and why this is its own arc

**Every design whose channel is shallower than its burst now stalls where it previously did not.**
That is a timing change across 87 XSI gates and the calibration corpus. It is the entire reason this
has not already been done, and the reason it must not ride along inside another stage.

The change itself is four lines. The arc is the re-measurement.

## Stages

### S1 — the measurement, before any change

Find every channel where a burst exceeds its depth. That set **is** the blast radius, and it is
knowable without touching the model: walk each design's interfaces, compare each producer's burst
size against the bound `bind()` gave the slave.

Report it as a table. A design whose bursts all fit is a design this change cannot move — and if the
set turns out small, the arc is much cheaper than feared.

**Do not change `_push_to_endpoint` in S1.** The point is to know what will move before it moves.

### S2 — the change, with the deliberate `N > capacity` policy

Make the blocking path block on the whole burst, chunked at `capacity` so a burst larger than the
queue stalls repeatedly rather than deadlocking. **Leave `offer()` and `_admit` alone** — a converter
that cannot wait is correct as it stands, and the two paths are supposed to diverge here.

Re-measure everything S1 named. A number that moves is expected; a number that moves on a design S1
said could not move is a finding.

### S3 — retire the metronomes

`ShotTxPlayer.dac_word_rate` and its twin on `RfSampBufPlayer`. The player becomes a plain loop —
write a block, poll the lock, repeat — paced entirely by back-pressure flowing upstream from
`RFSampIF` through `Rfdc`. Nothing declares a rate.

`blk_words` stops carrying two meanings and becomes only the lock poll period.

## Traps

**`ntx` is load-bearing accounting, not dead weight.** The read side takes `min(nwords, ntx.level)`
from it *first*, then the remainder from `nrx` (`:457-468`). Deleting the overflow dump without
following that through leaves the consumer's accounting unbalanced — and it raises
`RuntimeError("Not enough words in RX queue")` rather than failing quietly, which is the good case.

**The split lives in two places.** `_push_to_endpoint` (blocking) and `StreamIF._admit`
(non-blocking). They are meant to share what a burst *does* to the queue and differ only in what
happens when there is no room. After this change they genuinely differ, so the shared-helper framing
in `_admit`'s docstring needs rewriting rather than leaving a comment that describes the old world.

**A bare `put(N)` with `N > capacity` hangs forever, and a hang in pysim looks like a slow test.**
Whatever chunking is chosen needs a gate that exercises `N > depth` directly.

**Cycle counts are measurements.** Nothing carries over.

## Not in scope

- `offer()` / `_admit` / the `dropped` counter. Correct as they stand.
- The RTL side. This is a pysim-fidelity change; no generated C++ or Verilog moves.
- `RFSampIF`'s own producer/receiver split, which already models this correctly (`put()` yields, and
  `deliver()` does not).
