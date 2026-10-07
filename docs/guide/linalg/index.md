---
title: Linear Algebra
parent: Guide
nav_order: 13.5
has_children: true
summary: "Reusable hardware for complex fixed-point linear algebra in waveflow.linalg. Each component is a bit-exact Python model, a pysim module, a Vitis HLS task body, a standalone unit that speaks framed messages, and a cost model calibrated on a packaged platform. This page covers what the components share: operand formats and the integer format id that carries them to C++, lane groups and message words, the message header and its statuses, the build step, and where the cost data live. The components are the systolic matrix multiply and the CG vector unit."
---

# Linear Algebra

`waveflow.linalg` holds hardware components for linear algebra on **complex fixed-point** data.
Each component comes in five forms, and the first four agree bit for bit:

| Form | What it is | Use it for |
|---|---|---|
| bit-exact model | a NumPy function on stored integers | golden values, accuracy studies |
| pysim module | a `FreeRunMod` whose `run_iter` calls the model | simulating a system in Python |
| HLS task body | a header in `waveflow/build/` | synthesis |
| standalone unit | the core behind a receiver, a loader and a store, speaking framed messages | a block on its own, fed from memory or by another unit |
| cost model | resources, and cycles per message, fitted on a packaged platform | pricing a configuration without synthesizing it |

| Component | Computes | Page |
|---|---|---|
| Systolic matrix multiply | `C = q(A·B)` and `C = q(Aᴴ·B)` | [Systolic Matrix Multiply](./systolic.md) |
| CG vector unit | the vector steps of conjugate gradient on `A X = B`, multi-RHS | [CG Vector Unit](./cg_vector.md) |

## Two layers: core and unit

A component has a **core** and a **standalone unit**.

The **core** takes one fixed-width command per job on a stream, and its operands and results as
[stream-of-blocks](../interface/primitive/sob.md) buffers. It composes inside a larger design
whose other tasks lay out the blocks.

