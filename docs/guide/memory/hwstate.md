---
title: HwState — storage inside a module
parent: Memory Modeling
nav_order: 1
summary: "HwState is storage a hardware module owns: codegen emits it as a static array inside the kernel, so it persists across firings and is synthesizable. It has no transactional interface — a hook receives it and indexes it directly. Declaring it with add_state is also what tells the extractor that a self.X read is deliberate persistent storage rather than an accidental capture."
---

# `HwState` — storage inside a module

`HwState` is the near side of the kernel boundary: storage the module **owns**, which codegen emits
as a `static` inside the generated kernel. It persists across firings, it is synthesizable, and it
has no protocol — a hook takes it as an argument and indexes it.

```python
from waveflow.hw.dataschema import DataArray
from waveflow.hw.hw_state import HwState

Coeff = IntField.specialize(bitwidth=32, signed=True)

#: 4 coefficients.  cpp_storage="raw" is what lowers it to a bare `ap_int<32> taps[4]` —
#: the form a `static` declaration wants (a struct would be legal C++ and the wrong shape).
TapArray = DataArray.specialize(Coeff, max_shape=(4,), cpp_storage="raw")


class FirBlock(FreeRunMod):
    def __post_init__(self):
        super().__post_init__()
        self.taps = HwState(TapArray())
        self.add_state(self.taps)
```

The schema can be declared as a class instead, which is the better choice when it has a name worth
reusing or needs a generated header:

```python
class TapArray(DataArray):
    element_type = Coeff
    static = True
    max_shape = (4,)
    cpp_storage = "raw"
```

## Why it must be declared

The extractor **forbids reading mutable `self.X`** from a kernel body. The reason is not
squeamishness: `self.gain` is a Python value at elaboration time, and in hardware it is either a
constant baked into the design or a register someone must write. Silently choosing one would be
guessing, so the rule refuses.

`add_state` does not relax that rule — it gives you a way to answer it. A declared object may be read
at a hook call site; an undeclared one is still rejected, and the error now says so:

> `Implicit capture of 'self.gain' at line 2. … Mark the value @sim_only, pass it explicitly, or —
> if it is storage that must persist across firings — declare it with self.add_state(self.gain).`

This is the same philosophy as `HwParam`, `DynParam`, and `StreamIF.depth`: an affordance that
records intent, rather than an inference that might be wrong.

## What it emits, and where

The declaration lands in a different place depending on the flow, and the difference is not
cosmetic:

**Control-driven kernel** (`HostActivated`, `ap_ctrl_chain`) — a `static` at the top of the kernel
function body:

```cpp
void poly_state(hls::stream<...>& s_in, hls::stream<...>& m_out, ...) {
    // Cross-firing state (HwModule.add_state) -- persists across firings.
    static float coeffs[4];
    while (true) { ... poly_state_impl::evaluate(cmd_hdr, s_in, m_out, coeffs); ... }
}
```

**Free-running kernel** (`FreeRunMod` leaf, `hls::task`) — a `static` at the top of the generated
*task body*. This is the only place it can go: the top merely instantiates tasks
(`hls_thread_local hls::task t0(fir_task, s_in, m_out);`), so there is no top-level scope to declare
into. It is also the better fit — a task has no "before the loop", and declared state is exactly what
fills that hole.

```cpp
static void state_accum_task(hls::stream<ap_uint<32> >& s_in,
                             hls::stream<ap_uint<32> >& m_out) {
    // Cross-firing state (HwModule.add_state) -- persists across firings.
    static ap_uint<32> total[4];
    ...
}
```

That the value genuinely survives re-firings in RTL is **verified, not assumed** — see
`examples/state_toy`, whose XSI gate feeds five all-ones vectors through a running total and requires
`1,2,3,4,5`. A static swept by reset would give `1,1,1,1,1` and csynth identically.

No explicit initializer is emitted. Statics are zero-initialized, which matches a freshly constructed
`DataArray` on the Python side, and it keeps the declaration out of the `config_rtl -reset`
initialized-static category.

## The storage, and the facts about it

`HwState` wraps any `DataSchema` instance — the schema is the *template* — and adds the facts that
belong to **this** storage rather than to its type:

