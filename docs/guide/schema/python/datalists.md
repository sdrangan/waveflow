---
title: Data Lists
parent: Python
grand_parent: Data Schemas
nav_order: 2
audience: python
api: [DataList]
summary: "DataList — an ordered, named composite of schema fields (the C++ struct counterpart), with nesting and per-field access."
---

# Data Lists — structured records

A **`DataList`** groups several named [fields](./fields.md) into one structured record — the
hardware equivalent of a C `struct` or a Python dataclass. You reach for it whenever related
values travel together: a command header, a packet, a configuration block. Each entry has a
name, a **schema** (its type), and a description — and an entry can itself be another schema,
an [array](./dataarrays.md) or a nested `DataList`.

## Example

From the [polynomial example](../../../examples/stream_inband/), a command header carrying a
transaction id, a coefficient array, and a sample count:

```python
class CoeffArray(DataArray):
    element_type = Float32
    static = True
    ncoeff = 4
    max_shape = (ncoeff,)
    cpp_storage = "raw"

class PolyCmdHdr(DataList):
    elements = {
        "tx_id":  {"schema": IntField.specialize(bitwidth=16, signed=False),
                   "description": "Transaction ID"},
        "coeffs": {"schema": CoeffArray,
                   "description": "Polynomial coefficients"},
        "nsamp":  {"schema": IntField.specialize(bitwidth=16, signed=False),
                   "description": "Number of samples"},
    }
```

A `DataList` entry can be a simple field (`tx_id`, `nsamp` are `IntField`s) or a whole nested
schema (`coeffs` is a `DataArray`). Every bit width is explicit and shared between the Python
model and the generated C++ — there is no separate, hand-maintained struct to drift out of
sync.

## Creating and accessing instances

Each named entry becomes an attribute on the instance — read and write it by name:

```python
cmd = PolyCmdHdr()
cmd.tx_id  = 42
cmd.coeffs = np.array([1.0, -2.0, -3.0, 4.0], dtype=np.float32)
cmd.nsamp  = 100

print(cmd.tx_id)    # 42
print(cmd.nsamp)    # 100
```

A `DataList` instance serializes directly to the packed bit representation used by simulation
interfaces, test vectors, and generated testbenches — see [Code Generation](../hls/codegen.md).

---

Related: the typed-array building block is [Data Arrays](./dataarrays.md); for fields that
share storage, see [Data Unions](./dataunion.md).
