# Plan — one shot-buffer design, on the lock

**Status: DECIDED 2026-09-02, NOTHING BUILT.** Owns the collapse of four shot-buffer designs into
two — one TX, one RX — both on `LockedT2pMemIF`. Supersedes the structure `plans/rf_shot_buf.md`
assumes; that file is rewritten in Stage C and stays intact until then as the record of how the
family got here.

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
