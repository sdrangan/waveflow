# Plan — settle `RfShotTx`'s wire format and protocol before anything talks to it

**Status: SCOPED HERE, NOTHING BUILT.** Started 2026-09-06. Owns the two changes that are cheap now
and expensive after a host driver exists: the message field widths, and the region count. Downstream
of `plans/rf_shot_unify.md`, which produced the design this settles.

---

## Next session starts here — Part A

```
claude "Read plans/rf_shot_wire_format.md, section 'Part A', and build it.
        Part A only.  Part B is a separate commit on the same branch and only
        after A's gates are green."
```

---

## Why now, and why these two together

Everything else outstanding on this family is internal — it can change after deployment at no cost.
**These two cannot.** One is the bytes an AXI DMA moves; the other is the host's retry loop. Change
either once a PYNQ driver exists and you re-do the driver, the scenario bundles, and the docs.

They are one plan because both move `rf_shot_tx`'s cycle counts, so bundling them costs **one**
re-measurement pass instead of two.

**Nothing here is required for the design to work.** It works: 35 tests, 13 of them driving real
Verilog, bit-exact against the pysim golden in both play modes. This is about not having to change it
later.

## Part A — the message layer

### What it is today

```python
from waveflow.hw.rf_samp_buf import IDX_BW              # = 16
IdxField = IntField.specialize(bitwidth=IDX_BW, ...)    # tid, nsamp, nrepeat
OpField  = IntField.specialize(bitwidth=8, ...)         # 3 opcodes in 8 bits
```

packed as `opcode[7:0] | tid[23:8] | nsamp[39:24] | nrepeat[55:40]` — one 64-bit word with 8 bits
spare.

Three things wrong with that, in ascending order of importance:

**1. `opcode` is 8 bits for 3 values.** Harmless, but it is a number nobody chose.

**2. `nsamp` is *checked against* the geometry rather than *derived from* it.** Construction refuses
a shot too big for 16 bits:

```python
if nw * spw >= (1 << IDX_BW):
    raise ValueError("... does not fit the 16-bit nsamp field. A verdict that wrapped would
                      report a short load as a correct one.")
```

The design **knows** `nword × samp_per_word`. A width that follows from it cannot overflow, and the
check becomes unnecessary rather than load-bearing. Right now the constant bounds the design instead
of the design sizing the constant.

**3. `IDX_BW` is imported from `rf_samp_buf.py` — the superseded family.** The clean lock-based design
has a hard dependency on a module marked for eventual retirement. Nobody chose that; it rode in with
Stage B's schema move, and it would block retiring `rf_samp_buf` later.

### What it becomes

Widths derived from the design's own geometry; `tid` and `nrepeat` as parameters of the module rather
than constants of another one; `opcode` sized to the opcode count; and **no import from
`rf_samp_buf`**.

### The decision this contains, and it must be stated not defaulted

**Derived widths will not sum to 64.** The header is one 64-bit word today, deliberately — an
`EnumField` may not straddle a word, and one word is what a DMA moves cleanly.

Two answers, and the plan picks the first:

* **Keep one word and pad explicitly.** Fields become overflow-proof *and* the wire size stays stable.
  The padding is declared, not incidental.
* Let the message shrink to its natural width. Smaller, and changes the wire size for no benefit any
  host wants.

**Pick the first, and say so on the page.** The point of the change is that a field cannot silently
overflow — not that the message gets smaller.

### Gate

The witness is that a shot too large for the *old* 16-bit field now builds and round-trips. That is
the check at `rf_shot_tx.py:348` becoming unnecessary, demonstrated rather than deleted on faith.

Plus: the generated header's bit layout re-recorded, `tests/docs` green (the layout is documented in
`tx_internal.md`), and no `IDX_BW` import anywhere.

## Part B — two regions on TX

### What changes

`N_REGION = 2` as `RfShotRx` already has. The loader alternates regions; the player switches at the
wrap.

### Why it is in this plan rather than being a nice-to-have

**`SHOT_BUSY` is host-visible.** With two regions, a finite shot playing region A can accept a load
into region B *without being truncated* — so `SHOT_BUSY` fires only when both regions are spoken for,
and **the host's retry loop changes shape**. That is protocol, and protocol is the worst thing to
churn after someone has written against it.

### What it collects along the way

Three things already recorded as wanted, all falling out of the same change:

* **The filler gap between waveforms disappears.** Measured today: `(F,3) (P,1) (F,2) (P,1) (F,15)` —
  every handover costs a gap, because the player must yield the region it is playing.
* **The two read-during-write collisions go away by construction.** They are currently benign *by
  measurement* — proven harmless because pysim raises on a yielded read and the backends agree
  byte-for-byte. With disjoint regions the writer and reader never share an address and the question
  does not arise. See `plans/t2p_lock_chan.md`, where this is recorded as the guarantee `RfShotRx` has
  and `RfShotTx` does not.
* **TX and RX stop being structurally different** for no reason but the order they were built.

### Cost

Memory. Two regions of `nword` instead of one. At the gated geometry — `depth=256`, `nword=64` —
there is room for four, so it is free there and a real trade-off only if a design wants a shot more
than half its memory.

### Gate

A finite shot playing region A **accepts** a load into region B and is not truncated — the assertion
that cannot pass on the single-region design. Plus the handover producing **no filler at all** between
waveforms, and the collision count going to **0** rather than 2.

## Order

**Part A first, alone, gated.** It is smaller, it is independent, and Part B is easier to reason about
on a settled message layer. Two commits on one branch, one re-measurement pass each.

## Traps

**Cycle counts are measurements.** Both parts move `rf_shot_tx`'s numbers. Re-record each with the
reason; a number that moves on `RfShotRx` — which neither part touches — is a finding, not a
re-record.

**The header layout is documented.** `docs/guide/rf/rfshotbuf/tx_internal.md` carries the bit table
and cites the gate that produces it. Part A makes that table wrong; update it in the same commit, not
later.

**`ShotTxHdr` / `ShotTxResp` are shared with the C++ twin**, generated by `DataSchemaStep`. A width
change regenerates headers on both sides — confirm the generated C++ matches, rather than assuming the
schema layer handled it.

**Two regions change what `base` means.** `RfShotTx.base` is currently the build-time placement of the
single region; with two it becomes the base of the *pair*, or disappears in favour of `N_REGION` as on
RX. `RfShotRx` puts the base address **on the wire** rather than having a parameter — follow that
precedent unless there is a reason not to, and say which.

## Not in scope

- `plans/filler_offer.md` — the metronome question. Independent, and Part B shrinks its problem
  without solving it: the handover gap goes, the *lead* filler S3 measured stays.
- `plans/rf_shot_buf.md`'s remaining stage, pre-trigger capture. Wants a settled geometry, so it comes
  after this.
- `RfShotRx`. Neither part touches it; it already has two regions and puts its base on the wire.
