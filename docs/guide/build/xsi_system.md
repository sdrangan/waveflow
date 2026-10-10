---
title: XSI System Simulation
parent: Build System
nav_order: 6.5
audience: python
summary: "Simulating a whole memory-mapped system at RTL -- several csynth'd kernels behind their adaptors, AMD's crossbar, an on-chip memory, and a host -- from the pysim system object, with nothing restated: add_system_steps(dag, sysm) puts csynth (one step per HLS top, fresh while its source stamp matches; build or check), the crossbar IP and the Verilog top (system_rtl), the scenario, pysim, the RTL run and the trace comparison on a BuildDag, and run_system_xsi(sysm) is the same as one call. It walks the graph to the Verilog top (crossbar slots, adaptors, stream nets, credit-link writers, interrupt outputs, tie-offs), generates the harness around the host's C++ twin (its threads on generated endpoints), runs it, and checks the host against pysim: both run one scenario file, every host endpoint records what crossed it, and the traces must be byte-identical; the cycle count is an exact gate of its own. Worked on mm_fir (618 / 611) and markov (1870)."
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
| all of the above, built and run | the system object | **`add_system_steps(dag, sysm, ...)`** ([a build DAG](#running-it)), or `run_system_xsi(sysm, ...)` |

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
`m_axi` pointers are `offset=off`, so a bus address is the address (a kernel that still has an
`s_axi_control` slave has its inputs tied low --
[why `offset=off`](../comp_codegen/freerunning_composite.md#how-a-pointer-reaches-a-task)); an `m_axi` pin
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

## Running it: the system on a build DAG {#running-it}

A system's whole flow -- from its generated sources to the trace gate -- is one
[`BuildDag`](index.md), and everything after the example's own code generation is framework:

```
codegen ──> csynth ──> system_rtl ──┐
                                    ├──> system_xsi ──┐
scenario ──┬────────────────────────┘                 ├──> compare
           └──> pysim ────────────────────────────────┘
```

```python
from waveflow.build.build import BuildDag
from waveflow.build.system_dag import add_system_steps

dag = BuildDag()
dag.add(MarkovCodegenStep(name="codegen"))     # the example's: headers, kernel tops, writer tops, .tcl
add_system_steps(dag, MarkovSystem(jobs=jobs, link="mm"), work_dir="xsi_work", top="markov_top")
```

[`add_system_steps`](../../../waveflow/build/system_dag.py) takes the system object -- not yet run --
finds the crossbar and the `SwHost` among the simulation's objects, takes the default cut (each kernel
whose device is a crossbar slave, then each memory on it; `inside=` overrides), and adds the six
framework steps below. `top` defaults to the system class in snake case (`MarkovSystem` ->
`markov_system`) and `xbar_name` to `xbar_<top>`. An example's script hands the DAG to
`run_dag_cli`, so it gets `--through <step>`, `--status`, `--force` and `--force-step` for free.

### csynth {#csynth}

One inner step per **HLS top** the cut instantiates (`spec.modules`): the kernels and the bus writers a
routed credit stream brings. The top is the rebuild unit, since Vitis has no incremental csynth and
each top is its own project. A top is **fresh while its source stamp matches**. The stamp records the
content of the sources csynth read, written beside the project
([`rtl_digest`](../../../waveflow/build/rtl_digest.py)). So a `codegen` that rewrites `include/` and
`gen/` with identical bytes re-runs no csynth, and neither does a `git checkout` that restores
identical files. A project with no stamp falls back to comparing mtimes, never to "clean". Today the
stamp covers all of `include/` and `src/`, so an edit there re-runs every top; an edit to one top's
`gen/<top>.cpp` re-runs that top only.

**Build or check** -- the `synth` parameter decides who may run the toolchain:

- `synth="build"` (the CLI default): a stale or missing top is synthesized, then stamped;
- `synth="check"` (the gate tests, `run_system_xsi`): a stale or missing top **fails the step**, naming
  the top and the source that changed. A gate never hides a 40-second csynth, and never runs against RTL
  built from other sources.

### system_rtl {#system-rtl}

The rest of the system's Verilog, after csynth, as an inner DAG:

1. `xbar_ip` -- the crossbar IP, generated by Vivado's `create_ip` from the pysim crossbar's ranges, and
   cached by its configuration under `<work_dir>/ip/`;
2. `system_top` -- the Verilog top (`render_system_top`), with the timing probes if any, written to
   `<work>/<top>.v`.

It also writes `<work>/rtl.json`, the manifest of every Verilog file the top compiles -- the IP's
simulation files, the framework's adaptor and memory leaves (`waveflow/build/rtl/`), each csynth'd
module's files, the top -- and the IP's include directories. That is all `system_xsi` reads of it.
csynth and `system_rtl` together are everything that makes Verilog: `--through system_rtl` builds all
of a system's RTL without simulating it.

### scenario {#scenario}

The host writes its scenario bundle (`SwHost.write_scenario`) to `<work>/scenario/`. It is its own step
because pysim and the RTL run must read **the same file** (the conformance gate below).

### pysim {#pysim}

The same system object, run in pysim from that scenario. Every host endpoint's trace goes to
`<work>/pysim_traces/` and the run's length in host clock cycles to `<work>/pysim.json`. Needs no
toolchain: `--through pysim` is the whole software side, with no Vivado.

### system_xsi {#system-xsi}

The system at RTL under a testbench, itself an inner DAG:

1. `harness` -- the host's C++ and its generated endpoints, bound to the top's ports
   (`render_system_tb`), pointed at the scenario and the trace directory;
2. `xsi_run` -- `xvlog` / `xelab` build the RTL that `rtl.json` lists and `g++` the testbench, the run,
   and the host's report parsed into `<work>/report.json`; the traces in `<work>/traces/`.

### compare {#compare}

Every host endpoint's trace, RTL against pysim, file for file -> `<work>/compare.json` (the mismatches;
empty: pass). A mismatch fails the step.

### Freshness: a hook each step answers late {#freshness}

`BuildDag`'s own rule is mtimes plus a cascade: a step runs when its outputs are older than its inputs
or when anything upstream ran. Both are wrong for csynth, whose inputs `codegen` rewrites every run,
usually with the same bytes. So a step may answer for itself:

```python
class BuildStep:
    def is_fresh(self, config, paths) -> bool | None:   # None (default): the mtime rule
```

The DAG asks it **just before the step would run** -- after its upstream ran -- and skips the step when
it says True, even if the cascade marked it. Downstream steps then see it as not having run, so the
cascade stops there. False runs it. `--force` / `--force-step` still win, and `--status` asks the hook
too. In a system DAG only csynth answers by content. `codegen`, `system_rtl`, `scenario`, `pysim`,
`system_xsi` and `compare` always answer False: each reads Python and C++ the DAG cannot see, and each takes seconds.

### Reading a run back, and `run_system_xsi`

`system_xsi.load_run(<work>)` reads `report.json`, `pysim.json` and `compare.json` back into an
`XsiRun`: the output, `done`, `cycles`, `polls`, `nops`, the bus `ops`, the workspace, scenario and trace
paths, `pysim_cycles` and `trace_mismatches`.

```python
from waveflow.build.system_xsi import run_system_xsi

run = run_system_xsi(MarkovSystem(jobs=jobs, link="mm"), work_dir, top="markov_top")
run.cycles, run.trace_mismatches          # 1870, []
```

[`run_system_xsi`](../../../waveflow/build/system_xsi.py) is the same DAG as a one-call wrapper:
[csynth](#csynth) in check mode, through [compare](#compare) (`compare_pysim=False`: through
[system_xsi](#system-xsi) only), read back with `load_run`. `probes=` adds timing probes. A second
system in the same DAG is `add_system_steps(..., prefix="<name>_")`: its steps and artifacts are
prefixed, and a top an earlier system's csynth builds is shared, as mm_fir's two topologies share one
`codegen` and one `csynth`.

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
- The writer tops' **code generation** is still each example's `codegen` (one `write_writer_project`
  call per writer), though the csynth set is derived from the cut; a writer the example forgot fails
  [csynth](#csynth) with its missing `.tcl`.
- The source stamp is coarse: it hashes all of `include/` and `src/`, so a body edit re-synthesizes
  every top, not only the one that includes it.
- AXI4-Lite / `HostActivated` DUTs remain out of reach at RTL (`BFM_DUALS["axilite_slave"]`).

**Source of truth:** [`waveflow/build/system_dag.py`](../../../waveflow/build/system_dag.py),
[`waveflow/build/system_top.py`](../../../waveflow/build/system_top.py),
[`waveflow/build/xsi/xsi_mm_host.h`](../../../waveflow/build/xsi/xsi_mm_host.h),
[`waveflow/hw/mm_host.py`](../../../waveflow/hw/mm_host.py) (the Python traces);
`tests/build/test_system_dag.py`, `tests/build/test_system_top.py`, `tests/build/test_xsi_system_top.py`, and the gates
`tests/examples/test_mm_fir_xsi.py`, `tests/examples/test_markov_xsi.py`.
