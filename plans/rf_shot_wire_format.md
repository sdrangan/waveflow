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

---

## Part A as built

Built 2026-09-06 on branch `rf-shot-wire-a` off `main` (PR #187 merged as `ff2cf79`). **Part B not
started.**

### The widths, and where each number comes from

| field | before | after | why |
|---|---|---|---|
| `opcode` | 8 | **2** | three opcodes. It was a number nobody chose. |
| `tid` | `IDX_BW` = 16 | **`TID_BW` = 16** | *this module's* constant now. Same value, chosen here. |
| `nrepeat` | `IDX_BW` = 16 | **`NREPEAT_BW` = 16** | same. |
| `nsamp` | `IDX_BW` = 16 | **derived**, 16 at the gated geometry | `nsamp_bw_for(nword, samp_per_word)` |
| `status` (resp) | `IDX_BW` = 16 | **`STATUS_BW` = 8** | five verdicts, byte-rounded — a host reads bytes. |
| `nsamp_loaded` | `IDX_BW` = 16 | **derived**, same as `nsamp` | the host compares the two. |
| — | (8 bits spare, incidental) | **`_rsvd`, declared** | the padding is named. |

Both messages are **one 64-bit word at every geometry**. Header at the gated geometry:
`opcode[1:0] | tid[17:2] | nsamp[33:18] | nrepeat[49:34] | _rsvd[63:50]`.

**`ShotTxHdr` and `ShotTxResp` are now `ParamSchema`s**, and `shot_tx_schemas(nword,
samp_per_word)` is the single place that decides — the pysim twin, the generated C++ and the build's
`DataSchemaStep` all take their pair from it.

### The `IDX_BW` import is gone

`from waveflow.hw.rf_samp_buf import IDX_BW` — the clean lock-based design's one hard dependency on
the superseded family — no longer exists. The only remaining mentions of the name in
`rf_shot_tx.py` are past-tense prose explaining what changed.

### Assumption recorded: `nsamp` has a 16-bit FLOOR, it is not an exact fit

The plan says *derived from the geometry*. Derived **exactly** would be a regression, and the plan
did not anticipate it: at the gated geometry 256 samples needs **9 bits**, and a 9-bit `nsamp` makes
a host's mistyped length **alias onto a legal one** — 768 wrapping to 256 and being *accepted as
correct*. That is the identical failure the old check existed to prevent (*"a verdict that wrapped
would report a short load as a correct one"*), arrived at from the other side.

So `nsamp_bw_for` returns `max(bit_length(nword × samp_per_word) rounded up to a byte, 16)`:

* **derived** — it grows with the geometry, which is the whole point;
* **floored at what the wire carries today**, so no host sees the field narrow;
* **byte-rounded**, because a host writes bytes.

The width is a *floor* on what the design needs, not a tight fit. Overflow of the design's own length
is impossible by construction, which is what the removed check was about.

### The check at `rf_shot_tx.py:348` is gone, and demonstrated rather than asserted

```python
if nw * spw >= (1 << IDX_BW):
    raise ValueError("... does not fit the 16-bit nsamp field ...")
```

Deleted. The witness is
`tests/hw/test_rf_shot_tx.py::test_a_shot_too_large_for_the_old_16_bit_field_builds_and_round_trips`,
which does four things in order: derives the width for `nword = 65536` (**24 bits**, `> 16`),
**constructs `RfShotTx` at the geometry the old code refused**, round-trips a **262144**-sample
length through serialize/deserialize and checks it comes back exactly, and confirms both messages are
still one 64-bit word. Without it the change would be unfalsifiable.

`test_both_messages_are_one_word_and_the_padding_is_declared` holds the wire size at 64 across four
geometries including `nword = 1<<20`.

### The generated C++ matches — confirmed, not assumed

Regenerated and read. `struct ShotTxHdr` carries `ap_uint<2> opcode`, `ap_uint<16> tid/nsamp/nrepeat`,
`ap_uint<14> _rsvd`, `bitwidth = 64`, and a `pack_to_uint` whose ranges are the layout above.
**csynth passes** (Vitis HLS 2025.1, Fmax **360.77 MHz** — unchanged), which is the real test that the
hand-written body compiles against the derived widths.

Two things that needed fixing for that, and neither was optional:

1. **The task body hard-coded `ap_uint<16>`** in four places — comparing `h.nsamp` against
   `NW * SPW`, assigning `nsamp_loaded`, and the local `status`. A literal width there is a second
   opinion about the wire. They are now `decltype(h.nsamp)` / `decltype(r.nsamp_loaded)` /
   `decltype(ShotTxResp::status)`, so the body follows whatever the schema derived.
2. **`specialize()` names the subclass after its params** (`ShotTxHdr_nsamp_bw16_rsvd_bw14`), and
   that name becomes the generated **struct** name — which the body, which says `ShotTxHdr h;`, could
   not compile against. `shot_tx_schemas` pins `__name__` back to the base. One geometry is emitted
   per build, so there is never more than one `ShotTxHdr` in an include directory.

### The example used the base classes, and that was a latent second source

`examples/rf_shot_tx` built its frames with the unspecialized `ShotTxHdr` — correct at this geometry
**only by coincidence**, because the base defaults happen to equal what 256 samples derives. It now
takes `HDR, RESP = shot_tx_schemas(NWORD, SPW)`, so a geometry change cannot leave the testbench
speaking a different wire from the design. The XSI gate's response reader follows.

### No cycle count moved

`tests/examples/test_rf_shot_tx_xsi.py`: **20 gates, 0 skipped**, and the gate asserts every recorded
value exactly — `resp_last` 269 / 500, DAC 359 words, 0 zero-filled, 1 underrun at cycle 4, blocks
`(F,3)(P,12)(F,7)` and `(F,3)(P,1)(F,2)(P,1)(F,15)`, 18/55 both-live cycles, 0/2 collisions, writes
`192..255`, II=1 on five loops.

**That it did not move is expected and worth stating why:** the header is still exactly one beat and
the payload is untouched, so the *number of transfers* is identical. Only the bit positions inside
the word changed, and a cycle count cannot see those. `RfShotRx` is untouched and its 9 gates are
unchanged, which is the finding the plan asked to watch for and did not occur.

pysim is bit-identical too: `(F,192) (P,768) (F,320)` and the same five verdicts.
