---
name: waveflow-schema
description: Define Waveflow data types -- a DataSchema (DataList, DataArray, IntField, EnumField, FixedField, FloatField) for a command, header, response, status, packet or sample burst -- and move it in Python and in Vitis HLS C++ (serialize / deserialize, read and write it on a stream or memory, generate its C++ header). Use whenever data crosses a stream, a memory or a register boundary in a Waveflow design, or when the user asks for message formats, packing, word layouts or test vectors.
---

# Waveflow data schemas

In Waveflow a message's layout is written **once**, as a Python `DataSchema` class. Its C++ struct, its
serializers on every channel, and its Python serialize / deserialize are all **generated** from that one
class, so the Python model and the kernel cannot disagree about a single bit. Everything below follows
from protecting that property.

## The rules (all of them, every time)

1. **Every message is a schema.** A command, header, response, status or tagged message is a `DataList`
   of typed fields; an array of samples is a `DataArray` (or the array utils of its element type).
   Never describe a message as "word 0 is ..., word 1 is ..." in code.
2. **Size every field.** `IntField.specialize(bitwidth=W, signed=...)`, `EnumField.specialize(enum_type=E,
   bitwidth=W)` when a protocol fixes the width, `FixedField.specialize(W, I, q_mode=..., o_mode=...)`
   for `ap_fixed<W, I>`. A spec's `uint16` is a 16-bit unsigned `IntField`, not a 32-bit one.
3. **Named codes are `EnumField`s** over a Python `IntEnum`. Never a bare integer constant for an
   opcode, a message type or a status code. The C++ side gets an `enum class` of the same name.
4. **Never pack or unpack by hand -- in Python or in C++.** No shifts, masks, `.range(hi, lo)`,
   `int.from_bytes`, `np.frombuffer` + bit twiddling, or "the field is in bits 16..31". Use:

   | where | read | write |
   |---|---|---|
   | Python, words | `Schema().deserialize(words, word_bw=W)` | `inst.serialize(word_bw=W)` |
   | Python, element array | `read_array(words, elem_type=T, word_bw=W, shape=n).val` | `write_array(x, elem_type=T, word_bw=W)` |
   | pysim stream port | `yield from s_in.get_schema(Schema)`; `s_in.get_array(T, count=n)` | `yield from m_out.write(inst)` |
   | HLS, one message | `m.read_axi4_stream<W>(s)`, `read_stream<W>`, `read_array<W>(words)`, or `streamutils::read_msg<W>(m, s)` | `m.write_axi4_stream<W>(s, last)`, ..., `streamutils::write_msg<W>(m, s, last)` |
   | HLS, element array | `<elem>_array_utils::read_axi4_stream_lane<W>`, `read_array_slice<W>`, `elem_read<W>` | the `write_*` twins |
   | test-data files | `inst.write_uint32_file(path)` / `arrayutils.write_uint32_file(...)` | C++: `streamutils::read_uint32_file(v, path)` |

   **Writing the schema is not enough -- check the read side.** The common failure is to declare a
   `DataList` and then decode its words by hand anyway.
5. **Never hand-edit a generated file** (`include/*.h`, `*_array_utils.h`, `streamutils*.h`). Change the
   Python class and regenerate. If the generated code cannot express what you need, **stop and say so**;
   do not work around it.
6. **Generate the headers with build steps**, never by writing the struct yourself:
   `StreamUtilsStep(output_dir="include")`, one `DataSchemaStep(cls, word_bw_supported=[...],
   include_dir="include")` per schema (enums and nested arrays included, inner before outer), and
   `ArrayUtilsStep(elem_type, [...])` for each element type a kernel moves as an array. **The array
   utils go to the element type's own `include_dir`, not the step's**: declare the element with it,
   `FixedField.specialize(12, 1, include_dir="include")` / `FloatField.specialize(bitwidth=32,
   include_dir="include")`, or its header lands in the project root. Add
   `framed=True` to `DataSchemaStep` when the body uses `read_msg` / `write_msg` on an internal framed
   stream.
