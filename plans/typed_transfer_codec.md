# The typed-transfer codec — one adapter, every transport

**Status:** S1 + S2 + S3c + S3a (Case 2) + S5 + S3b/Case 3 (`array_ref`) + S5d (the `COMPUTE`
opcode) + S5e (the rename to `bram_access`, and the docs restructure) LANDED (2026-08-27/28);
Case 1, the partitioning gate and S4 proposed.  Steps 1 and 2 of
the parent thread were already done before that (see *Already landed*); this file is step 3, the one
with teeth.

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

## The three access cases — the organizing frame

Every endpoint's access vocabulary falls into one of three cases, and the case is decided by
**what physically happens and therefore what owns the time**.  All three are essential; they are not
three spellings of one idea, and collapsing any of them models a cost the hardware does not have.

| | Case 1 — timed transfer | Case 2 — pipelined overlap | Case 3 — in place |
|---|---|---|---|
| `StreamIF` | `get` / `write` (schema, array, raw words) | `get_pipelined` / `write_pipelined` | — no addressing |
| `MMIF` | `read/write_schema`, `read/write_array` | `*_pipelined`, `*_anchored`, `*_spanned` | — every access is a bus transaction |
| `BramIF` | `read/write_array` — 1 element/cycle/port | `read_pipelined` / `write_pipelined` | **`array_ref`** |
| `HwState` | — already local | — | **`array_ref`** |

### Case 1 — non-overlapping timed transfer

Data physically moves into an internal structure.  The endpoint owns a latency model and the call
elapses time.  This is what exists today on `StreamIF` and `MMIF`; `BramIF` needs it at **one
element per cycle per port**.

### Case 2 — pipelined overlap

Two transfers that can proceed at once — reading one endpoint while writing another.  The `tstart`
anchoring is the whole mechanism: `write_pipelined(data, t_start)` treats the burst as having begun
at `t_start` and shortens the wait if that is in the past, so two anchored phases **overlap** and
cost `max(a, b)` rather than `a + b`.  Already implemented for streams and m_axi.

### Case 3 — in place, and it is UNIQUE to directly-addressable storage

The case that makes `array_ref` necessary, and the reason is **timing, not copies**.

A kernel computing against a BRAM does not transfer anything: in C++ it is `foo(&buf[addr], n)`, and
the function reads and writes the memory through its port.  Modelling that as `read_array` +
compute + `write_array` invents **two transfers that do not exist** and charges the design for them.
That is a wrong number, not a cosmetic loss.

Only directly-addressable storage has this case.  A stream has no addressing; every m_axi access is
a bus transaction whether you want one or not.  `BramIF` and `HwState` are the two citizens — and
that is what S4 was always reaching for: `HwState` is *pure* Case 3, with no transfer semantics at
all, which is exactly why hooks hand-write indexing against it today.

**The caller owns the timing**, because the cost is the compute loop's `II x n`, not a transfer.
What the endpoint owes the caller is the *number* to compute from — its accesses per cycle — so the
body multiplies a declared rate rather than a guessed one.

Two things this forces on the design:

- **A ref is directional, and enforced.**  `BramIFMaster.access` already says `"read"` or `"write"`
  per port; a read-port ref is a read-only view.  numpy makes that real instead of advisory —
  `flags.writeable = False` — so a stray write raises rather than silently reaching nothing.  This
  is `mem_read` / `mem_write`'s existing refusal, generalized.
- **Port contention is visible in the body's cost.**  `y[i] = f(x[i])` with both refs through the
  **same** port is two accesses per element, II=2; through the two ports of a true-dual-port memory
  it is II=1.  So a ref must be per-port (it is — `array_ref` lives on the master endpoint), and the
  body's timing depends on which ports its refs came from.

### Open, and gated before it is claimed: array partitioning

Partitioning the *local destination* of a Case 1 read buys nothing — the port is the bottleneck.
Partitioning the **port** (`ARRAY_PARTITION` on a `mode=bram` interface array) does work in Vitis,
but it emits **N separate port pairs**, i.e. N physical memories in the wrapper.  That is a topology
change, not a rate coefficient, and it lands on `wrapper_gen` and the hazard manifest.

