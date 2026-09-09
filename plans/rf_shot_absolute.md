# Plan — the index is a timestamp

**Status: BUILT. S1 2026-09-07, S2 2026-09-08, S3 2026-09-08. The arc is closed.**
`absolute_index` is a build-time `HwParam` on `ShotTxPlayer` / `RfShotTx` (S1) and on
`PingPongCapture` / `RfShotRx` (S2), defaulting to `0` — the behaviour each design already had —
lowered as a template argument on both task bodies. All four builds are synthesized and gated at RTL.
S3 is `examples/rf_shot_loopback`: both halves closed through one converter with a delayed path
between them, where **the channel delay is a difference of memory addresses**.

---

## What was built, and what it measured

**The three edits, in `waveflow/hw/rf_shot_tx.py` and identically in
`waveflow/build/shot_tx_player_task.h`:**

1. the advance and the wrap left `if (playing)` and became `if (ABS || playing)`; **the repeat count
   and the `done` stayed behind**, under a nested `if (playing)`;
2. accept sets `pending = arm` and leaves `rd` alone, where it used to write `rd = 0`;
3. `if (ABS) { if (pending && rd == 0) { playing = 1; pending = 0; } }`, **above** the write loop.

Plus the two the traps demanded: the `SHORT` answer is decided on `arm` rather than on `playing`, so
it is still owed immediately at accept; and the ACQUIRE branch clears `pending` as well as `playing`.

**One body, not two.** `ABS` is a template argument and the branches are `if (ABS)`; Vitis folds it.

### The numbers

| | default build | `absolute_index = 1` |
|---|---|---|
| `cmd` startup filler | 192 samples | **256** — the deferral, under the one-pass bound of 256 |
| `cmd` playout | 3 x 256 samples | **3 x 256**, unchanged — the `nrep` split held |
| `cmd_loop` playout | A, a gap, B | **nothing** — every load preempted before its boundary |
| DAC blocks zero-filled | 0 | **0** — a longer wait is longer filler, never a stall |
| `RESP_LAST_CYCLE` | 273 / 502 | 271 / 500 |
| `II` on all five loops | 1 | **1** — the `pending` bit costs nothing |

**No default-mode number moved.** Every one of `test_rf_shot_tx_xsi.py`'s 31 gates holds after a
fresh csynth of the changed body: 359 DAC words, 273 / 502, one underrun at cycle 4, zero
zero-filled, the same segment shapes, the same transients, the same port-overlap and collision
counts. That was the point of defaulting to `0`.

`WANT_XSI_GATES` **98 -> 115**: one new file, `tests/examples/test_rf_shot_tx_abs_xsi.py`, collecting
17. `ABS` is a template argument, so the mode is a second piece of RTL and needs its own `csynth` and
its own xsim snapshot; a gate that only ever elaborated the default would be asserting the mode's
behaviour against a simulator.

### The `_II_MODULES` rename, which had to be measured twice

Adding the argument renamed the player's modules in **both** builds:
`shot_tx_player_task_64_64_16_*` became `..._64_64_16_0_*` in the default build and `..._64_64_16_1_*`
in the absolute one. The first reading of the report directory said the default build's name was
*unchanged* — a plausible story about Vitis dropping a trailing zero — and it was a reading of RTL
synthesized before the parameter existed. `rtl_staleness` refused the stale project, the II gate
**skipped**, and the session gate failed on the skip. That is the sequence the whole no-silent-skip
apparatus exists for, working.

## Assumptions taken during the build

These were not in the plan; each is what its reasoning implies.

**`cmd_loop` under `absolute_index = 1` plays nothing, and that is gated rather than worked around.**
It is the second half of the cost line above, measured: its loads are spaced
closer than one pass, so each is preempted while still armed. Rather than invent a wider-spaced
scenario, the run is asserted as it is. It is the honest statement of what the mode costs, and it is
also the only place a stale arm would be visible: an ACQUIRE that cleared `playing` without clearing
`pending` would start the outgoing shot at the next boundary out of a memory the incoming load has
already rewritten, and the capture is supposed to be empty.

