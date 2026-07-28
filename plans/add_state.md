# Plan: `add_state` — declared cross-firing state in a `HwModule`

> **Status (2026-07-28): Stages 1–3 BUILT and fully gated; `examples/fir_block` is BUILT in pysim.**
> The FIR composite (`cmd_rx → MemRStream → fir_compute → MemWStream`) runs the plan's firing
> sequence green against a stateless golden, with both flavours of state falsification-tested. **Not
> yet done:** the FIR's codegen + csynth + XSI, Stage 4, and the width sweep. See
> [`examples/fir_block`](#examplesfir_block--built-in-pysim).
>
> **Status (2026-07-27): Stages 1, 2, and 3 are BUILT and fully gated — including XSI.**
> Stage 1's gate passed on both halves (hook signature byte-identical to the regmap version; the
> emitted kernel csynths with `coeffs` absent from the RTL port list). **Stage 2's XSI gate passed:
> a `static` in a free-running `hls::task` body demonstrably persists across re-firings in real
> RTL** — five all-ones vectors through `examples/state_toy` give 1,2,3,4,5 per lane, closing the
> reset-semantics trap empirically. 31 unit tests + 3 Vitis tests + the XSI gate; suite at its
> 6-failure baseline. **Not yet done:** Stage 4 (templated extents, deliberately deferred) and
> `examples/fir_block`. See [What shipped](#what-shipped).

## Motivation

The extractor **forbids reading mutable `self.X`** from a kernel body
([`_validate_no_implicit_capture`](../waveflow/build/hwcodegen.py), one of the four *meaning* rules —
see [`codegen_check_family.md`](codegen_check_family.md)). The rule is right: the extractor cannot tell
a *constant baked into the design* from a *register someone must write*, and guessing either way is
wrong. But the consequence is that **no module can carry state across firings** — both toys are
deliberately stateless, and free-running state has been the standing open item since `FreeRunMod`
landed.

`add_state` does not relax the rule. It makes the author **say which**:

```python
class FirBlock(FreeRunMod):
    def __post_init__(self):
        super().__post_init__()
        self.taps      = TapArray()
        self.init_cond = TailArray()
        self.add_state(self.taps)
        self.add_state(self.init_cond)
```

and the state objects then pass into a hook exactly the way an endpoint does:

```python
    @synthesizable
    def compute(self, taps: TapArray, init_cond: TailArray,
                s_in: StreamIFSlave, m_out: StreamIFMaster) -> ProcessGen[None]:
        ...
```

Codegen emits persistent storage rather than an elaboration-time literal. Same philosophy as
`HwParam` / `DynParam` / `StreamIF.depth`: an affordance that records intent, not a refusal.

## The motivating example — a block FIR

Chosen because it needs *two distinct flavours of state in one design*, so a single-flavour toy cannot
stand in for it:

1. **Load-once, held** — a `LOAD_TAPS` command fills a coefficient register file that survives every
   later firing.
2. **Per-block carry** — `FILTER_DATA` streams a block, and the tail (last `T-1` samples) is kept as
   the next block's initial condition. The command also selects *zeros instead of the carry*, so the
   state has a documented reset path that is part of the design rather than an artifact of `ap_rst`.

Slots in at the end of the teaching order (`regmap` → `pure_stream` → `stream_inband` → `shared_mem` →
`mem_queue`), as the first *stateful* example. Not the same design as the archived
[`_archive/rowwise_fir`](../examples/_archive/rowwise_fir), which was *stateless* per-row
load-compute-store.

### Fixed-point, and the second thing this example is for

Samples, coefficients, and output all share **one** `FixedField` format; the accumulator is
full-precision. That is deliberately not a hand-derived width — `fixpoint.mult` gives
`<Wa+Wb, Ia+Ib>` and `fixpoint.fixed_sum` grows integer bits by `ceil(log2 N)`, so
"sized for no overflow" is **computed by the format algebra**, and `quantize` back to the sample
format is the single declared lossy step. A `for`-loop of `add` would grow `I` by *one bit per tap*
(`+T`) instead of `+ceil(log2 T)` — use `fixed_sum` over the window, not repeated `add`.

With one format knob driving everything, the example doubles as the **resource-vs-bitwidth probe**: one
`HwParam` sweep, one artifact per width (the monomorphized-QoR shape), plotting DSP/LUT/FF against `W`.
The interesting structure is the DSP packing cliff — a Zynq-7020 DSP48E1 is a 25×18 multiplier, so the
per-tap multiply fits one DSP up to a coefficient width of 18 and needs several above it. Sweep
`W ∈ {8, 12, 16, 18, 20, 24}` to cross it.

**Ceiling on the sweep — and it is a *Python* ceiling, not a hardware one.** The accumulator format is
`W_acc = 2W + ceil(log2 T)`, and `fixputils.MAX_WIDTH = 64` raises `NotImplementedError` at
format-derivation time. At `T = 32` that caps the sweep at **`W ≤ 29`**; HLS would synthesize
`ap_fixed<69, …>` without complaint. So a wide-`W` point fails in **pysim** while csynth succeeds — an
asymmetry worth a loud error message rather than a puzzled afternoon. The useful sweep range sits well
below the cap, so this constrains the example's *edges*, not its point.

## What already exists

The end shape is built and Vitis-verified today — for **regmap-backed** arrays.
[`examples/stream_inband/poly.py`](../examples/stream_inband/poly.py) declares
`CoeffArray(DataArray)` with `cpp_storage = "raw"`, reads it with `self.regmap.get("coeffs")`, and
passes it to a hook. The generated top ([`gen/poly.cpp`](../examples/stream_inband/gen/poly.cpp)):

```cpp
void poly(..., float coeffs[4]) {
    ...
    // coeffs is already in scope via the kernel signature.
    ap_uint<8> err = poly_impl::evaluate(cmd_hdr, s_in, m_out, coeffs);
```

| piece | where | status |
|---|---|---|
| `DataArray` → `elem_t name[N]` hook parameter | `hwgen.py::hook_signature` | **built** |
| array *return* → appended out-parameter | `hwgen.py::hook_signature` / `_emit_function_call` | **built** |
| an array declared at outer scope, passed by bare name into the hook | `hwgen.py::_emit_regmap_get` | **built** |
| `self.X` whitelist for reads that name a *resource* (endpoint / `RegMap` / `AXIMMQueue` / schema type) | `hwcodegen.py::_validate_no_implicit_capture` | **built** |
| call-site inlining of a resource read into the argument list | `hwcodegen.py::_try_inline_regmap_get` | **built** |

`add_state` adds one more admitted resource kind and swaps the **storage decision** — s_axilite port on
the top signature → `static` array — leaving everything downstream unchanged. That is the whole idea:
this is a second storage class on a path that already carries arrays end to end.

## Design decisions

### 1. Where the `static` lands differs by flow

This is the real decision, not a detail.

| flow | site | why |
|---|---|---|
| **`HostActivated`** (`ap_ctrl_chain`, poly-shaped) | `static` at the top of the kernel function body, passed to the hook by name | there *is* a top body; this is the literal sketch |
| **`FreeRunMod` leaf** (`hls::task`) | `static` inside the **generated task body** (`hwgen.py::task_files_to_str`) | the top only instantiates tasks — `hls_thread_local hls::task t0(fir_task<64>, s_in, m_out);` — so there is no top-body scope to declare into and no ctor slot to pass through |

The task-body site is the *better* fit anyway: a task's statics persist across re-firings, which is
precisely "carry the tail to the next block". There is no "before the loop" in an `hls::task`, and
declared state is what fills that hole.

### 2. A block FIR with addresses is a composite — and load is **sequential with** compute

`LOAD_TAPS` / `FILTER_DATA` carrying source and destination addresses means `m_axi`, and task-body
emission refuses `m_axi`-owning bodies by scope (`hwgen.py::_reject_m_axi_task` — a scope boundary, not
a law of HLS). So `FirBlock` is a **composite** in the interleaver anatomy:

```
s_cmd -> [ sequencer ] -> [ mem_r_stream ] -> [ fir_compute ] -> [ mem_w_stream ] -> s_done
                                                    ^ taps + init_cond as add_state
                                                      (stream-in / stream-out, no m_axi)
```

**One compute leaf handles both opcodes.** `LOAD_TAPS` and `FILTER_DATA` arrive over the *same* read
stream, opcode-tagged in the descriptor; `run_iter` dispatches on it, writing `taps` on a load firing and
reading it on a filter firing. Taps therefore arrive as **plain `add_state`** — no channel, no second
component, no ping-pong. There is deliberately **no overlap** between loading job *n*'s taps and
computing job *n−1*: the two are separate firings of one task, strictly ordered by the command stream.

This is the whole reason the design stays inside Stages 1–2. Overlap is the one requirement that would
force taps to stop being state and become a channel, pulling in a second component and the SOB lock
protocol — see [Deferred — overlapping the tap load](#deferred--overlapping-the-tap-load). The FIR gains
little from it, and this plan already introduces enough that is new.

The opcode dispatch itself is inside the extractor's existing vocabulary: `==` / `!=` against an enum
constant is what `poly` already does (`if cmd_hdr.cmd_type == PolyCmdType.END`). Ordering comparisons
are **not** lowered (the deferred condition-IR), so keep the dispatch to equality.

### 3. Write intent is declared, not inferred

`float taps[4]` decays to a pointer, so a hook can already write its array argument. Two hooks touching
one static in the same task create a dependency Vitis honors in the II. `add_state(obj, access=…)`
(`R` / `W` / `RW`, default `RW`) costs nothing now and saves a QoR mystery later — and it is the hook
that `bind_storage` / port-count decisions hang off in Stage 3.

### 4. Sizing is literal in v1

A raw-array hook parameter prints a **literal** count (`coeffs[4]`), and `hook_template_params` derives
template parameters *only* from stream widths. A state array whose length is a build-time knob needs
`hook_signature` to emit `Coeff taps[NTAPS]` **and** `NTAPS` threaded into the hook's template list.
That is real work, not plumbing — it is Stage 4, and v1 sizes state off `HwConst` the way `CoeffArray`
sizes itself off `ncoeff: HwConst[int] = 4`.

Also: `DataArray._declared_count()` requires `len(max_shape) == 1`, so 2-D state (per-channel taps) is
out of scope for this plan. Fail loud on it rather than emitting something unreviewed.

### 5. A state hook arg resolves its type from the **instance**, not the annotation

A bitwidth sweep makes the state's element type a *per-instance* type: `Samp` comes from
`FixedField.specialize(W=self.samp_w, …)` through the instance→type bridge (the `VmacAccel.Cmd`
pattern), so the array class is built in `__post_init__` and cannot be named in a static annotation.
But `hook_signature` reads `typing.get_type_hints(method)`, so the annotation can only be the bare
`DataArray` base — and `cpp_type(annot.element_type)` on the base would be wrong.

**Resolve the concrete class from the registered state object instead.** `add_state(self.taps)`
registers the *instance*, so `type(self.taps)` **is** the specialized class, complete with its
`element_type` and `max_shape`. Precedent exists in the same function: an `MMIFMaster` hook arg already
sizes its pointer by resolving the component's m_axi master off the bound `self` (`method.__self__`)
rather than trusting the annotation. This is that move, applied to state.

Without it, `add_state` works only for statically-declared formats — which would rule out the sweep
that motivates half the example. Fold it into Stage 1, not Stage 4: it is about *type resolution*, not
about templating the extent.

## Stage 1 — `add_state` + the `HostActivated` top

1. `HwModule.add_state(obj, access="RW")` → records into an ordered `self._state` dict, keyed by the
   attribute name (resolved the way `_endpoint_name` resolves endpoints off `vars(comp)`).
2. One branch in `_validate_no_implicit_capture`: a `self.X` read that resolves to a registered state
   object is admitted, alongside `InterfaceEndpoint` / `RegMap` / `AXIMMQueue`.
3. A `StateRefStmt` (or a widened `_try_inline_regmap_get`) so `self.taps` at a call site lowers to the
   bare identifier — the `_emit_regmap_get` "already in scope" path, with the comment reworded.
4. `kernel_to_cpp` emits, at the top of the kernel body, one `static <elem> <name>[<count>];` per
   registered state object, before the extracted statements.

**Gate.** Retrofit `poly`'s `coeffs` as a **non-regmap** state array on a scratch subclass and check the
generated hook signature and call site are byte-identical to today's, modulo the declaration line and
the dropped s_axilite port. Then `-m vitis` csim + csynth on it. This gate is cheap and it exercises the
whole path against a design already known to be bit-exact.

## Stage 2 — the `FreeRunMod` task body

Same emission, different site: `task_files_to_str` declares the statics at the top of the generated
`<name>_task.h` body. No new mechanism — Stage 1's registry and call-site lowering are reused verbatim.

**Gate.** A minimal stateful stream leaf (accumulator or 3-tap FIR, no `m_axi`): pysim vs XSI
bit-exact over the firing sequence `LOAD_TAPS` → `FILTER_DATA` × 2 → `LOAD_TAPS` → `FILTER_DATA`. That
sequence is chosen, not arbitrary: ≥3 compute firings exercise the carry rather than the first firing's
zeros, a reload mid-stream proves the held state is actually replaceable, and the no-output opcode
appears both first and mid-stream (see the last trap). Add to `-m xsi`.

**Then** build `examples/fir_block` as the composite above, once Stage 2's gate is green.

## Stage 3 — pragma metadata on `DataArray`

Nothing in the generator emits `ARRAY_PARTITION` today — every occurrence in the tree is a hand-written
header or the MCP corpus. Add a **structured** ClassVar (partition type / factor / dim, `bind_storage`)
to `DataArray`, admitted through `allowed_specialize_kwargs`, and emit the pragmas immediately after the
declaration.

Structured, not a free pragma string: the same call `StreamIF.depth` got. A partition factor is a
physical property of the storage, and the generator should be able to *reason* about it (does the
declared factor match the hook's unroll?) rather than paste it.

**Gate.** csynth II report on the Stage-2 leaf with and without `complete` partition on the tap array —
the numbers must move in the direction the pragma claims.

## Stage 4 — templated state size

`hook_signature` emits `Coeff taps[NTAPS]`; `hook_template_params` gains a second source (state extents
driven by `HwParam`) alongside stream widths; `task_template_params` follows. Deferred deliberately —
it is the only piece here that is more than wiring, and Stages 1–3 do not depend on it.

## Deferred — overlapping the tap load

**Not in this plan.** Load and compute are separate, strictly-ordered firings of one task (decision 2);
job *n*'s taps are not staged while job *n−1* computes. The FIR gains little from the overlap, and the
mechanism costs a second component plus a lock protocol — enough new surface to be its own plan. The
analysis is kept here because it is the *reason* decision 2 is safe to make, and because the shape
below is what any later kernel with a real overlap requirement should adopt rather than reinvent.

The mechanism is **not** `add_state` — the moment two firings overlap, the taps stop being module state
and become a **channel**. `StreamOfBlocksIF` already is that channel
([`interface.py`](../waveflow/hw/interface.py) `SobIFMaster` / `SobIFSlave` →
`hls::stream_of_blocks<T[N], depth>` via `composite_gen.py::SobEdge`), pysim-modelled with a
free-buffer counter and XSI-verified on the interleaver.

**The shape, if it is ever built:** the loader holds the **master copy** as `add_state` and write-locks a
**working copy** into the SOB *every job*. The two features compose — state is the durable copy, the
block is the per-job handover:

```
s_cmd -> [ tap_load ]  --taps(SOB, depth=2)-->  [ fir_compute ] -> ...
             ^ add_state(taps): master copy       ^ read_lock per firing
```

Producer and consumer **must be separate components** ([`interface.py`](../waveflow/hw/interface.py):
one task body is sequential, so it cannot fill job *n* while reading job *n−1*).

**Why re-issue per job rather than acquire conditionally.** SOB is a *consume-once* channel: one
`write_lock` scope produces one block, one `read_lock` scope consumes one, and the counts must match
globally. Taps are load-once/use-many, so a compute that read-locks every firing would deadlock on job 2
waiting for a block the loader never sends. Re-issuing balances the counts by construction. The
alternative — acquire only when a "taps changed" bit is set — puts a lock scope inside an `if`, making
the producer/consumer balance **data-dependent**: it passes csim and hangs in RTL on the third job of one
specific vector. Not without a two-task XSI toy proving it first.

**Codegen surface (state it in the docs, because pysim invites the wrong model).** There are no
`write_lock()` / `read_lock()` *methods*. The C++ locks are **RAII scopes** built inside the task body —
construction acquires, scope exit commits ([`il_load_inband_task.h`](../waveflow/build/il_load_inband_task.h),
[`il_compute_inband_task.h`](../waveflow/build/il_compute_inband_task.h)). Two rules follow, both
load-bearing:

- **Scope closure is the commit** — the producer's `{ }` must close before the consumer can acquire.
- **Release order must match acquire order** — il_load's own comment: *"P filled FIRST so p_blk releases
  before x_blk (matches il_compute's read-lock order)."* Reversed, it deadlocks.

The Python `acquire_write` / `commit_write` / `acquire_read` / `release_read` are a **simulation** API;
codegen emits scopes, never calls.

**Gate, when it happens.** A dedicated toy (not the FIR): pysim shows the overlap — the loader acquires
buffer *j+1* while compute still holds *j* — and XSI reproduces it bit-exact with the firing spans
overlapping in the activity diagram. Depth is already a parameter (`derive_internal_edges` threads
`depth=int(iface.depth)` into the declaration), so re-running at `depth=2` and `depth=3` and confirming
the occupancy moves as predicted costs a constructor argument. Worth doing when a kernel needs it; the
*process*, not the FIR's numbers, would be the deliverable.

## Deferred — resident buffers with partial update (`ResidentBufIF`)

SOB's limits, stated so the boundary is explicit: one producer / one consumer; consume-once (a block
cannot be *held* across several consumer firings); **whole-block handover, no partial update**; and a
compile-time block size. The third is the one that bites at scale — copy-per-job is free for `NTAPS`
and is not free for a large resident resource whose job rewrites a slice of it.

The hand-rolled alternative is a top-level `ap_uint<DW> buf[NPP][NMAX]` plus an index sent to compute.
**That is `stream_of_blocks` with the proof removed**: the index stream is the lock, `NPP` is the depth,
"avoid collisions" is what the lock enforces. Two consequences:

1. **Verify before designing on it.** Two concurrently-running `hls::task`s sharing a plain top-level
   array have no cross-process dependence analysis — each task schedules as an independent process and
   a shared BRAM between them is unarbitrated, silently. This is *not* the `m_axi`-pointer case
   (`mem_r_stream_task` passes one, and AXI arbitrates). Build the two-task toy and check csynth **and**
   XSI under back-pressure before this becomes a design.
2. **If it is needed, it is an interface kind, not an example-level hand-roll.** The credit loop —
   `ready_idx` forward, `free_idx` back, pre-seeded with `0..NPP-1` — belongs in the slot SOBIF
   occupies: a pysim model, a generated edge, the protocol written once. The governing law already
   exists in the free-run token-pacing rule (every stage gets a per-job token or the pipe deadlocks at
   `done = N+1`); a credit loop is that law applied to *buffers* instead of jobs.

Two traps it would have to answer, neither of which SOB makes you think about:

- **It is a cycle in the task graph** (load → compute → load). Nothing in `derive_internal_edges`
  assumes a DAG, so it is structurally fine — but the return FIFO's depth must be `>= NPP` or the loop
  self-deadlocks, and `depth` here is a physical single-source property, not a hint.
- **Seeding the credits.** An `ap_ctrl_none` composite has no init phase, so the initial `NPP` free
  indices must be injected by something that fires first (the sequencer's first firing). This is the
  part hand-rolled versions get wrong.

Gated on a **measured** need, not an anticipated one.

## Traps

- **Reset semantics.** A `static` with an initializer interacts with `config_rtl -reset`.
  "Loaded once, held across firings" is exactly the case that breaks if reset sweeps it. **Verify
  empirically** on csynth + XSI (Stage 2's gate is the place); do not trust the documented default.
- **pysim / RTL state equality.** In pysim `self.taps` is a live `DataArray` the hook mutates; in C++ it
  is a `static`. Bit-exactness now depends on matching *initial* state and reset behavior, not just
  per-firing arithmetic. Cheap to get right, silent when wrong — which is why the Stage-2 gate runs
  three firings, not one.
- **State is not a `DynParam`.** A `DynParam` binds once at pre-sim and is constant for the run; state
  changes every firing. Same "declare your intent" family, opposite lifetime. The docstrings must say so
  or they will be conflated.
- **`add_state` is not a memory.** It declares registers / BRAM *inside* the kernel. Anything the host
  must write goes through a regmap; anything spanning a bus goes through `MemRStream` / `MemWStream`.
  Three storage stories, one element-coordinate interface — do not let this become a fourth.
- **The `LOAD_TAPS` firing produces no output — and that is the deadlock risk in the *sequential*
  design.** Making load and compute separate firings of one task (decision 2) buys simplicity, but it
  introduces a firing that consumes input and emits nothing downstream. This codebase has already been
  bitten here twice: the free-run pacing law (an un-paced `ap_ctrl_none` N-stage pipe deadlocks at
  `done = N+1` — every stage needs a per-job token) and the `mem_r_stream_framed_task` phantom-read bug
  (a relay that read when `fwd_bursts == 0`; the fix was an `if (nfwd > 0)` guard). So: the sequencer
  must still issue a token per job on a `LOAD_TAPS`, `mem_w_stream` must not be handed a zero-length
  write it will block on, and the done-echo must fire for both opcodes. Design the token path for the
  no-output opcode **before** writing the compute body, and make the Stage-2 gate's firing sequence
  `LOAD_TAPS` → `FILTER_DATA` × 2 → `LOAD_TAPS` → `FILTER_DATA`, so a no-output firing appears both
  first and mid-stream.

## What shipped

| piece | where |
|---|---|
| `add_state` / `StateEntry` / `discover_state` / `state_entry_for` | `waveflow/hw/hw_module.py` |
| capture-rule entry + the error message that names the affordance | `waveflow/build/hwcodegen.py::_validate_no_implicit_capture` |
| call-site lowering (state object → bare identifier) | `waveflow/build/hwgen.py::_emit_call_arg` |
| instance-resolved hook arg type (decision 5) | `waveflow/build/hwgen.py::hook_signature` |
| `state_decls_to_cpp` / `state_decl_type` / `array_pragmas` | `waveflow/build/hwgen.py` |
| declarations spliced into both sites | `kernel_body_to_cpp` (Flow 1), `task_files_to_str` (Flow 2) |
| `hls_partition` / `hls_bind_storage` + validation | `waveflow/hw/dataschema.py::DataArray` |

**Two things the work turned up that the plan did not predict.**

*`cpp_type()` mapped `FixedField` to `ap_int<W>`.* `FixedField` subclasses `IntField` (an `ap_fixed`
*is* a scaled integer, so it reuses the word serialization), and the `issubclass(typ, IntField)` branch
caught it first — right width, wrong semantics, and arithmetic in a hook would have silently lost the
binary point. Fixed at the source with a `FixedField` branch ahead of `IntField`. This was latent well
before `add_state`; the fixed-point FIR is simply the first design that would have hit it.

*Re-registering a state name must replace, not raise.* The first cut refused a second `add_state` for a
name already registered with a different object — which is exactly what a subclass swapping in a
differently-specialized array looks like. What is declared is "*this attribute* is state", so
`discover_state` now re-resolves each entry against the live attribute; rebinding `self.taps` after the
declaration follows the attribute instead of silently emitting the stale object's type.

**The reset-semantics trap: answered.** The emitted declaration carries no explicit initializer
(statics are zero-initialized, matching a freshly constructed `DataArray`), which keeps it out of the
`config_rtl -reset` initialized-static category. csynth accepts it; and the XSI gate then showed the
value actually survives re-firings in RTL — `examples/state_toy` emits 1,2,3,4,5 per lane across five
firings where a reset-swept static would emit 1,1,1,1,1. Verified, not assumed.

**A third unpredicted finding.** `render_top` emitted `task_fn<>` for a `KernelTask` with no template
args — a compile error on a non-template function. Unreachable before now: every task body in the tree
was hand-written and width-templated. A *generated* body bakes its width when the endpoints were built
from an already-`int()`-ed `HwParam`, so nothing stays symbolic to template on, and the empty-bracket
case became reachable. Fixed in `composite_gen.py::render_top`.

**Gate hygiene note.** The first version of the "not a top-level port" assertion parsed the wrong XML
element, found zero ports, and passed vacuously. It now asserts the port list is non-empty and that the
`s_axi_control` ports are present before checking `coeffs` is absent. A gate that cannot fail is not a
gate.

## `examples/fir_block` — built in pysim

The composite is exactly decision 2's picture, minus the SOB blocks the interleaver needs (a FIR is a
streaming kernel — there is no random access to buffer for):

    s_cmd -> [ fir_cmd_rx ] -> [ MemRStream ] -> [ fir_compute ] -> [ MemWStream ] -> s_done
                                                        ^ taps + carry, add_state

`FirCmdRx` frames `[MemRCmd | FirDesc]` — **one** read per job for both opcodes, which is what keeps
the no-output opcode off a special path. `FirCompute` dispatches on `FirDesc.op`.

**The no-output firing, answered.** `LOAD_TAPS` frames `MemWCmd(len=0, fwd_bursts=1)`. The writer's
`S2A` loop then trips zero times — no AXI transaction at all — while `ECHO` still emits the descriptor,
so the job completes like any other and the token path is uniform. The alternative shapes (a separate
done path, or writing the taps back to scratch) both add a stage the design does not otherwise need.

**A finding the plan did not predict: pysim and RTL disagreed at `len=0`.** `MemWStream`'s pysim twin
drained its data words unconditionally, and `get(nwords_max=0)` does *not* mean "read nothing" — it
pulls a whole burst off the buffer and truncates the returned array to zero words. So a zero-length
write silently ate the *next* command's burst in pysim while the RTL body stayed correctly in step.
Fixed at the source (`mem_stream.py`, both the in-band and plain paths); the guard is what the emitted
C++ already did. Latent since the in-band writer landed — `len=0` simply had no caller until now.

**The golden is stateless on purpose.** It convolves the whole signal with globally-indexed history
(`x[i-k]`, zero before the start), switching coefficient sets at each reload. It never mentions a
carry, so "block-wise output == global convolution" *is* the statement that the carry is right, and it
cannot pass by sharing a bug with the implementation. `tests/examples/test_fir_block.py` then breaks
each flavour of state in turn (ignore the carry; honour only the first `LOAD_TAPS`) and requires the
golden to reject both — plus guards on the scenario itself, so weakening the program or the tap sets
cannot quietly silence the gate.

**Transport is one sample per 32-bit word**, whatever `W` is. Not an oversight: it keeps the width
sweep clean (only the arithmetic width moves — bus width and word counts stay put), and dense sub-word
packing is not expressible at the interesting widths anyway, since the lane readers need an integer
`MEM_DW/elem_bw` and `W = 18` is not one. Packing is the vectorization example's subject.

**Still to do here:** the generated task body + hook (`fir_compute` is written as two `@synthesizable`
hooks, but the opcode branch and the variable-length framing have not been put through the extractor),
csynth, the XSI gate on the same firing sequence, and then the `W` sweep — which wants Stage 4, since
`ntap` as a build-time knob is exactly the templated-extent case.

## Related

- [`codegen_check_family.md`](codegen_check_family.md) — the rule family this adds an allow-list entry to
- [`dyn_param.md`](dyn_param.md) — the sibling binding site, and the docstring contrast above
- [`../docs/guide/comp_codegen/extractor.md`](../docs/guide/comp_codegen/extractor.md) — the `self.X` ban
  as documented; needs an update in Stage 1