Treat it as its own gate, the same shape as S3d: does a partitioned `mode=bram` port emit N x 14
signals, and can the wrapper instantiate N memories against them?  **Until that is measured, the
BRAM rate is a flat 1 element/cycle/port and the model says so** rather than carrying a factor
nobody has checked.


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
seven artifact-producing examples (`bram_access`, `interleaver`, `mem_copy`, `regmap`, `rf_loopback`,
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

### S3 — a `BramIF` that offers all three access cases

The payoff, and the answer to "does `BramIF` have vector operations" (today: no, only single-word
`mem_read` / `mem_write`).  **This step is larger than a cleanup and is a capability decision.**

#### S3a — pipelined vector ops on a BRAM port (the LT model)  — **LANDED**

**This section replaces an earlier draft that proposed `array_ref`, a zero-copy reference.  That
draft was wrong about the requirement, and the correction is the important part of this plan.**

The house pattern is *vectorized Python, looped HLS, timing carried by the LT model*.
`examples/stream_inband` is the reference implementation — `PolyAccel.evaluate`:

```python
samp_in, tstart = yield from s_in.get_pipelined(Float32, count=cmd_hdr.nsamp)
y = <numpy over the whole array>                       # no element loop anywhere
t_out_start = tstart + self.proc_latency * self.clk.period
yield self.timeout(proc_time)
yield from m_out.write_pipelined(array(Float32, y), t_out_start)
```

against a `poly_evaluate_impl.tpp` that is a `for (i = 0; i < nsamp; i += pf)` lane loop.  The Python
never iterates elements.  That is the point of the tool: **a per-element loop in a pysim body is a
defect, not a fidelity feature.**  Allowing vectorized operations with an LT model is a major
motivation of Waveflow, and a design body that loops has opted out of it.

`examples/bram_access` currently loops on both paths and is the outlier to fix.

##### What is missing

Only the BRAM half.  The stream half already exists (`StreamIFSlave.get_pipelined`,
`StreamIFMaster.write_pipelined`), and `MMIFMaster` has the m_axi twins.  Needed:

```python
BramIFMaster.read_pipelined(element_type, count, addr)   -> (data, tstart)
BramIFMaster.write_pipelined(data, addr, t_start)        -> None
```

**The BRAM's LT model is the simplest one in the repo**, and every term is already published rather
than invented:

- **throughput** — II=1, one element per cycle. A true-dual-port memory is one access per cycle per
  port, and `access` already says which port this is.
- **fill** — `READ_LATENCY` cycles before the first read answer, reached through the bound `BramIF`
  from the memory's Verilog `localparam`. It is a *pipeline fill*, paid once per transfer and not
  per element.
- **anchoring** — `write_pipelined(data, addr, t_start)` treats the burst as having started at
  `t_start`, shortening the wait if that is in the past. So anchoring a memory write at the feeding
  stream read's `tstart` makes the two phases **overlap**, and the cost is `max(read, write)` rather
  than their sum. This is exactly `StreamIFMaster.write_pipelined`'s existing contract; nothing new
  is needed for it.

**The proof that this belongs on the interface** is already in `bram_access`: `BramReadCmd.run_iter`
hand-writes `yield self.timeout(self.buf_r.read_latency / self.clk.freq)` behind a
`model_read_latency` flag. That is `read_pipelined`'s fill term, written out longhand in a design
body because there was nowhere else to put it. It should move onto the endpoint and the flag should
go with it.

##### The target shape

```python
# write task — one vector in, one vector to memory, the two overlapping
x, tstart = yield from self.data_w.get_pipelined(self.element_type, count=cmd.nsamp)
if ok:
    yield from self.buf_w.write_pipelined(x, cmd.waddr, tstart)

# read task — the fill is the interface's, not the body's
y, tstart = yield from self.buf_r.read_pipelined(self.element_type, cmd.nsamp, cmd.raddr)
yield from self.data_r.write_pipelined(y, tstart)
```

No `for` in either.  The C++ tasks are **unchanged** — they keep their `#pragma HLS PIPELINE II=1`
loops, exactly as `poly_evaluate_impl.tpp` keeps its lane loop.

##### The honest complication: the scenario framing has to change with it

`write_scenario` currently materializes the payload **one word per burst**
(`[np.array([x]) for x in sc.data_w]`), and its docstring justifies that by "one pysim firing equals
one RTL firing" — the rationale that this section retires.  A pysim slave dequeues one burst per
`get`, so a vectorized `get_pipelined(..., count=n)` needs the payload written as **one burst of n
words**.

That is a change to the vectors both backends play, so it must be re-gated, not assumed:

- The kernel does not care — `bram_write_cmd_task` reads a raw `hls::stream<ap_uint<W>>` `nsamp`
  times and never inspects TLAST on the payload.  So the change is invisible to the DUT.
- The **driver's** TLAST pattern changes (one assertion instead of `n`), and whether that moves the
  XSI cycle count is a measurement, not a prediction.  **Measure it; do not assert it is unchanged.**
- The pysim cycle predictions (`first_data_cycle` / `last_data_cycle` in the results JSON) will
  change, and *that is the deliverable*: they should now come from a declared LT model instead of a
  hand-rolled timeout, and they can be compared against the XSI count as a real check rather than a
  coincidence.

##### As built, and the two measurements (2026-08-27)

`BramIFMaster.read_pipelined(element_type, count, addr) -> (data, tstart)` and
`write_pipelined(data, addr, t_start)`.  Both are the stream's own model with the memory's number
plugged in — deliberately, so there is no second anchoring convention:

- **read** = `latency_init + nwords` cycles with `latency_init = READ_LATENCY`, and its `tstart` is
  `now - (n-1)*period`, character for character `StreamIFSlave.get_pipelined`'s back-calculation.
  Returns a `DataArray`, so it feeds `StreamIFMaster.write_pipelined` directly.
- **write** = `n` cycles, anchored by `max(0, n*period + (t_start - now))` — `_push_to_endpoint`'s
  clamp verbatim.
- `BramIF` gained an **optional** `clk`.  The scalar `mem_read` / `mem_write` are untimed and need
  none; a pipelined op refuses loudly without one, the same shape as `read_latency` refusing when
  unbound.  Only `bram_access`'s two `BramIF`s set it so far.
- `T2pBram` gained `store_block` / `load_block`, range-checked over the **whole extent before any of
  it lands**.  `load_block` returns a copy, and says so: a live view is Case 3.
- `examples/bram_access` has **no `for` in either `run_iter`** (asserted structurally, by `ast`, not
  by grep — the docstrings talk about loops).  `model_read_latency` is gone; the flag existed only
  because the fill was hand-written in the body.  `src/*.h` are **untouched** and keep their
  `#pragma HLS PIPELINE II=1`.

**Measurement 1 — the XSI cycle count did NOT move: 394, unchanged.**  The payload reframing takes
`data_w` from 332 one-word bursts to 4 bursts of `[256, 4, 8, 64]`, so `bounds.bin` changes and the
driver asserts TLAST 4 times instead of 332.  `words.bin` is byte-identical.  The DUT never inspects
TLAST on the payload, and the evidence is stronger than the count alone: every SVG rendered from the
**traced waveform** is byte-identical too, so the RTL run is the same run.

**Measurement 2 — the pysim predictions moved, and by a term that can be named.**

| | first_data_cycle | last_data_cycle |
|---|---|---|
| before (looped bodies) | 261 | 353 |
| after (Case 2) | 263 | 361 |

The delta is **+1 cycle per read command, cumulative** (verified per command by re-running the old
bodies under monkeypatch: +2, +3, +4, +5, +6, +7, +8 across the seven data-returning reads).  Spans
are unchanged — 63 cycles for the 64-word read, 3 for the 4-word one — so II=1 is intact and the
extra cycle is not a rate change.  It is the memory->stream pipeline boundary: the old model charged
`READ_LATENCY + n` (one hand-written fill, then n stream beats) and represented no memory-side
transfer at all; the new one charges the memory's n beats AND the stream's n beats, overlapped by
the anchor, which is `max(n, n) + 1`.  The `+1` is the one-cycle offset between "the memory
delivered word k" and "the stream delivered word k" — a real stage the old model did not have.

**A third change fell out of this, and it was a latent defect.**  `TimedStreamSink` stamped every
word of a burst with the burst's *completion* cycle.  Invisible while every burst was one word;
vectorizing made it report a 64-word read as sixty-four simultaneous arrivals, a cadence of 0 where
the XSI `AxisSlave` measures 1.  It now back-calculates per word using `get_pipelined`'s convention,
so the two ends of a channel agree about when a word moved.

##### `array_ref` is NOT replaced by this — see Case 3

S3a is Case 2.  It does not subsume `array_ref`, and an earlier revision of this plan wrongly said it
did, on the grounds that pysim does not need zero copies.  That reasoning was about the wrong thing:
pysim does not care about copies, but it does care about **time**, and an in-place computation
performs no transfer.  Routing it through a Case 1 or Case 2 op would charge the design for two
transfers that never happen.

All three cases ship.  `array_ref` is specified under **Case 3** above and remains a requirement.

#### S3b — a reference must never silently become a copy (Case 3's one hard rule)

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
keeps working unchanged, and `examples/bram_access` migrates as `elem_type=Word64` — same behaviour.

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
  object* and the new element check is an identity comparison.  `bram_access` and the three
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
examples).  `-m xsi`: `bram_access` 11/11, plus `rf_shot_buf` / `rf_samp_buf_rx` / `rf_samp_buf_tx` /
`rf_blk_delay` 21/21 — every BRAM-backed design still gates at RTL with its recorded numbers.