**The negative control runs in pysim, not at RTL.** What it must isolate is the *parameter*, and the
pysim pair differ in nothing else; the RTL pair differ in a snapshot as well, so a failure there would
have a second candidate explanation. The positive half of the same pairing *is* asserted at RTL.
There is a second, independent negative control at the toolchain-free tier
(`tests/hw/test_rf_shot_tx.py`), and a **positive** control for the `nrep` trap: the wrong edit,
shipped as a class, which plays two passes where three were asked for while answering every header
normally.

**The second build is a subclass, `RfShotTxAbs`, defined in the example.** It sets
`cpp_kernel_name` and the parameter's default and overrides nothing else. It exists for the *name*: a
Vitis project, an xsim snapshot and a generated ports header are all keyed on the kernel's name, and
the two variants have to sit in one example directory to be compared against each other. The
testbench graph is the same `RfShotTxTB`, cut at a different DUT class — a second graph would be a
second model of one design.

**`assert_finite_completed` also refuses a run that ends with a shot still armed.** A boundary that
never arrives is a player that stopped counting, and it would otherwise look like a shot that was
never loaded.

---

## Why

`docs/guide/rf/rfshotbuf/tx_options.md` names three coherent designs and says today sits at the
**intersection** of the other two's constraints: fixed size, which the sounding design needs, with
relative indexing, which the general design settles for. A user accepts that a shot must be exactly
`depth` words and gets nothing back for it.

Absolute indexing is what fixed size was supposed to buy. **Memory index becomes a timestamp**: if
transmitted sample *j* sits at `mem[j mod depth]` and a received sample *j* likewise, TX and RX
correlate **by address**, with no timestamping and no bookkeeping. For channel sounding that
correlation *is* the measurement.

## What changed — three edits, and they were small

The design already contains the counter. `play_chunk` writes `BW` words on **every** firing —
`samp_out.write(playing ? buf[rd + i] : SHOT_TX_FILLER)` — so the number of words it has emitted
since reset *is* the absolute word index, and `#pragma HLS reset variable=rd` anchors it at the
epoch. Three edits turn `rd` into that index:

| | today | with `absolute_index` |
|---|---|---|
| advance | `if (playing) { rd += BW; ... }` | `rd += BW` **unconditionally**, wrap unconditionally |
| on accept | `rd = 0` — *"a new waveform starts at its beginning"* | **do not touch `rd`**; set `pending` |
| start | `playing = (nrepeat != 0)` on accept | `playing = 1` when `pending && rd == 0` |

Python sites: `waveflow/hw/rf_shot_tx.py` — the accept reset, the guarded advance, and the filler
branch. C++ twin: `waveflow/build/shot_tx_player_task.h` — the same three, in the same order.

## The boundary rule, and why this is not a semantic tradeoff

The obvious cost of absolute indexing is that playout begins wherever the counter happens to be, so
**the first sample out is not the first sample of your waveform**. That is right for sounding and
wrong for *play this pulse now*, and it is what would make this a mode rather than an improvement.

**Deferring the start to a buffer boundary removes the tradeoff instead of trading it.** The
constructor already requires `blk_words` to divide `depth`, so `rd` takes exactly the values
`0, BW, 2BW, ... D-BW` and `rd == 0` occurs **exactly once per pass** — an exact test, with no
crossed-zero case to get wrong. Because the waveform fills the whole buffer *and* starts on a
boundary, sample *j* is then always emitted at an absolute index congruent to *j*: the full timestamp
property, with *a waveform starts at its beginning* kept intact.

The cost has **two** parts, and only the first is latency. A shot that plays waits at most one pass —
at the gated geometry `depth / blk_words = 4`, so at most three chunks of extra filler after a load
lands. But **a load arriving inside that window cancels the arm outright**, so a host that reloads
faster than one pass gets *nothing*, silently: every command still answers `SHOT_LOADED`. That is
starvation, not latency, and it is the number to look at before choosing this mode for a design
that switches waveforms quickly.

