# Plan — the shot is the buffer

**Status: BUILT 2026-09-07.** Removes `nword`, `base` and the header's `nsamp` from `RfShotTx`, and
the two verdicts that existed only to police the last of them. Does not touch `RfShotRx`, which
shares none of them. **See *As built* at the end.**

---

## Done — nothing starts here

Built 2026-09-07 on branch `rf-shot-geometry-build`. The bad-opcode decision was taken as the plan
recommended (renamed, wire value kept), `depth` became what `nword` was, and the two numbers that
moved were attributed by experiment rather than re-recorded. *As built* has the detail.

---

## Why

Three parameters and one header field describe a geometry that has exactly one degree of freedom, and
a reader cannot tell which of them they are allowed to choose.

* **`nsamp` on the header carries no information.** It must equal `nword × samp_per_word` or the
  command is refused. `ShotTxHdr`'s own docstring says what it is: *"what the host believes it is
  sending, and catching that belief disagreeing with what arrived is the verdict's whole job."* A
  checksum wearing a parameter's clothes — and a reader meets it as a parameter first.
* **`base` has no user-facing justification.** Its only stated reason was that a non-zero value
  exercises `base + offset`, the addressing arithmetic `bram_toy` stayed green through. That is a
  reason for a *test*, not for a knob on a public constructor — and the docs page that tried to
  explain it read as private history to every reader who met it.
* **`nword` then looks arbitrary**, because the one thing it interacts with (`nsamp`) is redundant and
  the other (`base`) is unexplained.

**The simplification: the shot IS the buffer.** Length is `depth`; there is no placement; the header
carries no length.

### What that removes, and it is more than three names

| | before | after |
|---|---|---|
| constructor | `depth`, `nword`, `base`, `blk_words` | `depth`, `blk_words` |
| header | `opcode`, `tid`, `nsamp`, `nrepeat` | `opcode`, `tid`, `nrepeat` |
| verdicts | LOADED, SHORT, WRONG_LEN, BUSY, ZERO_LEN | LOADED, SHORT, BUSY (+ see below) |
| addressing | `mem[base + i]`, wrap at `nword` | `mem[i]`, wrap at `depth` |

**`SHOT_ZERO_LEN` retires entirely** — it is `h.nsamp == 0`, and there is no `nsamp`.
**`SHOT_WRONG_LEN`'s length check retires** — it is `h.nsamp != NW*SPW`.

### And the addressing bug class disappears rather than going untested

This is the part worth stating plainly, because it inverts the argument that kept `base`:

`base` existed, and *then* needed a gate to prove its arithmetic was not broken. Remove it and there
is no arithmetic: the loader writes `mem[i]`, the player reads `mem[i]`, and the wrap is `i % depth`
with `depth` already required to be a power of two — **a mask, not an addition**.

So the coverage `base` was justified by is not lost. The thing it was covering stops existing.

## What stays, and why

**`nsamp_loaded` on the response stays.** It is what actually *landed*, not what was asked for — on
`SHOT_SHORT` the difference **is** the diagnosis, and it is the number a DMA cannot produce because
`sendchannel.transfer()` knows it pushed bytes, not whether they were a whole waveform. Unlike the
header's `nsamp`, it carries information the host has no other way to get.

**`SHOT_SHORT` stays, and becomes more important.** With no declared length on the wire, `TLAST`
arriving before the buffer is full is the *only* way a short transfer is detectable. The pad still
works — the load loop runs `depth` times whatever arrives, so the buffer fills, emits its token and
the design stays live.

**`blk_words` stays.** It is the burst width the converter edge requires, not a geometry choice.

**Fixed length stays.** The load loop's counted trip count is what lets it reach II=1, and the pad
needs a known length to pad *to*. Variable length is a different plan and it pulls in an allocator —
see *Not in scope*.

## One thing to decide

**Line 130 gives an unknown opcode `SHOT_WRONG_LEN`:**