#### S3d — MEASURED: the csynth gates (2026-08-27, Vitis HLS 2025.1, xc7z020clg484-1)

Three minimal free-running `ap_ctrl_none` + `hls::task` tops, each with two `mode=bram
storage_type=ram_1wnr latency=1` ports, modelled on `gen/bram_access.cpp`.  All three **csynth
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

- ~~**No `*_pipelined` variants.**~~ **CORRECTED — this bullet was wrong.**  It confused two
  different things.  The *m_axi* pipelined family (`*_anchored`, `*_spanned`, `BusTiming`,
  `num_trans`) really is bus-occupancy modelling and really does not belong on a BRAM port — that
  much stands.  But `read_pipelined` / `write_pipelined` are the **LT model of a pipelined loop**,
  which a BRAM port has and which is the whole point of the tool.  See S3a: they are now the
  requirement, not a thing to avoid.
- **No acquire/commit scope.**  The tempting precedent is SOB's `acquire_write` / `commit_write`,
  but that lifecycle exists to enforce *exclusivity* (ping-pong, a producer/consumer handshake).
  `BramIF` deliberately refuses to arbitrate — `bram_access`'s docs say keeping the ranges disjoint
  is the CALLER's job, and the read-during-write collision is a reachable outcome with a negative
  gate built around it.  A `with` scope would look like arbitration and provide none.  The bare
  reference is more honest **because it promises nothing**.