The **unit** wraps the core in three tasks: a receiver that validates each request, a loader that
lands the operands in the core's blocks, and a store that writes the reply. It speaks
[framed messages](#messages) on two streams, `s_in` and `s_out`. It never touches memory itself:
fed from memory, it sits between the framework's in-band
[`MemRStream` and `MemWStream`](../memory/memstream.md).

## Formats

Every operand's fixed-point format is a plain field of the component: a
`waveflow.utils.fixputils.Format` (width, integer bits, signedness, rounding, saturation). It is
never an index into a registry.

**How a format reaches C++.** The framework names task instances and calibration keys after
*integer* template arguments, so a task body cannot take a type as one. Each instance passes one
integer instead, its **format id**: a 31-bit CRC of the canonical text of everything its types
contain (`waveflow.linalg.formats.format_id`). The build step renders one specialization of a
traits template per id:

```cpp
template <int ID> struct wf_systolic_traits;
template <> struct wf_systolic_traits<998446828> {
    typedef ap_fixed<12, 3, AP_RND, AP_SAT> a_t;
    // ... the other operands and the exact types the datapath needs
};
```

Two instances with different formats get two ids, so they share one design. Equal formats give
one id and one specialization. If two different format sets ever produce the same id, the build
step raises an error; it never picks one.

**The memory format.** In a message, an operand travels as a complex memory element whose two
parts are `lane_bits` wide (default 16), with the register's integer bits
(`waveflow.linalg.formats.mem_format`). Widening a register to it only adds fraction bits, so the
conversion is exact in both directions. A register may not be wider than `lane_bits`. Rounding and
saturation modes do not travel with the data.

## Lane groups and message words

Two layouts. Each has a Python function and a C++ twin, and the twins are checked against each
other in C-simulation.

* **Lane groups** (`waveflow.linalg.lanes`, `wf_lanes.h`). Inside a component, a matrix is held
  row-major in groups of `L` complex values, so `L` columns are touched per cycle. Lane `l` sits at
  bits `[2W·l, 2W·l + 2W)`, with the real part in the low `W` bits and the imaginary part in the
  high `W` bits: the `ComplexField` order. Converting between a group and its values reinterprets
  bits; there is no arithmetic.
* **Message words** (`to_words` and `from_words`, `wf_matrix_io.h`). Between components, a matrix
  travels row-major as memory elements, as many to a word as fit: `word_bits / (2·lane_bits)`.
  That is two to a 64-bit word at the default 16-bit lanes. Message words are 32 or 64 bits.

The values these functions handle are the **stored integers** of the register format: a 12-bit
register holding 0.5 with 3 integer bits stores `0.5 · 2⁹ = 256`.

## Messages

A message is a header burst followed by a payload burst, on a framed stream (each burst ends with
`last`). Requests and replies share one header, `waveflow.linalg.message.LinalgHeader`:

| Field | Bits | Meaning |
|---|---|---|
| `tag` | 32 | opaque to the unit; copied into the reply |
| `length` | 32 | payload words that follow the header in this message |
| `op` | 8 | the operation, defined by each unit |
| `status` | 8 | `0` in a request; a status in a reply |
| `nfollow` | 16 | how many more messages belong to the same job after this one |
| `m`, `k`, `n` | 16 each | the problem dimensions (`0` where an operation does not use one) |

The header takes 3 words on a 64-bit stream and 5 on a 32-bit stream.

A unit answers **every** request with a reply. The reply carries the request's tag, operation and
dimensions, a status and, if the request was served, the result:

| Status | Value | Meaning |
|---|---|---|
| `OK` | 0 | served; the result follows |
| `BAD_OP` | 1 | an operation the unit does not have |
| `BAD_DIMS` | 2 | dimensions outside what the unit was built for, or not multiples of its tiles |
| `BAD_LENGTH` | 3 | `length` does not match the dimensions |
| `BAD_SEQUENCE` | 4 | a valid operation at a point of the job where it is not allowed |

A request the unit cannot serve gets a reply with its status and no payload, and the unit reads and
discards its payload, counting by its `length`. **Nothing is clamped**: a request is either served
exactly as stated or refused.

## Building a design

Each component instance says what its design needs generated in a `LinalgParts`: its traits, the
task bodies to copy from `waveflow/build/`, and its command schemas. The functions in
`waveflow.linalg.build` take it from there:

| Function | Does |
|---|---|
| `collect_parts(top)` | gathers the parts of `top` and every module below it, without repeats |
| `linalg_headers_dag(traits, bodies, include_dir, word_bits, schemas)` | a `BuildDag` that writes `streamutils_hls.h`, the header and command schemas, the helper headers, the bodies, and `wf_linalg_traits.h` with the array utilities of every memory element |
| `gen_linalg_headers(root_dir, ...)` | runs that DAG under `root_dir` and returns the include directory |

The top of a design is then generated like that of any free-running composite
([Hardware modules and Flows](../flows/)). The
[systolic page](./systolic.md#building-and-testing-it) lists what a built unit's include directory
holds.

## Cost data

The calibrated cost models live in a packaged [platform](../platform/),
`waveflow/calib/platforms/xczu48dr_250mhz_vitis2024_1/`, whose name carries the part, the clock
and the tool version that the numbers belong to:

| Path | Holds |
|---|---|
| `platform.json` | the part (`xczu48dr-ffvg1517-2-e`), the clock (250 MHz) and the tool (Vitis HLS / Vivado xsim 2024.1) |
| `models/<task>/params.json` | each task's fitted resource model, in the framework's layout, so a composed estimate (`add_rm`, then `compose()`) prices a design that contains the task |
| `models/systolic_unit_channels/params.json`, `models/cg_vector_unit_channels/params.json` | each unit's channels: its stream-of-blocks buffers, FIFOs and memory adapters |
| `components/systolic_unit/params.json`, `components/cg_vector_unit/params.json` | each unit's cycle model, per message |
| `provenance.json` | per component, the tool, the step and model version, and the builds the models were fitted on |

A model measured with one tool version describes that version only. The components' `get_rm`
refuses any other platform, even one with the same part and clock, and `waveflow.linalg.cost` and
`cg_cost` read only this one. Pricing another part, clock or tool version takes a new calibration and a
platform of its own; the component pages ([systolic](./systolic.md#calibration-and-accuracy),
[CG](./cg_vector.md#calibration-and-accuracy)) describe how this one was calibrated.