```c
if (h.opcode != SHOT_OP_LOAD && h.opcode != SHOT_OP_LOOP) {
    status = SHOT_WRONG_LEN;            // refused, never reinterpreted
```

That was already an overload — *"wrong length"* reported for a wrong opcode — and it is the one use of
`SHOT_WRONG_LEN` that survives losing `nsamp`. Two options, and the choice should be deliberate:

* **Rename it `SHOT_BAD_OPCODE`.** Four verdicts, each meaning one thing. The wire value can stay put
  so no host changes.
* **Fold it into `SHOT_SHORT`.** Three verdicts, but it makes a malformed command look like a
  truncated one, which is exactly the conflation `SHOT_SHORT` exists to avoid.

**Take the rename.** A verdict whose name describes a different fault than the one it reports is how a
host ends up debugging the wrong thing.

**Taken.** `SHOT_BAD_OPCODE`, wire value 2 — unchanged, so no host changes.

## Gates

**The load still fills the buffer and plays it.** Every existing behavioural gate should hold with its
*meaning* unchanged — the design does the same thing, described with fewer numbers.

**A short transfer still answers `SHOT_SHORT` with the right `nsamp_loaded`.** This is the gate that
matters most, because losing the header's length makes `TLAST` the sole detector.

**`SHOT_ZERO_LEN` and the length half of `SHOT_WRONG_LEN` are gone**, so their gates go with them.
Removing a gate is a real change: say which ones and why, rather than letting the count drift.

**The wrap is a mask.** Assert a run long enough to wrap `depth`, since that arithmetic is now the
only address arithmetic left.

## Traps

**The wire changes.** The header loses a field, so bit positions move and the generated C++ headers
regenerate. Unlike `rf_shot_wire_format.md` Part A — which kept the message exactly one 64-bit word —
this changes what the message *contains*. Scenario bundles that encode a header by hand will need
regenerating; find them rather than assuming there are none.

**Cycle counts are measurements.** A header is still one beat, so transfer counts should not move —
but `nword` becoming `depth` changes the shot's length at the gated geometry unless `depth` is set to
what `nword` was. **Decide which**, and say so: keeping the played length identical makes every
recorded number comparable, and that is worth more than a rounder `depth`.

**Decided: `depth = 64`.** Transfer counts did not move (197/197 words, 359 DAC words). Two numbers
did — the last verdict's cycle and the loop path's read-during-write collisions — and *As built*
records the experiment that attributed both to the wire change rather than to the geometry.

**`RfShotRx` shares none of this.** It has `N_REGION` and its own geometry, and uses neither `nword`
nor `base`. A number moving there is a finding.

**The docs carry the old geometry.** `tx.md`'s geometry table, `tx_options.md`'s axis 1, and
`tx_internal.md`'s layouts all describe four parameters and a four-field header.

## Not in scope

- **Variable-length shots.** `nsamp` would become a real parameter and the length would come from the
  command — but *"where does a shot of unknown length go"* has no build-time answer, so it needs an
  allocator, which `plans/t2p_lock_chan.md` S3 fenced off as *"where this stops being an interface and
  starts being arbitration."* A different plan, and a bigger one.
- **Absolute indexing** (`mem[j mod BUF_LEN]`, TX↔RX correlation by address). It *needs* the fixed
  length this plan keeps, so it stays reachable — see `docs/guide/rf/rfshotbuf/tx_options.md`. More
  work, and it depends on MTS holding.
- `RfShotRx`.

---

## As built — **the shot is the buffer, and one number moved for a measured reason**

Built 2026-09-07, branch `rf-shot-geometry-build`.

### The new constructor and the new header

```python
dut = RfShotTx.for_word(word, depth=64, blk_words=16, sim=sim, name="dut", clk=clk)
```