- ~~**Do not vectorize `bram_access`'s loops.**~~ **CORRECTED — this was wrong too**, and for
  the same reason.  It defended the word-at-a-time loop as a faithful twin of the `II=1` C++ body.
  That is not how this repo models pipelined loops: `PolyAccel` is vectorized Python against a
  looped `.tpp`, with the timing in the LT model.  `bram_access` is the design that should be
  vectorized FIRST, not exempted.

#### S3f — naming

Case 1: `read_array` / `write_array`, the names every other endpoint already uses.
Case 2: `read_pipelined` / `write_pipelined`, matching the stream and m_axi families — a new name for
the same concept would be the drift this plan exists to remove.  (The stream family spells its read
side `get_pipelined`; the BRAM has no `get`, so `read_pipelined` is right there.)
Case 3: `array_ref` — no `get` prefix, because nothing is fetched.  Element-typed and
extent-bounded, so it is range-checked at the call.
`mem_read` / `mem_write` stay for scalar access.

### S5 — `access="readwrite"`, and the `storage_type` that makes it safe  — **LANDED**

The step Case 3 needs before it has anything interesting to demonstrate, and the one that turns a
number into a mechanism.

#### S5a — the premise: the memory was never the restriction

`waveflow/build/rtl/bram_t2p.v` is **symmetric**.  Both ports carry `din`, `we` *and* `dout`:

```verilog
if (a_en) begin  if (|a_we) mem[a_addr] <= a_din;  a_dout <= mem[a_addr];  end
if (b_en) begin  if (|b_we) mem[b_addr] <= b_din;  b_dout <= mem[b_addr];  end
```

