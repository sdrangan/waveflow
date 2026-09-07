# Plan — the shot is the buffer

**Status: DECIDED 2026-09-07, NOTHING BUILT.** Removes `nword`, `base` and the header's `nsamp` from
`RfShotTx`, and the two verdicts that existed only to police the last of them. Does not touch
`RfShotRx`, which shares none of them.

---

## Next session starts here — S1

```
claude "Read plans/rf_shot_geometry.md and build it.  One stage.  The header loses a
        field, so the wire changes -- re-measure, do not re-record.  The bad-opcode
        decision in 'One thing to decide' is the only open question."
```

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
