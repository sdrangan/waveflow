---
title: Fields
parent: Python
grand_parent: Data Schemas
nav_order: 1
audience: python
api: [IntField, FloatField, EnumField, BooleanField]
summary: "The scalar schema fields — IntField, FloatField, EnumField, BooleanField — their bitwidths, signedness, and Python values."
---

# Fields — the basic typed values

A **field** is the smallest typed unit of data in a Waveflow design: a single value with
an explicit **bit width** and interpretation (integer, float, …). Everything larger is
built out of fields — structured records ([Data Lists](./datalists.md)) and typed arrays
([Data Arrays](./dataarrays.md)).

## Why bit widths are explicit — the hardware mindset

In ordinary software a number is "just an `int`": the language picks 32 or 64 bits and you
never think about it. In **hardware, every value has a chosen bit width**, because each bit
is physical — it costs flip-flops, wires, logic, power, and timing margin. A counter that
only ever reaches 1000 needs **10 bits**, not 32; the other 22 bits are wasted silicon. So
in Waveflow a field's width is part of its *type*, declared up front, and it maps directly
to the arbitrary-precision types Vitis HLS uses (`ap_int<W>` and friends).

The first surprise coming from software is exactly this: **you size every number.**

## The two simple fields

Waveflow has two basic numeric fields. (A third — fixed-point — is important enough in DSP
to get [its own page](./fixpoint.md); we set it aside here.)

### `IntField` — arbitrary-precision integer

```python
from waveflow.hw.dataschema import IntField

Int16 = IntField.specialize(bitwidth=16, signed=True)    # signed 16-bit
UInt8 = IntField.specialize(bitwidth=8,  signed=False)   # unsigned 8-bit
Flag  = IntField.specialize(bitwidth=1,  signed=False)   # a single bit
```

`bitwidth` can be anything from 1 to thousands of bits — you pick exactly what the value
needs.

### `FloatField` — IEEE floating point

```python
from waveflow.hw.dataschema import FloatField

Float32 = FloatField.specialize(bitwidth=32)    # IEEE single precision
Float64 = FloatField.specialize(bitwidth=64)    # IEEE double precision
```

### `BooleanField` — a one-bit flag

A boolean flag is common enough to get its own type. `BooleanField` is a fixed
`ap_uint<1>` (an `IntField` subclass), but its `.val` is a Python `bool`:

```python
from waveflow.hw.dataschema import BooleanField

enable = BooleanField(True)
enable.val            # True  (a Python bool)
```

It accepts `bool` or `0` / `1` and **rejects any other value**, so a flag can never
silently hold `2`. It packs into exactly **one bit** (`ap_uint<1>`), so several flags cost
only a few bits in a record — handy for `m_axi` control words. Use it directly (no
`specialize` needed); the equivalent raw form is the one-bit `IntField` shown above.

## Why arbitrary-precision integers matter in hardware

Software integers come in a few fixed sizes (8/16/32/64). Hardware has no such restriction —
you build a register exactly as wide as you need, and **narrower is better**: less area,
less power, shorter carry chains (so a higher clock speed), and room to pack more parallel
units onto the chip. A 12-bit ADC sample is a 12-bit value; a counter over five states is 3
bits. `IntField`'s free choice of `bitwidth` models this directly (it lowers to
`ap_int<W>` / `ap_uint<W>`), so your Python model carries the **same** precision the
hardware will — no accidental 32-bit assumptions creeping in.

## Why floating point is "expensive" in hardware

Floating point is wonderfully convenient in software — huge dynamic range, no manual
scaling — but in hardware an FP unit is **big and slow**. An FP multiply or add consumes
several DSP blocks plus extra logic for exponent handling and normalization, and takes
multiple pipeline cycles; the same operation in integer or fixed-point can be a single DSP
in one cycle. So on a high-throughput datapath, designers usually **avoid floating point**
and work in integer/fixed-point instead — accepting the burden of choosing scales and
widths in exchange for far less area, power, and latency. `FloatField` is still useful (for
golden references and non-critical control values), but the performance-critical numeric
work is where [fixed-point](./fixpoint.md) comes in.

## Declaring and using fields

A specialized field is a *type*; make an instance by calling it with a value, and read or
write the value through `.val`:

```python
x = Int16(42)
print(x.val)        # 42
x.val = -7          # set a new value

f = Float32(1.5)
print(f.val)        # 1.5
```

Fields know how to serialize themselves to the packed bit representation used in simulation
and in generated C++ — that machinery is shared with the larger schema types and is covered
under [Code Generation](../hls/codegen.md).

## Modeling overflow: wrap vs saturate

What happens when a value doesn't fit its bit width? Hardware does not raise an exception —
it does one of two things, and **which one is a design choice you model explicitly**:

- **Wrap** (two's-complement overflow) — the high bits are simply dropped, so the value
  "wraps around." Cheap (it's just truncation) and the default behavior of plain integer
  arithmetic.
- **Saturate** — the value is clamped to the largest/smallest representable, so an overshoot
  sticks at the maximum instead of wrapping to a negative. Costs a comparator, but is far
  safer for signals (a saturating adder never flips a loud sample into a quiet one).

Waveflow provides both as vectorized helpers:

```python
from waveflow.utils.fixputils import truncate, saturate

# An 8-bit signed value holds [-128, 127]. What becomes of 200?
truncate(200, wid=8, signed=True)   # wrap  -> -56
saturate(200, wid=8, signed=True)   # clamp ->  127

# They work on numpy arrays too (vectorized — element-wise):
import numpy as np
saturate(np.array([200, -300, 50]), wid=8, signed=True)   # -> [127, -128, 50]
```

These same two behaviors, applied automatically on every assignment, are what the overflow
modes `AP_WRAP` / `AP_SAT` select for [fixed-point](./fixpoint.md) fields.

---

Next: group fields into records with [Data Lists](./datalists.md), or into typed arrays with
[Data Arrays](./dataarrays.md).