Either port can read or write on any cycle — that is what *true* dual-port means, as against
*simple* dual-port (one write, one read).  The header comment "Port A is the write side, port B the
read side" describes how `bram_access` uses it, not what the module can do.  Vitis's own
`RAM_T2P_BRAM_1R1W` message says the same thing: 1R1W is the **usage**, T2P is the memory.

So `access` in ("read", "write") is **Waveflow's** restriction, not the hardware's.

**And `bram.py`'s justification for it conflates two things.**  It cites
`ERROR: [HLS 200-976] Cannot read as well as write over function parameter` as evidence that "a port
used both ways is what Vitis refuses."  That error is the **dataflow checker** complaining about an
argument crossing two `hls::task` bodies — one process writing what another reads.  It is not a
prohibition on a bidirectional `mode=bram` port, and S3d already showed every bram port emits `Din`
*and* `Dout` regardless of declared direction.  Fix that docstring as part of this step.

#### S5b — MEASURED (2026-08-27, Vitis HLS 2025.1, xc7z020clg484-1)

One free-running `hls::task` with a single `mode=bram` port, an opcode branch, and two loops: a
write-only `buf[a+i] = data.read()` and an in-place `buf[a+i] = buf[a+i]*3+1`.  Csynthed twice,
changing only `storage_type`:

| `storage_type` | compute II | write II | the `_B` half of the pair | safe against the current wrapper |
|---|---|---|---|---|
| `ram_1wnr` — **what codegen emits today** | 1 | 1 | **DRIVEN** — live `Addr_B`, `EN_B`, `WEN_B` tied 0 | **NO — silently wrong** |
| `ram_1p` | **2** | 1 | not declared at all | **yes** |

Both csynth clean, so the first question is answered: **Vitis does accept one `mode=bram` port read
and written inside one `hls::task` body.**

The second answer is the important one.  Under `ram_1wnr` Vitis reaches II=1 on the in-place loop by
**reading on port B and writing on port A** — and `bram_access_top.v` wires only the A halves
(`.a_addr(buf_w_addr_a >> 3)`, `.b_addr(buf_r_addr_a >> 3)`), so those reads would go to a dangling
port.  X or stale data, a clean csynth, and nothing visible until RTL.

**The shipped example is safe today, and that was checked rather than assumed**: every `_B` signal in
the generated `bram_access.v` is tied to a constant (`assign buf_r_Addr_B = 32'd0;` and so on),
because a unidirectional port needs one access per cycle and Vitis only uses A.  The hazard appears
*only* when a port becomes read-write.

#### S5c — the invariant, and the derivation

> **The wrapper wires ONE physical memory port per declared `bram` port, so the pragma must forbid
> Vitis from using two.**

Today that holds by accident of direction.  With `readwrite` it has to be enforced, which makes
`storage_type` a derived property rather than a constant:

```
access="read" | "write"   ->  storage_type=ram_1wnr    (1 physical port, II=1)
access="readwrite"        ->  storage_type=ram_1p      (pins Vitis to 1 port, II=2 in place)
```

`ram_1p` is *structurally* safe rather than safe-by-convention: it does not declare the `_B` half at
all, so no wrapper can mis-wire it.

**The alternative topology, deliberately not chosen here:** wire both halves of one declared port to
both ports of the memory, giving II=1 in place — at the cost of consuming the entire true-dual-port
memory, leaving nothing for a concurrent streaming reader.  That is a real option and it neighbours
the partitioning question (both are "one declared port, N physical ports").  Revisit it there.

#### S5c-built — as landed (2026-08-27)

- `BRAM_ACCESS = ("read", "write", "readwrite")`; `check_bram_access` is the one validator.
- `bram_storage_type(access)` in `waveflow/hw/bram.py` carries the invariant and S5b's table;
  `BramIFMaster.storage_type` is the derived property, and `composite_gen._bram_port` takes it as an
  argument instead of spelling a constant.
- `mem_read` / `mem_write` / `read_pipelined` / `write_pipelined` share one `_check_access(want, op)`
  — ``"readwrite"`` satisfies both directions.  Its message says *why* the refusal exists (a body
  writing through a `"read"` port is a body whose pragma lets Vitis take a second physical port).