**It also survives the loosely-timed model, which is the part that was not obvious.** The player only
ever writes whole chunks, so the LT lead is structurally a whole number of chunks. Both backends
therefore start on a boundary of their **own** counter: they disagree about which **pass** a shot
lands in and agree exactly about **phase within the pass**. Phase is what a sounding correlation
uses, so the property this plan adds is not one the LT relaxation takes away.

## The parameter

`absolute_index: HwParam[int] = 0` on `ShotTxPlayer` and on the `RfShotTx` composite, lowered as a
template argument. **Zero is today's behaviour**, so every existing gate keeps its meaning and any
number that moves is a finding rather than an expected consequence.

It is genuinely build-time: it changes the RTL, so it is an `HwParam` and not a runtime flag. The
timing-fidelity mode (`tx_options.md`, *Loosely timed vs. matched timing*) is **not** — the RTL is
identical either way and it belongs on the simulation side. Do not add it here.

## Gates — all built

**The address is the phase.** For every played word, its address equals the absolute word index
modulo `depth`. This is the whole feature, asserted directly.

**A playout starts on a boundary.** Every segment begins at an absolute index that is a multiple of
`depth`. One assertion, and the one a reader can check by eye against a log.

**The negative control: at `absolute_index = 0` the same assertion fails.** Without it the parameter
could do nothing and every gate would still pass. `plans/lt_transient.md` shipped three gates whose
negative controls were never committed; do not repeat that.

**Load time moves the pass, not the phase.** Run the same shot with the load arriving at two
different times and assert the phase relation holds in both while the starting pass differs. This is
the experiment the merged TX/RX example wants, and it is checkable with TX alone.

**Both settings reach RTL.** Two variants, so `csynth` runs twice and `WANT_XSI_GATES` grows. Say by
how much and why rather than letting the count drift.

## Traps

**THE `nrep` DECREMENT MUST STAY GATED ON `playing`.** Today the wrap and the repeat count live in
one block:

```c
if (playing) { rd += BW; if (rd >= D) { rd = 0; if (!loop) { if (--nrep_left == 0) ... } } }
```

Moving that block out of `if (playing)` is the natural edit and it is **wrong**: the count would
decrement once per pass while the design is playing filler, and a finite shot would end early or
never start. Split it — advance and wrap unconditionally, `nrep_left` and `done` still gated on
`playing`. It fails silently: the word counts still add up.

**The SHORT path must not defer.** `if (!playing && !loop) done_out.write(1)` answers a shot that
must never play. The loader is blocked on that token, so routing it through `pending` deadlocks
rather than failing a gate. A short shot is owed its `done` immediately, at accept.

**The ACQUIRE path must clear `pending`, not just `playing`.** In practice every RELEASE carries a
fresh play command that overwrites it, but a stale arm surviving a lock handover is the kind of
defect that works until it does not.

**`SHOT_BUSY` widens.** `busy` clears on `done`, so deferring a start holds it up to one pass longer.
Cycle counts move predictably — re-measure, do not re-record.

**Preemption is unchanged, and do not claim otherwise.** An ACQUIRE still sets `playing = 0` before
granting, so the outgoing waveform is still cut mid-pass. Deferral governs only when the *incoming*
one starts.

**II must be re-measured.** The state test is trivial and the wrap moves *out* of a branch, which is
simpler rather than harder — but `test_every_pipelined_loop_reaches_ii_1` is the witness, and its
`_II_MODULES` names carry the template arguments, so a new parameter **renames every module**. Read
the names off the report directory; a stale one makes that gate skip, which reads as a pass.

## S2 — the receive half

**BUILT 2026-09-08** — what follows is the scope as written, and *What S2 built* below is what landed.

**The reason S1 gave for deferring this was wrong, and correcting it is what makes
S2 small.** S1 said *"a capture that drops a block loses its place in a way a player cannot."* That
describes today's implementation, not a necessity.

`PingPongCapture._chunk` is the mirror of the player:

```python
words = yield from self.samp_in.get(nwords_max=bw)   # EVERY firing, unconditionally
self.n_blocks += 1
...
    self.n_dropped += bw
    return                                            # <- `wp` does NOT advance
...
self.wp += bw
```

