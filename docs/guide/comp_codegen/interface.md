---
title: Endpoint interfaces
parent: Module Code Generation
nav_order: 8
audience: hls
applies_to: [HwModule]
api: [kernel_signature, StreamIF, MMIFMaster, VitisRegMapMMIFSlave]
summary: "How each declared endpoint on an HwModule is realized as a Vitis HLS port: stream endpoints become hls::stream<axi4s_word<bw>>& with #pragma HLS INTERFACE axis; m_axi masters become ap_uint<bw>* with m_axi offset=slave bundle=gmem; a VitisRegMapMMIFSlave becomes s_axilite register-field ports plus the ap_start/ap_done control protocol. Also how a slave endpoint's handler binds to the kernel body."
---

# Endpoint interfaces

> **Most of the time you do not need this page.** Declare an endpoint in Python and the mapping to
> Vitis ports is handled for you — codegen derives the kernel's whole argument list and every
> `#pragma HLS INTERFACE` from your endpoints, and you never write one by hand.
>
> You need it when you look *inside* the generated kernel. Chiefly: **writing a
> [custom hook](../custom_hooks/)**, because the C++ arguments your hook receives *are* these ports —
> you cannot write the body without knowing that a stream endpoint arrives as an
> `hls::stream<axi4s_word<bw>>&` and an `m_axi` master as a plain pointer. Also when reading a
> synthesized block's port list to integrate or debug it.

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
| m_axi master (e.g. `MMIFMaster` / `DirectMMIF` master) | `ap_uint<bw>* <name>` | `#pragma HLS INTERFACE m_axi port=<name> offset=slave bundle=gmem depth=<name>_depth` — **plus** `s_axilite port=<name> bundle=control` when the component has a regmap (see below) |

## Stream endpoints → `axis`

Every stream endpoint becomes an `hls::stream` reference of AXI4-Stream words, with an `axis`
interface pragma. The word bitwidth is the endpoint's concrete `bitwidth` (or the variant's
`HwParamValue`). The generated `poly` kernel:

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

The `ap_start` / `ap_done` control bits are not emitted as data ports; they are the
**control protocol**. When a regmap drives the component, the kernel's `return` port also binds
`s_axilite ... bundle=control`, giving the host the `ap_start`/`ap_done` handshake that launches the
[regmap-launched](./hostactivated.md) `on_start` body.

## m_axi master → `m_axi` pointer

A memory-mapped master endpoint becomes an `ap_uint<bw>*` pointer with an `m_axi` pragma
(`offset=slave bundle=gmem`), the burst region bounded by a generated `<name>_depth` header constant.
The generated `hist` kernel:

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

### `offset=slave` needs a home for the pointer

`offset=slave` means the pointer's **base address is not a port** — it arrives in an AXI-Lite register
that the host writes before launching. But `bundle=gmem` names the *`m_axi`* bundle; it says nothing
about *where that offset register lives*. So when the component has a regmap, codegen also emits

```cpp
#pragma HLS INTERFACE s_axilite port=m_mem  bundle=control
```

binding the offset register into the **same** `control` slave as `ap_start`/`ap_done`. Without it Vitis
silently invents a *second* AXI-Lite bundle for the offset alone, and the kernel exposes two address
spaces (`s_axi_control` **and** an auto-named `s_axi_control_r`) — one block, two slaves, for no reason.

> **A component with an m_axi but no regmap still has this problem.** There is no `control` bundle for
> the offset to join, so Vitis auto-creates one *and* `ap_start` stays on raw pins — meaning the block
> needs two different masters: one to write the base address over AXI-Lite, another to pulse a wire.
> `block_scale` is in this state today. `hist` was, until it gained a regmap and became
> [`HostActivated`](../flows/modules.md). This is a good reason to give a memory-mapped
> kernel a regmap even when it has no scalar arguments to put in one.

## The control protocol on `return`

`ap_ctrl_hs` is the **protocol** — `ap_start` / `ap_done` / `ap_idle` / `ap_ready`. Which pragma binds
the `return` port decides only *how those signals are reached*, and follows from the endpoint mix (in
`kernel_signature`):

| Endpoint mix | `return` binds | What the RTL exposes |
|---|---|---|
| a regmap slave is present | `s_axilite ... bundle=control` | `ap_start` is **not a port** — Vitis generates a `<kernel>_control_s_axi` adapter that drives it from register `0x00` |
| else m_axi masters present | `ap_ctrl_hs` | `ap_start`/`ap_done`/`ap_idle`/`ap_ready` are **top-level pins**, driven by whatever instantiates the block |
| else (stream-only) | `s_axilite ... bundle=control` | as the first row |

The first row is worth dwelling on, because it explains a name: **`s_axilite port=return` does not give
you a different kernel — it gives you the same pin-driven core plus a generated AXI-Lite→pin adapter.**
That adapter is a real, separate RTL module, and `on_start` is *its* callback — which is why `on_start`
exists exactly when a regmap does.

Neither row is "better": a host-launched accelerator (XRT) *needs* the AXI-Lite control registers,
while a block launched by another block inside an IPI system wants the raw pins. See
[Hardware modules and Flows](../flows/).

> `ap_ctrl_none` — free-running, no handshake at all — is a third protocol that nothing generates yet
> (the `free_running_kernel` [target](./index.md)). Note a component whose entry is `run_proc` rather
> than `on_start` is **not** free-running; it is `ap_ctrl_hs` on raw pins, per the middle row.

## How a slave endpoint's handler binds

A slave endpoint carries the Python handler that becomes (part of) the synthesized body:

- A **`VitisRegMapMMIFSlave`** is constructed with `on_start=self.on_start` (see
  [`examples/regmap/simp_fun.py`](../../../examples/regmap/simp_fun.py)). That handler is exactly the
  method [`extract_kernel`](../../../waveflow/build/hwcodegen.py) lowers as the kernel body — the
  regmap slave both adds the `s_axilite` ports *and* designates the `ap_start`-triggered entry point.
- For **stream / m_axi** endpoints there is no separate handler method: the free-running `run_proc`
  body reads and writes them directly, and those per-port read/write calls are lowered to
  `hls::stream` / `m_axi` transactions. The transaction methods themselves (`read_stream_lane`,
  `read_array_lane`, …) are documented in [Custom Hooks: Kernel transfer reference](../custom_hooks/reference.md).

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
