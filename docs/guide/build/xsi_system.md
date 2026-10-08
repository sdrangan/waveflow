---
title: XSI System Simulation
parent: Build System
nav_order: 6.5
audience: python
summary: "Simulating a whole memory-mapped system at RTL -- several csynth'd kernels behind their adaptors, AMD's crossbar, an on-chip memory, and a host -- from the pysim system object, with nothing restated: run_system_xsi(sysm). It walks the graph to the Verilog top (crossbar slots, adaptors, stream nets, credit-link writers, interrupt outputs, tie-offs), generates the harness around the host's C++ twin (its threads on generated endpoints), runs it, and checks the host against pysim: both run one scenario file, every host endpoint records what crossed it, and the traces must be byte-identical; the cycle count is an exact gate of its own. Worked on mm_fir (618 / 611) and markov (1870)."
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
| the host's C++ | its threads, in a header beside the example ([Software threads](sw_threads.md)) | (hand-written, once) |
| the host's endpoints | the wired host: each endpoint's view, address and interrupt | `render_host_endpoints_h` |
| the harness and `main` | the host's ports, bound to the top's ports | `system_tb_spec`, `render_system_tb` |
| all of the above, run | the system object | **`run_system_xsi(sysm, ...)`** |

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
names. A host is the same: a `SwHost` whose threads run as SimPy processes in pysim, and a C++ class of
the same threads -- **in a header beside the example** -- that `cpp_model` / `cpp_header` name. The C++
is the program only: it derives from a **generated** `<Host>_endpoints` that declares every endpoint
on its view, with its interrupt, and runs on the software-thread runtime, where each thread is a fiber
and every bus call blocks. [Software threads](sw_threads.md) is the full API, side by side with Python;
[A host is a hooked module](../custom_hooks/bfm_model.md#host) is the hook.

The harness runs until the host's `done()` -- every thread finished -- says it has everything it asked
for (`Harness::run_until`). That completion is the measured cycle count; the host reports it, with
every bus operation it issued.

## Running it

```python
from waveflow.build.system_xsi import run_system_xsi

run = run_system_xsi(MarkovSystem(jobs=jobs, link="mm"), work_dir, top="markov_top")
run.cycles, run.trace_mismatches          # 1870, []
```

[`run_system_xsi`](../../../waveflow/build/system_xsi.py) takes the system object -- not yet run -- and
nothing else:

1. **discovers** the crossbar and the `SwHost` among the simulation's objects, and the default cut (each
   kernel whose device is a crossbar slave, then each memory on it; `inside=` overrides);
2. **checks the RTL** of every module the top instantiates is synthesized and not stale, naming the
   build to run if not;
3. **generates** the crossbar IP (cached by its configuration), the top, the harness with the host's C++
   and its generated endpoints, and the scenario the host writes;
4. **runs** it under XSI and parses the host's report;
5. **checks the host**: runs the same system in pysim from the same scenario and compares every
   endpoint's trace (below).

It returns an `XsiRun`: the output, `done`, `cycles`, `polls`, `nops`, the bus `ops`, the workspace,
scenario and trace paths, `pysim_cycles` and `trace_mismatches`. `probes=` adds timing probes.

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
with their schemas and compare against the scenario. Where the C++ must *act* on a field (which job a
response answers), it reads the generated struct by name -- `qresp.get<MkvResp>().tx_id`.

## Timing probes

Once a system runs, `render_system_top(spec, probes)` adds one-bit probe outputs a testbench samples
every cycle. A probe names the **pysim object** it watches — `beat(sysm.gen.s_cmd)`,
`stall(sysm.fir.m_out)`, `last(ep)`, `beat(writer.m_mem, "AW")` — and the spec resolves it to the
top's net or crossbar slot (`spec.probe_expr`), so no one has to know what the walk named a net.
`system_tb_spec(..., probes=names)` adds a `ProbePin` per probe to the harness.

## What it does not do yet

- An **off-chip memory** — a `FlatMemory` BFM beside the top, as mem_copy's — and a BRAM window
  *shared with a kernel* are refused with a named error rather than wired.
- `system_tb_spec` binds host ports that are crossbar masters or interrupt sinks; a host that also
  streams straight into a kernel is not resolved.
- `run_system_xsi` does not **build**: synthesizing the kernels stays each example's `*_build` script,
  which it names when the RTL is missing or stale.
- AXI4-Lite / `HostActivated` DUTs remain out of reach at RTL (`BFM_DUALS["axilite_slave"]`).

**Source of truth:** [`waveflow/build/system_top.py`](../../../waveflow/build/system_top.py),
[`waveflow/build/xsi/xsi_mm_host.h`](../../../waveflow/build/xsi/xsi_mm_host.h),
[`waveflow/hw/mm_host.py`](../../../waveflow/hw/mm_host.py) (the Python traces);
`tests/build/test_system_top.py`, `tests/build/test_xsi_system_top.py`, and the gates
`tests/examples/test_mm_fir_xsi.py`, `tests/examples/test_markov_xsi.py`.
