---
title: XSI System Simulation
parent: Build System
nav_order: 6.5
audience: python
summary: "Simulating a whole memory-mapped system at RTL -- several csynth'd kernels behind their adaptors, AMD's crossbar, an on-chip memory, and a host -- from the pysim system object, with nothing restated. Name the cut and system_top_spec walks the graph to the Verilog top (crossbar slots, adaptors, stream nets, credit-link writers, interrupt outputs, tie-offs); the host is a hooked module whose C++ realization lives in a header beside the example; system_tb_spec / render_system_tb generate the harness. Both hosts run one scenario file, every host endpoint records what crossed it, and the conformance gate is byte-identical per-endpoint traces plus an exact cycle count. Worked on mm_fir (618 / 611) and markov (1870)."
---

# XSI system simulation

A single free-running kernel is driven at RTL by BFMs on its ports ([XSI Build Rung](xsi.md),
[BFM testbenches](bfm.md)). A **system** is more: several csynth'd kernels, each reached through a
[memory-mapped adaptor](../interface/axi_mm/slave.md), AMD's crossbar between them, perhaps an on-chip
memory, and a host program that drives it all over the bus. The pysim system object already holds
every fact about that system — its `StreamIF`s, crossbar bindings, adaptor views and `IrqIF` lines —
so the RTL simulation is derived from it rather than written:

| piece | comes from | framework call |
|---|---|---|
| the Verilog top | a walk of the pysim graph, cut at a set of modules | `system_top_spec`, `render_system_top` |
| the crossbar IP | the pysim crossbar's ranges | `generate_axi_xbar(spec.xbar, ...)` |
| the host's C++ | the host's `bfm_model()` — a header beside the example | (hand-written, once) |
| the harness and `main` | the host's model ports, bound to the top's ports | `system_tb_spec`, `render_system_tb` |
| the address map the host uses | a walk of the crossbar | `bus_address_headers` |

The two worked cases are [mm_fir](../../examples/mm_fir/rtlsim.md) (one kernel, a register bank and
three queues, two adaptor shapes) and [markov](../../examples/markov/rtlsim.md) (two kernels joined by a
routed credit stream, four bus masters, a shared BRAM).

## The cut is a set

```python
from waveflow.build.system_top import system_top_spec, render_system_top

sysm = MarkovSystem(jobs=jobs, link="mm")
spec = system_top_spec(sysm.xbar, [sysm.gen, sysm.chain, sysm.mem], top="markov_top")
verilog = render_system_top(spec)
```

`inside` names what is synthesized into the top: the kernels and any on-chip memory. Everything that
belongs to them comes along — each kernel's device (the views and adaptor `build_mm_device` built), and
the two bus writers of a routed credit stream between two inside kernels. Everything else on the graph
is a BFM beside the top, and **an endpoint bound to nothing inside the set becomes a top port**: the
host's bus master becomes an AXI4 slave group `s<k>_axi`, and an interrupt line whose sink is outside
becomes an output `irq_<view>`.

What the walk emits, element by element:

| graph element | emitted |
|---|---|
| a csynth'd kernel | an instance of its module; pins from the same `TopSpec` its csynth top was built from |
| a crossbar master inside the cut | an SI slot on internal wires |
| a crossbar master outside the cut | a top AXI4 slave port group |
| a crossbar slave | an MI slot: one view, an adaptor's views, or an on-chip memory as a BRAM window |
| a `StreamIF` between two of them | stream nets, named after the channel; a deep link between two *kernels* gets an `mm_sync_fifo` (a view's link *is* the view's FIFO) |
| a routed credit stream | its two writers (`mm_writer_gen`), each `target` from the address map |
| an `IrqIF` leaving the cut | a top output |

The **tie-off rules** live once, in [`system_top.py`](../../../waveflow/build/system_top.py): a kernel's
`s_axi_control` inputs low (the `m_axi` base stays 0, so a bus address is the address); an `m_axi` pin
the crossbar lacks tied low or left open; a Vitis master's one-bit IDs against the crossbar's wider ones;
a stream input nothing drives (`TLAST` low, `TKEEP`/`TSTRB` all ones); padded crossbar slots answering
nothing. The spec is answerable before it is rendered — `spec.si`, `spec.mi`, `spec.nets`,
`spec.irqs`, `spec.modules` (the csynth'd modules whose RTL a workspace compiles).

## The host is a hooked module

A kernel has two realizations joined by a hook — its Python model, and the HLS body `kernel_task()`
names. A host is the same: its Python `run_proc` over the bus endpoints, and an `XsiSimObj` that
`bfm_model()` names, **in a header beside the example** (`BfmModel(header=...)`). One model spans the
host's bus master and the interrupt pins it waits on, so its C++ is the host program itself, written on
the C++ endpoints of [`xsi_mm_host.h`](../../../waveflow/build/xsi/xsi_mm_host.h) — which read line for
line like the Python ones. See [A host is a hooked module](../custom_hooks/bfm_model.md#host).

```python
from waveflow.build.system_top import system_tb_spec, render_system_tb

host = sysm.host
host.scenario = scenario_path          # DynParams: emitted into the harness
host.trace_dir = traces_path
host.write_scenario(host.scenario)
tb = system_tb_spec(spec, sysm.xbar, [host])          # resolves host.bfm_model() against the top
main, files = render_system_tb(spec, tb)              # main .cpp; ports header, harness, host .h
```

The harness runs until the host's `done()` says it has everything it asked for
(`Harness::run_until`). That completion is the measured cycle count — the host reports it, with every
bus operation it issued.

## One scenario, and the conformance gate

Nothing static can show that a C++ host behaves like its Python twin. So the two are held together the
way kernels are — by a gate on data:

- **One scenario.** What the host sends comes from a file both realizations read (a burst bundle the
  Python host writes, `write_scenario`). A scenario baked into the C++ would be a second copy, and there
  would be nothing to compare.
- **Every host endpoint records what crossed it**, on both sides: each packet a queue writer sent, each
  config committed, the words each read took, each status read, each region read back from memory
  (`BusReader` / `MmBusReader`). One burst bundle per endpoint.
- **The gate:** for the same scenario, **each endpoint's trace is byte-identical** between the pysim run
  and the RTL run. Per endpoint, not globally — the interleaving across endpoints is timing, and pysim
  is loosely timed. The RTL cycle count is an exact gate of its own.

Because the data comes back as traces, the checks are Python: decode the response and status traces
with their schemas and compare against the scenario. The C++ never holds a field position — except one
it must *act* on (which job a response answers), and that position is handed to it from the schema's
own serializer (`field_position`).

## What it does not do yet

- An **off-chip memory** — a `FlatMemory` BFM beside the top, as mem_copy's — and a BRAM window
  *shared with a kernel* are refused with a named error rather than wired.
- `system_tb_spec` binds host ports that are crossbar masters or interrupt sinks; a host that also
  streams straight into a kernel is not resolved.
- AXI4-Lite / `HostActivated` DUTs remain out of reach at RTL (`BFM_DUALS["axilite_slave"]`).

**Source of truth:** [`waveflow/build/system_top.py`](../../../waveflow/build/system_top.py),
[`waveflow/build/xsi/xsi_mm_host.h`](../../../waveflow/build/xsi/xsi_mm_host.h),
[`waveflow/hw/mm_host.py`](../../../waveflow/hw/mm_host.py) (the Python traces);
`tests/build/test_system_top.py`, `tests/build/test_xsi_system_top.py`, and the gates
`tests/examples/test_mm_fir_xsi.py`, `tests/examples/test_markov_xsi.py`.