`n_blocks` is the unconditional counter — the absolute block index, already present. `wp` is
fill-driven, and that is the *only* reason the address is relative, exactly as `rd = 0` on accept was
on TX. **Derive the address from the block count and a drop leaves a hole rather than a shift**: the
capture loses the data, not its place.

### It also makes the design more deterministic

Which region a block lands in becomes a function of its **index** rather than of reader timing, so
`_free_region()`'s search collapses to *is the region this index belongs to free? if not, drop and
advance*. Fewer states, and a window's identity no longer depends on when a reader drained.

### The question S2 has and S1 did not: what is in a hole

`CAP_LOST` and the cumulative `n_dropped` already answer **whether** something was lost. Under
absolute indexing a reader also wants **where**, because the samples either side of a hole are still
correctly placed and still usable — which is the whole point.

Decide deliberately and record the choice. `assert_windows_contiguous` — *"the source is a ramp, so a
gap in the numbers is a gap in the capture and no counter has to be believed"* — is the existing
mechanism and may be enough; a per-block valid mask is the alternative and costs wire.

### Gates

Mirror S1's, including its controls, which is most of the value of doing this second:

* **The address is the phase**, on the write pins.
* **A window's `base_addr` is a function of its index**, not of reader timing.
* **The negative control**: at `absolute_index = 0` both assertions fail.
* **A drop leaves a hole, not a shift** — force a drop by starving the reader and assert the blocks
  *after* it are still at their absolute addresses. This is the assertion that S1's wrong reason
  claimed was impossible, so it is the one that carries the correction.
* **A positive control that ships the wrong edit**, as `_CountsPassesWhilePlayingFiller` does for TX.
  The natural wrong edit here is advancing `wp` on a drop while leaving the region search alone.

### Traps

**The lock is on the critical path here in a way it was not on TX.** TX's player owns one region and
yields it; RX holds two and hands them over continuously. An index-driven region choice changes *when*
a region is claimed, so re-read `plans/t2p_lock_chan.md` S2's disjoint-region argument before moving
that logic — the property that made the region enforced at RTL by construction (140 both-live, 0
shared) must survive.

**`base_addr` on the wire keeps its meaning and its width.** It is still an address, still 28 bits.
Do not turn it into an index; the lock speaks in addresses and `CaptureWindowHdr`'s docstring says so.

**Module names carry the template argument**, so `_II_MODULES` in the RX gate file must be re-anchored
against the report directory. S1 lost a run to reading names off RTL synthesized before the parameter
existed.

### What S2 built

**Two edits in `waveflow/hw/rf_shot_rx.py` and identically in
`waveflow/build/pingpong_capture_task.h`:**

1. the advance and the wrap left the placement guard and became `if (ABS || have)`; **the
   announcement stayed behind**, under a nested `if (have)`;
2. the region search became a single question asked **once per region**, at the block whose `wp` is
   the region's first address — *is the region this index names free?* — held in a `claimed` bit for
   the whole region.

`wp % region_words == 0` is the boundary test, and it is exact for the same reason TX's `rd == 0` is:
`blk_words` divides `region_words`, so it is hit exactly once per region. `base_addr` is untouched —
still an address, still 28 bits — and `PingPongWindow` takes no new template argument, because it
follows the address it is handed and has no opinion about where that address came from.

**`region_words` must be a power of two under the mode**, and the constructor refuses otherwise: the
region is `wp // region_words` and the boundary test is `wp % region_words`, which are a shift and a
mask at a power of two and a divider on the datapath's critical path at anything else.

#### The numbers

| | default build | `absolute_index = 1` |
|---|---|---|
| clean RTL run: ADC words / window words / last cycle | 640 / 516 / 2205 | **identical** |
| both memory ports live together | 140 | **132** |
| of those, writer and reader in the same region | 0 | **0** |
| `II` on all four loops | 1 | **1** |
| stalled pysim run (`stall_blocks=6`, 60 blocks): words lost | 160 | **448** |
| published `n_dropped` values | 32, 64, 96 — *not* whole windows | **256, 256** — whole windows |