`nword` and `base` are gone from all three classes (`ShotTxLoader`, `ShotTxPlayer`, `RfShotTx`), and
the template arguments went with them: `shot_tx_loader_task<W, D, SPW>` (was `<W, D, NW, SPW, BASE>`)
and `shot_tx_player_task<W, D, BW>` (was `<W, D, NW, BASE, BW>`).

| | before | after |
|---|---|---|
| `ShotTxHdr` | `opcode` 2, `tid` 16, **`nsamp` 16**, `nrepeat` 16, `_rsvd` 14 | `opcode` 2, `tid` 16, `nrepeat` 16, `_rsvd` **30** |
| `ShotTxResp` | unchanged — `tid` 16, `status` 8, `nsamp_loaded` 16 †, `_rsvd` 24 | same |

Both messages are still exactly one 64-bit word. `nsamp_loaded` **stayed**, and its width is now
derived from `depth × samp_per_word` rather than `nword × samp_per_word` — the same number at this
geometry, and the right one in general now that a full load *is* the buffer.

**`depth = 64`, not 256.** It is what `nword` was, so the played length is unchanged: the pysim
playout, its segments, its transients and `c_lead` are byte-for-byte what they were, and the
committed `playout.svg` regenerated **identically**. That is what makes "the numbers held" evidence
rather than a coincidence.

### The verdicts: four, and one renamed

`SHOT_WRONG_LEN` → **`SHOT_BAD_OPCODE`**, wire value **2, unchanged**, so no host changes.
`SHOT_ZERO_LEN` (value 4) is gone entirely. The verdict chain went from four tests to two:

```
opcode not one of the three -> SHOT_BAD_OPCODE      (was SHOT_WRONG_LEN)
busy                        -> SHOT_BUSY
otherwise accept; SHOT_SHORT is decided after the transfer, from how much arrived
```

The two that went both read `h.nsamp`. `SHOT_SHORT` is now the **only** way a short transfer is
detectable, which is why it and `nsamp_loaded` both stayed.

### Gates removed, and why — none of them were gates

**No gate file lost a test to this.** The two retired verdicts were asserted *inside* gates that
survive, not by gates of their own:

* `test_all_five_verdicts_plus_the_fence_in_one_stream` → `..._four_...`: the pysim scenario dropped
  one frame (six → five) and `tid` 2 now provokes `SHOT_BAD_OPCODE` instead of `SHOT_WRONG_LEN`. It
  still proves **malformed beats transient** — `tid` 2 arrives while a finite shot is playing and is
  answered on its opcode rather than `SHOT_BUSY`.
* `test_all_five_verdicts_and_the_fence_appear_across_the_two_streams` → `..._four_...`, same shape.
* `test_the_memory_holds_the_shot_at_the_declared_region_and_nowhere_else` →
  `test_the_shot_fills_the_whole_memory_and_the_region_is_all_of_it`. Its "the words either side of
  the region did not move" half had nothing left to check — the region *is* the memory. Its other
  half (the counted pass writes every element, so a short frame is padded) survives.

**One gate was added**: `test_the_player_sweeps_the_whole_buffer_and_wraps`. With `base` gone the only
address arithmetic left is `rd` wrapping at `depth`, so that is what gets measured — on the read
port, at RTL. Three claims: every element is read, nothing outside `[0, depth)` is, and the pointer
goes `depth-1 → 0` at least twice, since `cmd` plays three passes. A player that saturated instead of
wrapping would replay one word forever and every counter downstream would still add up.

`WANT_XSI_GATES` 97 → **98** (`test_rf_shot_tx_xsi.py` 30 → 31).

### Two numbers moved, and the cause was measured rather than assumed

| | before | after |
|---|---|---|
| `WANT_RESP_LAST_CYCLE["cmd"]` | 269 | **273** |
| `WANT_RESP_LAST_CYCLE["cmd_loop"]` | 500 | **502** |
| `WANT_RDW_COLLISIONS["cmd_loop"]` | 2 | **0** |

