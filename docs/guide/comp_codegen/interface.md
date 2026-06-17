---
title: Endpoint interfaces
parent: Component Code Generation
nav_order: 2
audience: hls
applies_to: [HwComponent]
api: [kernel_signature, StreamIF, MMIFMaster, VitisRegMapMMIFSlave]
summary: "How each declared endpoint on an HwComponent is realized as a Vitis HLS port: stream endpoints become hls::stream<axi4s_word<bw>>& with #pragma HLS INTERFACE axis; m_axi masters become ap_uint<bw>* with m_axi offset=slave bundle=gmem; a VitisRegMapMMIFSlave becomes s_axilite register-field ports plus the ap_start/ap_done control protocol. Also how a slave endpoint's handler binds to the kernel body."
---

# Endpoint interfaces

## Concept

A generated kernel's argument list and its `#pragma HLS INTERFACE` block are derived directly from
the component's declared endpoints by
[`kernel_signature(comp)`](../../../waveflow/build/hwgen.py). Each endpoint type maps to a specific
Vitis port realization, emitted in a canonical order — **streams, then regmap fields, then m_axi
masters**.

| Endpoint (Python) | Kernel argument | Interface pragma |
|---|---|---|
| `StreamIFMaster` / `StreamIFSlave` | `hls::stream<streamutils::axi4s_word<bw>>& <name>` | `#pragma HLS INTERFACE axis port=<name>` |
| `VitisRegMapMMIFSlave` (per field) | `<cpp_type>& <field>` (or `<elem>[<count>]` for a raw array field) | `#pragma HLS INTERFACE s_axilite port=<field> bundle=control` |
| m_axi master (e.g. `MMIFMaster` / `DirectMMIF` master) | `ap_uint<bw>* <name>` | `#pragma HLS INTERFACE m_axi port=<name> offset=slave bundle=gmem depth=<name>_depth` |

## Stream endpoints → `axis`

Every stream endpoint becomes an `hls::stream` reference of AXI4-Stream words, with an `axis`
interface pragma. The word bitwidth is the endpoint's concrete `bitwidth` (or the variant's
`HwParamValue`). From [`examples/stream_inband/gen/poly.hpp`](../../../examples/stream_inband/gen/poly.hpp):

```cpp
void poly(
    hls::stream<streamutils::axi4s_word<32>>& s_in,
    hls::stream<streamutils::axi4s_word<32>>& m_out,
    ...
```

Master and slave streams both realize as `hls::stream<...>&` — the direction is a property of how the
body reads or writes them, not of the port type.

## Regmap slave → `s_axilite` + `ap_ctrl`

A [`VitisRegMapMMIFSlave`](../../../waveflow/hw/regmap.py) does **not** become one port — it expands
to **one `s_axilite` port per user-declared register field**, each on `bundle=control`. The
generated `simp_fun` kernel turns the `x` / `a` / `b` / `y` register fields into four scalar
references:

```cpp
void simp_fun(
    ap_int<32>& x,
    ap_int<32>& a,
    ap_int<32>& b,
    ap_int<32>& y
);
```

The auto-prepended `ap_start` / `ap_done` registers are not emitted as data ports; they are the
**control protocol**. When a regmap drives the component, the kernel's `return` port also binds
`s_axilite ... bundle=control`, giving the host the `ap_start`/`ap_done` handshake that launches the
[regmap-launched](./structure.md) `on_start` body.

## m_axi master → `m_axi` pointer

A memory-mapped master endpoint becomes an `ap_uint<bw>*` pointer with an `m_axi` pragma
(`offset=slave bundle=gmem`), the burst region bounded by a generated `<name>_depth` header
constant. From [`examples/shared_mem/gen/hist.hpp`](../../../examples/shared_mem/gen/hist.hpp):

```cpp
void hist(
    hls::stream<streamutils::axi4s_word<32>>& s_in,
    hls::stream<streamutils::axi4s_word<32>>& m_out,
    ap_uint<32>* m_mem
);
```

(Note the canonical order: the two streams precede the `m_mem` master.) Reading and writing through
this pointer inside the body — the lane/slice transactions — is [Custom Hooks](../custom_hooks/)
material.

## The control protocol on `return`

Which protocol binds the `return` port follows from the endpoint mix (in `kernel_signature`):

- a regmap slave is present ⇒ `s_axilite ... bundle=control` (host-launched via `ap_start`/`ap_done`);
- else m_axi masters are present ⇒ `ap_ctrl_hs`;
- else (stream-only) ⇒ `s_axilite ... bundle=control`.

## How a slave endpoint's handler binds

A slave endpoint carries the Python handler that becomes (part of) the synthesized body:

- A **`VitisRegMapMMIFSlave`** is constructed with `on_start=self.on_start` (see
  [`examples/regmap/simp_fun.py`](../../../examples/regmap/simp_fun.py)). That handler is exactly the
  method [`extract_kernel`](../../../waveflow/build/hwcodegen.py) lowers as the kernel body — the
  regmap slave both adds the `s_axilite` ports *and* designates the `ap_start`-triggered entry point.
- For **stream / m_axi** endpoints there is no separate handler method: the free-running `run_proc`
  body reads and writes them directly, and those per-port read/write calls are lowered to
  `hls::stream` / `m_axi` transactions. The transaction methods themselves (`read_stream_lane`,
  `read_array_lane`, …) are documented in [Custom Hooks: Kernel patterns](../custom_hooks/patterns.md).

## API

- [`kernel_signature(comp, variant_suffix="")`](../../../waveflow/build/hwgen.py) — builds the concrete top-function signature + `#pragma HLS INTERFACE` lines from the endpoints.
- [`StreamIFMaster` / `StreamIFSlave`](../../../waveflow/hw/interface.py) — stream endpoints → `axis` ports.
- [`VitisRegMapMMIFSlave`](../../../waveflow/hw/regmap.py) — regmap slave → `s_axilite` field ports + control protocol; carries the `on_start` handler.
- [`MMIFMaster` / `DirectMMIF`](../../../waveflow/hw/aximm.py) — memory-mapped master → `m_axi` pointer.

## Quick reference

- Argument order is canonical: streams → regmap fields → m_axi masters.
- Stream endpoint ⇒ `hls::stream<axi4s_word<bw>>&` + `axis`.
- Regmap slave ⇒ one `s_axilite` port *per field* + `ap_start`/`ap_done` control; its `on_start` is the kernel body.
- m_axi master ⇒ `ap_uint<bw>*` + `m_axi offset=slave bundle=gmem depth=<name>_depth`.
- `return` protocol: `s_axilite` when a regmap (or stream-only) drives control, `ap_ctrl_hs` for m_axi-only kernels.
