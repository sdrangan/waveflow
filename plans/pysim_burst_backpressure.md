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
