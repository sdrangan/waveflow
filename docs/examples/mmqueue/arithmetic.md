---
title: Fixed-point arithmetic
parent: AXI-MM Command Queue (VMAC)
nav_order: 3
has_children: false
---

# Fixed-point arithmetic

The [data types](./datatypes.md) page showed that VMAC's formats are *derived* from
the command by `VmacFormats`. This page is what those formats **mean numerically**:
the operand / accumulator / output number formats, how precision grows through the
datapath, and the single requantize that lands the result back in the output format.
It is VMAC's *use* of the fixed-point machinery — the machinery itself is
[FixedField](../../guide/schema/python/fixpoint.md) and
[ComplexField](../../guide/schema/python/complex.md), not re-taught here.

## Three formats along the datapath

A value passes through three component formats, each a fixed-point `(W, I, F)`
(width, integer bits, fractional bits):

| stage | format | from `VmacFormats` |
| --- | --- | --- |
| **operand** | `(data_bw, int_bits, F_in)` with `F_in = data_bw − int_bits` | `in_format()` / `operand_elem()` |
| **accumulator** | wide, op-dependent (below) | `accumulator_format(cmd)` |
| **output** | `(out_bw, *, F_out)` with `F_out = F_in` | `output_format(cmd)` |

The operands `A`/`B` are read in the operand format; the datapath accumulates in the
**wide** accumulator format with no loss; the result is requantized **once** to the
output format on writeback. Everything between read and requantize is full precision.

## How precision grows

The accumulator format depends on the op — a product widens the fraction, a sum does
not — and on `reduce`, which widens the integer part:

- **`scalar_mult` (`α·A`) and `inner_prod` (`A·conj(B)`)** — both are complex
  *products*, so the fractional bits double: `F_acc = 2·F_in`. (The C++ accumulator
  is `std::complex<ap_fixed<ACC_BW, ACC_BW − 2·F_in>>`, i.e. exactly `2·F_in`
  fractional bits within the `acc_bw` budget.)
- **`sum` (`A + B`)** — an add keeps the scale: `F_acc = F_in`.
- **`reduce`** — summing `n_rows` results can grow the magnitude, so the reduce adds
  `⌈log₂ n_rows⌉` integer bits to the accumulator. The width budget `acc_bw` must
  hold the result; `VmacFormats` checks this and **fails loud** if it does not.

So the datapath is `cmult` / `cadd` into a wide complex `ap_fixed` accumulator,
optionally `cadd`-reduced down the rows, all carried at full precision.

## The single requantize

The result is brought back to the output format **once**, at writeback, by
`derived_shift(cmd)` — a right-shift of `SHIFT = F_acc − F_out` fractional bits,
then round and saturate per the command's modes:

- product ops (`scalar_mult`, `inner_prod`): `SHIFT = F_in` (the fraction doubled,
  the output keeps `F_in`, so shed `F_in` bits);
- `sum`: `SHIFT = 0` (same scale in and out).

Rounding (`q_rnd`: truncate vs. round) and overflow (`o_sat`: wrap vs. saturate) are
the standard fixed-point modes; both are carried in `output_format` as the
`FixedField`'s quantization/overflow modes. There is exactly one requantize per
result element — the accumulator is never rounded mid-datapath — which is what keeps
the model and the kernel bit-identical.

## Why bit-exactness matters here

Because every format is derived and there is a single, precisely-sized requantize,
the Python golden and the synthesized C++ kernel compute the **same bits**. That is
not a nicety — it is the conformance contract the [C and RTL simulation](./rtlsim.md)
page checks **byte-for-byte**: Vitis csim/cosim output is compared against the one
`execute` golden's image, and any drift in a shift, a rounding mode, or an
accumulator width shows up as a mismatch. The format algebra in `VmacFormats` is what
makes that contract hold by construction rather than by luck.

## See also

- [FixedField](../../guide/schema/python/fixpoint.md) — the `(W, I, F)` model,
  `QMode`/`OMode`, and the conformance harness behind the rounding/overflow modes.
- [ComplexField](../../guide/schema/python/complex.md) — the complex component type
  the operand/accumulator/output formats wrap.
- [Data types](./datatypes.md) — where these formats come from (`VmacFormats`).