Everything else held: `CMD_SENT`/`CMD_TOTAL` 197/197, `DAC_WORDS_RECV` 359, `blocks_zero_filled` 0,
`DAC_UNDERRUN` 1 at cycle 4, the playout block shapes, `WANT_PORT_OVERLAP_CYCLES` 18/55, the LT
transients, and II = 1 on all five loops. The pysim side is unchanged in every respect.

**Three causes ruled out by experiment, one found:**

* **Not the scenario.** The replacement frames have identical word counts (65,65,65,1,1 and
  65,65,1,65,33,1). Re-running `cmd` with `tid` 2 refused by the *last* verdict test instead of the
  first — a bundle-only change, no rebuild — gave **273 either way**, and with both payload-carrying
  refusals on the busy path, 273 again. Which branch refuses a frame costs nothing.
* **Not the geometry.** The old design was rebuilt in a `git worktree` at the **new** geometry —
  `depth=64`, `base=0`, `nword=64`, four-field header, five verdicts — and answered **269**, exactly
  what it answers at `depth=256` with the region at the top. It also still collided **twice**
  (cycles 469 and 470, addresses 24 and 25). Shrinking the memory and dropping `base` cost zero
  cycles and moved no collisions.
* **Not the load.** The write burst runs cycles **70..133** on both designs, to the cycle.
* **It is the wire.** One fewer header field to unpack and two fewer verdict tests is a differently
  scheduled body, and the response path lands a few cycles later. The same re-timing slid the
  writer's address sweep *past* the yielded player's read address instead of *through* it, which is
  the collision count going to zero.

**Zero collisions is luck, not a guarantee**, and the constant stays pinned rather than becoming
`assert == 0`: the player's reads are still unconditional and still discarded, and a version that
collides again is not thereby broken. `WANT_PORT_OVERLAP_CYCLES` is the number carrying the claim —
it held at 18/55, which is what says the read port is still unconditional.

### `RfShotRx` is untouched

Not one line, and no number of its own moved: `tests/examples/test_rf_shot_rx_xsi.py` (9 gates) and
`tests/hw/test_rf_shot_rx.py` pass unchanged. It has `N_REGION` and its own geometry and used neither
`nword` nor `base`, exactly as the plan said.

### Assumptions recorded

* **`nsamp_bw_for` takes `depth`.** The plan says `nsamp_loaded` stays but not what sizes it. A full
  load is `depth` words, so that is the largest value the field can ever carry.
* **`NSAMP_BW_FLOOR` stays at 16, with a new reason.** Its old one — stopping a host's mistyped
  *length* from aliasing onto a legal one on the way in — retired with the header's field. What
  survives is that a host reads this field, and holding it at one width across geometries is what
  lets a host be compiled against the wire once. The padding is declared either way.
* **`BAD_OPCODE = 3` in the testbenches.** `OPCODE_BW` is 2 and the legal values are 0, 1, 2, so 3 is
  the only illegal opcode the wire can carry — which is what makes `SHOT_BAD_OPCODE` reachable from a
  real frame rather than only from a hand-built object.
* **The scenarios' refused frames kept their word counts.** `tid` 1 and 2 of `cmd_loop` exist to be
  *drained*, buying waveform A airtime before B arrives. They were a wrong length and a zero length;
  they are two bad opcodes now, with the same payload shapes, so the airtime is unchanged.
* **`shot_codes(base, nwords=...)`'s parameter was renamed** from `nword`. It is how much waveform a
  testbench builds, not the design's retired parameter, and leaving the retired name on a helper
  invites a reader to meet it as the design's.
* **`LOCK_BAD_RANGE`'s cross-reference moved** from `SHOT_WRONG_LEN` to `SHOT_SHORT`, in
  `locked_mem.py` and `mem_lock.h`. Both cite the *refuse, never clamp* discipline; the verdict that
  carried it is gone, and `SHOT_SHORT` is where it still lives — a truncated waveform is not a
  shorter one, it is the wrong signal.
