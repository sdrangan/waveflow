# The typed-transfer codec — one adapter, every transport

**Status:** S1 + S2 + S3c LANDED (2026-08-27); S3a / S4 proposed.  Steps 1 and 2 of the parent thread
were already done before that (see *Already landed*); this file is step 3, the one with teeth.

## The complaint, stated precisely

"We seem to rewrite `write_array`, `write_schema`, ... for each interface, in HLS and Python."

Half true, and the half that is false matters, because it decides how urgent this is.

**Not duplicated: the serialization.**  `MMIFMaster.write_schema` is two lines —
`obj.serialize(word_bw)` then `self.write(...)`.  `write_array` delegates to
`DataSchema.to_words_numpy` or `waveflow.hw.arrayutils.write_array`.  `StreamIFSlave.get` reaches
the same `arrayutils.read_array` / `DataSchema.deserialize`.  The layout walk has **one**
implementation.  On the C++ side the three emitters (`write_array_impl`, `write_stream_impl`,
`write_axi4_stream_impl`) come out of a *single* generator function branching on `dst_type`
(`dataschema.py`, `_emit_write_*`) — roughly ten lines of signature boilerplate per transport over a
shared field walk.

So the generated header repeating the field order six times (three transports x two directions) is
**generator surface area, not a drift hazard**: one author, no way for the copies to disagree.  Do
not "fix" it by staging streams through a temporary array — that costs a local buffer and can break
II=1 / dataflow, which is why the direct emission exists.

**Duplicated: the adapter.**  Every transport re-writes the same three-step dance:

```
nwords = schema_type.nwords_per_inst(bitwidth)     # how many words does this schema cost
raw    = <transport-specific fetch>                # the ONLY part that differs
return read_array(...) if count else schema_type().deserialize(...)
```

Written out at least four times today: `StreamIFSlave.get`, `StreamIFSlave.get_pipelined`,
`MMIFMaster.read_schema`, `MMIFMaster.read_array` (plus the write duals, plus
`read_array_pipelined` / `read_array_anchored` / `write_array_pipelined`).  Six lines each, and each
copy is free to drift from the others.

## The second complaint: three expressions of "untimed direct access to storage"

Found independently, none aware of the others:

| | where | shape |
|---|---|---|
| `BramIFMaster.mem_read/mem_write` | `waveflow/hw/bram.py` | plain methods, local word index |
| `_DirectBackedMMIFMaster` | `waveflow/hw/memory.py:370` | duck-typed MMIFMaster, generators that yield nothing |
| `HwState` | `waveflow/hw/hw_state.py` | no access vocabulary at all — hooks hand-write indexing |

