---
title: Simulating at RTL
parent: Flow Overview
nav_order: 5
audience: python
api: [HwSystem, DutTop, harness, KernelTask, add_system_steps, run_dag_cli, SystemTopSpec, SwHost]
summary: "The third step of the flow: the DUT as hardware. An HwSystem(sim, dut) is a simulation with one module named the DUT -- what is synthesized -- and a testbench of everything else, each part with an RTL twin (a stream driver's AxisMaster, a host's C++ program); anything without one stays in pysim. Each module's HLS body behind kernel_task() -- hand-written or generated, one body for the module alone and inside a composite (framed ports). The system flow on a build DAG: codegen, csynth, system_rtl, scenario, pysim, system_xsi, compare; run under XSI cycle by cycle from the same scenario pysim ran. What becomes what: Vitis HLS, hand-written framework RTL, vendor IP, the testbench."
---

# Simulating at RTL

The second simulation runs the **hardware**: the Verilog of the DUT, cycle by cycle, in Vivado's
simulator, with everything around it as C++ models that speak its pins. It is slower than pysim and
holds less, and it answers the question pysim cannot: does the hardware do what the Python does.

## The system: `HwSystem(sim, dut)` {#the-system}

An RTL simulation starts from an **`HwSystem`**: a `Simulation` with one module named the **DUT**. The
**testbench** is everything else, derived, never listed. Build the modules on one simulation, wire them,
and name the DUT:

```python
sim = Simulation()
kernel = PolyKernel(name="poly_kernel", sim=sim, clk=clk)
host = PolyHost(name="host", sim=sim, cmds=cmds, clk=clk)            # the testbench: a SwHost
device = build_mm_device(kernel, sim=sim, clk=clk, mem_dwidth=W)     # the kernel's views and front
xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=1, nports_slave=len(slaves), ...)
...                                                                   # bind, address map, interrupts
dut = DutTop.of("poly_mm_dut", sim, comps=[device], ifs=[xbar])
sysm = HwSystem(sim, dut, run_until=host.done, check=check)
```

([The polynomial behind AXI-MM](../../examples/stream_kernel/memory_mapped.md).) For one module the
whole of it is one line, because the testbench is generated: `harness(PolyKernel, workload="test")`.

The constructor checks the rules, each a refusal that names what to fix:

- the DUT is a root of this simulation;
- **every testbench part has an RTL twin** -- a stream driver's `AxisMaster`, a sink's `AxisSlave`, a
  memory model, a host's C++ program ([BFM testbenches](../build/bfm.md));
- no DUT port is left unbound.

The second rule is the line between the two simulations: **anything can join a `Simulation`; only what
has an RTL twin can join an `HwSystem`.** A wireless channel model has no twin, so a study with one is a
pysim study, and its RTL simulation draws the DUT and testbench where everything has one.

