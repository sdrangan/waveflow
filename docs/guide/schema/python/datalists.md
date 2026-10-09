---
title: Data Lists
parent: Python
grand_parent: Data Schemas
nav_order: 2
audience: python
api: [DataList]
summary: "DataList — an ordered, named composite of schema fields (the C++ struct counterpart), with nesting and per-field access; what a field read returns (a NumPy scalar, an enum, an ndarray) and where to convert it with int()."
---

# Data Lists — structured records

A **`DataList`** groups several named [fields](./fields.md) into one structured record — the
hardware equivalent of a C `struct` or a Python dataclass. You reach for it whenever related
values travel together: a command header, a packet, a configuration block. Each entry has a
name, a **schema** (its type), and a description — and an entry can itself be another schema,
an [array](./dataarrays.md) or a nested `DataList`.

## Example

From the [polynomial example](../../../examples/stream_inband/), a command header carrying a
command type, a transaction id, a sample count, and the coefficients the command is evaluated
with:

```python
class CoeffArray(DataArray):
    element_type = Float32
    static = True
    ncoeff = 4
    max_shape = (ncoeff,)
    cpp_storage = "raw"

class PolyCmdHdr(DataList):
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField,     # an EnumField: DATA or END
                     "description": "DATA or END"},
        "tx_id":    {"schema": IntField.specialize(bitwidth=16, signed=False),
                     "description": "Command ID: echoed, or reported on error"},
        "nsamp":    {"schema": IntField.specialize(bitwidth=16, signed=False),
                     "description": "Sample count (0 for END)"},
        "coeffs":   {"schema": CoeffArray,
                     "description": "c0..c3, constant term first"},
    }
```

A `DataList` entry can be a simple field (`tx_id`, `nsamp` are `IntField`s, `cmd_type` an
`EnumField`) or a whole nested schema (`coeffs` is a `DataArray`). Every bit width is explicit and shared between the Python
model and the generated C++ — there is no separate, hand-maintained struct to drift out of
sync.

## Creating and accessing instances

Each named entry becomes an attribute on the instance — read and write it by name:

```python
cmd = PolyCmdHdr()
cmd.cmd_type = PolyCmdType.DATA
cmd.tx_id  = 42
cmd.coeffs = np.array([1.0, -2.0, -3.0, 4.0], dtype=np.float32)
cmd.nsamp  = 100

print(cmd.tx_id)    # 42
print(cmd.nsamp)    # 100
```

A `DataList` instance serializes directly to the packed bit representation used by simulation
interfaces, test vectors, and generated testbenches — see [Code Generation](../hls/codegen.md).

## What a field read returns

Reading a field returns a value that already has the right type. Use it as it is:

| Field | A read returns |
|---|---|
| `IntField`, up to 32 bits | `numpy.uint32` / `numpy.int32` (a 16-bit field reads as `uint32`) |
| `IntField`, 33 to 64 bits | `numpy.uint64` / `numpy.int64` |
| `FloatField(32)` | `numpy.float32` |
| `EnumField` | the enum member itself |
| an array entry (`DataArray`) | a plain NumPy `ndarray` of the container dtype |

```python
cmd = Cmd().deserialize(words, word_bw=32)     # op, tx_id (U16), n (U32), addr (U64), taps (4 x S16)
cmd.op                  # <Op.WRITE: 1>
cmd.tx_id               # np.uint32(7)
cmd.taps                # array([ 1, -2,  3, -4], dtype=int32)

results[cmd.tx_id]                              # an index or a dict key
for k in range(cmd.n): ...                      # a count
x = read_array(words, S16, word_bw=32, shape=cmd.n)   # a shape
resp = Resp(tx_id=cmd.tx_id, n=cmd.n)           # back into a schema
print(f"tx_id={cmd.tx_id}")                     # tx_id=7
if cmd.op == Op.WRITE: ...                      # compare
```

So **don't wrap a field read in `int(...)`** when the value is hardware data or an identifier,
and don't wrap an array entry in `np.asarray(...)`. These wrappers do nothing, and they hide the
type the reader should see.

### Where to convert

Convert explicitly at the one point where the value **leaves hardware semantics for plain
Python**. There, the container type is wrong:

- **Arithmetic that can go negative or overflow.** An unsigned value wraps with only a warning:
  `cmd.n - 5` with `n = 3` is `4294967294`. Write `int(cmd.n) - 5`. The same goes for `-n`
  (`-(-int(n) // pf)` rounds up; `-(-n // pf)` on a `uint32` does not).
- **A `uint64` mixed with a signed 64-bit value.** NumPy promotes the pair to **float64** and
  loses the low bits: `cmd.addr + np.int64(1)` is `np.float64(4097.0)`.
- **JSON and text output.** `json.dumps` rejects `numpy.uint32`. A list or tuple of NumPy
  scalars prints as `[np.uint32(7)]`, which matters for a golden file or a printed table.
  A bare f-string prints the number.
- **APIs that want a Python `int`**, such as `.bit_length()`, or `str()` into generated source,
  where a `bool` field would print `True`.

Keep that conversion visible with a short comment saying which of these it is.
On the words going into a decode, see [Data arrays](./dataarrays.md#3-reading-the-values-val).

---

Related: the typed-array building block is [Data Arrays](./dataarrays.md); for fields that
share storage, see [Data Unions](./dataunion.md).