```python
self.taps = HwState(
    TapArray(),                                    # the template
    partition={"type": "cyclic", "factor": 4},     # how it is laid out
    bind_storage={"type": "RAM_2P", "impl": "BRAM"},
)
```

**`partition` / `bind_storage`** are structured specs, not pragma strings, so a generator can *ask
questions about them* — does the declared factor match the consuming loop's unroll? — instead of only
echoing them. They emit immediately after the declaration:

```cpp
static float total[4];
#pragma HLS ARRAY_PARTITION variable=total cyclic factor=4 dim=1
#pragma HLS BIND_STORAGE variable=total type=RAM_2P impl=BRAM
```

They live on the `HwState` and **not** on the `DataArray` on purpose. Partitioning is a property of
*this storage*: two state arrays of the same schema can legitimately want different layouts, and a
schema is shared, cached, and specialization-keyed, so an instance's physical layout would be a
category error there.

### Why there is no access mode here

An earlier version of `HwState` carried an `access` mode (`"R"` / `"W"` / `"RW"`). It is gone,
because read/write permission is a property of a **hook**, not of the storage.

The argument is short. A block FIR's `load_taps` writes the taps and its `filter_block` reads them —
one module-level mode cannot state both, so whatever it said would be wrong for one of the two
hooks. And at the storage level it had nowhere to go in the generated C++, so nothing consumed it:
it emitted a comment that looked like a guarantee and was checked by nothing.

Worse, one of the values was incoherent. An `HwState` lives inside the kernel — not host-visible
(that is a regmap field), not bus-reachable (that is a [`MemoryMod`](./memorymod.md)) — and the
declaration emits no initializer, since statics are zero-initialized and that keeps them out of the
`config_rtl -reset` category. So a read-only state that no hook writes is *permanently zero*. `"R"`
described a design that cannot work.

**Where it belongs.** On the hook argument, where it becomes a `const` qualifier:

```cpp
void load_taps(hls::stream<...>& s_in, Coeff taps[4]);
Vec  filter_block(Vec x, const Coeff taps[4], Samp carry[31]);
```

That version is expressible, enforced by the compiler for free (a hook declared read-only that writes
its argument does not compile), and genuinely varies between hooks. It is also the move this codebase
already made for endpoints, where direction *is* the argument's type — `StreamIFSlave` versus
`StreamIFMaster` — rather than a tag beside it. Not yet built; it lands when hook signatures are next
touched.

**And separately, initialization.** What the storage holds at power-on is a different axis: zeros,
a baked `static const` ROM, don't-care, or "a command writes it before first read". That interacts
with whether reset restores the value, and the repo already has four unrelated initialization
mechanisms (`DataSchema.init_value()`, regmap reset defaults, `MemoryMod.load_segs`, `DynParam`), so
it wants a uniform story rather than a fifth. A block FIR does not need one — `LOAD_TAPS` writes the
taps before any `FILTER_DATA` reads them, making the initial value genuinely don't-care. A
fixed-coefficient filter would.

## In a hook

`HwState.val` delegates to the wrapped instance, so the wrapper is invisible to the arithmetic:

```python
    @synthesizable
    def accumulate(self, x: Vec4, total: HwState) -> Vec4:
        total.val[:] = total.val + x.val
        return Vec4(total.val.copy())
```

Two details worth knowing. The state must be a **parameter**, not a `self.` read inside the hook body
— the implicit-capture rule applies to hook bodies too, so that is required rather than stylistic.
And the C++ type comes from the **registered instance**, not from the annotation, so state whose
element format was built per instance (a `FixedField` specialized off a `HwParam`) emits the format it
actually has. That is why annotating the parameter `HwState` costs nothing.

## What it is not

- **Not a memory.** No transactions, no latency, no bus. Anything spanning a bus is
  [`MemoryMod`](./memorymod.md); anything the host writes is a regmap field.
- **Not a `DynParam`.** A `DynParam` binds once at pre-sim and is constant for the run; state changes
  every firing. Same "declare your intent" family, opposite lifetime.
- **Not timed.** Its cost is whatever the hook that touches it costs. If you need a storage object
  with a latency model, you want [`MemoryMod`](./memorymod.md).
