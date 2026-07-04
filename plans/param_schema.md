# Parameterization — `Param` for `DataSchema` (unified with `HwParam`)

**Status: mechanism spiked & validated (leaf + cascade) — ready to implement.** The "do parameterization
properly" pass — to run **before the next large build** (GEMM / CG), per [project-parameterization-plan]
and the user's sequencing constraint. It also **unblocks VMAC Phase 3** (the kernel needs `VmacAccel` to
be a real `HwComponent`).

## Why

Two stopgaps each reinvented parameterization instead of reusing one primitive:

1. **Schema `specialize`** — Region/Scalar/VmacCmd hand-wrote cached `type()` + `_specializations`.
   Level 1 (`ParamSchema` + `elements_for`, merged) removed *that* boilerplate, but `elements_for` is
   still a *builder function* and params aren't symbolic.
2. **`VmacAccel`** — a plain class with `int` attrs + a hand-written `specialize`, when it should be a
   `HwComponent` with `HwParam` (its widths *are* the HLS template params).

**Goal:** one parameter primitive (`Param`, unified with `HwParam`) and a **declarative** API where a
schema's `elements` reference parameters directly; the framework does specialization + caching + codegen.

## Where we are — Level 1 (done, merged)

`ParamSchema(DataList)` (`examples/vmac/param_schema.py`): a subclass declares `_param_defaults` +
`elements_for(**params)`; the base supplies a generic cached `specialize(**params)` + the default
`elements`. Boilerplate gone — but `elements_for` is a builder function and params aren't symbolic. The
stopgap.

## The goal — Level 2 (declarative)

```python
class Region(ParamSchema):
    awidth = Param(32)                                    # symbolic; supports arithmetic
    elements = {
        "addr":       IntField.specialize(awidth, signed=False),
        "row_stride": IntField.specialize(awidth, signed=True),
        "col_stride": IntField.specialize(awidth, signed=True),
    }
```

`Region.specialize(awidth=64)` resolves + caches. **Four** mechanisms (all spiked — see below):

1. **`Param`** — a symbolic value with arithmetic (`2*data_bw`, `data_bw+1`) so derived widths compose;
   names itself from the namespace key (in `__init_subclass__`), carries a default.
2. **Defer-if-symbolic** — `specialize` returns a `LazyField` when any arg is a `Param`/expression
   (records `(fn, args, kwargs)`, `.resolve(env)` later) instead of computing. A small **shared decorator**
   on the ~5 core `specialize` methods (`IntField`/`FloatField`/`EnumField`/`MemAddr`/`DataArray`).
   **Transparent for concrete calls** — `IntField.specialize(16)` returns the same class as today (verified).
3. **`ParamSchema`** — via **`__init_subclass__`, not a custom metaclass** (avoids the `DataList`/`ABCMeta`
   conflict): collects the `Param` attrs, captures the (now-lazy) `elements`, installs the default
   `elements` + a fresh cache, and provides a generic cached `specialize(**vals)`. Replaces Level 1's
   `elements_for`.
4. **Cascade** — `ParamSchema.specialize` **also defers when given a symbolic val**, so a *nested*
   parameterized schema that shares params (`"a": Region.specialize(mem_awidth=mem_awidth)`) becomes a
   `LazyField` the outer schema's resolve walk resolves. Nested schemas reuse their own cache → **shared
   schema identity** (the same `Region.specialize(64)` class wherever it appears).

### Spike results (validated, read-only)

Two read-only spikes proved the whole mechanism (~30 lines of core):

- **Leaf:** `awidth = Param(32); elements = {"addr": IntField.specialize(awidth, ...)}` →
  `Region.specialize(awidth=64)` gives 64-bit fields; defaults give 32-bit; `2*data_bw` arithmetic
  composes; caching holds; **concrete fallback intact**; the instance→type bridge works.
- **Cascade:** `VmacCmd.specialize(mem_awidth=64, data_bw=24)` propagated *both* params into the nested
  `Region` (64) and `Scalar` (64/24); outer caching holds; the nested `Region` is the **same cached class**
  as `Region.specialize(64)`; nested instances round-trip.

**No remaining feasibility unknowns** — what's left is engineering.

## Unify `Param` with `HwParam`

`HwParam[T]` (on `HwComponent`) and the schema `Param` should be the **same concept**. The crux is a
**binding mismatch**:

- `HwParam` is **instance**-based — `comp = MyComp(width=32)` binds at instantiation; codegen reads the
  values as HLS template params.
