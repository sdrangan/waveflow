---
title: Endpoint kinds
parent: Module Code Generation
nav_order: 8.5
audience: hls
snippets: run
summary: "The boundary-kind vocabulary — the seven names an endpoint can lower to, where each one is declared, and the two tables that consume them. One dispatch feeds both backends: the HLS port emitter and the XSI BFM lookup read the same kind, which is why a BFM model is not something you write by hand."
---

# Endpoint kinds

Every endpoint on an `HwModule` that reaches the kernel's boundary has a **kind** — one of seven
names. The kind is declared on the endpoint class, and **two separate tables consume it**: the Vitis
HLS port emitter, and the XSI testbench's BFM lookup.

That last point is the one most worth having. It is natural to assume that HLS lowering is automatic
while BFM models are hand-written. **They are the same dispatch**, and the source says so at the
table:

> *"Keys are `kind_of_endpoint`'s vocabulary, so this table and the boundary-port lowering cannot
> disagree."*

What *is* hand-authored on the XSI side is the testbench **graph** — which drivers exist and what
vectors they play — never the per-port model.

## The vocabulary

```python
from waveflow.build.composite_gen import BFM_DUALS
print("\n".join(sorted(BFM_DUALS)))
```

```text
axilite_slave
axis_in
axis_out
bram
maxi_read
maxi_write
mm_slave
```

| kind | the C++ port | the BFM dual |
|---|---|---|
| `axis_in` | `hls::stream<axi4s_word<bw>>&` | `AxisMaster` |
| `axis_out` | `hls::stream<axi4s_word<bw>>&` | `AxisSlave` |
| `maxi_read` | `const ap_uint<bw>*` + `#pragma HLS stable` | `AxiMmReadSlave` |
| `maxi_write` | `ap_uint<bw>*` | `AxiMmWriteSlave` |
| `mm_slave` | *not a kernel port* | **none, and none planned** |
| `axilite_slave` | `s_axilite` + `ap_ctrl_hs` | **none — a known gap** |
| `bram` | `ap_uint<bw> buf[N]` + `mode=bram` | **none needed** |

The bottom three rows are the interesting ones, and they are three *different* answers rather than
one absence. See [the holes are rows](#the-holes-are-rows-not-silence).

## Where a kind is declared

On the endpoint class, as a `ClassVar` — not in a table in `build/`:

```python
from waveflow.hw.interface import StreamIFSlave, StreamIFMaster
from waveflow.hw.memif import MMIFMaster, MMIFReadMaster, MMIFWriteMaster
from waveflow.hw.bram import BramIFMaster

for cls in (StreamIFSlave, StreamIFMaster, MMIFReadMaster, MMIFWriteMaster,
            BramIFMaster, MMIFMaster):
    print(f"{cls.__name__:18s} {cls.boundary_kind!r}")
```

```text
StreamIFSlave      'axis_in'
StreamIFMaster     'axis_out'
MMIFReadMaster     'maxi_read'
MMIFWriteMaster    'maxi_write'
BramIFMaster       'bram'
MMIFMaster         None
```

**Endpoints own what they *are*; `build/` owns what is *done* with them.** The endpoint says
`axis_in`; the port emitter decides that means an `hls::stream` argument and the testbench decides it
means an `AxisMaster`. Adding an endpoint type is one class attribute, not an edit to a dispatch in
another package.

### Three states, not two

`boundary_kind` distinguishes three cases, and the two refusals are different diagnoses:

| state | meaning | example |
|---|---|---|
| a string | a boundary port of that kind | `StreamIFSlave` → `axis_in` |
| declared `None` | **under-specified — refused** | a bare `MMIFMaster` |
| not declared at all | **not a boundary port** | `BramIFSlave`, `SobIFMaster` |

A bare `MMIFMaster` is legal hardware — a read-and-write `m_axi` is a plain pointer with all
channels — but it does not say which direction it is, and guessing wrong emits a `const` pointer for
a port that gets written. So the direction is the *type*: construct an `MMIFReadMaster` or an
`MMIFWriteMaster`.

The "not declared" case is not an error either. A `BramIFSlave` is the far end of a wrapper wire and
a `SobIFMaster` is internal to a kernel; neither is a pin on the elaborated design.

### Why a class attribute and not an `isinstance` chain

This *was* an eight-branch `isinstance` chain, and the chain carried a **silent ordering
dependency**: `RegMapMMIFSlave` had to be tested before `MMIFSlave`, and `MMIFReadMaster` before
`MMIFMaster` — subclass before base. Reorder those lines and there is no error; an `axilite_slave`
quietly lowers as `mm_slave`, and the first symptom is RTL that does not match the design.

Inheritance has no such hazard. A subclass's own declaration wins, and a subclass that declares
nothing inherits the right answer — which is exactly what `VitisRegMapMMIFSlave` relies on.

## The holes are rows, not silence

`BFM_DUALS` records what it *cannot* do as table rows rather than as prose, so "which duals exist" is
one lookup and the gaps are part of the answer:

```python
from waveflow.build.composite_gen import BFM_DUALS
for kind in ("maxi_read", "mm_slave", "axilite_slave", "bram"):
    d = BFM_DUALS[kind]
    print(f"{kind:14s} model={str(d.model):18s} needs_model={d.needs_model}")
```

```text
maxi_read      model=AxiMmReadSlave     needs_model=True
mm_slave       model=None               needs_model=True
axilite_slave  model=None               needs_model=True
bram           model=None               needs_model=False
```

`needs_model` is what separates a **gap** from a **non-requirement** — the distinction a bare `None`
cannot express:

* **`mm_slave`** — a port that is an AXI-MM *slave* would need the testbench to master the bus into
  it. No model does that and none is planned: in this flow the kernel is always the master and the
  testbench always supplies the memory.
* **`axilite_slave`** — **the known gap.** A regmap / `HostActivated` DUT presents an AXI4-Lite
  control slave and nothing in `waveflow/build/xsi/` answers it, so such a DUT cannot be XSI-lowered
  at all today.
* **`bram`** — `needs_model=False`, and this one is *by design*. A BRAM port is not a pin on the
  elaborated design: the wrapper joins it to a memory inside the synthesized scope, so there is
  nothing for a testbench to drive. If a memory ever did need a model, the wrapper would be the
  thing that is wrong.

## See also

- [Endpoint interfaces](interface.md) — how each kind becomes a Vitis HLS port, kind by kind
- [XSI testbench in HLS](xsi_tb.md) — the generated harness that consumes the BFM duals
- [Primitive interfaces](../interface/primitive/index.md) — the Python side, and the access cases
- [A module realized as Verilog](rtl_module.md) — the wrapper, which is `bram`'s counterpart