- **`BramIF.bind` requires the two `access` declarations to be IDENTICAL, not merely compatible.**
  Considered and rejected: a subset rule (accessor's use ⊆ memory port's capability), which would
  allow a `"read"` accessor on a `"readwrite"` memory port.  Rejected because `BramIFSlave.access`
  is documented as *what the accessor does through this port* — a restatement of one fact, not a
  permission grant — so two differing statements are a disagreement whichever is broader, and an
  over-broad memory-side claim is one nothing checks.
- `T2pBram` gained `port_access: tuple[str, str] = ("write", "read")`, which is what makes
  `readwrite` reachable at all: a `readwrite` accessor needs a memory port that says the same.

**The `$error` was re-examined, and the conclusion is "still correct, and fenced":**

`bram_t2p.v` asserts `a_en && |a_we && b_en && (a_addr == b_addr)` — *A writes while B touches the
same address*.  That is **one-sided**, and symmetric hardware could produce the mirror case (B writes
while A touches).  For every configuration this step permits it is still exactly right: with
`port_access=("readwrite", "read")` the writer is still A, a port reading and writing across cycles
is not a collision, and B never drives `we`.  The blind spot is a *writing port B*, so **that is
refused at construction** rather than left to go wrong, with the reason named.

Lifting the refusal is one line — `a_en && b_en && (a_addr == b_addr) && (|a_we || |b_we)`, strictly
stronger and provably identical for every design that exists — but it edits `bram_t2p.v`, which is
**copied into every example's `xsi/`**, so it moves tracked files in `bram_access`, `rf_shot_buf`,
`rf_samp_buf_rx`, `rf_samp_buf_tx` and `rf_blk_delay`.  It also has a Python twin
(`bram_trace.find_read_during_write`, whose roles are named `write`/`read`) that must move with it.
That is a change with its own diff to expect, not a rider on this one.  `bram_hazard_manifest` maps a
`"readwrite"` accessor onto the **write** role — exact, because the writer is always A — and refuses
loudly if two accessors ever claim the same role.

#### S5d — the example: one design, two opcodes  — **LANDED**

Rather than a second example — each one costs a full docs page set — `BramWriteCmd` becomes
`BramWriteCompute`, taking a `WriteComputeCmd` whose `opcode` is `WRITE` or `COMPUTE`.  The opcode
shape is already idiomatic here (`FirOp` is `LOAD_TAPS` / `FILTER`; `PolyCmdType` before it).

This is *stronger* than two examples, not a compromise: the same memory, the same port and two
opcodes put the cadence difference in **one** waveform, as a controlled experiment.

- `WRITE`   — payload in, `write_pipelined` into the memory.  1 access/element, **II=1**.
- `COMPUTE` — `array_ref` (Case 3), read-modify-write in place.  2 accesses/element, **II=2**.

**The lesson is the mechanism, not the number.**  "In-place is II=2" is false in general.  "The
wrapper gives you one physical port, so the pragma pins Vitis to one, so read-modify-write costs two
cycles per element" is true and explains itself — and S5b's table is the evidence, including the
`ram_1wnr` row showing what the fast-but-broken alternative actually emits.

Gates for the example: pysim values, and an **XSI count that is a measurement** — a COMPUTE command
is new RTL work, so the recorded number will change and the new one must be explained from the
waveform, never by editing the expected constant.

##### As built (2026-08-27), and the three things that were nearly wrong

`BramWriteCmd` -> `BramWriteCompute`, taking a `WriteComputeCmd` (four words now: the opcode is a
field, so it is a word).  `BramOpField` is in `SCHEMA_CLASSES` in its own right, so the C++ compares
`c.opcode == BramOp::WRITE` against a generated `enum class`.  `buf_w` is `access="readwrite"` and
`T2pBram.port_access=("readwrite", "read")`; the emitted pragma moved to `storage_type=ram_1p` on
that port and `buf_r` kept `ram_1wnr`.  `src/bram_write_cmd_task.h` -> `bram_write_compute_task.h`,
extended with a `compute_inplace` loop; the `WRITE` branch is untouched.