7. **Use field values as they come.** A field read is already a typed NumPy scalar, an enum member or
   an `ndarray`. Do not wrap it in `int(...)` / `np.asarray(...)` "to be safe". Convert only where the
   value leaves hardware semantics -- arithmetic that can go negative, JSON output, an API that needs a
   Python `int` -- with a short comment saying which.
8. **Word counts come from the schema**: `Schema.nwords_per_inst(W)` in Python,
   `Schema::nwords<W>()` in C++. Never compute `ceil(bits / W)` yourself: the layout rule pads (an array
   starts and ends on a word boundary; a field never straddles words), so the count can be larger.

## Which page to read before which step

Read the page **before** writing the code, with `waveflow_get_doc` (MCP) or the `docs/guide/` checkout.

| step | page |
|---|---|
| choosing field types and widths | `schema/python/fields.md`, `schema/python/fixpoint.md`, `schema/python/complex.md` |
| a record (`DataList`), what a field read returns | `schema/python/datalists.md` |
| arrays, `VarDataArray`, `.val` | `schema/python/dataarrays.md` |
| several message kinds on one stream (a tag + per-type schemas) | `schema/python/tagged_union.md` (a fixed-size alternative: `schema/python/dataunion.md`) |
| the word layout rule, per-interface C++ methods | `schema/hls/serialization.md` |
| generating the headers | `schema/hls/codegen.md` |
| arrays in HLS: lanes, `pf`, the lane loop | `vectorization/hls/arrayutils.md`, then `vectorization/hls/raw.md` |
| test vectors between Python and the C testbench | `schema/hls/tbutils.md` |
| the command / response / status shape a schema usually serves | `systems/command_response.md` |

For a worked example, `waveflow_get_example stream_inband` (`poly.py` for the schemas,
`poly_body_impl.tpp` for the C++ that reads and writes them). If a name you need is not on those pages,
`waveflow_search` it -- **do not guess an API name.**

## Patterns

**A command and its response**

```python
from enum import IntEnum
from waveflow.hw.dataschema import DataList, EnumField, IntField

U16 = IntField.specialize(bitwidth=16, signed=False)

class Op(IntEnum):
    DATA = 0
    END = 1

OpField = EnumField.specialize(enum_type=Op)

class Cmd(DataList):
    elements = {
        "op":    {"schema": OpField, "description": "DATA or END"},
        "tx_id": {"schema": U16,     "description": "echoed in the response"},
        "n":     {"schema": U16,     "description": "samples that follow"},
    }
```

```cpp
#include "include/cmd.h"
Cmd cmd;
cmd.read_axi4_stream<W>(s_in);          // the whole header, by its schema
if (cmd.op == Op::END) return;
```

**Several kinds of message on one stream** -- one `DataList` per kind, each **starting with the same
`EnumField` tag**; the reader decodes a one-field `Tag` schema first, then the rest by that kind's
schema (`schema/python/tagged_union.md`). Not a hand-written `if (word & 0xF) == ...`.

**Samples after a header** -- the header is a schema; the samples are an array of an element schema,
moved with the generated lane routines (`vectorization/hls/arrayutils.md`). Not `.range(32*k+31, 32*k)`.

## Before you finish, check your own code

- [ ] Every message on every port is a schema class; every code is an `EnumField`.
- [ ] Every width matches the spec.
- [ ] No `.range(`, `>>`, `<<`, `&` masks, or `from_bytes` on message or sample words, in Python or C++.
- [ ] Every read of a message uses the schema's reader -- not only every write.
- [ ] No hand-written copy of a generated struct; the headers come from `DataSchemaStep` /
      `ArrayUtilsStep`.
- [ ] No defensive `int()` / `np.asarray()` around field reads.
