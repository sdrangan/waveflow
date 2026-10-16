---
title: XSI System Simulation
parent: Build System
nav_order: 6.5
audience: python
api: [HwSystem, DutTop, MmSlaveDevice, SystemTopSpec, SystemTopSpec.from_system, SystemTopSpec.render_verilog, system_top_spec, SubTop, RtlInstance, render_system_top, add_system_steps, run_system_xsi]
summary: "Simulating a whole memory-mapped system at RTL -- several csynth'd kernels behind their adaptors, AMD's crossbar, an on-chip memory, and a host -- from the pysim system object, with nothing restated. A system is a subclass of HwSystem: its sim, run(), and dut(), a DutTop tree of devices (each kernel with its views and the bus writers acting for it) and memories, which is the default cut. SystemTopSpec is the RTL top as data, walked from it; each device is a sub-top that renders and elaborates alone. add_system_steps(dag, sysm) puts csynth (one step per HLS top, fresh while its source stamp matches; build or check), the crossbar IP and the Verilog top (system_rtl), the scenario, pysim, the RTL run and the trace comparison on a BuildDag, and run_system_xsi(sysm) is the same as one call. It walks the graph to the Verilog top (crossbar slots, adaptors, stream nets, credit-link writers, interrupt outputs, tie-offs), generates the harness around the host's C++ twin (its threads on generated endpoints), runs it, and checks the host against pysim: both run one scenario file, every host endpoint records what crossed it, and the traces must be byte-identical; the cycle count is an exact gate of its own. Worked on mm_fir (618 / 611) and markov (1870)."
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
| the Verilog top | a walk of the pysim graph, cut at a set of modules | [`SystemTopSpec.from_system`, `.render_verilog()`](#the-spec) |
| the crossbar IP | the pysim crossbar's ranges | `generate_axi_xbar(spec.xbar, ...)` |
| the host's C++ | its threads, in a header beside the example ([Software threads](sw_threads.md)) | (hand-written, once) |
| the host's endpoints | the wired host: each endpoint's view, address and interrupt | `render_host_endpoints_h` |
| the harness and `main` | the host's ports, bound to the top's ports | `system_tb_spec`, `render_system_tb` |
| all of the above, built and run | the [system object](#the-system) | **`add_system_steps(dag, sysm, ...)`** ([a build DAG](#running-it)), or `run_system_xsi(sysm, ...)` |

The two worked cases are [mm_fir](../../examples/mm_fir/rtlsim.md) (one kernel, a register bank and
three queues, two adaptor shapes) and [markov](../../examples/markov/rtlsim.md) (two kernels joined by a
routed credit stream, four bus masters, a shared BRAM).

## The system: `HwSystem` {#the-system}

Every system flow takes a **system object**: an instance of a subclass of
[`HwSystem`](../../../waveflow/hw/hw_system.py) (`waveflow.hw.hw_system`). The class states
what every downstream step relies on, so a system that compiles against it is one the flows accept:

| member | what it is | markov's |
|---|---|---|
| `sim` | the `Simulation` holding every module and interface of the graph. The subclass builds the graph, typically in `__post_init__` | one `Simulation`, with the kernels, memory, host, crossbar and links on it |
| `dut()` | the **default DUT**: a `DutTop`, the root of the tree of what is synthesized | `markov_dut`: `gen_dev`, `chain_dev`, `mem`, with `xbar` and `u_link` as its links |
| `run()` | simulate the whole graph in pysim and return what the system reports | runs until the host is done; the host's results |
| `cut()` | the DUT as a list, each device's kernel then each memory: what `inside=` takes | `[gen, chain, mem]` |
| `elements()` | every module and interface of the graph | |

`dut()` and `run()` are abstract, so a subclass missing either cannot be instantiated, and a flow
given an object that is not a `HwSystem` refuses it by naming this contract.

**The DUT is a tree.** `build_mm_device` returns each kernel's **device**, a composite module
(`MmSlaveDevice`) holding the kernel, its views and its adaptor. Binding a routed credit stream adds
each of its bus writers to the device of the kernel it acts for. `DutTop` holds the devices and the
on-chip memories, and the interfaces between them:

```python
from waveflow.hw.hw_system import DutTop, HwSystem

@dataclass
class MarkovSystem(HwSystem):
    jobs: list

    def __post_init__(self):
        self.sim = Simulation()
        ...                                      # kernels, memory, host; devices; crossbar; links
        self.dut_top = DutTop.of("markov_dut", self.sim,
                                 comps=[self.gen_dev, self.chain_dev, self.mem],
                                 ifs=[self.xbar, self.u_link])

    def dut(self) -> DutTop:
        return self.dut_top

    def run(self):
        self.sim.run_sim(until=self.host.done)
        return self.host.results
```

**DUT and testbench are roles, not kinds.** Everything outside the DUT tree is the testbench: here
only the host. `dut()` is the cut a build takes when it is given none. A build may draw another one
with `inside=`, and the same module is then DUT in one build and testbench in another. Nothing about
the module changes. [The design cut](../flows/modules.md#the-cut) is the same rule for a single
kernel. The tree is also the unit structure everything after simulation uses: each device is one
[sub-top](#devices) of the Verilog, one unit to synthesize, and one IP to package.

[Building the markov system](../../examples/markov/system.md) walks one system from its modules to
its DUT tree.

### A system around one graph: `GraphSystem` {#graph-system}

Most systems with a single kernel already have their graph: a composite holding the kernel and the
testbench participants around it. `GraphSystem` is the `HwSystem` for that case. A subclass states
only what is its own, and the base derives the rest:

| a subclass states | the base derives |
|---|---|
| `graph()`: build and return the graph | `sim`, from the graph |
| `dut_modules(g)`: the modules inside the default cut | `dut()`, a `DutTop` of them; `clk`, the DUT's clock; `data_width`, its widest boundary port |
| `write_inputs(root)`: the scenario's inputs | `write_scenario`: the inputs, then `vectors/run.json` (the graph's `n_cycles`), then every participant that reads or records a bundle anchored at *root* |
| `check()` (optional): the golden on a pysim run | `run()`: pysim, to the graph's `run_until` when it has one, then `check()` |

A recording that pysim cannot match by construction is marked on its participant in `graph()`
(`recording_check = "example"`), and the example checks it instead of `compare`.
[The mem_copy system](../../examples/memcpy/system.md) is the worked example.

## The system top: `SystemTopSpec` {#the-spec}

### What it is

A **`SystemTopSpec`** is the RTL top of a system, as data. It is a frozen dataclass in
[`waveflow/build/system_top.py`](../../../waveflow/build/system_top.py), derived from the pysim system
by walking its graph. It is the middle of three layers, each derived from the one before:

| layer | what it is | markov's |
|---|---|---|
| the pysim system | the Python object you write and simulate: kernels, interfaces, crossbar, host | `MarkovSystem` |
| **the spec** | the RTL top as data: every instance, slot, net and port, cut at a set of modules | `SystemTopSpec`, top `markov_top` |
| the Verilog | text rendered from the spec | `markov_top.v` |

Everything about the top is decided when the spec is built, and the spec can be queried before
anything is rendered or synthesized. The Verilog is then a pure function of it.

### Constructing it

From the system object, not yet run:

```python
from waveflow.build.system_top import SystemTopSpec

sysm = MarkovSystem(jobs=jobs, link="mm")
spec = SystemTopSpec.from_system(sysm, top="markov_top")
```

`from_system` takes the system's default cut, [`HwSystem.cut()`](#the-system): the kernels and
memories of its DUT tree, the same cut [`add_system_steps`](#running-it) takes. It finds the bus and
the host by their **roles**: the bus is the DUT tree's link that realizes as bus IP (the crossbar's
`rtl_config`), and the host is the bus master on it whose owner is outside the DUT. The underlying walk is the function `system_top_spec(xbar, inside, ...)`,
which takes the crossbar directly. `from_system` calls it, and code that already holds the crossbar
can call it too.

### Parameters

| parameter | default | meaning |
|---|---|---|
| `sysm` | (required) | the [system object](#the-system), not yet run. It must hold one crossbar and one software host |
| `inside` | `sysm.cut()` | the modules synthesized into the top: kernels (`FreeRunMod`s with a csynth'd module) and on-chip memories (`MemoryMod`). See [the cut](#the-cut) |
| `top` | the system class in snake case (`MarkovSystem` → `markov_system`) | the Verilog module name of the system top; the sub-tops are named `<top>_<device>` |
| `xbar_name` | `xbar_<top>` | the crossbar IP's module name |
| `id_width` | enough bits to route every crossbar master | the crossbar's AXI ID width |

#### The cut is a set {#the-cut}

`inside` names what is synthesized into the top. Everything that belongs to those modules comes
along: each kernel's device (the views and adaptor `build_mm_device` built), and the two bus writers
of a routed credit stream between two inside kernels. Everything else on the graph is a BFM beside
the top, and **an endpoint bound to nothing inside the set becomes a top port**: the host's bus master
becomes an AXI4 slave group `s<k>_axi`, and an interrupt line whose sink is outside becomes an output
`irq_<view>`.

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
nothing.

### Generating the Verilog

```python
verilog = spec.render_verilog()                          # one string: the whole <top>.v
verilog = spec.render_verilog(probes=timing_probes(sysm)) # with timing-probe outputs
```

The text is one file holding a module per [device and memory](#devices) (the sub-tops), then the system
top that instantiates them beside the crossbar. *probes* maps a name to a probe on a pysim endpoint
(`beat(sysm.gen.s_cmd)`, `last(...)`, `stall(...)`). Each becomes an output `probe_<name>` of the
top, computed in the module that holds its net. `render_verilog` is the method form of the function
`render_system_top(spec, probes)`. On a build DAG the `system_top` step writes this text to
`<work>/<top>.v` ([system_rtl](#system-rtl)).

One device (or the memory) can also be rendered **alone**:

```python
verilog = spec.render_sub_verilog("gen")         # markov_top_gen, as a file of its own
leaves, kernels = spec.sources_of("gen")         # its compile list: leaf sources, csynth'd modules
```

The module is exactly the one the whole file holds for it. Its ports are AXI4 groups, interrupt lines
and probes, so it needs nothing around it. `elaborate_sub_top(spec, "gen", root=..., work_dir=...)`
(`waveflow/build/system_xsi.py`) compiles and elaborates it with only its own sources, and an XSI gate
runs that for every markov and mm_fir sub-top. A per-device synthesis or an IP is built from this
unit.

### Reading it

| field / method | answers |
|---|---|
| `kernels` | each csynth'd module in the top (`KernelRtl`): its module name, instance, ports, and the pysim module it realizes |
| `modules` | the csynth'd modules, each once: whose RTL a workspace compiles |
| `si`, `host_axi` | each crossbar master slot. A master outside the cut (the host) is a top port `s<k>_axi`; one inside is a wire `si<k>_axi` |
| `mi` | each crossbar slave slot (`MiSlot`): a view, an adaptor's views, the memory, or a padded stub |
| `nets` | each stream link (`StreamNet`): its FIFO depth, the side-channel facts its tie-offs follow, and which sub-top drives and reads it |
| `irqs` | the interrupt lines leaving the cut, as top outputs `irq_<view>` |
| `subs` | the [devices and the memory](#devices), each a sub-top (`SubTop`) |
| `instances()` | the inventory: every RTL instance (`RtlInstance`) with its module, parameters, sources, identity, owning pysim object, sub-top and path |
| `leaf_sources()` | the hand-written leaves the top uses, each once: its compile list |
| `probe_expr(p)`, `probe_home(p)` | a timing probe named by a pysim endpoint, resolved to a Verilog expression and to the module it is computed in |

```python
spec.modules        # ('markov_gen', 'markov_chain', 'mm_queue_writer_64_128', 'mm_credit_writer_64')
[s.inst for s in spec.subs]                  # ['u_gen_sub', 'u_chain_sub', 'u_mem_sub']
len(spec.instances())                        # 15
```

**Every consumer reads the same spec.** None of them re-walks the graph or parses the Verilog:

| consumer | reads the spec for |
|---|---|
| `render_verilog` / `render_system_top` | the Verilog itself |
| `system_tb_spec`, `render_system_tb` | the XSI harness: which host model binds to which top port |
| `system_synth` ([Systems](../resource/systems.md)) | joining Vivado's per-instance report to the inventory, by path |
| `estimate_system` | pricing each instance from the record store |
| `measure_instances` (`rtl_ooc`) | the leaves to synthesize out of context |

So the inventory and the rendered top cannot disagree (a test checks that every inventory instance is
rendered exactly once, in the module its `sub` names). On a build DAG, the `system_rtl` step holds the
spec as `.spec`, and the later steps read it from there. A single composite kernel has the same
arrangement one level down: `composite_top_spec` returns its `TopSpec`.

### Devices: the top's sub-tops {#devices}

The top is not flat. Every instance is in one of three places, and each **device** and each **memory**
is rendered as its own Verilog module (a *sub-top*) that the system top instantiates. The places are the
system's own DUT tree (`HwSystem.dut()`): `build_mm_device` returns each device as a composite holding its
kernel, views and adaptor, so the walk reads a kernel's device off the tree rather than inferring it.

| place | holds | in markov |
|---|---|---|
| a **device**, one per kernel inside the cut | the kernel, its views or adaptor (front and leaves), the bus writers that act for it, and any FIFO between them | `u_gen_sub`: `MarkovGen`, its adaptor's front and two views, the forward writer and its FIFO. `u_chain_sub`: `MarkovChain`, its front and two views, the credit writer |
| the **memory** | its front, BRAM window and RAM | `u_mem_sub` |
| the **system top** | the crossbar, padded MI slots, and any stream link whose ends are in two different devices | `u_xbar` |

**Where a bus writer goes.** A writer acts for the kernel whose stream feeds it. A routed credit
stream's **forward writer** carries the sender's data, so it sits in the sender's device. Its **credit
writer** returns the receiver's credit, so it sits in the receiver's device. Neither sits with the
crossbar. `MmCreditStreamIF` makes that placement itself, when it is bound: it adds each writer, and the
stream feeding it, to the device of the kernel it acts for. Move a writer to another device and the
sub-tops follow the tree.

What crosses a sub-top's boundary is clock, reset, its AXI4 slots (its MI slot as a slave and each of
its masters' SI slots), its interrupt lines and its timing probes. In a memory-mapped system nothing
else crosses: every link between devices goes over the bus. A sub-top's ports are named for the nets
they carry, so every instance name, net name and tie-off is what a flat top would have had, and each
instance's hierarchical name is `spec.instances()[i].path` (`u_gen_sub/u_u_fwd_wr`). A probe on a net
inside a device is computed there and passed up as a port; a probe on a bus master's SI is computed in
the top.

The same modules serve every consumer of the top: XSI simulates them, [`system_synth`](../resource/systems.md)
synthesizes them globally (its per-instance report reads one level deeper, under each sub-top), and a
per-device synthesis or IP packaging reads one sub-top at a time
([plans/ip_packaging_routes.md](../../../plans/ip_packaging_routes.md)).

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
   `<work>/<top>.v`: one file holding each [device's sub-top module](#devices), then the system top.

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

1. `harness`: each testbench participant's C++ twin (and a host's generated endpoints) bound to the
   top's ports (`system_tb_spec`, `render_system_tb`);
2. `xsi_run`: `xvlog` / `xelab` build the RTL that `rtl.json` lists, `g++` builds the testbench, and
   the run executes. With a host, its report is parsed into `<work>/report.json` and its traces land
   in `<work>/traces/`. Without one, the run starts from a fresh copy of the scenario
   (`<work>/xsi_run/`), and what the participants recorded is gathered into `<work>/traces/`.

**Tracing** (`add_system_steps(..., trace=True)`). The run also elaborates a second top whose
`$dumpvars` dumps each kernel's scope at level 1 inside the system top: the kernel's ports, and for a
wrapped kernel the wrapper's wires to its memories. It writes `<work>/<top>_trace.vcd`, and
`<work>/hazards.json`, each wrapped kernel's hazard manifest with the `scope` its nets live under.
`waveflow.utils.bram_trace` resolves names under that scope, so a waveform check reads a system run
as it read a kernel run. `SystemTopSpec.scope_of(kernel)` gives a kernel's scope for any other
reader.

### compare {#compare}

What the testbench recorded, RTL against pysim -> `<work>/compare.json` (the mismatches; empty:
pass). With a host, every endpoint's trace is compared file for file. Without one, every recording
is compared on its words: each sink's `out_bundle`, each memory's `dump_segs`. A participant whose
recording pysim cannot match by construction says so (`recording_check = "example"`), and the
example checks it instead. A mismatch fails the step and names the recording, the participant that
records it, and the first word that differs.

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