**1. The wrapper had to learn that `ram_1p` has no `_B` half — and this is a real bug S5 could not
have caught.**  S5's csynth gate synthesized a read-write port but never *wrapped* one, and
`wrapper_gen` connected all fourteen pins on the strength of a comment reading *"Vitis emits it
whether or not the kernel uses it"* — true of `ram_1wnr`, false of `ram_1p`.  The design csynthed
clean and then failed elaboration with `cannot find port 'buf_w_Dout_B'`.  Fixed at the declaration:
`bram_emits_b_half(storage_type)` in `bram.py`, read by the wrapper and by
`bram_port_signals(name, halves)` so a trace manifest cannot name nets that are not in the design
either.  The emission ORDER is preserved for ports that do have a B half, so no other example's
wrapper moved a byte.

**2. Payload consumption is opcode-dependent, and `write_scenario` re-derives the split.**  A
`COMPUTE` consumes no payload words.  The bundle is framed against the `WRITE` commands only, read
off the opcodes rather than from how a scenario happened to build its `data_w`, and the total is
checked against it — a payload framed against *every* command would hand each later `WRITE` the
previous one's data, silently, from the first `COMPUTE` onwards.

**3. The ORDER of both command streams is load-bearing, and the hazard scan caught the first draft.**
The reader is much faster per command than the writer, so anything the writer must finish first has
to be early for the writer and late for the reader.  Moving the sentinel write from second to fourth
(to make room for the compute) pushed it into the reader's sentinel read: **four real
read-during-write collisions at 1020…1023**, found by `test_scenario_zero_has_no_hazard` off the
waveform, not by any value check — the values were still correct.  Scenario zero now reads: ramp,
sentinel, phase-2 write, seed, compute, refused compute on the write side; and the reader gets a
128-word spacing read of a stretch nothing rewrites, which buys the compute the room it needs.

##### The measurements

**csynth (gate 3), achieved II, from the report:** `write_payload` **1**, `compute_inplace` **2**,
`read_payload` **1**.  An II=1 on the in-place loop would have meant Vitis found a second physical
port, which is S5b's silent-breakage case; the test says so and refuses it in that direction too.

**The waveform says the same thing, and better** — the same task writes 32 words at 512 and then
rewrites *the same 32 words* in place, adjacent in time, through the same port:

| burst | writes | cycles | per element |
|---|---|---|---|
| seed `WRITE` at 512 | 32 | 375…406 (32) | **1.00** |
| `COMPUTE` at 512 | 32 | 418…480 (63) | **1.97** |

**XSI: 394 -> 568**, and every cycle is accounted for from the trace.  Per read command:
`274 | singles 274…306 | refused | 64 words 320…383 | 128 words 391…518 | 4 words 526…529 |
32 words 537…568` — i.e. 8 cycles of per-command overhead, then one cycle per returned word.  The
reader now returns **233** words against 73 and issues ten commands against eight:
`160 extra words + 2 x 8 command overhead + 8 (the token arms later, because the command grew a
word) ~= +174`.  **The COMPUTE's own 63 cycles cost the count nothing** — it runs at 418…480 while
the reader is busy with its 128-word read at 391…518, which is two free-running tasks sharing a
true-dual-port memory doing exactly what the design is for.

##### Docs left stale, deliberately — **resolved by S5e**

`codegen.md` was refreshed here (its generated blocks are re-taken from the build).  The rest of the
page set still described the pre-opcode design and quoted the old vectors and cycle numbers.  That
was a measurement-heavy refresh of the same size as this step, and S5e touched every one of those
pages anyway, so it was done there.

#### S5e — the rename, and the docs restructure  — **LANDED (2026-08-28)**

`bram_simple` was a poor name (`simple` says nothing), and the right name depended on what the
example *ended up* being: after S5d it is no longer "two tasks streaming through a memory" but **a
memory outside the kernel, reached three ways**.  Hence `bram_access`, which shares a word with the
three-access-cases table in `guide/interface/overview.md`.  (`bram_wrapper` was the option to avoid:
it names the plumbing rather than the lesson, and the wrapper is not unique to this example.)

**The rename was done first and gated before any prose was written**, on the reasoning that a rename
changes no logic: full build (pysim -> codegen_dut -> codegen_tb -> csynth) under the new name, and
XSI still **568** with `WANT_CYCLES` untouched.  It reached further than the directory — the two
tops, every artifact named for them, the `BramSimple` / `BramSimpleTB` classes, the plan file
`plans/bram_simple.md`, the test modules, and the memory index.  The survivor a grep for
`bram_simple` misses is the **TikZ source**, where the name is written `bram\_simple`.

