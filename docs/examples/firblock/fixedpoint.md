---
title: Fixed point
parent: Block FIR with state
nav_order: 3
---
# Fixed point — one declared format, one derived accumulator

The arithmetic itself is the guide's subject: [`FixedField`](../../guide/schema/python/fixpoint.md) for
the format, and [fixed-point vectorization](../../guide/vectorization/python/fixed.md) for the
operators, the growth rules, and a worked dot product. This page is about the three decisions a real
filter has to make on top of that — what to declare, what to derive, and how the derived answer reaches
the C++.

## One format for everything

Samples, coefficients, and output all share a single format:

```python
def samp_type(samp_w=DEFAULT_SAMP_W, samp_i=DEFAULT_SAMP_I):
    """The one sample/coefficient/output format — ap_fixed<W, I>, signed."""
    return FixedField.specialize(W=int(samp_w), I=int(samp_i), signed=True)
```

Defaults are `W = 16, I = 2` — Q2.14, so values live in `[−2, 2)` with 14 fractional bits.

That is a design choice, not a necessity: a filter could perfectly well carry coefficients in a
narrower format than its samples. Using one buys two things. It makes the width sweep a **single
knob** — move `samp_w` and the samples, the taps, the state, and the output all move together, so the
resource curve has one independent variable instead of three. And it makes `y` directly comparable to
`x`, which is what lets the golden be a plain convolution.

The format is built **per instance** from an `HwParam`, not declared as a class. That is what makes it
sweepable, and it is why the hook's state arguments resolve their concrete type from the *registered
instance* rather than from a type annotation — an annotation could only name the bare `DataArray` base.

## The accumulator is derived, never declared

The one place a fixed-point design usually goes wrong is the accumulator. Here nothing is hand-sized:

```python
prod = mult(_as_fixed(win, self.samp_cls),
            _as_fixed(np.asarray(taps.val, dtype=np.int64), self.samp_cls))
acc = fixed_sum(prod, axis=1)                 # +ceil(log2 T) integer bits, NOT +T
y = quantize(acc, self.samp_cls)
```

At `W = 16, I = 2, T = 32` that gives:

| step | format | why |
|---|---|---|
| `mult` | `ap_fixed<32, 4>` | `<Wa+Wb, Ia+Ib>` — a product needs both operands' bits |
| `fixed_sum` over `T` | `ap_fixed<37, 9>` | summing `T` terms adds `ceil(log2 T) = 5` integer bits |
| `quantize` | `ap_fixed<16, 2>` | back to the declared format — **the only lossy step** |

So the accumulator "sized for no overflow" is *computed*, and by construction it cannot silently
overflow: every product enters `acc` exactly (same fractional bits), and the sum has room for the
worst case.

{: .warning }
> **Reduce with `fixed_sum`, never a loop of `add`.** Repeated pairwise `add` grows the integer bits by
> **one per tap** — `+T` — instead of `+ceil(log2 T)`. At `T = 32` that is a 32-bit growth where 5 will
> do, which blows through the width ceiling below and produces an accumulator four times wider than the
> arithmetic needs.

`quantize` is the single declared lossy step, and it is written down rather than implied. It uses the
format's `AP_TRN` / `AP_WRAP` — truncate toward −∞, wrap on overflow — which is the Vitis default and
therefore what the RTL does with a plain assignment.

## Getting the derived format into the C++

This is the part specific to this example. The kernels are hand-written C++, so they need a concrete
accumulator type — and the temptation is to type `ap_fixed<37, 9>` into the header, or worse, to write
`2*W + 5` and call it parameterized.

Both are wrong for the same reason: they *re-derive* in C++ a result the Python already knows, and the
two can then disagree. Instead the build **runs the algebra** and emits the answer:

```python
def acc_format(ntap, samp_w, samp_i):
    """The accumulator format, DERIVED by running the format algebra — never hand-typed."""
    samp = samp_type(samp_w, samp_i)
    prod = mult(_as_fixed(np.zeros((1, int(ntap)), dtype=np.int64), samp),
                _as_fixed(np.zeros(int(ntap), dtype=np.int64), samp))
    return fixed_sum(prod, axis=1).element_type.get_format()
```

It multiplies and sums *dummy zeros* purely to ask the type system what format comes out, then writes
it into a generated header:

```cpp
namespace fir_types {
    typedef ap_fixed<37, 9, AP_TRN, AP_WRAP> acc_t;   // full precision: no rounding until quantize
    static const int NTAP   = 32;
    static const int SAMP_W = 16;
}
```

The kernels then say `acc_t acc = 0;` and know nothing about how wide it is. If the growth rules ever
change, the C++ moves with them — and the RTL accumulator is bit-exact with the Python one *by
construction* rather than by inspection.

## The ceiling — and it is a Python ceiling

`fixputils.MAX_WIDTH = 64` raises `NotImplementedError` at format-derivation time, because the model is
backed by a single numpy `int64` and a wider format would silently wrap.

The accumulator is `W_acc = 2W + ceil(log2 T)`, so at `T = 32` the sweep caps at **`W ≤ 29`**:

| `W` | `W_acc` at `T = 32` | |
|---|---|---|
| 24 | 53 | fine |
| 29 | 63 | the last width that fits |
| 30 | 65 | **raises** |
| 32 | 69 | **raises** |

The asymmetry is the thing to remember: **a wide-`W` point fails in pysim while csynth would happily
build `ap_fixed<69, …>`.** The hardware is not the constraint; the model is. This is a real edge — the
[sweep](./resource_fit.md) runs `W ∈ {8, 12, 16, 24}` and would hit the ceiling at `W = 32` with 32 taps,
which is why that point is absent from the grid rather than merely uninteresting.

It constrains the *edges* of the design space, not its useful middle: a DSP48E1 is a 25×18 multiplier,
so the widths worth studying sit well below 29 anyway.

## Why the golden can be bit-exact

Everything above is what makes the [testbench](./testbench.md) able to demand *bit-exactness* rather
than a tolerance. The Python model and the RTL run the same declared format, the same derived
accumulator, and the same single quantize — so there is no rounding difference to absorb, and any
mismatch is a real bug rather than numerical noise.

That is also why an approximate check would be worse than useless here: the bug this kernel actually
had (a delay line seeded one slot off) produces *plausible* numbers. A tolerance would have hidden it.

## Where to next

- [Python](./python.md) — the design in code, including where the formats are threaded through.
- [The two kernels](./kernels.md) — `acc_t` in use, and the MAC that fills it.
- [The sweep and its results](./resource_fit.md) — what changing `W` actually costs in DSPs, measured: a *fall*
  at 8 bits where two multiplies share one DSP48, and a doubling at 24 where one product splits.
