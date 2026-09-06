# Plan — a pysim burst write should stall when its consumer is full

**Status: S1 and S2 MERGED (PRs #184, #185). S3 RAN AND WAS REFUTED (PR #186), 2026-09-06.**
A pysim burst write now blocks until its consumer has room, and no gate number moved.

**S3 did not retire anything, and that is the result rather than a shortfall:** back-pressure paces a
producer's *rate*, not how far *ahead of its data* it may get, so all three metronomes were kept —
each for its own measured reason. The follow-up is `plans/filler_offer.md`.

Owns the back-pressure semantics of `QueuedTransferIF._push_to_endpoint`. Does **not** own `offer()`,
which is correct as it stands.

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

---

## S1 as measured

**S2 is a real arc, not a cheap one: 19 of the 57 write-path channels carry a burst larger than
their queue, across 9 of the 12 designs measured.** The reason is structural rather than incidental —
`DEFAULT_STREAM_DEPTH` is **2**, so every `StreamIF` that does not declare a depth bounds its slave at
2 words, while most producers here write a converter block (16, 64, 256 words) or a whole frame
(65, 129, 512). Those are the writes that will begin stalling.

Measured 2026-09-04 on branch `pysim-burst-s1`, at the configuration each design's **gated** run uses.
`_push_to_endpoint` and `_admit` were unchanged: they were wrapped **in the measuring process only**,
so every write was observed as it happened and the model itself was never edited.

### Method, and which half is which

* **The bound is static.** It is the slave endpoint's `queue_size` after `bind()`, which is the
  channel's `depth` unless the endpoint declared its own. Read straight off the graph.
* **The burst is dynamic.** It is `words.shape[0]` at the call site, and several producers compute it
  from a remaining-count or a frame length rather than a constant, so reading the code gives the
  *intent* and running gives what is actually presented to the queue. The histogram of every observed
  size is in the raw data; the table reports the maximum, which is what decides fit.
* **Nothing is guessed.** Every row below was observed carrying at least one burst.

### The counts

| | channels |
|---|---|
| observed, total | **69** |
| — on the `write` path (S2's scope) | **57** |
| — on the `offer` path only (**out of scope**) | 12 |
| exist but never carried a burst — *unclassifiable* | **0** |
| **write path, burst > bound — THE BLAST RADIUS** | **19** |
| write path, burst ≤ bound — cannot move | 38 |
| unbounded (`queue_size is None`) — can never stall | **0** |
| slave kept its own `queue_size` rather than taking the channel depth | 13 |

**There are no unbounded channels and no unexercised ones.** Both of those were live possibilities
before measuring; both are empty, which means the table is complete and every channel has a real
bound to be compared against.

**The `offer` split matters and the table keeps it separate.** Four channels carry a burst larger than
their queue on the `offer` path — every one of them is an ADC edge (`Rfdc` → an ingress task). Those
are **correct as they stand** and S2 does not touch them: a converter presents a beat whether or not
the fabric is ready, and what the fabric does not take is counted in `dropped`. Folding them into the
blast radius would have inflated it by a fifth and pointed S2 at code the plan says to leave alone.

### The designs

**Affected (≥ 1 non-fitting write channel) — 9:** `bram_access`, `fir_block`, `mem_copy`,
`rf_blk_delay`, `rf_loopback`, `rf_samp_buf_rx`, `rf_samp_buf_tx`, `rf_shot_rx`, `rf_shot_tx`.

**Unaffected — 3:** `rf_relayout` (writes one word at a time), and **`rf_repeat_play` /
`rf_circ_play`, which are the precedent for the plan's own design lever**: they already declare
depths of 12, 192, 256 and 320 against bursts of 3, 4 and 64. Every channel fits, so S2 cannot move
them. That is the "size the pysim depth ≥ the burst" recipe already in the tree and already gated —
worth pointing S2 at, because it is evidence the lever works rather than a hope that it will.

### The 19, by shape

Three shapes account for all of them, and they are not equally hard:

1. **A converter block into a default-2 channel** — `rf_shot_tx` `tb_dac_axis` / `tb_dut_samp_if`
   (16), `rf_shot_rx` `tb_dut_dense_if` (16), `rf_blk_delay` / `rf_loopback` `*_dac_axis` (64),
   `rf_samp_buf_tx` `tb_dac_axis` (256), `fir_block` `tb_fir_rdata_if` / `tb_fir_wdata_if` (32),
   `mem_copy` `tb_copier_copy_data_if` (128). The burst is a **declared parameter** (`blk_words` and
   its kin), so the depth lever applies directly: give the channel the block quantum and it fits.
2. **A whole frame from a file-driven driver** — `rf_shot_tx` `tb_cmd_axis` (65),
   `rf_samp_buf_tx` `tb_cmd_axis` (512), `bram_access` `tb_data_w_if` (256). `StreamDriver` pushes one
   burst per bundle frame, so the burst is the *frame length*, which is data rather than a parameter.
   These will stall unless the testbench channel is sized to the longest frame.
3. **A small command or response frame just over 2** — `bram_access` `tb_cmd_r_if` (3) /
   `tb_cmd_w_if` (4), `fir_block` `tb_cmd_if` (6) / `tb_fir_cmd_rd_if` (5), `rf_samp_buf_rx`
   `tb_cmd_axis` (3). A multi-word schema into a 2-deep channel. Cheapest to fix and easiest to miss.

Plus two that are neither: `bram_access` `tb_data_r_if` (128 into 64) and `rf_shot_rx` `tb_win_axis`
(129 into 64) — both into a **sink that declared its own `queue_size = 64`**, so raising the channel
depth will not help them; the sink's own declaration is what binds.

### Also recorded while in there

**The read side lives in TWO places, not one.** The plan names `:457-468` (`run_proc`, the push
model); the identical accounting is repeated at **`:491-502`** in `get()`, the **pull** model — and
pull is what most of these designs actually use. S2 must follow both, or the consumer's accounting
goes unbalanced on whichever path it missed.

**The write split lives in exactly two places**, as the plan says, and this was checked rather than
assumed: `_push_to_endpoint` (`:622-635`) and `StreamIF._admit` (`:963-973`) are the only two writers
of `nrx`/`ntx`. Nothing else duplicates it.

**Three more sites read the accounting and would need to follow a change:**

| site | what it does |
|---|---|
| `interface.py:946` | `offer()`'s admission test — `blocked = ep.nrx.level >= ep.nrx.capacity` |
| `interface.py:916` | a docstring describing the split as "blocking at one single point (`nrx.put(1)`)" — false after S2 |
| `_admit`'s docstring (`:958-962`) | says it is *"the same split `_push_to_endpoint` uses"*, "factored out so the two cannot drift" — after S2 they are **meant** to differ, so this framing inverts |

**Four tests assert on queue internals** and will need re-reading, not necessarily re-recording:
`tests/hw/test_interface.py:207-208` (`nrx.level == 0` and `ntx.level == 0` after drain — the `ntx`
half is the one that becomes trivially true), `tests/hw/test_reverse_stream.py:358-359, 650, 944`, and
`tests/hw/test_stream_depth.py:38-53` (which pins that an endpoint with no `queue_size` gets
`capacity == inf` — the case S1 found **zero** instances of in any gated design).

### Assumption recorded: what counts as a "design"

The twelve measured are the ones with a runnable pysim golden reachable from an example module: the
ten `-m xsi`-gated designs plus `rf_repeat_play` and `mem_copy`. Non-RF examples without a
`FreeRunMod` testbench graph (`toy`, `schemas`, `timing`, `vecunit`, `basic_vec`, …) were not walked,
because they do not build a `QueuedTransferIF` graph to measure. If S2 turns up a stall in one of
those, it is a channel S1 did not cover rather than a channel S1 said was safe.

### Assumption recorded: the maximum is what decides fit

A channel whose producer writes 1 word most of the time and 65 once is reported as **65**, because one
burst over the bound is one stall. The full histogram is preserved in the raw data
(`s1_bursts.json`, regenerable) so S2 can tell a channel that stalls once from one that stalls on
every block — `rf_shot_tx` `tb_cmd_axis` is the former (`1×4, 33×1, 65×6`) and `tb_dac_axis` the
latter (`16×44`).

### The table

`path` is which of the two writers presented the burst: **`write`** is `_push_to_endpoint`, the path
S2 changes; **`offer`** is `_admit`, which S2 leaves alone. `bound` is the slave's `queue_size` after
`bind()`.

| design | channel | producer -> consumer | path | burst (max) | bound | fits? |
|---|---|---|---|---|---|---|
| `bram_access` | `tb_cmd_r_if` | StreamDriver -> BramReadCmd | write | 3 | 2 | **NO** |
| `bram_access` | `tb_cmd_w_if` | StreamDriver -> BramWriteCompute | write | 4 | 2 | **NO** |
| `bram_access` | `tb_data_r_if` | BramReadCmd -> TimedStreamSink | write | 128 | 64 | **NO** |
| `bram_access` | `tb_data_w_if` | StreamDriver -> BramWriteCompute | write | 256 | 2 | **NO** |
| `bram_access` | `tb_dut_go_if` | BramWriteCompute -> BramReadCmd | write | 1 | 1 | yes |
| `bram_access` | `tb_resp_r_if` | BramReadCmd -> TimedStreamSink | write | 2 | 64 | yes |
| `bram_access` | `tb_resp_w_if` | BramWriteCompute -> TimedStreamSink | write | 2 | 64 | yes |
| `fir_block` | `tb_cmd_if` | StreamDriver -> FirCmdRx | write | 6 | 2 | **NO** |
| `fir_block` | `tb_done_if` | MemWStream -> StreamSink | write | 5 | 64 | yes |
| `fir_block` | `tb_fir_cmd_rd_if` | FirCmdRx -> MemRStream | write | 5 | 2 | **NO** |
| `fir_block` | `tb_fir_rdata_if` | MemRStream -> FirCompute | write | 32 | 2 | **NO** |
| `fir_block` | `tb_fir_wdata_if` | FirCompute -> MemWStream | write | 32 | 2 | **NO** |
| `mem_copy` | `tb_cmd_if` | StreamDriver -> Sequencer | write | 2 | 2 | yes |
| `mem_copy` | `tb_copier_cmd_if` | Sequencer -> MemRStream | write | 2 | 2 | yes |
| `mem_copy` | `tb_copier_copy_data_if` | MemRStream -> MemWStream | write | 128 | 2 | **NO** |
| `mem_copy` | `tb_done_if` | MemWStream -> StreamSink | write | 1 | 64 | yes |
| `rf_blk_delay` | `tb_adc_axis` | Rfdc -> RfSampBufIngress | offer | 64 | 2 | **NO** |
| `rf_blk_delay` | `tb_dac_axis` | RfSampBufPlayer -> Rfdc | write | 64 | 2 | **NO** |
| `rf_blk_delay` | `tb_loop_cmd_if` | BlkDelay -> RfSampBufCapture | write | 1 | 2 | yes |
| `rf_blk_delay` | `tb_loop_load_if` | BlkDelay -> RfSampBufLoader | write | 1 | 64 | yes |
| `rf_blk_delay` | `tb_loop_rx_wr_if` | RfSampBufIngress -> RfSampBufCapture | offer | 1 | 1 | yes |
| `rf_blk_delay` | `tb_loop_samp_if` | RfSampBufCapture -> BlkDelay | write | 1 | 64 | yes |
| `rf_blk_delay` | `tb_loop_tx_rd_if` | RfSampBufPlayer -> RfSampBufLoader | offer | 1 | 1 | yes |
| `rf_blk_delay` | `tb_loop_tx_wr_if` | RfSampBufLoader -> RfSampBufPlayer | offer | 1 | 1 | yes |
| `rf_blk_delay` | `tb_rxresp_axis` | RfSampBufCapture -> StreamSink | write | 1 | 64 | yes |
| `rf_blk_delay` | `tb_txresp_axis` | RfSampBufLoader -> StreamSink | write | 1 | 64 | yes |
| `rf_circ_play` | `ctb_dac_axis` | TxPlayer -> Rfdc | write | 64 | 256 | yes |
| `rf_circ_play` | `ctb_dut_cmd` | RfCircPlay -> TxLoader | write | 4 | 12 | yes |
| `rf_circ_play` | `ctb_dut_resp` | TxLoader -> RfCircPlay | write | 3 | 12 | yes |
| `rf_circ_play` | `ctb_dut_samp` | RfCircPlay -> TxLoader | write | 64 | 192 | yes |
| `rf_circ_play` | `ctb_dut_tx_link_ack` | (interface-owned) -> (interface-owned) | offer | 1 | 4 | yes |
| `rf_circ_play` | `ctb_dut_tx_link_fwd` | (interface-owned) -> (interface-owned) | write | 64 | 320 | yes |
| `rf_circ_play` | `ctb_wave_axis` | StreamDriver -> RfCircPlay | write | 64 | 256 | yes |
| `rf_loopback` | `rf_tb_adc_axis` | Rfdc -> RfSampIngress | offer | 64 | 2 | **NO** |
| `rf_loopback` | `rf_tb_dac_axis` | RfSampBlockRelay -> Rfdc | write | 64 | 2 | **NO** |
| `rf_loopback` | `rf_tb_dut_blk_fifo` | RfSampIngress -> RfSampBlockRelay | write | 64 | 64 | yes |
| `rf_relayout` | `tb_dut_dense_if` | RfRelayoutToDense -> RfRelayoutToSlots | write | 1 | 2 | yes |
| `rf_relayout` | `tb_in_if` | StreamDriver -> RfRelayoutToDense | write | 1 | 2 | yes |
| `rf_relayout` | `tb_out_if` | RfRelayoutToSlots -> StreamSink | write | 1 | 64 | yes |
| `rf_repeat_play` | `tb_cmd_axis` | RepeatPlayHost -> TxLoader | write | 4 | 256 | yes |
| `rf_repeat_play` | `tb_dac_axis` | TxPlayer -> Rfdc | write | 64 | 256 | yes |
| `rf_repeat_play` | `tb_dut_link_ack` | (interface-owned) -> (interface-owned) | offer | 1 | 4 | yes |
| `rf_repeat_play` | `tb_dut_link_fwd` | (interface-owned) -> (interface-owned) | write | 64 | 320 | yes |
| `rf_repeat_play` | `tb_resp_axis` | TxLoader -> RepeatPlayHost | write | 3 | 256 | yes |
| `rf_repeat_play` | `tb_samp_axis` | RepeatPlayHost -> TxLoader | write | 64 | 256 | yes |
| `rf_samp_buf_rx` | `tb_adc_axis` | Rfdc -> RfSampBufIngress | offer | 256 | 2 | **NO** |
| `rf_samp_buf_rx` | `tb_cmd_axis` | StreamDriver -> RfSampBufCapture | write | 3 | 2 | **NO** |
| `rf_samp_buf_rx` | `tb_dut_wr_if` | RfSampBufIngress -> RfSampBufCapture | offer | 1 | 1 | yes |
| `rf_samp_buf_rx` | `tb_out_axis` | RfSampBufCapture -> StreamSink | write | 1 | 64 | yes |
| `rf_samp_buf_rx` | `tb_resp_axis` | RfSampBufCapture -> StreamSink | write | 3 | 64 | yes |
| `rf_samp_buf_tx` | `tb_cmd_axis` | StreamDriver -> RfSampBufLoader | write | 512 | 2 | **NO** |
| `rf_samp_buf_tx` | `tb_dac_axis` | RfSampBufPlayer -> Rfdc | write | 256 | 2 | **NO** |
| `rf_samp_buf_tx` | `tb_dut_rd_if` | RfSampBufPlayer -> RfSampBufLoader | offer | 1 | 1 | yes |
| `rf_samp_buf_tx` | `tb_dut_wr_if` | RfSampBufLoader -> RfSampBufPlayer | offer | 1 | 1 | yes |
| `rf_samp_buf_tx` | `tb_resp_axis` | RfSampBufLoader -> StreamSink | write | 3 | 64 | yes |
| `rf_shot_rx` | `tb_adc_axis` | Rfdc -> RfRelayoutToDense | offer | 16 | 2 | **NO** |
| `rf_shot_rx` | `tb_dut_dense_if` | RfRelayoutToDense -> PingPongCapture | write | 16 | 2 | **NO** |
| `rf_shot_rx` | `tb_dut_lock_if_cmd` | (interface-owned) -> (interface-owned) | write | 1 | 1 | yes |
| `rf_shot_rx` | `tb_dut_lock_if_resp` | (interface-owned) -> (interface-owned) | write | 1 | 1 | yes |
| `rf_shot_rx` | `tb_dut_rdy_if` | PingPongCapture -> PingPongWindow | write | 1 | 2 | yes |
| `rf_shot_rx` | `tb_win_axis` | PingPongWindow -> StreamSink | write | 129 | 64 | **NO** |
| `rf_shot_tx` | `tb_cmd_axis` | StreamDriver -> ShotTxLoader | write | 65 | 2 | **NO** |
| `rf_shot_tx` | `tb_dac_axis` | RfRelayoutToSlots -> Rfdc | write | 16 | 2 | **NO** |
| `rf_shot_tx` | `tb_dut_done_if` | ShotTxPlayer -> ShotTxLoader | write | 1 | 1 | yes |
| `rf_shot_tx` | `tb_dut_lock_if_cmd` | (interface-owned) -> (interface-owned) | write | 1 | 1 | yes |
| `rf_shot_tx` | `tb_dut_lock_if_resp` | (interface-owned) -> (interface-owned) | write | 1 | 1 | yes |
| `rf_shot_tx` | `tb_dut_rep_if` | ShotTxLoader -> ShotTxPlayer | write | 1 | 1 | yes |
| `rf_shot_tx` | `tb_dut_samp_if` | ShotTxPlayer -> RfRelayoutToSlots | write | 16 | 2 | **NO** |
| `rf_shot_tx` | `tb_resp_axis` | ShotTxLoader -> StreamSink | write | 1 | 64 | yes |

**Raw data**: the histogram of every observed burst size per channel is regenerable by
re-running the measurement described under *Method* above; it was not committed, because it is a
derived artifact of a run rather than a decision.

---

## S1 — a second, independent measurement (2026-09-05)

Run separately from the one above and recorded separately **on purpose**: the scopes differ, so the
numbers are not interchangeable and blending them would produce a table nobody could reproduce.

* **Above:** 12 designs with a runnable pysim golden, reached through their example modules.
* **Here:** the whole `-m "not vitis and not xsi"` suite, by wrapping
  `QueuedTransferIF._push_to_endpoint` in the measuring process only. 18,077 burst writes observed;
  the suite finished at exactly the 6 baseline failures under observation.

The two agree where they overlap. Two things this scope adds:

### 1. `CrossBarIF` is in S2's blast radius, and it is unbounded

The table above reports **0** unbounded channels, which is right for the twelve designs — every
`StreamIF` there got a real bound from `bind()`. It is **not** right for S2's scope.

`examples/interface/crossbar_demo.py` declares `queue_size: int | None = None`, so its channels have
`capacity = float('inf')`. Ten such channels were observed carrying bursts. And `CrossBarIF` routes
through the **blocking** path — `interface.py:1663`, inside `class CrossBarIF(QueuedTransferIF)`:

```python
yield from self._push_to_endpoint(out_ep, words)
```

They are **safe** — an unbounded container can never block, so `put(N)` on one always completes
immediately — but they should be counted as safe rather than be absent. The coverage note above
(*"build no QueuedTransferIF graph to measure"*) does not cover this case: `CrossBarIF` **is** a
`QueuedTransferIF`. The gap is a class of *interface*, not only a class of example.

**For S2:** whatever chunking replaces the split must stay correct when `capacity` is `inf`. A naive
`min(remaining, capacity)` chunk loop against an infinite capacity is a bug waiting to happen.

### 2. The cost of getting the order wrong: 65,878 extra stalls

The count above says *how many* channels move. This says *what it costs* to move them without the
depth review first.

**Flipping the semantics alone would add 65,878 extra producer stalls across the pysim suite.** Those
are SimPy events, so it is a simulation-speed cost on top of the timing churn — the suite gets slower
in proportion to how badly each burst overflows its queue.

It concentrates in a few channels. A 512-word burst into a 2-deep queue is **255 stalls per write**:

| channel | burst | bound | stalls per write |
|---|---|---|---|
| ten `*_cmd_axis` (`al1`, `far`, `fc0`, `fc2`, `fc16`, `sus2`, `sus3`, `tb`, `w1`, …) | 512 | 2 | 255 |
| `tb_copier_copy_data_if` | 512 | 2 | 255 |
| `stream_if39`, `stream_if51` | 257 | 2 | 128 |

**This is the quantitative case for the conclusion the first measurement already reached.** Raise the
depths that are free to raise, *then* flip. Done in that order most of those 65,878 stalls never
happen, because a 512-into-512 channel is one event again. Done in the other order the suite pays for
every one of them and then has to be walked back.

---

## S2 Task 0 — the boundary / internal split, and it is 13 / 6

**Most of the 19 are BOUNDARY, so this is an afternoon and not an arc.** Committed before any
semantics changed, because everything downstream branches on it.

Classified **structurally**, not by name: a channel is INTERNAL iff the composite that owns it is one
that lowers to a kernel, so the channel becomes a `#pragma HLS STREAM depth=N` FIFO. A channel owned
by the **testbench** has one end on a DUT boundary port, and a top-level AXI-Stream argument cannot
carry a depth at all.

| | count | what `depth` means there | S2's move |
|---|---|---|---|
| **BOUNDARY** (testbench-owned) | **13** | a pysim-only number — Vitis ignores it at a top-level port | raise it to ≥ the burst |
| **INTERNAL** (kernel-owned) | **6** | **physical** — it is the generated FIFO | leave it; the stall is real and must be paid |

### The 13 boundary channels

| design | channel | burst | bound |
|---|---|---|---|
| `bram_access` | `tb_cmd_r_if` | 3 | 2 |
| `bram_access` | `tb_cmd_w_if` | 4 | 2 |
| `bram_access` | `tb_data_r_if` | 128 | 64 † |
| `bram_access` | `tb_data_w_if` | 256 | 2 |
| `fir_block` | `tb_cmd_if` | 6 | 2 |
| `rf_blk_delay` | `tb_dac_axis` | 64 | 2 |
| `rf_loopback` | `rf_tb_dac_axis` | 64 | 2 |
| `rf_samp_buf_rx` | `tb_cmd_axis` | 3 | 2 |
| `rf_samp_buf_tx` | `tb_cmd_axis` | 512 | 2 |
| `rf_samp_buf_tx` | `tb_dac_axis` | 256 | 2 |
| `rf_shot_rx` | `tb_win_axis` | 129 | 64 † |
| `rf_shot_tx` | `tb_cmd_axis` | 65 | 2 |
| `rf_shot_tx` | `tb_dac_axis` | 16 | 2 |

† the two whose **sink declared its own `queue_size = 64`**. `bind()` applies the channel depth only
when the endpoint declared none, so raising the channel does nothing for these; the sink's own
declaration is what binds.

### The 6 internal channels — the stalls that have to be paid

| design | channel | producer → consumer | burst | depth |
|---|---|---|---|---|
| `fir_block` | `tb_fir_cmd_rd_if` | `FirCmdRx` → `MemRStream` | 5 | 2 |
| `fir_block` | `tb_fir_rdata_if` | `MemRStream` → `FirCompute` | 32 | 2 |
| `fir_block` | `tb_fir_wdata_if` | `FirCompute` → `MemWStream` | 32 | 2 |
| `mem_copy` | `tb_copier_copy_data_if` | `MemRStream` → `MemWStream` | 128 | 2 |
| `rf_shot_rx` | `tb_dut_dense_if` | `RfRelayoutToDense` → `PingPongCapture` | 16 | 2 |
| `rf_shot_tx` | `tb_dut_samp_if` | `ShotTxPlayer` → `RfRelayoutToSlots` | 16 | 2 |

These are **real** hardware FIFOs two words deep, and the producer really does stall filling them.
Raising any of them would change the generated RTL and is a design decision, not a modelling one —
out of S2's scope. `mem_copy`'s 128-into-2 is the worst of them at **64 stalls per burst**.

### A contradiction in the tree, surfaced rather than routed around

Three places say **do not put a depth on a boundary channel**:

1. `composite_gen.py:965` `_check_boundary_depth` — **raises `LoweringError`** for any boundary port
   whose interface declares a depth ≠ `DEFAULT_STREAM_DEPTH`, because *"a depth that is silently 2 is
   worse than no depth, the number in the Python reads like a fact"* — written after a real defect
   *"that hid an ADC dropping 72 of 512 words while pysim reported a clean run."*
2. `reference-fifo-depth-is-physical`, the calibration blocker.
3. `examples/rf_shot_tx/rf_shot_tx.py` — *"No depth overrides on the three that become the DUT's own
   boundary ports."*

And one place does it anyway, deliberately and while green — `examples/rf_repeat_play`, which is why
it is one of the three unaffected designs:

> *"Deep enough for a block plus slack: the player hands a whole block over at once and the Rfdc
> consumes one per event, so a shallower queue would make the handover itself the pacing rather than
> the metronome."* — `rf_repeat_play.py:440-442`, `depth = 4 * blk_samp`

**They are reconcilable, and the reconciliation is the rule S2 follows.** `_check_boundary_depth`'s
subject is an interface **the composite itself owns** — one that becomes a kernel port. It never
fires on a testbench-owned channel, because codegen elaborates the DUT *unbound* and the boundary
endpoint's `interface` is then `None`. A testbench channel is not lowered to anything: the TB is a
`SEQUENTIAL_XSI_TB`, not a kernel.

So: **the depth on a channel the DUT owns is a hardware claim and stays refused; the depth on a
channel the testbench owns is a modelling choice and is free.** All 13 above are the second kind.

**What this does and does not cost in fidelity.** At RTL the DUT's port FIFO really is 2 deep and the
XSI BFM really does back-pressure at it — that measurement is untouched, because the two backends are
compared on **data** (byte-identity), never on each other's cycle counts. What a deeper pysim TB queue
removes is a stall against a *model* (a `StreamDriver` is a model of a DMA whose own timing is already
a modelling choice), not a stall against hardware.

**`examples/rf_shot_tx`'s comment is reversed by this stage** and is rewritten rather than left to
contradict the code beneath it.

---

## S2 step 2 as built — and the plan's chunking policy does not work

**The change is what the plan asked for at the top, and *not* what it asked for on overflow.** A
burst that fits now blocks until the whole thing fits — one event, real back-pressure. A burst larger
than its queue cannot cost `ceil(N / capacity)` stalls, because **that deadlocks**, and the deadlock
is a property of the model rather than a bug in the loop.

### The finding: chunking at capacity deadlocks against this consumer

The plan's fix was *"chunk into at most `capacity` at a time, so the producer stalls once per
queue-full's worth."* Implemented literally, the suite hangs. Reduced to nine lines:

```
t=0  put nrx 2      # chunk 1 of a 3-word burst into a 2-deep queue
t=0  put nrx 1      # chunk 2 -- blocks, queue is full
     -> producer stuck: 2 in nrx, data_buffer still EMPTY
```

The reason is the shape of the read side, which the plan and S1 both looked at without noticing what
it implies:

* the words travel as **one whole burst** through `data_buffer`;
* the consumer (`run_proc` `:457-468`, `get` `:491-502`) takes that burst **first**, and only then
  retires its words from `ntx`/`nrx`.

So a producer that chunks never reaches `data_buffer.put`, the consumer never receives, and nothing
is ever retired. **`ceil(N / capacity)` stalls is unreachable**, and implementing it would have been
modelling a word-by-word handshake this simulator does not have.

### What was built instead

`_admit_blocking(ep, nwords)` reserves `min(nwords, capacity)` in one `put`, and parks any remainder
in `ntx` exactly as before:

| | behaviour | events |
|---|---|---|
| `N <= capacity` | block until the **whole burst** fits | **1** — the intended semantics, exactly |
| `N > capacity` | block until the queue is **empty**, then hand the whole burst over | 1 stall per burst |
| `capacity == inf` | `min(N, inf) == N`, cannot block | 1, never waits |

The second row is why **`ntx` survives**, and why the read side needed no change at all: the overflow
still has to be accounted somewhere the consumer can retire it from, and the read side already takes
`min(nwords, ntx.level)` before touching `nrx`. A burst larger than its queue is modelled as *one
burst in flight at a time* rather than as a word-granular FIFO — the honest reading of a model whose
data moves in bursts.

**No chunk loop means the `inf` case needs no branch**, which is the tidier answer to S1's second
measurement: `min(N, inf)` is `N`, whereas a chunk loop against an infinite capacity never shrinks
its remainder and spins forever.

### Assumption recorded: one stall per oversized burst, not ceil(N/capacity)

The plan's event-cost table says an overflowing burst should cost `ceil(N / depth)` events, on the
grounds that *"pushing 16 words into a 2-deep FIFO really does stall the producer 8 times."* That is
true of the hardware and **not reachable in this model**, for the reason above. The six channels it
applies to are all INTERNAL (see Task 0), so the under-count is confined to designs whose real FIFO
is genuinely 2 deep, and it is an under-count of *stalls*, never of data.

Making it reachable means changing what `data_buffer` carries — a word-granular handover rather than
a burst — which is a much larger change to the transfer model and is **not** what this plan scoped.
Recorded here so a later stage can decide it deliberately rather than discover it.

### What moved: nothing

**The whole non-vitis suite is at exactly its 6 baseline failures, and `-m xsi` is unchanged at 87.**
That is not a null result, it is the result of doing step 1 first:

* the 13 boundary channels were sized to their bursts, so they never reach the blocking path's
  waiting branch at all — a burst that fits costs one event, exactly as before;
* the 6 internal channels do now stall, and no gate's asserted number depends on it. Their stalls
  are real and are now modelled; nothing downstream was pinned to their absence.

Had the order been reversed, S1's second measurement put the bill at **65,878 extra producer stalls**
across the suite, concentrated in the channels step 1 had already fixed.

### The three gates

`tests/hw/test_stream_depth.py::TestABurstWriteWaitsForRoom`:

* **`test_a_producer_into_a_full_channel_actually_stalls`** — the property the arc exists for. Two
  runs of the same graph, one with an idle consumer and one with a slow one: writes end
  `[4.0, 8.0, 12.0]` free and `[4.0, 8.0, 14.0]` stalled. Before S2 both read `[4.0, 8.0, 12.0]`.
* **`test_a_burst_larger_than_the_channel_does_not_hang`** — `N > depth` directly, asserting all
  three writes *completed* and that `nrx`/`ntx` are both zero afterwards. A hang looks exactly like a
  slow test, so the assertion is on completion rather than on a time.
* **`test_an_unbounded_channel_still_works_and_never_stalls`** — `capacity == inf`, asserting the
  writes cost only transfer time against a consumer 100× slower than the producer.

### Docstrings rewritten rather than left describing the old world

* `offer()`'s (`:916`) said `_push_to_endpoint` "blocks at one single point (`nrx.put(1)`)". It now
  says the split is what *distinguishes* the two paths.
* `_admit`'s said it was "the same split `_push_to_endpoint` uses… factored out so the two cannot
  drift". They are now meant to differ, so it says that, and says why `offer()` is right to keep the
  old policy: a converter cannot stall a physical sample grid.

**`offer()` and `_admit` are otherwise untouched**, as the plan requires.

---

## S3 as measured — **the hypothesis is REFUTED. No metronome was retired.**

S3's premise was that `dac_word_rate` exists only because a pysim producer could not be paced by its
consumer, so S2 should have made it redundant. **It is not redundant, and the reason is precise:**

> **Back-pressure paces the *rate*. It does not bound how far *ahead of the data* a free-running
> producer may get.**

Nothing in the repo was changed to remove a metronome. What follows is the measurement.

### The experiment, on `ShotTxPlayer` — the one the plan is actually about

`examples/rf_shot_tx`, the gated configuration, with `dac_word_rate` neutralised and **nothing else
touched**:

| | lead filler | playout, samples | underrun | blocks delivered | plays | grants |
|---|---|---|---|---|---|---|
| **with** the metronome | **192** | `(F,192) (P,768) (F,320)` | 0 | 20 | 3 | 1 |
| **without** it | **640** | `(F,640) (P,640)` | 0 | 20 | 3 | 1 |

**Throughput is untouched and correctness is untouched** — no underrun, the same 20 blocks, the same
three passes, the same single grant, the same verdicts. What moves is *when the shot appears*: the
first real sample is delayed from 3 blocks to 10, and the trailing quiet disappears because the run
ends mid-playout.

**And it moves AWAY from the hardware.** The RTL gate records the lead filler as **3 blocks**
(`WANT_SEGMENT_BLOCKS["cmd"] = [(True, 3), (False, 12), (True, 7)]`). With the metronome pysim reads
3 blocks and agrees; without it pysim reads 10 and
`test_both_backends_agree_sample_for_sample` would fail at sample 192. The metronome is not
compensating for missing back-pressure — it is holding the player to the converter's grid, which is
what `TREADY` does at RTL and what a queue depth cannot do.

### Why back-pressure cannot substitute

A `FreeRunMod` player never stops having something to write: when it is not playing it writes
**filler**. So the moment the downstream has room it fills it — with filler — and the shot, when it
finally arrives, queues *behind* that. Back-pressure limits the standing occupancy; it does not stop
the producer from getting the *wrong words* into the pipe first.

The metronome prevents that by making the player's own firing rate equal the converter's. That is a
different quantity from queue occupancy and no depth setting expresses it.

### It is NOT an artefact of S2's depth raise, and that was checked

S2 step 1 raised `tb_dac_axis` from 2 to `2 * blk_words = 32`, which is the obvious suspect. Sweeping
that depth with the metronome off:

| `dac_axis` depth | 32 | 16 | 8 | 4 | 2 |
|---|---|---|---|---|---|
| lead filler (samples) | 640 | 576 | 576 | 576 | 576 |

So the depth raise accounts for **64 samples of it (one block)** and the remaining **384 (six
blocks)** is intrinsic to removing the metronome. The two do interact — a deeper queue gives the
free-running player more room to run ahead into — but the interaction is the smaller half, and even
at the RTL's own depth of 2 the disagreement is six blocks.

### The other two, decided on their merits rather than by analogy

**(2) `RfSampBufPlayer.dac_word_rate` — KEPT, and it is a different shape.** It is not a plain
metronome: `period = max(fabric, demand)` models **which side is the bottleneck**. Measured with
`demand` neutralised, `n_underrun` moves **1903 → 1793**, so it is load-bearing on a number a gate
reads. Its own docstring records the failure mode as verdict-level rather than timing-level — *"the
loader could never stay ahead of it and every command would eventually be refused as too late"* — and
records a bug that *"showed a gap every third block"*, so this code has been wrong before in exactly
this area. `fabric` is not redundant under any reading, and the `max()` is doing real work.

**(3) `RfTxStream.slot_period` — KEPT, and it is load-bearing differently again.** It **raises when
unset** (*"slot_period was never set, so this player…"*), deliberately at use rather than in
`__post_init__`, because `elaborate()` passes `HwParam`s and nothing else. Its docstring gives the
same verdict-level failure as (2): without the converter's rate the player runs at the fabric's and
*"every command comes back `TX_TOO_LATE` for a reason that is in the model rather than in the
design."* Retiring it is not a timing change, it is removing a guard.

**Since (1) — the simplest, most plainly removable of the three — is refuted, (2) and (3) are refuted
a fortiori**: both do strictly more than pace, and both were already documented as doing so.

### What this means for the plan's S3

S3 as written cannot be done, and the sentence *"paced entirely by back-pressure flowing upstream
from `RFSampIF` through `Rfdc`"* is the part that does not hold. Back-pressure flows, and it is not
enough. **Nothing declares a rate** is reachable only by giving a free-running producer some other
way to know the converter's grid, and the honest options are:

* **leave it** — one float per player, documented as the converter's grid rather than as a
  workaround for missing back-pressure (what this stage did);
* **derive it** — the player could read `samp_rate` off the converter's clock the way `Rfdc` already
  does, which removes the *hand-computation* (`float(self.samp_rate) / SPW` in the example) without
  removing the concept. This is the wart the docs note already calls out, and it is a real,
  separable improvement;
* **make the filler path back-pressure-aware** — have the player offer filler rather than write it,
  so it cannot run ahead on words nobody asked for. This is a design change, not a modelling one, and
  it would need its own gates.

### `blk_words` still carries two meanings

The plan expected S3 to reduce it to *"only the lock poll period"*. It cannot, because the second
meaning — words per pysim output burst — is what the surviving metronome is charged against
(`deadline = _t0 + _blocks * (bw / dac_word_rate)`). With the metronome kept, the burst granularity
still has to be expressed, and it is still the same boundary. **Unchanged, and the docstring that
says it means two things is still true.**

### What was changed

**One documentation clause, which S2 falsified independently of S3's outcome.**
`docs/guide/rf/rfshotbuf/tx.md` said *"pysim does not back-pressure a burst write"*. Since S2 that is
false. The note now says what is actually true — back-pressure paces the rate, and S3 measured that
this is not sufficient — rather than being deleted along with a parameter that is still there.

`examples/rf_shot_tx/rf_shot_tx.py`'s `float(self.samp_rate) / SPW` **stays**, because the parameter
it feeds stays.
