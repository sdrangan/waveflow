---
title: Cross-firing state
parent: Block FIR with state
nav_order: 2
---
# Cross-firing state — two flavours in one module

[`HwState`](../../guide/memory/hwstate.md) is the mechanism: storage a module owns, emitted as a
`static` inside the generated kernel, declared with `add_state` so the extractor knows a `self.X` read
is deliberate. That page explains it in general. This one is about *using* it for a real design, and
about the thing a single-storage example cannot show — **two pieces of state with different
lifetimes, interacting, in one module.**

## The two flavours

```python
# THE point of this example.  Two storages, two lifetimes, one module.
self.taps = HwState(tap_array_type(t, self.samp_cls)(),
                    partition={"type": "complete"})
self.carry = HwState(carry_array_type(t, self.samp_cls)())
self.add_state(self.taps)
self.add_state(self.carry)
```

|  | `taps` | `carry` |
|---|---|---|
| holds | the `T` coefficients | the last `T−1` input samples |
| written by | a `LOAD_TAPS` firing | **every** `FILTER` firing |
| read by | every `FILTER` firing after the load | the *next* `FILTER` firing |
| lifetime | **held** until explicitly replaced | **one firing** |
| survives a reload? | it *is* the reload | yes — a tap swap does not reset the signal history |

They are the same mechanism and opposite disciplines. `taps` is *configuration*: written rarely, read
constantly, and a firing that forgets to write it is correct. `carry` is *pipeline history*: written by
every firing, and a firing that forgets to write it corrupts the next one.

The last row is the one worth dwelling on. A `LOAD_TAPS` in the middle of a stream swaps the
coefficients **without** disturbing the sample history — the filter changes, the signal does not
restart. That falls out of the two being separate storages, and it is why the gate program is
`LOAD → FILTER × 2 → LOAD → FILTER` rather than something shorter: only a reload *mid-stream* can show
that the two lifetimes are genuinely independent.

## The shapes are derived from one parameter

Neither array is written down with a literal extent:

```python
def tap_array_type(ntap, samp_cls):
    """cpp_storage="raw" is what lowers it to a bare Samp taps[T]."""
    return DataArray.specialize(samp_cls, max_shape=(int(ntap),), cpp_storage="raw")

def carry_array_type(ntap, samp_cls):
    """The last T-1 samples of the previous block."""
    return DataArray.specialize(samp_cls, max_shape=(max(int(ntap) - 1, 1),), cpp_storage="raw")
```

`T` and `T−1` both follow from `ntap`, and the element type from the sample format — so changing
either parameter moves the storage, the arithmetic, and the generated C++ together. `T−1` is not an
arbitrary choice: it is exactly how much history `y[0]` of the next block needs, and one element more
would be dead storage.

{: .note }
> `max(ntap - 1, 1)` guards the `T = 1` degenerate case, where a filter has no history at all and a
> zero-length array would be invalid C++.

## What gets emitted, and where

`add_state` is what makes those declarations *the generated ones*. `state_decls_to_cpp` emits, from the
two registrations above:

```cpp
    // Cross-firing state (HwModule.add_state) -- persists across firings.
    static ap_fixed<16, 2, AP_TRN, AP_WRAP> taps[32];
    #pragma HLS ARRAY_PARTITION variable=taps complete dim=1
    static ap_fixed<16, 2, AP_TRN, AP_WRAP> carry[31];
```

Three things are single-sourced there, and none of them is retyped by hand in the kernel: the
**element type** (from the sample format), the **extents** (from `ntap`), and the
**`ARRAY_PARTITION` pragma** (from the `partition=` argument on `taps`). The task bodies for this
design are hand-written — see [The two kernels](./kernels.md) — but they declare **no storage of their
own**; they `#include` this. So the arithmetic can be hand-tuned while the storage still cannot drift
from the Python model that the golden runs against.

The partition is on `taps` and deliberately not on `carry`: the window reduction reads all `T`
coefficients every sample, so they must all be addressable at once, whereas `carry` is touched once at
the start and once at the end of a firing.

### Why it lands *inside* the body

For a free-running `hls::task` the declaration is emitted at the top of the **task body**, not at file
scope and not in a top-level function — because the top only instantiates tasks, so there is no other
scope to declare into. A `static` there is what survives the runtime re-firing the body per command.

The consequence is a slightly unusual-looking C++ file: the generated `fir_compute_state.inc` is
`#include`d *at function scope*. That is deliberate, and it is the emission site the mechanism targets:

```cpp
template <int MEM_DW>
static void fir_compute_serial_task(...) {
    ...
#include "fir_compute_state.inc"     // <- the statics land here
    FirDesc d;
```

## The declared reset path

A filter needs a way to say "start this block from silence" — the first block of a new signal, or a
discontinuity. The tempting answer is to lean on `ap_rst`. This design does not:

```python
prev = np.zeros(t - 1, dtype=np.int64) if zero_state else np.asarray(carry.val, dtype=np.int64)
```

`zero_state` is a field on the command, so **resetting the history is a documented operation of the
design**, selected per firing by the host, rather than a side effect of how the tool happened to
implement a `static`. Two reasons that matters:

1. **It is testable.** A reset you can request is a reset you can write a gate for. `zero_state` is
   what the first block of every scenario uses, so every run exercises it.
2. **The alternative is not actually reliable.** "Loaded once, held across firings" is exactly the case
   that breaks if reset sweeps the storage, and whether it does depends on `config_rtl -reset` and on
   whether the static carries an initializer — a tool detail, not a design decision.

On that second point the emitted declaration carries **no explicit initializer**. Statics are
zero-initialized, which matches a freshly constructed `DataArray` on the Python side, and it keeps the
storage out of the `config_rtl -reset` initialized-static category.

## Does it actually persist in RTL?

That question is not rhetorical, and csynth cannot answer it. An initialized `static` swept by `ap_rst`
would csynth perfectly and then quietly zero itself every firing — the design would look right and be
wrong. Only a real RTL run can tell.

Two pieces of evidence, in increasing strength:

- [`state_toy`](../../guide/memory/hwstate.md) — the minimal case, a per-lane running total. Five
  all-ones vectors give `1,2,3,4,5` per lane if the state persists and `1,1,1,1,1` if it does not. The
  failure mode is loud by construction.
- **This example** — `fir_block`'s [RTL gate](./rtlsim.md) runs the full
  `LOAD → FILTER × 2 → LOAD → FILTER` program through real RTL and requires every output block to be
  bit-exact against a golden that never mentions a carry. That is a stronger statement: not just that
  *a* value survived, but that both storages survived, independently, across a mid-stream reload.

## The trap this kernel actually fell into

Worth stating because it is the failure that got past csynth. State being *stored* correctly is not
the same as state being *loaded* correctly. In the filter loop, each iteration shifts its delay line
**before** it accumulates, so the value seeded into that line must be the state at the **top** of the
iteration — not the invariant that holds at multiply time. Seeding the more natural-looking one slides
the history by a slot and silently drops the newest carry sample.

It csynthed cleanly. It passed the first block, because `zero_state` starts that one from zeros so it
never reads the carry at all. Only the *second* block's first samples were wrong. The full story is on
[The two kernels](./kernels.md); the lesson for this page is that **a stateful design needs a gate that
exercises at least three firings**, since the first one frequently does not touch the state.

## Where to next

- [Fixed point](./fixedpoint.md) — the format the storage above is parameterized by.
- [The two kernels](./kernels.md) — how the state is read and written inside a firing.
- [`HwState` guide](../../guide/memory/hwstate.md) — the mechanism itself, and what it is *not*
  (it is not a `DynParam`, and it is not a memory).
