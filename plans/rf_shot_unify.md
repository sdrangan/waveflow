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