**On a clean run the two builds are identical, cycle for cycle**, and that is the finding rather than
a coincidence: with nothing dropped the fill pointer and the block count advance together. It is also
why every gate that *separates* the two modes runs on a stalled run — a negative control on a clean
one would be asserting a property the default build already has.

**No default-mode number moved.** All nine gates in `test_rf_shot_rx_xsi.py` hold after a fresh
csynth of the changed body, including the 140 / 0 that `plans/t2p_lock_chan.md` S2 established.

`WANT_XSI_GATES` **115 -> 127**: one new file, `tests/examples/test_rf_shot_rx_abs_xsi.py`,
collecting 12.

#### The decision: what is in a hole

**No new wire. The existing header localizes it, and that is a consequence of the claim-per-region
rule rather than a hope.**

Holding the claim for a whole region means `have` cannot change midway, so a region is filled
entirely or skipped entirely: **an announced window is never partly stale**, and every published
`n_dropped` is a whole number of windows. Loss therefore arrives in units of one window, between two
announcements rather than inside one — and there are no per-block holes for a per-block mask to mark.

Where the hole *is* then follows from the header a host already receives. Every announced window is
exactly `region_words` words and every word the capture could not place is counted, so window *w*'s
first sample sits at absolute word index `w * region_words + n_dropped`, and the gap before it runs
from the previous window's count up to this one's. `window_abs_index()` is that arithmetic — one
author, beside `split_windows()` — and `RfShotRx.assert_windows_absolute()` is the contract asserted
against it, in both layers: the header claim (*this window says it is at the address its index
names*) and the data claim (*the ramp codes that arrived under that claim are the ones whose indices
name it*). A design that placed correctly and announced wrongly passes the second alone.

A valid mask would be a second encoding of something the header already determines, on a wire this
family has deliberately held at one 64-bit word. **`assert_windows_contiguous` was sufficient** and
is what the clean gates still use; what S2 added beside it is the *absolute* form, which the ramp
alone could not express.

#### Assumptions taken during the build

**The drop gates run in pysim, and the RTL gate says why.** `stall_blocks` is a pysim modelling field
that reaches no template argument — a reader that dawdles is not a thing the RTL can be asked to do —
so the drop-leaves-a-hole gate, its negative control and its positive control all live at the
toolchain-free tier. That is the same reasoning `plans/t2p_lock_chan.md` S2 recorded when it declined
to ship a dirty RTL build, and it is stated in the gate file rather than left to be noticed. *(A
`ready_gap` throttle on `xsi_bfm.h`'s `AxisSlave` would make a slow host expressible at RTL and give
this arc a real RTL fault-injection lever. It is a testbench-model change, not a design one, and it
is not built.)*

**The RTL gate re-measures the disjoint-region property rather than inheriting it.** That is the trap
this stage names, and the answer is that the property survives for the same reason it held before: a
region is claimed only while its `full` flag is clear, and only the capture sets that flag, at the
*end* of filling. So the reader cannot acquire a region mid-fill in either mode. 132 cycles both
live, 0 in the same region, both ports visiting both regions equally.

**The absolute stalled run needs a longer horizon than the default one.** At 40 blocks the absolute
build's coarser loss closes the horizon before a window announced *after* the loss has been drained,
and a stalled run with no post-loss window cannot show the thing S2 exists to show. `ABS_STALL_BLOCKS
= 60` is that number, measured.

**`assert_ran`'s alternation check is a property of a clean run**, and under `absolute_index` that is
not a technicality: a skipped region does not lose its turn, so a run that skipped an odd number of
them announces the same base twice in a row and is working correctly. Its docstring now says so.

**`RfShotRx` gained a `capture_cls` ClassVar**, the same seam `RfShotTx.player_cls` is, so the
positive control is one class rather than a copy of the composite.

#### The `_II_MODULES` rename

`ABS` is on the capture's template arguments and not on the window reader's, so exactly one module is
renamed and it is renamed in **both** builds: `pingpong_capture_task_64_256_2_16_*` became
`..._64_256_2_16_0_*` in the default build and `..._64_256_2_16_1_*` in the absolute one. Both files
were re-anchored against the report directory. S1 lost a run to assuming a trailing zero is dropped
from the mangled name; it is not.