- Schema specialization is **type**-based — `IntField.specialize(W)` / `VmacCmd.specialize(...)` produce
  a *type*.

**Resolution:** keep schemas type-based, keep components instance-based, and **bridge instance → type**
with a computed property:

```python
class VmacAccel(HwComponent):
    mem_dwidth: HwParam[int] = 512
    mem_awidth: HwParam[int] = 32
    data_bw:    HwParam[int] = 32
    acc_bw:     HwParam[int] = 64
    out_bw:     HwParam[int] = 32
    # + an m_axi port + synth_fn (the kernel)

    @property
    def Cmd(self):
        return VmacCmd.specialize(mem_awidth=self.mem_awidth, data_bw=self.data_bw)
```

`accel = VmacAccel(data_bw=16)`; `accel.Cmd`; `accel.execute(cmd, mem)` (instance method). No
hand-written `specialize`; params are real `HwParam`; the instance's values drive the schema
specialization via the computed `Cmd`. The spike confirmed the **instance→type bridge works**
(`MiniAccel(awidth=48).Cmd` → a 48-bit schema). **Still open for the implementation:** whether `Param`
and `HwParam` are literally one class with two binding sites, or one shared symbolic core with two thin
wrappers — decide when wiring `VmacAccel` (Phase 3 of this plan).

## Codegen

**Concrete by default** — `Param` monomorphizes before C++, so the existing generator emits a concrete
`struct Region { ap_uint<32> addr; … }` (no new codegen for Level 2). A **templated** mode
(`template<int AWIDTH> struct Region { … }`, packing offsets become `constexpr`) is an *optional* later
mode for reusable HLS IP. Invariant: the C++ packing offsets must equal the Python schema's (concrete
matches trivially; templated needs `constexpr` offsets that reproduce them).

## Migration (after the framework lands)

- **VMAC schemas:** Region/Scalar/VmacCmd — Level 1 (`elements_for`) → Level 2 (`Param` + dict-literal).
- **`VmacAccel`:** plain class → `HwComponent` + `HwParam` + computed `Cmd` + m_axi port + `synth_fn`.
  This **unblocks VMAC Phase 3** (the kernel codegen builds on the real component).
- **Then resume VMAC:** Phase 3 (Vitis kernel + conformance), 4 (throughput), 5 (docs, rewritten).

## Phasing

1. ✅ **Spike (done).** Leaf + cascade validated read-only (see *Spike results*); mechanism + design
   decisions settled. No feasibility unknowns remain.
2. **Core framework** (the next CLI prompt). Promote the spike to production in `waveflow/hw/`:
   `Param`/`Expr`/`LazyField` + the defer-if-symbolic decorator on the 5 core `specialize` methods
   (`DataArray.max_shape` included) + `ParamSchema(DataList)` via `__init_subclass__` (with the
   cascade-deferring `specialize`). Heavy tests — leaf + cascade + a **concrete-behavior regression** vs the
   15-failure baseline. **Don't migrate VMAC yet.**
3. **`HwParam` unification + VMAC migration.** Decide the `Param`/`HwParam` shape; `VmacAccel` →
   `HwComponent` + `HwParam` + computed `Cmd` + m_axi port + `synth_fn`; migrate Region/Scalar/VmacCmd to
   the dict-literal form; retire the Level-1 `examples/vmac/param_schema.py`.
4. **Codegen check** — concrete struct unchanged + pack/unpack round-trip; (optional) templated mode later.
5. **Docs** — the `docs/guide/parameterization/` section, written against the real Level 2 API.
6. **Resume VMAC** — Phase 3 (Vitis kernel + conformance), now unblocked, then 4 / 5.

## Risks / notes

- **Touching core `specialize`** (5 methods) is the sharpest edge — the spike showed the guard is
  **transparent for concrete calls**, but the production version must keep that true across all 5:
  **existing concrete behavior byte-identical when no `Param` is present** (full suite vs the
  [15-failure baseline](project-test-baseline-failures)). **Watch circular imports** — `Param`/`LazyField`/
  the decorator want a low-level module (no `dataschema` import); `ParamSchema(DataList)` lives in/after
  `dataschema`.
- **Don't break the merged VMAC** — the public API (`VmacCmd.specialize(...)`, `VmacAccel(...)`) keeps
  working through the migration.
- **Bit-exactness is unaffected** (params monomorphize before C++); VMAC Phase 3 still validates the kernel.
