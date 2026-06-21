---
title: Data types — command and formats
parent: AXI-MM Command Queue (VMAC)
nav_order: 2
has_children: false
---

# Data types — the command and its formats

VMAC's data layer has two halves, both in
[`examples/vmac/vmac_datatypes.py`](../../../examples/vmac/vmac_datatypes.py): the
**command** the host enqueues (`VmacCmd`) and the **dependent types**
(`VmacFormats`) that a command implies for the datapath. This page is about those
*types*; the numeric behaviour they describe — how precision grows and is requantized
— is the [arithmetic](./arithmetic.md) page.

## The command — `VmacCmd`

A `VmacCmd` is the instruction the accelerator dequeues. It is a
[`ParamSchema`](../../guide/schema/python/) specialized on the accelerator's address
and component widths (`VmacCmd.specialize(mem_awidth=…, data_bw=…)`), so its fields
are sized to the design. Its fields:

| field | type | role |
| --- | --- | --- |
| `op` | `EnumField(OpCode)` | which element-wise op — `scalar_mult` / `inner_prod` / `sum` / `end` |
| `reduce` | `BooleanField` | sum each result down its rows to one output row |
| `n_rows`, `n_cols` | `UInt16` | the operand matrix shape |
| `a`, `b`, `y` | `Region` | operand A, operand B, destination Y descriptors |
| `alpha` | `Alpha` | the scaling operand for `scalar_mult` |

### Region descriptors

The operands are not inlined in the command — they are **pointers into shared
memory**, described by a `Region`:

| field | type | meaning |
| --- | --- | --- |
| `addr` | `IntField` (width `mem_awidth`) | base **element** offset of the region |
| `row_stride` | `IntField`, signed | outer pitch in **elements** between rows |

A region addresses a row-major matrix as `M[i, j] = mem[addr + i·row_stride + j]`.
Addresses and strides are in **element** units, not bytes or words — the
width-agnostic coordinate system the kernel and the SimPy
[`Region`](./pymodel.md#region--element-coordinates) both use. `a` is always read;
`b` is read for `inner_prod` / `sum`; `y` is the destination (collapsed to a single
row when `reduce` is set).

### The scalar — `Alpha`

`scalar_mult` needs a per-row complex scalar, and it can come two ways, captured by
the `Alpha` schema's `direct` flag:

- **direct (immediate)** — `direct == True`: the scalar travels in the command itself
  (`imm`, a complex value built from the operand's integer component type).
- **indirect (per-row)** — `direct == False`: `addr` / `stride` point at a column of
  scalars in memory, one per row, read like any other operand.

This is the small but real bit of ISA design in VMAC: an immediate for the common
case, an indirect for a per-row vector, selected by one boolean.

## The dependent types — `VmacFormats`

VMAC's fixed-point formats are **not stored per element**. Nowhere does a matrix
carry a scale or a width with each value. Instead the formats are a pure function of
the design's structural widths and the command, computed by `VmacFormats` — a frozen
dataclass over `data_bw`, `int_bits`, `acc_bw`, `out_bw`, and the rounding/overflow
modes `q_rnd` / `o_sat`. This is the page's signature idea: the **types are
derived, not declared**.

`VmacFormats` answers, for a given command:

- **`operand_elem()`** — the shared-memory element type: a
  [`ComplexField`](../../guide/schema/python/complex.md) whose real and imaginary
  parts are a `FixedField(data_bw, int_bits)`. This is the type `A`, `B`, and the
  indirect `α` are stored in.
- **`accumulator_format(cmd)`** — the wide intermediate format the op accumulates in,
  which *depends on the op* (a product op widens differently from a sum) and on
  `reduce` (a row sum adds `⌈log₂ n_rows⌉` integer bits).
- **`output_format(cmd)`** — the `FixedField` the result `Y` is written back in,
  at width `out_bw` with the command's rounding/overflow modes.
- **`derived_shift(cmd)`** — the single requantize shift from accumulator to output.
- **`lane_capacity(word_bw)`** — how many complex columns pack into one memory word,
  which sets the kernel's packing factor.

Because these are derived, the golden model and the synthesizable kernel cannot
disagree about a format: both read it off the same `VmacFormats`. The *arithmetic*
those formats encode — what "widens differently" means, how the single requantize is
sized — is the [next page](./arithmetic.md).

## The complex element

`A`, `B`, and `Y` are matrices of **complex fixed-point** values, modeled with the
framework's [`ComplexField`](../../guide/schema/python/complex.md) over a
[`FixedField`](../../guide/schema/python/fixpoint.md) component. VMAC does not
re-teach complex or fixed-point typing — it *uses* them: `operand_elem()` above is
just `ComplexField.specialize(FixedField(...))`. See those schema pages for how a
complex field serializes (real low, imaginary high) and how a fixed-point field's
`(W, I, F)` works.

## Next

- [Fixed-point arithmetic](./arithmetic.md) — what the derived formats *mean*
  numerically: the operand / accumulator / output formats, how precision grows
  through `cmult` → reduce → requantize, and the single requantize.