The details -- every argument, `participants()`, `cut()` -- are in
[The system: `HwSystem`](../build/xsi_system.md#the-system).

## What becomes what

![A generic system. A host (a SwHost, tan) is a bus master on an AXI crossbar (purple, vendor IP). The crossbar's slaves are three sub-tops, each in a dashed box. Device A holds a front with views (green, hand-written RTL), kernel A (blue, Vitis HLS) and a bus writer (blue) that acts for kernel A. Device B holds a front with views and kernel B, which also masters the bus through m_axi. The memory holds a front with a window and the RAM (both green). A solid box around the crossbar and the three sub-tops is the DUT; a dotted box around everything, host included, is the HwSystem. An interrupt line runs from device A's front across the DUT boundary to the host. A legend names the build step that realizes each colour: csynth for blue, system_rtl for green, xbar_ip for purple, system_xsi for tan.](../flows/figures/system_cut.svg)

| colour | what it is | how it becomes RTL |
|---|---|---|
| **blue** | kernels, and the bus writers a routed stream brings | an HLS body, synthesized by Vitis (`csynth`) |
| **green** | memory-mapped fronts and views, memories | framework Verilog, as is (`system_rtl`) |
| **purple** | the crossbar | AMD's `axi_crossbar`, generated from the pysim address map |
| **tan** | the testbench | C++ models, never synthesized (`system_xsi`) |

The Verilog is derived, never written: the build walks the DUT to a `SystemTopSpec` -- every instance,
crossbar slot, stream net and port -- and renders the top from it, a sub-top per device
([The system top](../build/xsi_system.md#the-spec)).

## A module's HLS body

Each kernel needs its HLS body: generated from `run_iter`, or **hand-written** and named by
`kernel_task()`. The polynomial's is hand-written, a C++ twin of `run_iter` read as the Python reads:

```python
def kernel_task(self):
    return KernelTask("poly_kernel_task", "poly_kernel_task.h", ("s_in", "m_out", "m_status"),
                      template_args=(W,))
```

1. read the command (`hdr.read_axi4_stream<W>(s_in)`, a generated reader), write the `Resp` header;
2. the samples: one word per iteration at II=1, its lanes by the generated float32 lane routines,
   TLAST on the last result; an early TLAST ends the loop;
3. write the `Status`; after an error, set a `static` flag under `#pragma HLS reset`, and return at
   once on every later firing.

The generator writes the top around it -- three `axis` ports, `ap_ctrl_none`, one `hls::task` -- and
its csynth script; nothing about the top is hand-written. A body that names no word type
(`KernelTask(..., framed=ports)`, `streamutils::read_msg` / `write_msg`, the lane routines'
`read_boundary_lane`) serves the module **alone and inside a composite**, so each stage of a pipeline is
synthesized once and run at RTL in its own harness and in the whole
([One body, wherever it sits](../comp_codegen/freerunning_override.md#framed)). Before shaping any loop:
[Design patterns for loop optimization](../vectorization/hls/loop_optimization.md).

## The system flow

One call puts the RTL simulation on a build DAG, after the example's own `codegen` (its headers, lane
routines, tops and bodies):

```python
dag = BuildDag()
dag.add(StreamKernelCodegenStep(name="codegen"))
add_system_steps(dag, harness(PolyKernel, workload="test"), work_dir="xsi_work", top="stream_kernel_top")
run_dag_cli(lambda: dag, description="...", default_through="compare", root_dir=HERE)
```

| step | what it does |
|---|---|
| [`csynth`](../build/xsi_system.md#csynth) | Vitis HLS, once per top; fresh while its sources are unchanged |
| [`system_rtl`](../build/xsi_system.md#system-rtl) | the crossbar IP if there is a bus, then the Verilog top derived from the DUT |
| [`scenario`](../build/xsi_system.md#scenario) | what the testbench plays, written once, and the run's cycle bound |
| [`pysim`](../build/xsi_system.md#pysim) | the system in Python, from that scenario |
| [`system_xsi`](../build/xsi_system.md#system-xsi) | the RTL under XSI with the testbench's C++ twins, from the same scenario |
| [`compare`](../build/xsi_system.md#compare) | what the testbench recorded, RTL against pysim, word for word |

`run_dag_cli` gives every example script `--through <step>`, `--status`, `--force`, and an exit status
that is 1 when a step fails. A build that changed nothing does no work: every step is fresh while what it
reads is unchanged ([freshness](../build/xsi_system.md#system-freshness)). csynth targets a Zynq-7020 at
100 MHz unless a top's script names a part and clock (`render_tcl(top, part=..., period_ns=...)`).

What `compare` proves, and what it does not, is the next page.

## Steps up

**A pipeline: [block_eq](../../examples/block_eq/system_flow.md).** Four systems on one DAG: each stage in
its harness, the composite, and an error scenario -- each its own `add_system_steps` with a `prefix`, all
sharing one `codegen` and the stages' csynth.

**A bus system: [markov](../../examples/markov/xsi.md).** `markov_system(...)` is an `HwSystem` whose DUT
is two devices, a memory and the crossbar; its host is a `SwHost` whose C++ twin runs the same two
threads under XSI on generated endpoints. `compare` checks the host's bus traces, RTL against pysim, byte
for byte -- and the run's exact cycle count is a gate of its own ([XSI system simulation](../build/xsi_system.md)).

Next: [Functional verification](verification.md).
