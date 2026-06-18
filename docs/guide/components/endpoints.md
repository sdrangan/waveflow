---
title: Endpoint methods
parent: Hardware Components
nav_order: 2
audience: python
applies_to: [HwComponent]
api: [StreamIFSlave, StreamIFMaster, MMIFMaster, MMIFSlave, VitisRegMapMMIFSlave, ArrayTransferIFSlave, SchemaTransferIFSlave]
summary: "The master/slave roles and the endpoint methods a component's behavior is built from: a master initiates transactions (write/push on a stream, read/write/read_array/write_array on an m_axi); a slave responds — a launch handler (on_start), a get/pop pulled in the body (stream/transfer slave), or a passive memory target. A table maps each endpoint type to the method you define or call and the interface page with its signature."
---

# Endpoint methods

A component's behavior is the **methods on its [endpoints](./overview.md)**. Every endpoint is either
a **master** or a **slave**, and the role decides whether the component *initiates* a transaction or
*responds* to one. The two are not a clean "master = code, slave = callback" split — read the roles
below as *who drives the transaction*.

## Master — the component initiates

A master endpoint drives the bus: the component **calls** the transaction method itself, from its
[lifecycle](../sim/lifecycle.md) body (`run_proc`, or `on_start` / a `@synthesizable` hook).

- **Stream** — [`StreamIFMaster`](../../../waveflow/hw/interface.py): `write(data)`,
  `write_pipelined(data, t_out_start)`, `push(value)`. `poly`'s `m_out` emits its response with
  `m_out.write(resp_hdr)` and `m_out.write_pipelined(...)`.
- **Memory-mapped** — [`MMIFMaster`](../../../waveflow/hw/memif.py): `read(nwords, addr)`,
  `write(words, addr)`, and the typed `read_array(elem_type, count, addr)` /
  `write_array(arr, elem_type, addr)`. A read is still *master-initiated* — the component issues it.
  `mem_demo`'s driver does `yield from self.master.write_array(values, Uint32, addr)` then
  `self.master.read_array(Uint32, count=n, addr=addr)`.

## Slave — the component responds

A slave endpoint is the target of a transaction. The component responds in one of three ways:

- **A launch handler.** [`VitisRegMapMMIFSlave`](../../../waveflow/hw/regmap.py) is constructed with
  `on_start=self.on_start`; the host writing `ap_start` over AXI-Lite invokes that handler. The
  component's inputs/outputs are the register fields (`self.regmap.get(...)` / `set(...)`). This is
  `simp_fun`'s only endpoint and `poly`'s control path.
- **A `get` pulled in the body.** A stream or transfer *slave* delivers incoming data when the
  component asks for it: [`StreamIFSlave`](../../../waveflow/hw/interface.py) —
  `get(schema_type, count)`, `get_pipelined(schema_type, count)`, `pop(value)`. `poly`'s `on_start`
  pulls its command with `cmd_hdr = yield from self.s_in.get(PolyCmdHdr)` and its samples with
  `s_in.get_pipelined(Float32, count=...)`.
- **A passive memory target.** [`MMIFSlave`](../../../waveflow/hw/memif.py) is a memory the peer
  master reads and writes; the component declares it and the transactions are serviced by the model
  (no handler method). `mem_demo`'s `MemComponent.s_mm` is one.

## By endpoint type

The method names are canonical; the **signatures and semantics live on the linked interface page**.

| Endpoint type | Role | Method you define / call | Interface page |
|---|---|---|---|
| `StreamIFSlave` | slave (consume) | `get(schema_type, count)` / `get_pipelined(...)` / `pop(value)` | [Stream](../interface/stream.md) |
| `StreamIFMaster` | master (initiate) | `write(data)` / `write_pipelined(data, t)` / `push(value)` | [Stream](../interface/stream.md) |
| `MMIFMaster` | master (initiate) | `read(nwords, addr)` / `write(words, addr)` / `read_array(elem, count, addr)` / `write_array(arr, elem, addr)` | [MM Interfaces](../interface/aximm.md) |
| `MMIFSlave` | slave (memory target) | passive — serviced by the model; no handler | [MM Interfaces](../interface/aximm.md) |
| `VitisRegMapMMIFSlave` | slave (launch) | define the `on_start` handler; fields via `regmap.get` / `regmap.set` | [Register Maps](../interface/regmap.md) |
| `SchemaTransferIFMaster` / `Slave` | master / slave | `write(obj)` / `get()` | [Schema Transfer](../interface/schema_transfer.md) |
| `ArrayTransferIFMaster` / `Slave` | master / slave | `write(elements)` / `get(count)` | [Array Transfer](../interface/array_transfer.md) |

> A master endpoint connects to a slave endpoint through an **interface** — e.g. an `MMIFMaster` and
> an `MMIFSlave` are bound to a [`DirectMMIF`](../interface/aximm.md). Declaring endpoints is *what* a
> component exposes; binding them to interfaces is how a system is *wired* — see
> [Interfaces](../interface/).

## Endpoints are *what*; lifecycle is *when*

This page is the *what* — the methods available per endpoint. *When* they run — `run_proc` for a
free-running component vs. `on_start` for a regmap-launched one — is the [Lifecycle](../sim/lifecycle.md)
page. The synthesizable C++ realization of each endpoint (an `hls::stream` / `m_axi` / `s_axilite`
port) is [Component Code Generation: Endpoint interfaces](../comp_codegen/interface.md).

## Quick reference

- Master = the component **calls** the transaction (`write` / `push` on a stream; `read` / `write` / `read_array` / `write_array` on an m_axi).
- Slave = the component **responds**: a launch handler (`on_start`), a `get` / `pop` pulled in the body, or a passive memory target.
- A `get` on a stream/transfer slave consumes *incoming* data; a `read_array` on an m_axi master *initiates* a read — different roles, both pull data.
- Method signatures live on the [interface](../interface/) pages; this table is the index.