This is the same gap the `add-state` arc left open ("the element-coordinate access interface is
still NOT unified across the three ... the real merge worth doing").  `BramIF` is a **fourth row**
on the storage table's own axes — beside the module, untimed, storage not synthesized, interface =
a wrapper wire — so it is a peer of those three, not a subclass of one.

## Non-goal: make `BramIF` inherit from the MM family

Rejected, and worth writing down so it is not re-proposed.  `BramIF` is a bare `Interface`
registered with `add_rtl_if` precisely because one end is inside the kernel and the other is
outside; a `QueuedTransferIF` in that role would make the kernel's memory ports vanish into a FIFO
that does not exist.  Inheriting would also drag in `addr_range`, two contention `Resource`s,
`half_duplex`, `bus_timing` and `poll_until` — every one meaningless on a BRAM port, and each an
attribute someone can set and be silently wrong about.

The shape they share is a *protocol*, so it is reached by composition.

## The design

### S1 — `TypedCodecMixin`  — **LANDED**

One mixin holding the two halves of the adapter, and nothing else:

```python
class TypedCodecMixin:
    def _typed_nwords(self, schema_type, count=None) -> int: ...
    def _unpack(self, raw_words, schema_type, count=None): ...
    def _pack(self, elements, schema_type, count=None) -> Words: ...
```

`_pack` is `MMIFMaster._pack_array_words` moved up verbatim (it already handles the
`to_words_numpy` fast path with the recursive fallback).  `_unpack` is the `read_array`-vs-
`deserialize` branch.  Neither is a generator: no simulated time passes in either, and the absence
of the `yield` is the statement that says so.

Transports supply **only the fetch**.  That is the whole point: `get` and `get_pipelined` should
differ in exactly the one thing that actually differs between them (a back-calculated `tstart`).

### S2 — adopt it  — **LANDED**

`StreamIFSlave` / `StreamIFMaster` / `MMIFMaster` mix it in; their typed methods shrink to
fetch + one call.  No public API changes, no generated C++ changes.  The `@synthesizable`
decorations stay on the public methods — the extractor lowers *call sites* via `stmt_class`, so it
never sees the helper bodies.

**Gate:** `git diff` on every generated `include/` and `gen/` artifact in `examples/` must be empty.
This step is a refactor; if a header moves, something was not equivalent.  **Run 2026-08-27 over all
seven artifact-producing examples (`bram_simple`, `interleaver`, `mem_copy`, `regmap`, `rf_loopback`,
`rf_relayout`, `rf_shot_buf`) with `--force`: empty.  PASSED.**

**As built, the mixin is FOUR methods, not three, and the fourth is a finding.**  `_unpack` and
`_unpack_elems` are separate because the two transports return genuinely different things and always
have: `StreamIFSlave.get(T, count=N)` hands back a `DataArray`, `MMIFMaster.read_array` hands back
the bare `np.ndarray` its callers index — and only the m_axi side takes the vectorized
`from_words_numpy` fast path (`DataArray.deserialize` has no such path, so folding them would have
silently changed one caller or the other).  A boolean flag on one `_unpack` would have hidden that;
two names state it.  `_codec_word_bw` is the fifth, and trivial: the stream side's width is a port
property (`self.bitwidth`), the m_axi side's is a per-call `word_bw` argument, so every helper takes
`word_bw=None` and resolves it there.

`StreamIFMaster` mixes it in per this plan but calls nothing yet — it has no typed array write.  Its
three `isinstance(data, DataSchema) -> serialize` copies (`write` / `offer` / `write_pipelined`) are
a *different* adapter (serialize-or-passthrough, no element type, no count) and were deliberately
left alone; fold them only if a `write_array` for streams ever earns its place.

### S3 — a typed `BramIF`, and the copy/reference split

The payoff, and the answer to "does `BramIF` have vector operations" (today: no, only single-word
`mem_read` / `mem_write`).  **This step is larger than a cleanup and is a capability decision.**

#### S3a — the two access modes are different HARDWARE, so both stay

The first draft of this plan proposed `read_slice` / `write_slice` over a BRAM port.  That is the
*copy* shape, and on reflection it is the less interesting half.  There are two lowerings, and an
author should be choosing between them explicitly:

| | what it is | costs | buys |
|---|---|---|---|
| `read_array` / `write_array` | stage a tile into a local array | the copy | access freedom — HLS can `ARRAY_PARTITION` the local for parallel reads |
| `array_ref` | compute in place against the shared memory | nothing | — but you are port-bandwidth-bound: a true-dual-port BRAM is 2 accesses/cycle, so `x[i]` + `x[i-1]` is II=2 or a second port |

So `read_array` on a BRAM port is not a legacy concession to be removed; it is a second, legitimate
lowering.  What is wrong today is that it is the *only* one, so the free option is unreachable.

#### S3b — a reference must never silently become a copy

`_DirectBackedMMIFMaster` (`waveflow/hw/memory.py`) already has both families, and already gets this
wrong — the worked example to design against.  `as_words()` returns a genuine numpy view
(`self._mem.segments[base]`), so writes alias.  `as_array()` calls `arrayutils.read_array`, which
does `array_obj = array_cls(); array_obj.deserialize(...)` — **a fresh object, unconditionally**.  So
a method named `as_*` silently degrades from a view to a copy the moment typed elements are asked
for, and writes to what it returns reach nothing.

The rule that prevents this, and it is checkable:

> `array_ref` is available exactly when `elem_type` has a native numpy dtype.  Otherwise it is
> refused **at declaration time**, not at the call site.  `read_array` / `write_array` (copy) stay
> available for every element type.

Considered and rejected: backing struct elements with a list of live instances so mutation aliases.
`y[i].field = v` would alias but `y[i] = obj` would not, and a reference API where one of those two
silently fails is worse than no reference API at all.

#### S3c — typing the memory is the PRECONDITION, not a separate nicety  — **LANDED**

`T2pBram.storage` is `np.zeros(depth, dtype=np.uint64)` — an array of **words**.  Over word storage
`array_ref(Word64, ...)` is a real numpy view and works today, but `array_ref(Float32, ...)` *cannot*
be: a Float32 inside a uint64 word is a packing, not a reinterpretation, so any typed accessor must
deserialize and the write goes nowhere.  That is S3b's defect reached from the other direction.

Type the memory — back it with `np.zeros(nelem, dtype=<elem dtype>)` — and `array_ref` is a plain
numpy slice.  Writes alias, and no packing exists anywhere to break the view.  **S3c gates S3a.**

**Shape: an `elem_type` + `nelem` BRAM (option 1), not an arbitrary `DataSchema` payload (option 2).**
A BRAM port in RTL is `addr` + `din`/`dout` + `we`/`en`: uniform width, address-indexed.  That is an
array, definitionally, and `elem_type` + `nelem` describes exactly what the port can express.

The case that settles it is a struct mixing a scalar and an array:

```cpp
struct ScaledArray { ap_int<16> scale; ap_int<16> data[128]; };
```

Vitis puts `scale` in a register and `data` in BRAM.  **It is not one memory.**  Modelling it as one
`BramIF` would model something the tool does not build, and `BramIF.bind`'s depth/bitwidth agreement
check — which exists to catch silent aliasing past the smaller array — has no meaning for it.

Option 1 does **not** exclude struct elements: `elem_type` may be a `DataList`, and `Rec buf[1024]`
is one memory (gated below).  Option 1 already spans everything that genuinely *is* one memory;
option 2 adds only the case that is not.

**`bitwidth` does not disappear, it becomes derived.**  RTL is always bits — `bram_t2p.v` stays
parameterized by WIDTH/DEPTH and a `float` is 32 bits on the wire.  So:

```python
@property
def bitwidth(self) -> int:
    return self.element_type().get_bitwidth()
```

which is exactly what `SobIFMaster` already does (`element_type` declared, `bitwidth` a derived
property).  This follows an established pattern here rather than inventing one.  `ramb18_count`
keeps working unchanged, and `examples/bram_simple` migrates as `elem_type=Word64` — same behaviour.

**As built (2026-08-27).**  `BramIFMaster` / `BramIFSlave` / `T2pBram` declare `element_type` +
`nelem`; `bitwidth` (and `T2pBram.dwidth`, the name the Verilog's `DW` is emitted from) are derived
properties.  Details worth knowing before S3a:

- **`depth` is GONE, not aliased.**  One address holds one element, so `nelem` *is* the depth; two
  names for one number is the drift this step exists to remove.  The three readers moved
  (`composite_gen._bram_port`, `BramIF.bind`, `T2pBram.addr_bits`).  The resource model's *feature*
  keys stay `depth`/`dwidth` — geometry vocabulary, and the key every stored measurement is filed
  under — sourced now from the declaration instead of declared twice.
- **`element_type` is a plain field, NOT an `HwParam`** (a type is not a build-time integer knob).
  `elaborate(T2pBram, {"element_type": ..., "nelem": ...})` still works — overrides go through
  `__init__`, and a class is hashable so the memo key is fine.
- **`word_element(N)`** is the vocabulary for a memory that really does hold raw words; it is
  `IntField.specialize(N, signed=False)`, which caches, so both ends of a bind get the *same class
  object* and the new element check is an identity comparison.  `bram_simple` and the three
  `rf_*_buf` framework modules migrated through it, keeping their `bitwidth` `HwParam` as the single
  width knob (at the default, `word_element(64) is Word64`).
- **The bind checks the element as well as the extent**, and that is the quieter half of the same
  class: two 32-bit ports disagreeing about float-vs-word line up at every address and return a
  correctly-shaped wrong number forever.
- **The power-of-two-byte refusal calls `_bram_addr_shift` itself** rather than re-stating the rule,
  so there is no second copy; the declaration site adds only *when*.  `check_bram_element` in
  `waveflow/hw/bram.py`.
- **pysim storage is `np.zeros(nelem, dtype=<element dtype>)`** — a `Float32` memory holds float32s
  and `store`/`load` no longer coerce to `int`.  A composite element has no numpy dtype, so its
  storage stays the packed word (and that is exactly the case S3b refuses a reference view for).
- **`_bram_addr_shift` was not touched, and neither was any wrapper code** — S3d's finding 3 held
  exactly.  `test_the_wrapper_undoes_the_shift_vitis_actually_emits` proves it from the real RTL.

**Gate (run 2026-08-27).**  Every example's tracked `gen/` + `include/` + `xsi/` artifact is
**byte-identical** after the migration (`git diff` empty across all seven artifact-producing
examples).  `-m xsi`: `bram_simple` 11/11, plus `rf_shot_buf` / `rf_samp_buf_rx` / `rf_samp_buf_tx` /
`rf_blk_delay` 21/21 — every BRAM-backed design still gates at RTL with its recorded numbers.

#### S3d — MEASURED: the csynth gates (2026-08-27, Vitis HLS 2025.1, xc7z020clg484-1)

Three minimal free-running `ap_ctrl_none` + `hls::task` tops, each with two `mode=bram
storage_type=ram_1wnr latency=1` ports, modelled on `gen/bram_simple.cpp`.  All three **csynth
clean**, and — the check that matters, because `mode=bram` on an unsized pointer degrades to an
`ap_vld` scalar port in silence — all three emit the full **14-signal A/B pair** per port:

| variant | element | Din/Dout | WEN lanes | addr scaling in RTL | FF / LUT |
|---|---|---|---|---|---|
| `w64` (control) | `ap_uint<64>` | 64 | 8 | `<< 32'd3` | 18 / 111 |
| `f32` (**gate 1**) | `float` | **32** | **4** | `<< 32'd2` | 90 / 703 |
| `rec` (**gate 2**) | `struct Rec { ap_uint<32> a, b; }` | 64 (packed) | 8 | `<< 32'd3` | 18 / 111 |

Both gates **PASS**.  What they establish:

1. **`float` on a `mode=bram` port is a real BRAM port**, and the data bus tracks the *element*
   width, not a fixed word width.  So `elem_type().get_bitwidth()` is the correct derivation.
2. **A flat struct element packs to ONE port** at the summed width — `Rec` gives a single 64-bit
   port pair, not two memories, and the datapath is byte-identical to the `ap_uint<64>` control
   (18 FF / 111 LUT).  Struct elements are one memory.  (`Rec` is deliberately flat:
   nested-struct-**by-value** is the shape that has DCE'd kernels here, and is still not gated.)
3. **The byte-address scaling is element-derived, and the existing wrapper already handles it.**  The
   generated RTL literally contains `buf_w_Addr_A_local = buf_w_Addr_A_orig << 32'd3` at 64 bits and
   `<< 32'd2` at 32 bits — which is `_bram_addr_shift(width) = log2(width/8)` exactly.  So typing the
   BRAM needs **no wrapper change**, provided `width` is sourced from the element type.
4. BRAM=0 in all three reports, as it must be: the memory lives outside the kernel.

**The constraint this exposes**, and it belongs on `elem_type` as a declaration-time refusal:
`_bram_addr_shift` refuses a width that is not a power-of-two byte count.  So a 14-bit element (the
RFdc dense-14 case) **cannot be a BRAM port element type**.  Refuse it where the type is declared,
with that reason, rather than at wrapper-generation time.

The gate sources are disposable; if S3 is built they should be re-landed as a real
`tests/hw/test_bram_typed_vitis.py` under `-m vitis`, asserting the port list and the two widths.

#### S3e — what S3 must NOT do

- **No `*_pipelined` / `*_anchored` / `*_spanned` variants.**  Those are bus-occupancy models
  (`BusTiming`, `num_trans`, the contention `Resource`s) and there is no bus here.  Offering them
  would invite a caller to model something the hardware does not have.
- **No acquire/commit scope.**  The tempting precedent is SOB's `acquire_write` / `commit_write`,
  but that lifecycle exists to enforce *exclusivity* (ping-pong, a producer/consumer handshake).
  `BramIF` deliberately refuses to arbitrate — `bram_simple`'s docs say keeping the ranges disjoint
  is the CALLER's job, and the read-during-write collision is a reachable outcome with a negative
  gate built around it.  A `with` scope would look like arbitration and provide none.  The bare
  reference is more honest **because it promises nothing**.
- **Do not apply `array_ref` to `examples/bram_simple`'s `BramWriteCmd.run_iter`.**  Its
  word-at-a-time loop is a line-for-line twin of the C++ `#pragma HLS PIPELINE II=1` body, and its
  payload bundle is framed one word per burst so one pysim firing equals one RTL firing.  A slice
  call cannot express that pacing.  If no *other* caller wants the reference form, S3a is not yet
  earned — decide that before writing it.

#### S3f — naming

`array_ref`, not `get_array_ref`: no `get` prefix, because nothing is fetched.  Element-typed and
extent-bounded, so it is range-checked at the call and `Region`-shaped.  `mem_read` / `mem_write`
stay for scalar access.

### S4 — `HwState` gets the same vocabulary

The original open item — `HwState` has no access vocabulary at all today, so hooks hand-write
indexing.  It is already a raw-storage `DataArray` (`cpp_storage="raw"`), i.e. already typed,
so S3c's work does not repeat here: it inherits `array_ref` directly.  Only worth doing once S3
has proven the shape on a second storage class.

## Already landed (steps 1 and 2 of the thread)

- `DirectMMIF`'s docstring claimed it "models a component wired directly to a BRAM or local
  register file."  Stale prose from before `BramIF` existed, and a live trap — nothing uses it that
  way and it cannot lower to a `mode=bram` port.  Replaced with what it *is* (the no-decode peer of
  `AXIMMCrossBarIF`), what it actually carries here (the AXI4-Lite control link, single-master links
  to a `MemoryMod`), and a pointer to `BramIF` for the case it was falsely advertising.
- `StreamIFSlave.get` / `get_pipelined` collapsed onto `_typed_nwords` + `_unpack` — the S1 shape,
  proven in one class before being generalized.  Dropped an unused `array` import on the way.

## Order

S1 -> S2 (gated on byte-identical generated artifacts) -> S3c (typed `BramIF`; its two csynth
gates are already MEASURED and green) -> decide whether S3a is earned -> S4.

S1+S2 are a pure refactor and can land alone.  S3c is mechanical once `bitwidth` becomes derived,
and the gates say the toolchain and the wrapper are both already ready for it.  S3a is the
capability decision, and it is the one with a real caller-side cost -- do not start it without a
caller that wants the reference form.
