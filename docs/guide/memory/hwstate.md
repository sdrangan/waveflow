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
from waveflow.hw.hw_state import HwState

class FirBlock(FreeRunMod):
    def __post_init__(self):
        super().__post_init__()
        self.taps = HwState(TapArray(), access="R")
        self.add_state(self.taps)
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
    static float coeffs[4];   // access=R
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
    static ap_uint<32> total[4];   // access=RW
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
    access="R",                                    # what the kernel does with it
    partition={"type": "cyclic", "factor": 4},     # how it is laid out
    bind_storage={"type": "RAM_2P", "impl": "BRAM"},
)
```

**`access`** (`"R"` / `"W"` / `"RW"`) is declared rather than inferred because C++ cannot tell: an
array argument decays to a pointer, so a read-only table and a mutated accumulator look identical.
Two hooks touching one static also create a dependency Vitis honours in the II, and saying which is
what lets the generator reason about it.

**`partition` / `bind_storage`** are structured specs, not pragma strings, so a generator can *ask
questions about them* — does the declared factor match the consuming loop's unroll? — instead of only
echoing them. They emit immediately after the declaration:

```cpp
static float total[4];   // access=RW
#pragma HLS ARRAY_PARTITION variable=total cyclic factor=4 dim=1
#pragma HLS BIND_STORAGE variable=total type=RAM_2P impl=BRAM
```

They live on the `HwState` and **not** on the `DataArray` on purpose. Partitioning is a property of
*this storage*: two state arrays of the same schema can legitimately want different layouts, and a
schema is shared, cached, and specialization-keyed, so an instance's physical layout would be a
category error there.

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
