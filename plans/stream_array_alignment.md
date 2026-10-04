# `stream_array_alignment` — generated C++ and Python pack a DataList with an array differently at word_bw=64

**Status: DIAGNOSED, NOT FIXED.** Found 2026-09-29 while building
`hwdesign/demos/stream/poly`. That demo's kernel and testbench are
unaffected, since both use the C++ packing. Only its Python decoder of the
co-simulation VCD disagrees.

---

## The prompt

```
Read plans/stream_array_alignment.md and fix the DataList packing
inconsistency it describes, so the generated C++ and the Python schema
agree on the stream layout at every word_bw, with the regression tests it
lists.  Decide the layout question in "Which layout is right" first and
say which you chose and why.  Then fix the secondary read_array issue.
```

---

## The bug

For a `DataList` that contains a `DataArray` field, the generated C++ and
the Python schema disagree about the word layout when `word_bw=64`. The
generated C++ also disagrees with itself.

The schema that shows it:

```python
Float32 = FloatField.specialize(bitwidth=32)
TxId    = IntField.specialize(bitwidth=16, signed=False)
Nsamp   = IntField.specialize(bitwidth=16, signed=False)

class CoeffArray(DataArray):
    element_type = Float32
    static = True
    max_shape = (4,)

class PolyCmdHdr(DataList):
    elements = {
        "tx_id":  {"schema": TxId},
        "coeffs": {"schema": CoeffArray},
        "nsamp":  {"schema": Nsamp},
    }
```

With `tx_id=0x10`, `coeffs=[0, 1, 0, -0.5]`, `nsamp=40`, at `word_bw=64`:

| Path | Words | Layout |
| --- | --- | --- |
| Python `serialize(word_bw=64)` | 3 | `[tx_id \| c0<<32]  [c1 \| c2<<32]  [c3 \| nsamp<<32]` (dense) |
| C++ `nwords<64>()` | 3 | agrees with Python |
| C++ `write_axi4_stream<64>` / `read_axi4_stream<64>` | **4** | `[tx_id]  [c0 \| c1<<32]  [c2 \| c3<<32]  [nsamp]` (array starts on a fresh word) |
| C++ `write_stream<64>` | **4** | same as axi4 |
| C++ `write_array<64>` | **4** | writes `x[0]..x[3]` |

Observed on the wire in RTL co-simulation, with the command header decoded
as 64-bit TDATA:

```
0x0000000000000010   0x3f80000000000000   0xbf00000000000000   0xbf00000000000028
```

and Python's `serialize(word_bw=64)` of the same header:

```
0x10   0x3f800000   0x28bf000000
```

At `word_bw=32` every field lands on its own word in both, so the two agree
and the bug is invisible.

### Three consequences

1. **The C++ and the Python cannot exchange this message at 64 bits.** A
   Python host, or a Python decoder of a VCD, that packs or unpacks with the
   schema reads the wrong fields: coefficients shifted by one word, and
   `nsamp` as 0.
2. **`write_array<64>` overflows.** It writes `x[3]`, but `nwords<64>()` is
   3, so any caller that sizes its buffer by `nwords()`, as it should,
   gets a one-word overrun. In the generated `write_array_impl(word_bw_tag<64>, ...)`,
   look for `x[3] = 0; x[3].range(15, 0) = self->nsamp;`.
3. **Stale bits in the last stream word.** In
   `write_axi4_stream_impl(word_bw_tag<64>, ...)` and
   `write_stream_impl(word_bw_tag<64>, ...)`, the array loop leaves `w`
   holding the last coefficient pair. Then `w.range(15, 0) = self->nsamp;`
   writes into it *without clearing it first*. The upper 32 bits of the last
   word are therefore the last coefficient, which is the `0xbf000000...28`
   above. Readers ignore those bits, so nothing breaks, but the wire content
   depends on unrelated data.

Where to look: the DataList code generator in `waveflow/hw/dataschema.py`,
where array fields inside a list are emitted, and `DataSchemaStep`, which
drives it. `nwords_value(word_bw_tag<64>)` is computed densely, while the
stream and array emitters start an array field on a word boundary.

