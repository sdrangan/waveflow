# Plan — one shot-buffer design, on the lock

**Status: COMPLETE — Stages A, B and C all merged (PRs #181, #182), 2026-09-04.** Four shot-buffer
designs are now two: `RfShotTx` and `RfShotRx`, both on `LockedT2pMemIF`. 22,210 lines deleted.
Nothing in this plan is outstanding; the *as built* sections below are its record.

Open work it spun off, tracked elsewhere: the wire-format arc (message widths, two-region TX — not
yet written down), and `plans/rf_shot_buf.md`'s remaining stage (pre-trigger capture).

---

## Next session starts here — Stage A

```
claude "Read plans/rf_shot_unify.md, section 'Stage A', and build it.
        MERGE ONLY -- delete nothing.  Stage B does the deletions, and only after
        Stage A's gates are green."
```

---

## Why

Four designs currently move samples through a BRAM, on two different mechanisms:

| design | tasks | mechanism | XSI gates |
|---|---|---|---|
| `RfShotBuf` (Stage A) | 2 | `ShotPhase` + `rdy` | 5 |
| `RfShotTx` (Stage B) | 5 | `ShotPhase` + `rdy` + `done` | 11 |
| `rf_shot_loop` (lock S1) | 3 | `LockedT2pMemIF` | 10 |
| `rf_pingpong_rx` (lock S2) | 2 | `LockedT2pMemIF` | 9 |

The cost is visible in the wiring. `RfShotTx` hand-wires **seven internal channels plus two
`BramIF`s** (`pay rep rdy_load rdy_play dense samp done bufw bufr`); `rf_shot_loop` binds **two**
endpoints and the lock owns the rest. Same samples, same memory.

**The decision: one design per layer, all on the lock.** Competing alternatives are not worth
maintaining, and the lock has now been verified by two independent consumers — including finding a
real defect in its own S1 wiring that only a second consumer could expose.

## `RfShotBuf` is absorbed, not retained

It is the **primitive layer**: two tasks, a memory, a `rdy` token, no command layer. Its entire job
is *arbitrate one writer against one reader over one memory* — which is exactly what
`LockedT2pMemIF` does, with something `ShotPhase` never had.

`ShotPhase` is **pysim-only by its own docstring**. Stage A's central safety claim has therefore never
had an RTL witness. The lock replaces it with `_RegionGuard` in pysim *plus* the S2 measurement: 140
cycles with both memory ports live, **0** with writer and reader in the same region. That is a
capability upgrade, not housekeeping, and it is the reason this collapse is worth its cost.

**The name survives, the class does not.** `RfShotBuf` stays as the family name in the docs
(`docs/guide/rf/rfshotbuf/` already holds `tx.md` and `tx_internal.md`). The classes become
`RfShotTx` and `RfShotRx`.

**The `t2p_bram` witness is NOT at risk.** It lives in `examples/bram_access` — the values
`100, 101, 107, 355, 228` are gated there, not in this family. `test_rf_shot_buf_xsi.py` mentions the
word only in a remark about `bram_toy`. Deleting this family does not touch it.

## The merge is real, not a rename

The two TX designs are **complementary in capability**, which is why Stage A is a build and not a
deletion:

| | `RfShotTx` | `rf_shot_loop` |
|---|---|---|
| play | finite `nrepeat`, then quiet | infinite |
| a load arriving mid-play | refused, `SHOT_BUSY` | **preempts** via the lock |
| `SHOT_LOAD` | yes | *refused* — "this design cannot provide" |
| `SHOT_LOOP` | — | required |

`rf_shot_loop` already imports `SHOT_END`, `SHOT_LOADED`, `SHOT_LOOP`, `SHOT_SHORT`,
`SHOT_WRONG_LEN`, `SHOT_ZERO_LEN`, `ShotTxHdr` and `ShotTxResp` from `rf_shot_tx`, so the vocabulary
is already shared. What does not exist is a player with both exit conditions.

### The semantics the merged design must have

**The play loop differs only in its exit condition.** Finite plays `nrepeat` passes then goes to
filler; infinite plays until asked to yield. Both poll the lock every `check_period` and both must
set filler *before* granting — that ordering is what the whole protocol turns on.

**`SHOT_BUSY` returns, for the finite case only.** Preempting a finite shot would silently truncate
something the host explicitly asked for; preempting an infinite one is the only way to ever end it.
So:

* a **finite** shot in progress → the loader answers `SHOT_BUSY` and does *not* request the lock
* an **infinite** shot in progress → the loader requests the lock, loads, releases

**That asymmetry means the loader must know when a finite shot has finished**, which is what the
`done` token already does in `RfShotTx`. Keep it for the finite path; it is not needed for the
infinite one.

**`SHOT_LOAD` and `SHOT_LOOP` both become legal.** Refusing either is what makes today's two designs
two designs.

## Stages

### Stage A — the merged TX, gated, deleting nothing

One lock-based `RfShotTx` supporting both opcodes. Build it **beside** the existing designs; do not
touch `rf_shot_buf.py`, `rf_shot_tx.py` or `rf_shot_loop.py` yet. If the merge turns out harder than
it looks, the working designs are still there.

**Gates:** finite play (`SHOT_LOAD`, *n* repeats, then quiet), infinite play (`SHOT_LOOP`, waveform
switched mid-play), a finite shot correctly answering `SHOT_BUSY` to a load arriving mid-play, and
all five verdicts. Both backends, byte-identical, with an XSI cycle count.

**Done when:** the merged design's gates are green and it demonstrably covers both predecessors.

### Stage B — the deletions

Only after Stage A is green. Remove `waveflow/hw/rf_shot_buf.py`, `rf_shot_tx.py`'s composite and its
Stage-A instantiation, `rf_shot_loop.py`, `examples/rf_shot_buf`, `examples/rf_shot_play`, and their
gates. Rename `rf_pingpong_rx` → `RfShotRx` to match. Lower `WANT_XSI_GATES` from 95 by exactly the
number of gates removed, and confirm the non-vitis baseline still stands at its 6 known failures.

### Stage C — the plan and the docs

Rewrite `plans/rf_shot_buf.md`, whose Stages C/D/E all assume the old structure. Finish
`docs/guide/rf/rfshotbuf/` — an `rx.md`, and the `tx.md` / `tx_internal.md` pages updated for the
merged design. Fix `choosing.md`, which still claims a **nonexistent** `RfStreamBuf` is built and
RTL-gated while calling `RfShotBuf` designed-not-built; both halves are false.

## One decision to make deliberately

`ShotTxHdr` and `ShotTxResp` live in `rf_shot_tx.py` today and `rf_shot_loop` imports them. In the
merged world they need an owner. Simplest is the unified TX module; a shared `rf_shot_msg.py` is the
alternative if RX ever wants them. **Say which on the page** rather than letting the import graph
decide it.

## Traps carried forward

**Set filler BEFORE granting.** Granting while still playing lets the loader write memory the player
is reading. The pysim guard is what proves this ordering; the waveform cannot.

**Enable-gating is closed.** A register guard costs nothing in II and still does not quiet the port —
Vitis owns the enable. Disjoint regions are the mechanism. See `plans/t2p_lock_chan.md`.

**A two-stream request/response with a blocking read deadlocks.** Use `read_nb` polling.

**Label every pipelined loop**, or the II gate misses by name and skips, which reads as a pass.

**Cycle counts are measurements.** Nothing from `rf_shot_play` or `rf_shot_loop` carries over.

## Not in scope

- `rf_samp_buf` / `rf_samp_buf_tx`, the *other* superseded pair. `examples/rf_blk_delay` is the
  evidence for the streaming argument, and deleting the evidence deletes the argument. Same
  reasoning, bigger call, its own conversation.
- Stage C of `plans/rf_shot_buf.md` — pre-trigger capture. `RfShotRx` gives continuous capture with
  nothing dropped, but its window is bounded by the region, so full-depth pre-trigger history is
  still unbuilt.
- `bram_t2p.v`'s one-sided `$error`.

---

## Stage A as built — decisions taken without the plan, and where they landed

Written during the Stage A build (2026-09-02), on branch `rf-shot-unify-a` off `rf-shot-unify-plan`.
**Nothing was deleted**: `rf_shot_buf.py`, `rf_shot_tx.py` and `rf_shot_loop.py` are untouched, and
their gates still run.

### The message-ownership decision the plan asked for

> **`ShotTxHdr` and `ShotTxResp` belong to the unified TX module — not to a shared `rf_shot_msg.py`
> — and Stage A gets there by *importing* them, not by copying them.**

A shared module would have exactly one consumer. `rf_pingpong_rx` (which Stage B renames `RfShotRx`)
defines its own `CaptureWindowHdr` and wants neither of these: a capture is asked nothing, so it has
no command to parse and no verdict to answer. A `rf_shot_msg.py` would therefore be a module created
for a second user that does not exist — the un-consumed-abstraction shape this family has already
paid for once with `CreditStreamIF`, and which `plans/t2p_lock_chan.md` opens by refusing.

**And Stage A must not duplicate them.** Two schema classes with one layout is the "two authors of one
statement" failure in its purest form: both would emit `rf_shot_tx_hdr.h`, both would be right today,
and nothing would notice when one moved. So `rf_shot_tx_unified.py` imports the six status codes, the
two opcodes and both schemas from `rf_shot_tx.py`, and **Stage B moves the definitions across when it
deletes that file** — a rename of the module plus a move of the four declarations, with no window in
which two copies exist.

The one schema Stage A *does* define is :class:`ShotPlayCmd`, because it is genuinely new: it is the
loader-to-player wire the merge needs and neither predecessor had.

### `ShotPlayCmd` carries the host's own opcode

The player needs exactly two things the lock has no opinion about — how many passes, and whether a
`done` is owed — and both are already in the header the host sent. So the internal command is
`{opcode: 8, nrepeat: 16}`, one beat, and `opcode` is `SHOT_LOAD` / `SHOT_LOOP` rather than a parallel
`PLAY_FINITE` / `PLAY_LOOP` vocabulary invented for the wire. Two rules cover every case both
predecessors had:

* **`nrepeat == 0` means play nothing.** That is `RfShotTx`'s existing convention for a `SHOT_SHORT`
  shot, so no third mode is needed to express it.
* **`opcode` says who is waiting.** `SHOT_LOAD` → the loader is blocked on a `done`; `SHOT_LOOP` →
  nobody is, and the player must not send one. A spurious `done` would clear a `busy` that a *later*
  finite shot set, and the next load would preempt it — the exact truncation `SHOT_BUSY` exists to
  prevent, arrived at from the other side.

It is a schema and not a raw word with a sentinel, because a sentinel in a bare `ap_uint` is how a
design ends up comparing against a magic number in two places.

### Three assumptions the plan does not answer

**1. `busy` covers *both* opcodes.** The plan says a finite shot in progress answers `SHOT_BUSY`; it
does not say what happens when the arriving frame is a `SHOT_LOOP`. It is refused too. The objection
is not to what the new shot *is* — it is that truncating the running one would be invisible, and that
is true whatever replaces it. The gate parametrizes both opcodes, because getting this right for one
and not the other is precisely the merge bug it exists to catch.

**2. A short shot is never played, on either path.** `RfShotTx` hands the player a repeat count of
zero so half a waveform never reaches the converter; `rf_shot_loop` plays the padded result and says
in its own docstring that it can only do that because it has no way to go quiet. The merged design
*does* have one, so **the stricter rule wins and both paths get it.** This is a deliberate change to
`rf_shot_loop`'s behaviour, in the safer direction, and it is recorded here rather than left to be
discovered as a difference.

**3. The player's read of the play command is BLOCKING, and that is safe.** It sits on the `RELEASE`
branch of the poll, so it is *control-dependent* on the poll's result and cannot be hoisted above the
loader's writes — which is the shape that made S1's request/response deadlock, avoided here for the
same reason `mem_lock_poll`-then-`grant` is safe. The loader writes the play command **before** the
release, so the wait is a bounded beat inside a gap the design is already in; and even a scheduler
that reordered the two writes would be correct, only slower.

### MEASURED (pysim, `nword=16`, `blk_words=4`, `depth=64`, region `[48, 64)`, 4 Mword/s DAC)

| gate | verdicts | playout, in words `(filler?, n)` |
|---|---|---|
| 1 — finite, `nrepeat=3` | `[(0, LOADED, 64)]` | `(F,8) (P,48) (F,184)` — **3 passes, then quiet** |
| 2 — infinite, switched | `[(0, LOADED, 64), (1, LOADED, 64)]` | `(F,8) (P,76) (F,4) (P,72)` |
| 3 — `SHOT_BUSY` | `[(0, LOADED, 64), (1, BUSY, 0)]` | `(F,8) (P,48) (F,184)` |
| 4 — five verdicts | `LOADED / BUSY / WRONG_LEN / ZERO_LEN / SHORT` + the `END` fence | — |

**Gate 3's playout is byte-identical to gate 1's**, and that is the assertion: the refused load
changed *nothing*. One grant instead of two, the memory still holding waveform A, and the running
shot still three whole passes. A design that preempted would have produced two passes — a perfectly
good shorter signal that every counter downstream still adds up for.

Gate 2 sends **no** `done` (0 across the run) and takes 2 grants; gate 1 sends exactly 1 `done` and
takes 1 grant. That pair is the merge: the same body, one exit condition apart.

**The dirty run is in the gate by name.** `test_a_player_that_grants_and_keeps_reading_raises`
subclasses the shipped player with the `playing = False` removed — one line — and the pysim guard
raises on the very next chunk. At RTL `bram_t2p.v`'s `$error` catches the same thing and XSI discards
it, so this is the only place the ordering is a *failure* rather than a plausible sample.

### The lowering, and the II the merge did not cost

`waveflow/build/shot_tx_loader_task.h` and `shot_tx_player_task.h`, shipped by
`RfShotTxUnifiedStep` — a step of its own while Stage A runs, so a build can ask for either family
without getting both. Stage B deletes the predecessors' and folds this one in.

**The player's C++ is `shot_loop_play_task.h` plus four lines**, and they are the merge:

```c
if (rd >= NW) { rd = 0; if (!loop) { if (--nrep_left == 0) { playing = 0; done; } } }
```

Both are register reads *outside* the pipelined loop body, which is why the exit condition costs
nothing — the shape one might expect to break II=1 does not, for the same reason
`plans/t2p_lock_chan.md` records about the `playing` guard.

**MEASURED (Vitis HLS 2025.1, xczu48dr, 4 ns target):**

| module | loop | achieved II |
|---|---|---|
| `shot_tx_loader_task_64_256_64_4_192` | `take_shot` | **1** |
| | `drain_tail` | **1** |
| | `await_grant` | **1** |
| `shot_tx_player_task_64_256_64_192_16` | `play_chunk` | **1** |
| `rf_relayout_to_slots_task_64_4_2_s` | *(Stage A's, unlabelled)* | **1** |

Estimated period **2.772 ns**, Fmax **360.8 MHz**.

**Counted at the port map:** three tasks against `RfShotTx`'s five, and `rep done samp` plus one
`add_if(lock)` against seven hand-wired channels plus two `BramIF`s. The four that vanished — `pay`,
`rdy_load`, `rdy_play`, `dense` — existed only to move samples between tasks the lock made
unnecessary.

`test_the_predecessors_are_untouched` asserts the merge-only rule directly: both old designs still
import and still lower.

### The RTL half: one snapshot, two streams

`examples/rf_shot_unified` and `tests/examples/test_rf_shot_unified_xsi.py` (20 gates;
`WANT_XSI_GATES` **95 → 115**, and Stage B lowers it again when the predecessors go).

The two scenarios load **the same `xsimk.dll`** and differ in three bundle names, driven by two
hand-written mains (`rf_shot_tx_unified_counters.cpp`, `rf_shot_tx_unified_loop.cpp`). That the RTL
is one design is the claim, so a second testbench *graph* would have been a second model of it.

They cannot be one stream, and the reason is the design rather than the harness: a file-driven driver
never reads a verdict, so a stream opening with a finite shot has every later frame answered
`SHOT_BUSY` — that *is* what `SHOT_BUSY` is — and a stream opening with an infinite one can never
demonstrate a refusal. One scenario per opcode is the minimum.

**MEASURED (Vivado 2025.1 xsim, 1400 cycles, `nword=64`, `blk_words=16`, `depth=256`, region
`[192, 256)` — the top of the memory — 256 Msamp/s DAC):**

| | `cmd` (finite) | `cmd_loop` (infinite) |
|---|---|---|
| verdicts | `LOADED / BUSY / WRONG_LEN / ZERO_LEN / END→LOADED` | `LOADED / WRONG_LEN / ZERO_LEN / LOADED / SHORT / END→LOADED` |
| playout, in converter blocks | `(F,3) (P,12) (F,7)` | `(F,3) (P,1) (F,2) (P,1) (F,15)` |
| last verdict at cycle | **269** | **500** |
| DAC words taken | 359 | 359 |
| blocks the grid zero-filled | **0** | **0** |
| underruns | 1, at cycle 4 (startup) | 1, at cycle 4 (startup) |
| lock grants | 1 | 3 |
| write addresses touched | `192..255` | `192..255` |

Both playouts are **byte-identical to the pysim golden** over the common horizon (1280 samples; the
RTL run is bounded in cycles and the pysim run in converter blocks, so only the tails differ in
length). The finite run's 12 sample blocks are 3 × 256 samples — three whole passes — and the 7
blocks behind them are gate 1: the design stopped **on purpose**. The loop run's 2-block gap is the
handover; its 15-block tail is the merged design's own improvement, since the last load is `SHORT`
and a short shot is loaded and never played, which `rf_shot_loop` cannot do.

`blocks_zero_filled == 0` on **both** paths is the sharpest number here. The infinite path's way to
fail it is a player that back-pressures the converter through a handover; the finite path's is a
player that simply stops writing when its passes run out — a failure `rf_shot_loop` never had to
survive, because it never stops. Quiet is a **value**.

### A FINDING: the yielded player still drives its read port, and on a preemption the addresses collide

`find_read_during_write` — `bram_t2p.v`'s own predicate, same address and same cycle — returns
**0 collisions on `cmd` and 2 on `cmd_loop`** (cycles 469 and 470, elements 216 and 217). Cycles with
both ports merely *live* inside the region: **18** and **55**.

This is the S1 observation reaching its conclusion. `plans/t2p_lock_chan.md` records that Vitis reads
the BRAM unconditionally at II=1 and muxes the filler in afterwards, and that a register guard was
measured not to quiet the port; at S1 that produced 34 both-live cycles in *both* designs and no
address collision, so the cycle-exact predicate stayed silent. Here the region is four times larger
and two of the three grants land **mid-play**, so the player's read pointer is somewhere in the
middle of the region when the loader starts writing at its base — and the two sweeps eventually cross.

**It is not a defect, and the evidence is in a different test.** pysim's `LockedMemSlaveIF.grant()`
takes the region out of the owner's hands and *raises* on the very next access, so if the RTL player
were **using** those words the two backends could not be byte-identical — and they are. The word is
fetched and thrown away.

So the gate records both numbers as measurements and pins them, rather than asserting zero. A rise
means the player started reading somewhere it should not; a fall means the read port stopped being
unconditional, which would change what a grant does and does not enforce at RTL. Asserting
`collisions == 0` here would have been a green gate bought by choosing a scenario that never
preempts, which is the opposite of what Stage A is for.

**There is no positive control in this gate, deliberately.** `rf_shot_loop`'s gate pairs its clean
run against a design with the player's `playing = 0` removed, and that control proves the *lock's*
ordering — the same `mem_lock.h`, the same `LockedT2pMemIF`, the same grant sequence this design
uses. A second deliberately broken design would be a second copy of a finding, not a second finding.
What is new at Stage A is the merge, and the merge is proven by running one RTL against both streams.

### The consequence that finding has, and it is structural: TX is a SINGLE-region design

The 2 collisions are not bad luck in a scenario. They follow from a property of the design that
`plans/t2p_lock_chan.md` does not currently make explicit, and that a reader of that plan would get
backwards.

`RfShotTxUnified` asks for **one** region and hands it back and forth —
`ShotTxLoader.region` is documented as *"`[base, base + nword)` — the one region this design ever
asks for."* So the writer and the reader **do** share addresses, in turn.

S2's answer to the RTL-enforcement question — *disjoint regions, so both ports staying live is
irrelevant rather than tolerated* — is therefore a guarantee **`RfShotRx` has and `RfShotTx` does
not**:

| | `RfShotRx` (2 regions) | `RfShotTx` (1 region) |
|---|---|---|
| address collisions | **0, by construction** | **2, benign by measurement** |
| what proves it | the region split | a byte-identical pysim run that raises on yielded reads |
| waveform / window switch | gapless | a filler gap at every handover |

Neither is wrong. But the family is **not uniformly protected**, and
`plans/t2p_lock_chan.md`'s *"disjoint regions are the mechanism"* reads as though it covers both
halves. It covers one. Say so in both plans.

#### The option this opens, NOT taken at Stage A

A **two-region TX** — load the new shot into region B while region A plays, switch at the wrap —
would give collisions of 0 by construction, and **gapless waveform switching**: no filler between
waveforms at all. Look at the measured loop playout, `(F,3) (P,1) (F,2) (P,1) (F,15)` — every
handover costs a gap. At `depth=256` holding an `nword=64` shot there is room for four regions, so
the memory cost is nil.

It goes further than tidiness: with two regions a **finite** shot playing A can accept a load into B
*without being truncated*, so the `SHOT_BUSY` asymmetry — the thing that made Stage A a merge rather
than a rename — would largely dissolve, surviving only for the case where both regions are spoken
for. And TX and RX would become the same shape, which is what a plan called *one shot-buffer design*
ought to mean.

**Deliberately not folded into Stage B.** Stage B is a pure deletion, where any regression is
trivially attributable to a removal; a single-to-two-region refactor inside it would destroy that
property. Decide it on its own merits afterwards. It is recorded here because Stage B is what makes
the single-region design the *surviving* one, and that is the moment to have looked.

### Assumption recorded: `n_plays` is not pinned on a mixed run

`RfShotTxUnified.assert_finite_completed(n_shots, n_plays=None)` takes the pass count as *optional*.
`n_plays` counts **total** wraps including a preempted loop's, so a run that mixes the two opcodes
has a value that depends on when the preemption landed. Pinning it would be pinning the scheduler
rather than the design; the finite gate pins it, the mixed one does not.

### Assumption recorded: `busy` clears on a harvest, not at the end of a run

The loader reads its `done` token non-blockingly, **after** the header — so `busy` is cleared by the
*next arriving frame*, not by the play-set ending. A run whose last shot finishes with nothing behind
it therefore ends with `busy` still set, and that is correct rather than a leak: the state is only
ever read when a frame is being judged. The gate asserts `n_done` and the *acceptance of a later
frame* instead, which is what a host would actually observe.

---

## Stage B as built — the deletions, and the two numbers that were not what the plan said

Built 2026-09-02 on branch `rf-shot-unify-b` off `rf-shot-unify-a`. **A pure deletion**, plus the one
move and the two renames the stage cannot avoid, so that any regression is trivially attributable to
a removal.

### What went

| deleted | what it was |
|---|---|
| `waveflow/hw/rf_shot_buf.py` | `RfShotBufLoad`, `RfShotBufRead`, `RfShotBuf`, `ShotPhase` |
| `waveflow/hw/rf_shot_tx.py`'s contents | the five-task finite composite and its Stage-A instantiation |
| `waveflow/hw/rf_shot_loop.py` | `ShotLoopLoad`, `ShotLoopPlay`, `RfShotTxLoop` |
| six `waveflow/build/*_task.h` | `rf_shot_buf_{load,read}`, `shot_tx_{load,play}`, `shot_loop_{load,play}` |
| `examples/rf_shot_buf`, `rf_shot_play`, `rf_shot_loop` | and their `.gitignore` entries |
| seven test files | including three `-m xsi` gate files |
| `docs/examples/rf_shot_play/` (3 pages) | docs for a deleted example |
| `docs/guide/rf/rfshotbuf/{tx,tx_internal}.md` | 620 lines describing the deleted five-task design |

**The task bodies were checked, not assumed.** `rf_relayout_to_{dense,slots}_task.h` have three live
consumers (`rf_relayout`, `rf_shot_rx`, `rf_shot_unified`) and stay; the other six had no consumer
outside the deleted modules and their own gates. `RfShotBufStep._SRC` went from **eight** entries to
**two**, and the step **keeps its name** — `RfShotBuf` survives as the family name, exactly as this
plan says under *`RfShotBuf` is absorbed, not retained*.

### GATE ARITHMETIC — and the plan's number was off by two

| file | gates |
|---|---|
| `test_rf_shot_buf_xsi.py` | 5 |
| `test_rf_shot_play_xsi.py` | **13** *(the plan said 11)* |
| `test_rf_shot_loop_xsi.py` | 10 |
| **removed** | **28** |

`test_rf_shot_play_xsi.py` has **11 test functions**, two of which are
`@pytest.mark.parametrize`d over two scenarios — so it *collects* 13 items, and `WANT_XSI_GATES`
counts collected items (`_XSI_SELECTED`), not functions. **115 → 87**, confirmed by
`pytest -m xsi --collect-only`: 87 collected, 87 passed, **0 skipped**.

### The move: the vocabulary went to the unified TX module, as Stage A said it would

`ShotTxHdr`, `ShotTxResp`, the three opcodes, the five status codes, `SHOT_STATUS_NAMES`,
`SHOT_TX_SCHEMA_CLASSES` and the three geometry constants (`WORD_BW`, `BUF_DEPTH`, `SHOT_WORDS`) now
live in `waveflow/hw/rf_shot_tx.py`. **No shared `rf_shot_msg.py`**, for the reason Stage A recorded:
it would have exactly one consumer. `RfShotRx` imports `WORD_BW` from the TX module and defines its
own `CaptureWindowHdr` — a capture is asked nothing, so it has no command to parse.

There was never a window with two copies: Stage A imported the definitions rather than duplicating
them, and the same commit that deleted the old module spliced them into the new one.

### The two renames

**`rf_shot_tx_unified.py` → `rf_shot_tx.py`, `RfShotTxUnified` → `RfShotTx`.** Stage A's own record
anticipated this — *"a rename of the module plus a move of the four declarations"* — and this plan's
§ *`RfShotBuf` is absorbed, not retained* states the surviving classes are `RfShotTx` and `RfShotRx`.
It also **repairs six cross-references for free**: `locked_mem.py`, `rf_relayout.py` and
`interface.py` all pointed at `waveflow.hw.rf_shot_tx` symbols that the merged module now owns.

**`rf_pingpong_rx.py` → `rf_shot_rx.py`, `RfPingPongRx` → `RfShotRx`**, with the example directory
and the three test files. Re-generated and re-synthesized under the new top name, and **every
recorded number is unchanged**: ADC 640 words / 0 dropped / 40 blocks, window 516 words, last window
at cycle **2205**, **140** cycles with both memory ports live and **0** with writer and reader in the
same region, Fmax 367.3 MHz. That the numbers did not move is what says the rename was
behaviour-neutral.

### What was deliberately NOT renamed, and why

* **`cpp_kernel_name = "rf_shot_tx_unified"`, `examples/rf_shot_unified/`, the XSI harness artifacts,
  `RfShotTxUnifiedStep` and `UNIFIED_TX_SCHEMA_CLASSES`.** Renaming these rewrites the RTL
  identifiers the Stage-A gate records, and Stage B must not change that gate. Cosmetic, and Stage C
  work.
* **`PingPongCapture`, `PingPongWindow`, `pingpong_{capture,window}_task.h`, `RfPingPongStep`.** They
  are named for the **mechanism** — two regions alternating — which is still exactly what they do.
  Renaming the bodies would also rename the synthesized modules the II gate looks up **by name**,
  which is how a gate starts skipping and reading as a pass.
* **`RfShotTxUnifiedStep` was not merged back into `RfShotBufStep`.** The separation no longer buys
  anything now that the predecessors' bodies are gone, but merging it would be a refactor of a
  survivor inside a stage whose whole property is that every regression is a removal.

### Assumption recorded: what counts as a "dangling reference"

The done-when says no dangling reference to a deleted symbol, module or example — and taken
literally that would delete the *provenance of measurements*. `waveflow/build/xsi/xsi_rfdc.h` carries
`// MEASURED 2026-08-31, on examples/rf_shot_play: the design put 192 beats on samp_out`, copied into
twelve example trees. That measurement **was** made on that example; renaming it to a surviving one
would be a lie, and deleting it would throw away why the constant is what it is.

So the line drawn was: **fix every reference a tool would follow** — imports, `_SRC` entries, file
paths, `:mod:` / `:class:` / `:attr:` / `:meth:` / `:data:` / `:func:` roles, and markdown links —
and **keep bare-prose historical mentions**, rewording them where they read as though the thing is
still present ("the infinite predecessor", "the retired `rf_shot_buf` example"). The byte-address
finding in `wrapper_gen.py` kept its story and was repointed at the gate that still checks it,
`test_bram_access_xsi.py::test_the_wrapper_undoes_the_shift_vitis_actually_emits`.

### Assumption recorded: the deleted docs pages are not replaced here

`tx.md` and `tx_internal.md` described the five-task design in detail, down to `rf_shot_tx.py` line
numbers and `examples/rf_shot_play/include/` paths. Keeping them would have left the guide
confidently describing a design that no longer exists, which is worse than a gap; rewriting them for
the merged design is **Stage C**, and doing it inside a deletion stage is exactly the refactor Stage
B is defined to exclude. `docs/guide/rf/rfshotbuf/index.md` survives — it is the family page, it
already says *under construction*, and it references no deleted symbol.

`plans/rf_shot_buf.md` and `plans/t2p_lock_chan.md` were **not** rewritten either: they are the
record of what those stages built and measured. Each got a note at the top saying where its products
live now, so a reader following them does not walk into a deleted path.

### One test was replaced rather than deleted

Stage A's `test_the_predecessors_are_untouched` asserted the merge-only rule — both old designs still
import and still lower. Its subject is gone, so it became `test_the_predecessors_are_gone`, which
asserts the two modules raise `ModuleNotFoundError` and that the surviving `rf_shot_tx` owns the
boundary vocabulary. The reason to gate an absence rather than enjoy it: `RfShotTx` still speaks
everything both predecessors spoke, so re-adding either would import cleanly and quietly restore the
two-designs-one-job state this plan exists to end.

### Not done, and deliberately

**The two-region TX** recorded above under *The option this opens, NOT taken at Stage A* was not
touched. Stage B is a pure deletion; a single-to-two-region refactor inside it would destroy the
property that makes the stage worth having. It is still open, and it is still the thing that would
make TX and RX the same shape.

---

## Stage C as built — the naming leftovers, and two pages that had been wrong all arc

Built 2026-09-02 on branch `rf-shot-unify-c` off `main` (PR #181 merged as `25ac62e`). **Prose and
renames only**; no design's behaviour changed, and the way that is checked is that every recorded
number is asserted exactly by a gate that still passes.

### 1. "unified" is gone

It was a name relative to two predecessors that no longer exist, so it had become debris. Stage B
left it deliberately — a pure deletion must not touch the Stage-A gate — and nothing constrained it
any more.

| | before | after |
|---|---|---|
| kernel | `cpp_kernel_name = "rf_shot_tx_unified"` | `"rf_shot_tx"` |
| example | `examples/rf_shot_unified/` | `examples/rf_shot_tx/` |
| build step | `RfShotTxUnifiedStep` | `RfShotTxStep` |
| schema list | `UNIFIED_TX_SCHEMA_CLASSES` | `SHOT_PLAY_SCHEMA_CLASSES` |
| testbench | `RfShotUnifiedTB` | `RfShotTxTB` |
| tests | `test_rf_shot_tx_unified*`, `test_rf_shot_unified_xsi` | `test_rf_shot_tx*`, `test_rf_shot_tx_xsi` |

TX and RX are now `examples/rf_shot_{tx,rx}` driving `waveflow/hw/rf_shot_{tx,rx}.py` through kernels
named `rf_shot_{tx,rx}` — the pair this plan asked for.

**`cpp_kernel_name` feeds the generated top and the wrapper**, so this renamed RTL files:
`rf_shot_tx_unified.v` → `rf_shot_tx.v`, `_top` → `rf_shot_tx_top`, and the FIFO modules with them.
Re-generated and re-csynthed.

**EVERY RECORDED NUMBER IS IDENTICAL**, and the gate asserting each one exactly is what proves it:
`resp_last` 269 / 500, DAC 359 words, 0 zero-filled, 1 underrun at cycle 4, blocks `(F,3)(P,12)(F,7)`
and `(F,3)(P,1)(F,2)(P,1)(F,15)`, 18 / 55 both-live cycles, 0 / 2 collisions, writes `192..255`, Fmax
**360.77 MHz** — the same figure as before the rename.

**The five II modules did NOT change name**, and that is why the II gate still finds them rather than
skipping (which would have read as a pass): they are named for the task **bodies**
(`shot_tx_loader_task_*`, `shot_tx_player_task_*`, `rf_relayout_to_slots_task_*`), and the rename
never touched a body filename.

Left alone deliberately: the AMD *Unified Installer*, `shared_mem`'s *unified BuildDag*, and
`rf_shot_tx.py`'s own record that the class *"was built under the name `RfShotTxUnified`"* — that one
is history, in the past tense, and true.

### 2. `choosing.md` was wrong in both directions, and the fix is structural

The page claimed **`RfStreamBuf` is built and RTL-gated**. No class of that name exists and none ever
has. It also called `RfShotBuf` *designed, not built*, when both its halves were by then gated at
RTL. Patching two sentences would have left the page's entire comparison built around a class that
does not exist.

**Both names are family names**, and the page now says so first and resolves them:

| family | transmit | receive |
|---|---|---|
| `RfShotBuf` (finite) | `RfShotTx` | `RfShotRx` |
| `RfStreamBuf` (continuous) | `RfTxStream` | `RfSampBufRx` |

and states that the two families are **not at the same stage**: the shot family is complete and on one
mechanism; the streaming family has a finished stream-based transmitter (`RfTxStream`) and an *older*
BRAM receiver (`RfSampBufRx`) whose replacement is `plans/rf_samp_new.md` Stage 2 and is unbuilt.

**Two further claims on that page were false and are now qualified rather than deleted.**

* *"Pre-trigger comes free"* and *"it is the only option"* — the **architectural** claim is true and
  worth keeping (a continuous buffer has already discarded the samples), but the **capability is not
  built**. The page now says *architecturally yes — not built*, and points at Stage C.
* *"change data mid-flight: no"* for the finite buffer. `RfShotTx` **can** now, by preempting a
  `SHOT_LOOP` — at the cost of a gap in the output, because TX holds one region. A real capability
  with a real price, and the row says both.

### 3. `docs/guide/rf/rfshotbuf/` is a section rather than a stub

Four pages, in the shape that worked before and for the reason it worked:

* `index.md` — the family page. The stale *"designed, not built"* status and the *Under construction*
  banner are gone; it already had `has_children: true`.
* `tx.md` — for someone who wants to **use** it: architecture, boundary ports, the messages as field
  tables, the two play modes and what a load does to each, the five verdicts, and the four rules that
  bite.
* `rx.md` — the same for `RfShotRx`, plus the section a reader most needs: **what this is not.**
* `tx_internal.md` — for developers and agents. Opens by saying users can skip it, and **cites the
  source with file:line** rather than paraphrasing.

Child pages carry `parent: RfShotBuf` **and** `grand_parent: RF converters` — Just the Docs binds
`parent:` by title string, and the nav gate fails without the disambiguation.

**Every number on those pages is one a gate currently asserts**, and the gate is named beside it. The
link gate caught one bad anchor on the first run, which is the gate doing its job.

### 4. `plans/rf_shot_buf.md` is rewritten around the one stage that is left

790 lines → 408. Its Stages A and B described designs that Stage B of this plan deleted; D and E are
done. What survives is the reasoning that is still binding (the in-band payload reversal, the
logic-side port, the response, the no-`has_response` argument) and the traps, which outlived the code
that found them.

**Five section headings are cited from code** — *The logic-side port*, *The caveat, and it is a Stage
A gate*, *The commands*, *Why no `has_response` flag*, and the opening paragraph — by
`rf_relayout.py`, `rf_shot_tx.py`, `streamutils.py`, three test files and two task headers. They are
preserved verbatim. A rewrite that renamed them would have broken ten citations silently.

**Stage C is kept alive with a section saying precisely why `RfShotRx` does not subsume it.** The
short form: `RfShotRx` is a **conveyor** — it hands out region A the instant it completes it and
begins overwriting region B, so the readable past is one region deep, and that is **not a bound you
can raise by making the memory bigger**, because `N_REGION = 2` splits whatever depth it is given.
Stage C inverts the relationship: nobody reads while armed, so the whole memory is history, and the
trigger is what turns a circular scribble into an addressable record. They cannot be one design with
a flag, because *always be handing out regions* and *never hand out anything until told* are opposite
answers to what the memory is for.

### Assumption recorded: what `UNIFIED_TX_SCHEMA_CLASSES` became

The plan names the rename but not the replacement. `SHOT_PLAY_SCHEMA_CLASSES` — named for what it
holds (`ShotPlayCmd`, the internal loader→player wire), mirroring `SHOT_TX_SCHEMA_CLASSES` for the
boundary vocabulary. The two lists stay separate because they are two different things: one is what a
host speaks, one is what the design says to itself.

### Assumption recorded: one schema description was stale and was corrected

`ShotTxHdr.opcode` still described itself as *"SHOT_LOAD or SHOT_END"* after `SHOT_LOOP` became legal.
That string reaches the generated `rf_shot_tx_hdr.h` and would have been reproduced verbatim in the
docs field table, so it was corrected to *"SHOT_LOAD, SHOT_LOOP or SHOT_END"*. It is a comment: the
example was re-generated and re-csynthed, and the gate's numbers are unchanged.

### Assumption recorded: `plans/t2p_lock_chan.md` still is not rewritten

One path inside the supersession note Stage B added to it was updated (`examples/rf_shot_unified` →
`examples/rf_shot_tx`), because that note exists **to point a reader at the surviving code** and a
pointer at a renamed directory defeats its own purpose. Nothing else in that file changed; it remains
the record of what the lock's two stages measured.

### Measurement provenance is still not repointed

`waveflow/build/xsi/xsi_rfdc.h` still records that its driven-vs-ready fix was measured on
`examples/rf_shot_play` — an example deleted at Stage B. The measurement happened there; repointing it
at a survivor would be a lie, and deleting it would throw away why the constant is what it is. Same
rule Stage B recorded: fix what a tool would follow, keep bare-prose history in the past tense.