## S3 — the worked example

**BUILT 2026-09-08** — what follows is the scope as written, and *What S3 built* below is what
landed. **One `Rfdc` at `n_rx=1, n_tx=1`**, a `BlockChannel` with a delay between
`tx_rf` and `rx_rf`, and both buffers at `absolute_index = 1` — which S2 has now made possible. The first thing in the repo to exercise the reason `Rfdc`
carries both directions in one module: *"the TX and RX sample counters must hold a fixed relation, and
that is a property of the converter."*

**The result worth building it for: the channel delay is an address difference.** A sample sent from
`mem[j]` arrives at `mem[(j + D) mod depth]`, so `D` is read off two window headers with no
timestamps and no correlation. That is a much stronger claim than *the capture matches*.

**It aliases at `depth`.** A delay longer than one buffer is indistinguishable from `D mod depth`, so
the geometry must be chosen against the delay being demonstrated. Say so in the example's docs; it is
the first thing a reader will trip over.

**A THIRD example, not a replacement.** `examples/rf_shot_tx` and `examples/rf_shot_rx` stay. A
loopback cannot isolate a TX defect from an RX one, and those two gate sets are the per-design
contracts. The cost is a third csynth and another rise in `WANT_XSI_GATES`, which is worth it —
*(if the intent was to retire the standalone examples, this is the line to change, and S3 shrinks.)*


### What S3 built

`examples/rf_shot_loopback`, plus one framework node and two gate files. **Both standalone examples
are untouched**, including their gate counts — asserted, by a gate in the new file that imports each
one's vocabulary and fails loudly if either is retired.

**The graph**, five participants and six edges::

    StreamDriver --[ShotTxHdr | samples]--> RfShotTx.s_in
    RfShotTx.samp_out --> Rfdc.tx_streams[0] | Rfdc.tx_rf --RFSampIF--> RfSampDelay
                                                                            |
    StreamSink <-- RfShotRx.w_out <-- Rfdc.rx_streams[0] | Rfdc.rx_rf <--RFSampIF--+

One converter carrying both directions; both buffers at `absolute_index = 1`; **the same `depth` on
both ends**, because *"sent from `mem[j]`, arrives at `mem[(j + D) mod depth]`"* needs one modulus
and not two.