**`overview.md` is deleted.**  Five of its seven sections were already in
`docs/guide/interface/primitive/bram.md` *with the context that makes them make sense* — "BRAM are outside the
Vitis top-level" is the guide's *Why a shared memory cannot live inside a kernel*, the 64-bit
geometry is its *addressing convention*, the `go` token is its *Sequencing belongs in the design*.
Restating them in the example is what made them read as arcana.  The two sections that were genuinely
example-specific moved rather than dropped: *The commands, and why both of them answer* into
`python.md` (as three transactions, not two), and *Overlap is legal here* into `timing.md`, where
there is now a measurement to attach it to.

`python.md` was rebuilt on the **author's own** structure — transactions, schemas, task bodies, top
level — extending their draft rather than replacing it; `pysim.md` / `rtlsim.md` / `timing.md` had
every number re-taken from a build run in that session, and `timing.md` gained the in-place
measurement.  The audience is now *someone who wants a memory in their design and needs an example*,
not a findings log.


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

**Done:** S1 -> S2 (artifacts byte-identical) -> S3c (typed `BramIF`; csynth gates green).

**Next, in order:**

1. ~~**S5 — `access="readwrite"` + the `storage_type` derivation.**~~  **DONE** — see *S5c-built*.
   Gates: `bram_access` artifacts byte-identical (its ports are unidirectional, so its pragma is
   still `ram_1wnr`), XSI 394 / 11-of-11 with `WANT_CYCLES` untouched, and a new
   `tests/build/test_bram_readwrite_vitis.py` under `-m vitis` that csynths a read-write port and a
   write-only control and greps the emitted Verilog: `ram_1p` declares **no** `_B` half, `ram_1wnr`
   does.  Framework, and its two csynth measurements were already taken (S5b).  Came first because
   Case 3 has nothing interesting to
   demonstrate without it, and because today's `ram_1wnr` default would make a read-write port
   *silently wrong* against the current wrapper.
2. ~~**Case 3 on `BramIF`** (`array_ref`)~~ **DONE** — see *S3b as built*.  S3b's rule is enforced
   rather than documented, and gated by four tests: the view is live in **both** directions, a
   composite element is refused, a read-port view raises on write, and an out-of-range extent is
   refused.  `examples/bram_access` calls none of it yet, so its artifacts and its XSI count (394)
   are unmoved.
3. ~~**S5d — the `COMPUTE` opcode on the example.**~~ **DONE** — see *S5d as built*: csynth II
   1/2/1, waveform 1.00 vs 1.97 cycles/element, XSI 394 -> 568 accounted for.  The XSI count changed
   (a COMPUTE command is new RTL work) — explain the new number from the waveform, never by editing
   the expected constant.
4. **Case 1 on `BramIF`** (`read_array` / `write_array` at 1 element/cycle/port).  **Deliberately
   last of the three cases: it has no caller.**  The taxonomy says it is essential and it is, but
   "essential to the taxonomy" is not "build it now" — the same argument that correctly deferred
   `array_ref` once already.  Build it when a design wants a staged copy.
5. **The partitioning gate** — and revisit S5c's rejected two-halves-of-one-port topology alongside
   it; both are "one declared port, N physical ports".
6. **S4 — Case 3 on `HwState`**, the merge the `add-state` arc left open.
7. **The rename** (S5e), once the example's subject has settled.

**Docs deliverable, attached to step 1.**  The three-case table belongs in
`docs/guide/interface/overview.md` as a core concept -- it cuts across every interface type, which is
what that section is for.  Write it **when Case 2 lands**, not before: two of its four rows
(`StreamIF`, `MMIF`) are true today, but the `BramIF` row would be three cells of fiction and
`HwState`'s one.  Fill each cell as its case ships.  `docs/guide/interface/primitive/bram.md` then links to it
rather than restating it, and `docs/guide/memory/hwstate.md` picks it up at step 5.

Independently of all of the above: **`_DirectBackedMMIFMaster.as_array` / `as_schema` are a live
defect** (S3b) -- `as_*` names that silently return copies.  Fix in `memory.py` whenever convenient;
it does not block anything here.