## Which layout is right

Pick one, and make every path follow it: Python `serialize` /
`deserialize` / `write_uint32_file`, C++ `nwords`, `pack_to_uint`,
`write_array` / `read_array`, `write_stream` / `read_stream`, and
`write_axi4_stream` / `read_axi4_stream`.

- **Dense** (what Python and `nwords` do now): fewer words, and consistent
  with `pack_to_uint`'s flat bit layout. An array element can straddle a
  word boundary, which makes the C++ read and write loops harder to
  pipeline.
- **Array-aligned** (what the C++ stream and array paths do now): an array
  starts on a fresh word, so its elements pack cleanly `pf` per word and the
  loops pipeline at II=1. It costs up to one extra word per array, and
  Python `serialize` and `nwords` must learn the rule.

Whichever you choose, **document it** where the schema layout is described
in the docs, because a host written in another language has to reproduce
it.

Also fix consequence 3 whichever layout you choose: clear `w` before
packing the fields that follow an array.

## Regression tests to add

1. **Python ↔ C++ agreement, per word_bw.** For a `DataList` with a scalar
   before an array, the array, and a scalar after it (the schema above),
   at `word_bw` 32 and 64:
   - `len(serialize(word_bw))` equals the generated `nwords<word_bw>()`;
   - the generated `write_axi4_stream<word_bw>` emits exactly `nwords` beats
     with TLAST only on the last;
   - Python `deserialize` of the C++-written words round-trips every field.

   The repo already compiles generated headers in some tests. If this needs
   a C++ compile, follow that pattern. Otherwise, assert on the generated
   source text for the beat count and the `x[...]` indices.
2. **No overrun.** The generated `write_array_impl(word_bw_tag<W>, ...)`
   never indexes `x[i]` with `i >= nwords<W>()`.
3. **No stale bits.** Write the header with a non-zero last array element
   and check that the last stream word's unused bits are zero.
4. Also cover a schema where the array is the **last** field and one where
   it is the **first**. Those shapes can hide the bug, because they
   straddle or don't straddle differently.

## Secondary: `read_array` corrupts 64-bit words passed as a Python list

`waveflow.hw.arrayutils.read_array(packed, ...)` given a **list** of Python
ints for `word_bw=64` loses the low bits of some words. If the list mixes
values above and below 2**63, `np.array(list)` picks `float64`, which has
only 53 bits of mantissa:

```python
import numpy as np
from waveflow.hw.arrayutils import read_array, write_array
from waveflow.hw.dataschema import FloatField
F = FloatField.specialize(bitwidth=32)
x = np.linspace(-2, 2, 40, dtype=np.float32)
w = write_array(x, elem_type=F, word_bw=64)
lst = [int(v) for v in w]
np.array(lst).dtype                        # float64
got = read_array(lst, elem_type=F, word_bw=64, shape=40).val
np.flatnonzero(np.asarray(got, np.float32) != x)   # [2, 4, 6, ...] -- every low-half element
read_array(np.asarray(w, np.uint64), ...)           # correct
```

Fix: in `read_array` (and anything else that accepts `packed` words),
convert with an explicit unsigned dtype for the word width, such as
`np.asarray(packed, dtype=np.uint64)` for `word_bw <= 64`. Never let NumPy
infer the dtype from a list. A list is the natural thing to build from VCD
samples or a socket, so it will be passed. Add the snippet above as a test.

## A related papercut, not a bug

`VcdParser.add_axiss_signals` adds TDATA through `add_signal`, whose
default `numeric_type` is `'int'` (signed). So a 32- or 64-bit TDATA word
with its top bit set comes back negative, and every caller has to mask it
before unpacking. TDATA is a bag of bits, so `add_axiss_signals` should
probably add it as `'uint'`. Check the callers of `extract_axis_bursts`
before changing it.

## Downstream

`hwdesign/demos/stream/poly/poly_timing.py` works around the secondary
issue and the papercut: it masks each word and passes a `uint64` array.
Its check of the decoded command header fails at `WORD_BW=64` until the
main bug is fixed. That demo's kernel and testbench need no change either
way, since they use the generated C++ throughout.