**`RfSampDelay`, in `waveflow/simulation/rf_tb.py`** — the `Channel` block `rf_sample_if`'s docstring
has always reserved the job for (*"gain, fractional delay, per-channel skew and multipath belong in a
`Channel` block"*), built at the one fidelity that rule allows: bulk delay, whole samples, no
interpolation. It keeps a sample tail across the block boundary, so a delay that is not a multiple of
`blksize` is expressible — which is what makes the reading sample-granular rather than block-granular.
It declares `blk_latency = 0`, stated rather than merely true, so a graph can **sum** it.

#### The numbers

| | value |
|---|---|
| configured path delay | 96 samples |
| loop's own declared structural latency | 64 samples = one converter block |
| **raw address difference the capture carries** | **160** |
| **measured path delay** (raw − loop) | **96** — equals the configured one |
| samples agreeing on that one difference | 4576, unanimous |
| the same path one buffer longer (352) | reads **96** — aliases exactly |
| transmit tile started one block late | reads **224** = 160 + 64 |
| `absolute_index = 0`, same graph | **two** distinct differences — no single delay at all |
| receiver drops / DAC underruns / edge overruns | 0 / 0 / 0 |

**The structural term is declared, not fitted.** `RfShotLoopbackTB.loop_blk_latency` sums one
converter hop with what the nodes on the path declare, exactly as `examples/rf_loopback` does, and
driving the path at **zero** reads exactly 64 — which is the gate that says the subtraction is not a
residual chosen to make the arithmetic work.

**Gate count: 14 in `tests/examples/test_rf_shot_loopback.py` + 12 in
`tests/hw/test_rf_samp_delay.py`, all toolchain-free. `WANT_XSI_GATES` is unchanged at 127** — see
the decision below.

#### The decision: no RTL rung, and the plan's own line changed

The scope above said *"the cost is a third csynth and another rise in `WANT_XSI_GATES`, which is
worth it"*. **It is not, and this is the line to change.** Three things, in order of weight:

* **both designs in this graph are already RTL-gated at this exact mode** — 17 gates for the
  transmitter at `absolute_index = 1` and 12 for the receiver. A loopback csynth would re-derive an
  addressing claim two green gate sets already stand behind.
* **what a loopback adds is a claim about the *pair*, and that claim is loosely-timed.** The address
  correspondence is a statement about two absolute counters and a path between them; nothing in it is
  a property of either kernel's RTL. S1 recorded the same thing from the other side: both backends
  start on a boundary of their own counter and agree exactly about phase within a pass.
* **it is not cheap.** A composite top *does* lower — measured: 6 tasks, 9 internal channels, 9 ports
  including four BRAM ports — but it would be the first kernel in the repo with **two locked
  memories**, needing a two-memory wrapper and hazard manifest, and the XSI testbench would need a
  **C++ twin for `RfSampDelay`** that nothing else wants.

If the loopback is ever closed at RTL, those are the two pieces to build first. The gate file and
`docs/examples/rf_shot_loopback/run.md` both say this rather than leaving the absence to be noticed.

#### Assumptions taken during the build

**The delay is a node, and it had to be.** `rf_sample_if`'s docstring says *"bulk delay is already
`t0`"*, which is true of an epoch and wrong as an answer here: the example's whole teaching point is
that an RX offset has two sources and an address cannot tell them apart. Folding the path into `t0`
would have made them one thing and destroyed the lesson. `RfSampDelay` is what the same docstring's
*"belong in a `Channel` block"* clause always pointed at.

**The epoch gate moves `t0_tx`, not `t0_rx`.** A *later receive* tile can only cancel structural
latency the loop already has; past one block the block simply waits in a queue and the reading stops
moving. Measured: `t0_rx` at +1 and +2 blocks both read 96. That saturation is the block-LT
resolution limit showing through, and a gate that swept `t0_rx` would be asserting a saturation curve
rather than a correspondence. `t0_tx` has no such floor — +1, +2 and +3 blocks read 224, 32 and 96,
each exactly one block on from the last.

**The aliasing gate compares readings, not captures.** A first draft asserted the two runs produce
identical *bytes* and it failed — correctly. A longer path holds more silence before its first
sample, so the two runs *are* distinguishable, just not by an address. That is now asserted as well,
in the same gate: it is what says the aliasing claim is not comparing a run with itself, and it names
precisely the information an address discards and a timestamp keeps.

**Two waveforms, spaced by eight refused frames.** The measured threshold at this geometry is four —
at zero and at two the first waveform never reaches the air and **every command is still answered
`SHOT_LOADED`**, which is the trap S1 recorded, now with a gate on it. A second waveform earns its
keep: the reading is unanimous across both, which says the correspondence is a property of the
addressing rather than of one payload.

**One `absolute_index` for the pair, not one per end.** A loopback with one end absolute and the
other relative would capture perfectly good samples and read an address difference that means
nothing. Making that unreachable by construction was cheaper than gating it.

**The figure draws an address axis and no time axis.** `examples/rf_shot_tx`'s figure draws a sample
axis because that page is about playout shape; this one is about a correspondence between two
memories, and a time axis would have hidden it behind two waveforms the reader has to take on faith.

## Not in scope

- **Matched timing.** `plans/lt_transient.md` S3. Independent of this, and not an `HwParam`.
- **Variable-length shots.** The other row of `tx_options.md`, and it needs an allocator —
  `plans/t2p_lock_chan.md` S3 fenced that off as *"where this stops being an interface and starts
  being arbitration."*
- **MTS itself.** `t0_tx ≡ t0_rx` is a property of the converter and the board. This plan makes the
  buffer able to use it; it cannot make it true. S3 shows what it buys by taking it away.
- **A loopback closed at RTL.** See *The decision* under S3, and the two pieces it would need.
