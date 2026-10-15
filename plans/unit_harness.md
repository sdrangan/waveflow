# Plan: a unit harness per `SynthUnit` -- verification, timing and calibration, one module at a time

**Status:** drafted 2026-10-12; revised 2026-10-13 (Stage 0); paused 2026-10-14 for
`plans/pipelined_ops.md`, which gave modules a way to state their timing ([timed
phases](../docs/guide/timing_model/timed_phases.md), merged PRs #274, #275).  **Revised again
2026-10-14, with the user, before any code:** the unit is a **test subsystem** (since named `SynthUnit`), not only a device;
**D9 is decided**; the first vehicle is a **free-running poly** -- first as a stream-only kernel, then
as a memory-mapped device -- and markov follows as the first multi-kernel system.  That example also
becomes the docs' first example, the start of retiring the host-activated examples.  The original Stage
0 (markov and mm_fir, port by port) is kept as a record at the end: it is the inventory the markov stage
needs.  **Stage 1 merged 2026-10-14 (PR #277).**  **Named the same day, with the user:** a test subsystem
is a **`SynthUnit`**; the system is flat (a testbench and a DUT), the DUT is the tree of
`SynthUnit`s, and a harness is itself an `HwSystem` whose DUT is one `SynthUnit` (D1, D2, D11).

## Motivation

Calibrating a module's timing used to mean running the **whole system** and extracting the module's
behaviour from a trace (the memcpy arc: `AddVcdTopStep`, `TraceManifestStep`, `waveflow/utils/trace.py`).
It works, but:

- **the system must be arranged so the right signals arrive at the right time** -- a module's timing is
  only as measurable as the scenario around it makes it;
- **every calibration point is a system run**, so a module's parameter sweep pays for its neighbours;
- **a module cannot be developed alone**: no place to verify and calibrate it before the system exists.

The better design was always one harness per module: function, timing and resources, for that module
alone.  It was not built because Vitis cosim cannot unit-test a free-running task with an `m_axi` port
(`reference-hls-task-no-maxi`).  That wall is gone -- XSI runs free-running modules with `m_axi` ports
through the framework's bus models, the cut is a build choice (`add_system_steps(..., inside=...)`),
re-runs are nearly free (`plans/incremental_xsi.md`), and resources are measured per device already
(`system_ooc.synth_ooc_link`).

And since `plans/pipelined_ops.md` a module **states its own timing** in a form a harness can measure:
timed phases, spans between the first and last beats of transfers at its own ports, calibrated from a
trace of those ports with the pairing and the features supplied by pysim (`waveflow/calib/phases.py`).
What was missing is the harness: a way to run one module alone, in pysim and at RTL, on a workload that
produces the features, and to observe the ports its phases name.  Poly's first calibration had to be a
hand-written wrapper around its own Vitis flow (`examples/stream_inband/poly_calibrate.py`); the harness
is what makes such wrappers unnecessary.

## What a `SynthUnit` is

A **`SynthUnit`** (a *test subsystem*) is a subgraph of a system's DUT that admits a harness (the
user's definition, 2026-10-14; named the same day).  It must satisfy:

1. **Its function and its timing are observable at its boundary** -- the events of its timed phases are
   on its own ports.
2. **It is hierarchical** -- a `SynthUnit` is made of `SynthUnit`s.
3. **Its resources and timing are either calibrated from its boundary and its synthesis reports (a
   leaf), or composed from its children's (a composite)** -- leaves are calibrated, composites are
   composed and then *verified* by property 1, never fitted.

**The system is flat; the DUT is the tree.**  An `HwSystem` is one experiment -- a testbench (hosts,
drivers, sinks, memories outside the cut) and a DUT -- so it cannot nest: a system inside a system would
bring its own testbench.  The hierarchy is inside the DUT: its root is the DUT itself (`HwSystem.dut()`,
the `DutTop`), its leaves are kernels and the crossbar, and between them are composites.  A **harness is
an `HwSystem`** whose DUT is one `SynthUnit` and whose testbench is stand-ins at that unit's
boundary -- Stage 1's `PolyKernelSystem` is exactly that for the leaf `poly_kernel`:

    Simulation =  any SimObjs                                     (pysim only)
    HwSystem   =  testbench participants  +  DUT                   (pysim and RTL)
                  (each lowerable: bfm_model,    └ tree of SynthUnits
                   xsi_model or a C++ twin)        (DUT -> composites -> leaves)
    harness(node) = HwSystem(testbench = stand-ins at node's boundary, DUT = node)

The name (the user, 2026-10-14): first `HwSubsystem`, renamed **`SynthUnit`** -- *synthesizable*, so on
the DUT side, and the *unit* the harness, the calibration and the records are about; not "...System",
since a system is a testbench plus a DUT.  The objects outside the DUT keep their existing name,
**testbench participants**: what makes one is that it can be lowered to the RTL testbench (a
`bfm_model()`, an `xsi_model()`, or a C++ twin like the `SwHost`).  An object that cannot be lowered
belongs in a plain `Simulation`, not an `HwSystem` -- a rule nothing checks yet (the XSI build is where
it fails today).

**Roles, not classes** (the user, 2026-10-14, keeping an earlier convention): one `SimObj` class may be
lowered several ways -- inside the DUT one day, on the testbench the next -- so neither side is a class
an object inherits.  A *testbench participant* is any `SimObj` outside **this** system's DUT, required to
have a testbench lowering (today an XSI one); a `SynthUnit` is **any `HwModule` in this system's DUT
tree** -- its children, boundary and identity are the module's own (D11), not a base class and not a
second tree.  Both are positions, decided by where the cut is drawn.

**A `SynthUnit` is always inside the DUT; nothing on the testbench side is one** (the user,
2026-10-14).  Testbench-side timing has two other sources: **stand-ins** -- drivers, sinks, the remote
queue -- are never calibrated, they set the environment by policy (D4); **real things outside the DUT** --
the `m_axi` path to DDR, the host processor -- are **platform** models (the bus model, the CPU model on
gem5), calibrated once per platform and shared by every design on it, not keyed by a design's identity.
The cut can move: a memory outside the DUT in one system is a leaf of the tree in another that puts it
inside.

It is independent of packaging: a kernel is a `SynthUnit` but rarely an IP; a **device** -- in
`ip_packaging_routes.md`, the group packaged as one IP -- is a composite that happens to be packaged
as one, a packaging fact about a node, not a separate kind of unit.  The
crossbar is a leaf of its own, calibrated once per configuration and composed into its parents.  A
child's boundary is observed **in place** whenever its parent runs (its declared events are logged at
any level); only a `SynthUnit` that needs **control** -- a workload sweep, a hostile environment,
development before its parent exists -- needs a harness of its own.

For one `SynthUnit`, the harness gives:

| | how |
| --- | --- |
| **function** | its golden model and scenarios, bit-exact in pysim and at RTL (XSI) |
| **timing** | its timed phases, measured at its own ports (RTL transfers paired with pysim's records), fitted, filed under its identity |
| **resources** | read, not measured: a leaf's csynth report or a device's out-of-context record, under the same identity |
| **robustness** | hostile environments on demand: a neighbour that stalls, starves credit, or answers late |

## Decisions

- **D1 -- the unit is a `SynthUnit`** (was: the device).  A device of `ip_packaging_routes.md` is a
  composite that is packaged as one IP; the kernel inside it is the leaf below, the DUT the root above.
  One identity keys a `SynthUnit`'s verification, timing records and resource record.
- **D2 -- a harness is an `HwSystem`, not a new kind of testbench.**  Its DUT is one `SynthUnit` (the
  cut), its testbench the stand-ins at that node's boundary (D3), and everything else is the machinery
  that already lowers a system: `system_top_spec`, the XSI workspace, the host's C++ twin.  No second
  testbench framework and no harness class: **every harness is just an `HwSystem`** (the user,
  2026-10-14), so verification and calibration operate on `HwSystem`s, as they already do
  (`add_system_steps`, `add_calib_steps`).  What D11 adds is a **factory**, node -> `HwSystem`.  A
  harness's testbench generally differs from the full system's: it drives the node's boundary, which
  for an inner node includes edges internal to its parent (in Stage 2 the kernel leaf's harness keeps
  Stage 1's driver and sinks, while the system's testbench is the host behind the crossbar).
- **D3 -- generic stand-ins for cut-away neighbours.**  A unit harness should not need a hand-written
  model of every neighbour.  The framework provides generic stand-ins: stream drivers and sinks (exist),
  a remote queue that accepts a writer's bursts and returns credit by a policy, a memory, an interrupt
  sink.  A neighbour's own `bfm_model()` may be used instead.
- **D4 -- the environment is a parameter.**  A stand-in's policy (always ready, a fixed latency, a stall
  pattern, credit starved) is part of the scenario (`run.json`), so one compiled harness serves
  calibration (contention-free) and robustness (hostile) alike.
- **D5 -- timing is measured at the ports, as declared phases.**  A `SynthUnit`'s timing is its timed
  phases; their events are its ports' first and last beats; the RTL side reads them from the harness's
  port trace or its bus models' timestamps, pysim logs them itself.  No internal net, no manifest.  (Was:
  "cycles per firing from port timestamps" -- the phases are what made that precise.)
- **D6 -- the harness does not re-measure resources.**  It reads the record a leaf's csynth or a
  device's out-of-context synthesis already filed.
- **D7 -- records under identity, library first.**  Fitted phases file in the calibration store under the
  `SynthUnit`'s identity; a measurement is content-addressed (a repeat is a cache hit); failures are
  rows; batches go through `SweepRunner`.
- **D8 -- the composition check is part of the plan.**  A composite's pysim, run on its children's
  calibrated phases with no fitting of its own, must reproduce its RTL run within tolerance; when it
  does not, the difference is undeclared time or a wrong decomposition, and that is the finding (poly
  showed both kinds: `plans/pipelined_ops.md`, the Stages 2-5 record).
- **D9 -- DECIDED 2026-10-14: a harness's boundary is the `SynthUnit`'s own boundary.**  For a
  composite packaged as one IP (a device), its sub-top's ports -- its AXI4 slave groups, master groups and interrupt lines, the ports its
  IP has -- with **no crossbar inside**: the crossbar is a sibling leaf, calibrated on its own and
  composed by the parent, so a device's measured timing is the device's own.  What this leaves is
  implementation, from the Stage 0 record: the host bound straight to a device's slave group(s) (today
  it binds only through a crossbar), a small testbench fan-out for a device with several slave groups,
  and stand-ins placed at the addresses the device's writers are tied to.
- **D10 -- calibration is a framework step.**  A harness's calibration is generated from the declared
  phases (`phases.py`: ports, RTL transfers, pairing, fit) plus **one hand-written hook**: a workload
  that produces given feature values.  No per-example wrapper like `poly_calibrate.py`; one scenario can
  carry every feature value (poly's `calib` carries nine lengths), so a calibration is one pass, not an
  outer loop -- a sweep over *build* parameters is an outer loop, and each point is its own identity.
- **D11 -- a `SynthUnit` is a role of an `HwModule`, and the unit tree is the module tree** (the user,
  2026-10-14).  An `HwModule` is already hierarchical (SystemC-style): its sub-modules, its internal
  interfaces, its boundary endpoints and its identity (`structure_signature` canonicalizes the whole
  sub-graph, so `identify_instance` already keys a composite -- checked on markov: `gen_dev` is
  `mm_slave_device-b10db324`).  A second tree restating children, boundary and identity would only drift
  from it, so there is no `SynthUnit` class: **any `HwModule` in a system's DUT tree is a `SynthUnit`**,
  and its children, boundary and identity are the module's own.  To group differently, change the
  module tree (wrap modules in a composite `HwModule`).  **Leaf or composite is decided by data**: a
  module that declares timed phases is calibrated; one that does not is composed from its children (so a
  csynth'd kernel built of sub-modules, like markov's `MarkovChain`, is a leaf when it declares phases).
  The one exception is the **crossbar**, an `Interface` with a config identity (`AxiXbarConfig`): a leaf
  with its own records, special-cased until it is a module.  What is new is small: a factory
  `harness(sysm, module) -> HwSystem` (the module as DUT, stand-ins generated at its boundary by port
  type) and a workload hook for a module's harness -- Stage 2's first piece of work.

- **D12 -- an `HwSystem` is a `Simulation` plus one named DUT** (the user, 2026-10-14).  It **has** a
  `Simulation` (every `SimObj`, registered on construction) and names one root `HwModule` as its DUT;
  its testbench is **derived** -- every `SimObj` in the simulation outside the DUT -- never listed:

      sim = Simulation()
      dut = MarkovDut(name="markov_dut", sim=sim)       # gen_dev, chain_dev, mem; xbar + link inside
      host = MarkovHost(name="host", sim=sim, jobs=jobs) # the SwHost
      host.bind(dut.s_axi)
      sysm = HwSystem(sim=sim, dut=dut)

  The constructor checks: the DUT is registered in the simulation; it is a root (no parent -- a
  sub-module's harness gets a fresh copy instead); every testbench participant has a testbench lowering
  (`bfm_model()` / `xsi_model()` / a C++ twin); every DUT boundary endpoint is bound.  Not *derived from*
  `Simulation`: that is the pysim engine (single-use, its clock only advances), while an `HwSystem` is
  lowered to pysim **and** RTL and rebuilt per run.  `HwSystem` becomes concrete -- the per-example
  subclasses' hooks move to where the knowledge is (scenarios to the participants, as the `SwHost`
  already writes its own; workloads to the modules, D10) -- and a named DUT class replaces the anonymous
  `DutTop.of(...)` where the DUT deserves an identity (`DutTop.of` stays for quick systems).  The harness
  factory is the same shape: a new `Simulation`, a fresh copy of the module from its class and
  parameters (as `system_top` already builds one for csynth), stand-ins at its boundary,
  `HwSystem(sim, dut=copy)`.  Migration: the new form beside the existing subclasses (about 15), which
  move one at a time -- the free-running poly first, markov in Stage 4.

## The vehicle: a free-running poly

The first harness is built on a new example (directory to be named; proposed `examples/stream_kernel/`,
taking `stream_inband`'s name once the host-activated example is retired): **poly's arithmetic as a
free-running kernel**.

- **The kernel** is a `FreeRunMod` whose firing is one command: the in-band header (with its
  coefficients), the samples, the response header, the results.  Poly's two timed phases, unchanged:
  the response after the header (sequential) and the samples-to-results loop (pipelined).  A new
  hand-written HLS task body: the same arithmetic as `poly_body_impl.tpp`, one command per firing,
  `ap_ctrl_none`.
- **Errors without a status register**: the response carries the status -- `tx_id`, an error code (OK,
  or TLAST came early) and the samples processed -- and the kernel answers what it read and goes on to
  the next command.  Everything poly reports stays observable on its streams (the command-response
  pattern, `docs/guide/patterns/command_response.md`).
- **Two wirings of one kernel**, as markov and mm_fir have: `link="direct"` -- streams to a stream driver
  and a sink -- and `link="mm"` -- the kernel behind queue views on an AXI-MM slave, driven by a host of
  two `SwThread`s (a writer and a reader, on interrupts, no polling).
- **A cross-check, not only a test**: poly's host-activated phases were fitted from Vitis cosim (response
  4, latency 34, fill `nwords - 1` at 32 bits).  The free-running kernel's, fitted in the harness under
  XSI, should agree within a cycle or two, and every difference needs a reason (a free-running top has
  no `ap_start` / `ap_done`).  The first harness is checked against an independent measurement of the
  same arithmetic.

## Stages

**Stage 1 -- the leaf: free-running poly on streams.  DONE (PR #277, record below).**  The example with `link="direct"`: golden,
kernel (`run_iter` + its HLS task body), its system (`GraphSystem`: a stream driver, the kernel, a sink),
its build DAG and XSI gate.  The leaf harness: run the kernel alone at RTL with its ports traced (the
system flow's traced run, `SystemXsiStep(trace=True)`), decode its declared ports' transfers, pair with
pysim, fit -- through a framework calibration step (D10), the workload hook being "commands of these
lengths".  Gate: bit-exact in pysim and at XSI on every scenario (errors included); the phases fitted in
the harness with held-out lengths exact; the fit compared with the host-activated cosim fit, every
difference explained; no example-specific calibration wrapper.  Two loose ends to close: `calib_phases`
should consume the calibration run's `compare` (its scenario ran at RTL but was not compared bit-exact),
and one cycle of the latency difference (the task's loop entry) is not yet accounted for.

**Stage 2 -- the composite: the same kernel behind AXI-MM.**  First D11: the `SynthUnit` tree and
harnesses generated from a node, Stage 1's harness re-expressed as the leaf node's.  Then `link="mm"`:
queue views (command/sample queue in, response/result queue out) on one front, a `SwHost` of two threads.
The composite (kernel + views + front, packaged as one IP) is the harness's DUT under D9: its sub-top as
the top, the host bound straight to its slave group, no crossbar.  Gate: bit-exact; the composite's
timing **composed** from the kernel leaf's phases and the views' with no fitting, checked against its RTL
(D8); then the root (the composite and the crossbar leaf, `AxiXbarConfig` records) composed and checked
against its RTL.

**Stage 3 -- calibration as a step, records under identity.**  The framework calibration step of Stage 1
generalized: one `XsiSnapshot` per configuration, the workload hook's scenario in `run.json`, the fit
filed under the `SynthUnit`'s identity (D7), a repeated calibration with no change running no
simulation.  Grid discipline: at least three values a feature, interior points held out.  Gate: a
repeat is a cache hit; a held-out point within tolerance; the filed record found from the identity alone.

**Stage 4 -- markov: the first multi-kernel system.**  Declare phases on markov's two stream kernels (the
generator and the chain's core), replacing their hand-fitted per-chunk overheads (7 and 15); calibrate
each as a leaf; the composites with the stand-ins of the Stage 0 record (the remote queue, the credit loop);
the composition check against markov's RTL (1870 cycles), with no system-level fitting.  Gate: within the
existing 5% from composition alone -- or a written account of what the decomposition missed.

**Stage 5 -- robustness.**  Hostile stand-in policies (stall, starved credit, late answers) swept per
harness; any hang or mismatch is a finding.  Gate: the policies run on every harness; what they find is
fixed or recorded with a reproducer.

**Stage 6 -- incremental, end to end.**  Change one `SynthUnit`'s parameter: only its harness rebuilds,
only its synthesis re-runs, its parents' estimates update from records.  Gate: the events show no work
for unchanged units.

**Stage 7 -- docs.**  The free-running poly becomes **the first example in the docs** (the user,
2026-10-14: the host-activated examples are to be retired over time).  It teaches, in order: the
command-response protocol, the golden, the kernel and its HLS body, the system flow, timed phases, the
harness and calibration, then the memory-mapped composite.  A guide page on module-level development:
`SynthUnit`s, harnesses, stand-ins and policies, calibrating a leaf, composing and verifying a parent.
Name steps and link them.

## The host-activated examples

The docs open today with three host-activated examples (`basic_vec`, `regmap`, `stream_inband`).  The
direction (the user, 2026-10-14) is to retire that flow over time.  This plan's part: the free-running
poly takes the first place in the docs (Stage 7), and `stream_inband`'s pages point to it.  What becomes
of `basic_vec` and `regmap` -- moved to a legacy section, or rewritten free-running -- is its own decision,
not taken here.

## Relationship to the pipelined-ops migration

`plans/pipelined_ops.md` Stage 6 retires `get_pipelined` / `write_pipelined` / `read_pipelined`.  Its users
are framework blocks with calibrated timing and cycle-exact gates (the mem streams, BRAM, locked memory,
the RF shot buffers, the SSR FFT), most of them needing memory-mapped, BRAM or stream-of-blocks events.
That migration runs **after** this plan's Stage 3, one block at a time with the harness: declare the
block's phases, add the event kind its first user needs, calibrate it in its harness, check its existing
gate.  The harness is what makes each migration a re-fit rather than a hand-calibrated guess.

## Record: Stage 0 findings (2026-10-13), the markov and mm_fir inventory

**The cut today.**  `inside=` takes kernels and memories, not devices (`SystemTopSpec.from_system(s,
inside=[s.gen_dev])` raises: "names no csynth'd module").  With markov's generator alone inside
(`inside=[s.gen]`) the walk refuses: "crossbar slave_1 (MemSlaveAdaptor 'chain_mm') is not the device
of a kernel inside the cut".  A bus neighbour cannot be cut away today; only the host (a bus master
outside the cut, rendered as an `s<k>_axi` top port) and a kernel's `m_axi` to no crossbar
(`maxi_ext`, an `m<k>_axi` top port facing a `FlatMemory`) can.

**mm_fir needs nothing new.**  It is one device and a host; its system run (`618` / `611` cycles,
both topologies) already *is* its device's harness.  It is the reference that Stage 2's generated
harness must reproduce.

**markov's devices, port by port** (the IP boundaries of `ip_packaging_routes.md` Stage 3; C++
models in `waveflow/build/xsi/`):

| device | port | what answers it in a harness | exists? |
|---|---|---|---|
| gen | `mi0_axi`, view `qcmd` (queue in) | host bus master (`AxiMmMaster`) + `MmQueueWriter` | yes |
| gen | `mi0_axi`, view `u_crd` (credit in) | a master returning credit by a policy (a plain bus write, `BusRw`) | **missing**: the policy |
| gen | `si1_axi`, forward writer -> chain's `qu` | a slave that takes the bursts and records the stream | **missing**: a remote-queue sink |
| gen | `irq_gen_qcmd` | `IrqPin` | yes |
| chain | `mi1_axi`, view `qu` (queue in) | a master feeding `u` bursts within the credit it holds | partly: `MmQueueWriter`, no credit |
| chain | `mi1_axi`, view `qresp` (queue out) | host + `MmQueueReader` | yes |
| chain | `si2_axi`, credit writer -> gen's `u_crd` | a slave taking the credit writes, feeding the feeder above | **missing**: a credit sink |
| chain | `si3_axi`, memory writer | `FlatMemory` + `AxiMmWriteSlave` | yes |
| chain | `irq_chain_qresp` | `IrqPin` | yes |
| mem | `mi2_axi` | host + `MmBusReader` | yes |

So D3's generic stand-ins are, concretely, **two**: a **remote queue** (an AXI slave that accepts a
writer's bursts into a recorded stream) and the **credit loop** around it (a sink for credit writes,
and a feeder or returner that honours them by a policy, D4).  Both are testbench C++ beside
`xsi_mm_host.h`; neither needs RTL.  Each needs a pysim twin so the harness runs in pysim too.

**mm_fir's device** (one device, host only; rendered from `mm_fir_build.system(...)`):

| topology | device ports | what answers them in a harness | exists? |
|---|---|---|---|
| `per_view` | `mi0_axi` .. `mi3_axi` (views `regs`, `qin`, `qout`, `qresp`, one front each) | the host's one bus master, fanned out to four slave groups by address | host yes; **the fan-out is new** under D9 |
| `one_front` | `mi0_axi` (all four views behind one front) | the host's bus master | yes |
| both | `irq_qin`, `irq_qout`, `irq_qresp` | `IrqPin` | yes |

The memory sub-top (`markov_top_mem`) is `mi2_axi` only: host + `MmBusReader` / `BusRw`, nothing new.
**Gate met:** every port of the three devices (and the memory) maps to an existing model or to one of
the two named stand-ins (the remote queue and the credit loop), plus, under D9, a fan-out.

**Five facts the inventory turned up**, each one Stage 1 has to respect:

1. **A device's module depends on its system's crossbar.**  Its AXI4 groups take the crossbar's ID
   width: markov's sub-tops have `[1:0]` IDs (4 masters), mm_fir's 1 bit.  The same kernel in another
   system renders another module.  For D9 this is the right thing (the harness runs the module the
   system runs), but the device's identity (D1, D7) then has to include the ID width -- or the sub-top
   must stop depending on it.
2. **A device embeds its neighbours' addresses.**  The forward writer's `target` is a tied scalar,
   `32'd2048` = the chain's `qu` at `0x4000` (the credit writer's: `512` = the generator's `u_crd` at
   `0x1000`).  So the generator's RTL is configured by the system's address map, and a harness must
   place the remote-queue stand-in exactly there.  It is part of the device's identity, as a tie is.
3. **A front decodes only the low `LAW` bits** (`axi_slave_front.v`: `addr_q <= AWADDR[LAW-1:0]`).
   A sub-top alone aliases each view modulo its window; nothing in it knows its base.  A harness can
   keep the system's addresses (the host twin's generated map stays valid) only if whatever routes them
   -- the crossbar today, D9's C++ router -- decodes by the system map.
4. **The host is bound to a crossbar today.**  `discover()` finds a host only as a master on a bus
   *inside* the DUT, and `system_tb_spec` binds its bus master only to an `s<k>_axi` slot.  Under D9
   (no crossbar in the harness) the host faces the device's `mi<k>_axi` groups directly: a binding
   that does not exist yet, in pysim (a testbench-side bus, latency 0, as pysim's twin of the router)
   and in the harness generator.
5. **Admission comes from the response.**  `MarkovHost`'s `slots` (MAX_IN_FLIGHT) are released by the
   reader on a response -- which the generator alone never produces.  A unit host for the generator
   frees a slot when the remote queue has taken a job's last word (the stand-in says so), or runs
   without admission and lets the command queue's room interrupt pace it.

**The unit hosts.**  Each is `MarkovHost` cut down, plus the stand-ins' threads -- on **one bus
master**: a credit returner that is a thread of the host shares its `AxiMmMaster` (C++ host threads
already share `bus_`), so no testbench-side arbiter is needed in front of a device's slave group.

| device | threads | stand-ins beside it | scenario |
|---|---|---|---|
| gen | writer (`qcmd`, room IRQ); credit returner (cumulative words to `u_crd`, by policy) | remote queue on `si1_axi` at `qu`'s address: records each burst as a packet, counts words consumed by policy | the jobs' commands |
| chain | feeder (`MkvCmd` + `u` bursts into `qu`, within the credit it holds); reader (`qresp`, data IRQ; `x` read straight from the memory model, as the direct wiring reads it) | credit sink on `si2_axi` at `u_crd`'s address (a latest-value register the feeder reads); `FlatMemory` + `AxiMmWriteSlave` on `si3_axi` | the forward stream -- the golden's, or **the gen harness's recording** (the two harnesses compose at the stream) |
| mem | writer + reader (`BusRw`) | none | words and addresses |
| mm_fir | `FirHost` unchanged | none | unchanged |

The remote queue and the credit loop need one object both sides see (the sink's consumed count, the
returner's next write): the harness's `shared` objects already do this for a memory's arena.

**How a harness is stated.**  A `GraphSystem` whose graph is the device's kernel (built into its device
with `build_mm_device`, at the system's base), the stand-ins and the unit host; `dut_modules` is the
kernel.  The pysim stand-in for the remote queue can be the real thing cut short: the routed credit
stream into a trivial consumer kernel that takes words by the policy -- its views and credit writer are
existing pysim, so the pysim harness needs no new bus model; only its RTL side is the new C++.  The
missing pieces for Stage 1 are then: the host binding of fact 4, the two C++ stand-ins, and the
cut taking a device whose neighbours are outside (Stage 0's first finding).

**Decided since:** D9 (above).  **Still open, for the markov stage:**

- **The credit returner**: a thread of the unit host on its bus master (proposed: no arbiter), or an
  autonomous stand-in with a master of its own (closer to the chain's real credit writer, but two
  testbench masters on one slave group need a testbench arbiter).
- **The chain's feeder input**: the golden's stream, or the gen harness's recording.
- **Fact 1**: the ID width in the device's identity, or a sub-top independent of it.

## Record: Stage 1 (2026-10-14), the leaf

`examples/stream_kernel/`: `PolyKernel` (a `FreeRunMod`, one firing = one command; errors answered in
the response -- `TLAST_EARLY` with the count that arrived -- never a halt), its hand-written HLS body
`src/poly_kernel_task.h`, `PolyKernelSystem` (a stream driver, the kernel, two sinks), its build DAG
`stream_kernel_build.py` (one `codegen` step, everything else the framework's).  pysim is bit-exact to
the golden on the five-command scenario (one ending early, one of a single sample); the RTL under XSI is
bit-exact to pysim on both sinks (`compare`).  Gate: `tests/examples/test_stream_kernel.py`.

**Two framework gaps, found by the first float kernel in the system flow, fixed:**

- **Vendor cores were never generated.**  csynth writes a `float` add/multiply as a Xilinx
  `floating_point` core: a Verilog shell plus `<name>_ip.tcl`, which only Vitis's own cosim ran.
  `xelab` stopped at `Module <..._ip> not found`.  `waveflow/build/hls_ip.py` runs the scripts in one
  Vivado batch (cached by digest, beside the crossbar's IP) for csynth's part; `rtl.json` carries the
  cores' VHDL wrappers and the precompiled libraries they bind (`floating_point_v7_1_20`); the run
  scripts `xvhdl` the wrappers and `xelab -L` the libraries; the snapshot stamps both.
- **The XSI clock was 20 ps.**  `clock_low/high` ran `xsi_.run(10)` at a 1 ps precision.  Vitis's RTL
  has no delays, so the period never mattered -- until the multiplier's `DSP48E1` model, which settles
  ~100 ps after the edge: every product read unsettled, no error, results wrong (all `c0` on the first
  command).  Now a 10 ns clock (`HALF_PERIOD_PS = 5000`).  Nothing read the period as a constant (VCD
  consumers infer it from edges); every XSI gate re-ran.  A standalone xsim test of the two cores
  (correct at 10 ns) is what separated the cores from the clock.

**The calibration step (D10).**  `waveflow/build/phase_calib.py`: `add_calib_steps(dag, sysm, ...)` adds
the system's `calibration_workload()` (the one hand-written hook, `HwSystem`'s, default None) as a
second system in the DAG (`calib_` prefix, sharing `codegen` and `csynth`), its XSI run traced, and
`calib_phases`: pysim runs the workload, the traced VCD is decoded at each kernel's scope
(`SystemTopSpec.scope_of`), the corpus joined and fitted (`waveflow/calib/phases.py`), the parameters
filed in each phase's `calib_dir`, the report in `<work>/phase_calib.json`; a pysim occurrence with no
RTL partner fails it.  `--through calib_phases`; no example code.  Workload: poly's nine lengths, 48 and
128 held out.

| span | fitted (free-running, XSI) | host-activated poly (cosim) |
|---|---|---|
| `samples.latency` | 30, exact on all 9 (holdouts too) | 34 |
| `samples.fill_time` | nwords − 1, exact | nwords − 1 |
| `resp.resp_time` (m_out last → m_resp first) | 4, all 9 | 4 (its response span) |

**The latency difference, explained.**  The two loop bodies are the same code, same part (xc7z020),
same 10 ns clock, same operator cores (`fmul_..._4_max_dsp`, `fadd_..._5_full_dsp`).  Inside the
`hls::task` the scheduler overlaps each float operator's last stage with the next one's first (the
SAMPLES pipeline is 24 deep; poly's function-level loop gives every stage its own state, 29 deep).  csynth
then *estimates* 12.96 ns and flags a Timing issue -- but Vivado place-and-route of the kernel
(`export_design -flow impl`) achieves **8.80 ns: met**.  The estimate is pessimistic about the chain, the
shorter pipeline is real.  The depth is 5 cycles shorter and the latency 4: at the ports the latency is
the depth plus 6 here (24 + 6) and plus 5 in poly (29 + 5) -- loop entry and the register slices; the
one cycle more the task's loop entry takes was not chased.  (Without the `PIPELINE II=1` pragma csynth picks II=2 inside the task; the pipeline style
`stp/flp/frp` changes nothing; `config_op` latencies restore depth 29 but still chain.)

**Open from Stage 1:** `system_synth` / the resource steps read a kernel's `syn/verilog` only, so a kernel
with vendor cores would synthesize them as missing modules -- the `.xci` `hls_ip` generates is what they
need (`read_ip`), not done.  The fitted parameters are filed in the example tree (`calib/`); Stage 3
files them under the subsystem's identity.

## Part B -- the `SynthUnit` as an agent boundary (sketch)

A device's harness is the contract a sub-agent would work against.  What crosses the boundary:

- **in:** the device's interface (schemas, ports, views, its address layout), its golden model and
  scenarios, the harness command, a budget (time, synthesis runs);
- **out:** a kernel body that passes the harness, fitted timing parameters, the records it filed, and a
  short report -- never the device's internals.

To work out with `plans/multi_agent_flows.md` (the open problem) and `plans/mcp_fir.md` (the agent-facing
tools, and its tools-vs-workspace experiment): who draws the cut, how a brief is generated from a
harness, what context rules apply, and whether an integration agent can verify a system it never saw
inside of.  The first experiment is in the multi-agent notes: one multi-device spec, one agent against
agents splitting by device.

## Open questions

- **The direct wiring.**  markov's `link="direct"` joins the kernels with a `CreditStreamIF`, no bus; a
  device harness then needs a credit-stream stand-in.  It is also the one case
  `ip_packaging_routes.md` left open (a credit-stream interface definition, or both kernels one IP).
- **Records for the device's resources.**  `synth_ooc_link` returns per-device counters but files no
  store records; D6 needs them filed under the device's identity (`identify` of the sub-top's
  contents: kernels' identities, leaves' identities with their ties).
- **A unit host.**  Generated from the device's views, or a scenario language over them?  The system's
  `SwHost` and its C++ twin are the starting point.
- **Ports that cannot show a firing.**  Where a device's timing hides behind an internal FIFO, when is the
  trace path still needed?
- **How hostile is hostile?**  Which stall policies are worth running by default, and at what cost.
- **Devices with more than one kernel**, if a later grouping produces them.
- **The free-running example's name** (proposed `stream_kernel`), and what becomes of `basic_vec` and
  `regmap` when the host-activated flow is retired.

## Relationship to other plans

- `plans/ip_packaging_routes.md` -- the device, its sub-top, its IP, its resource measurement.
  Prerequisite; complete (2026-10-13) but for the direct-wired case.
- `plans/incremental_xsi.md` -- `XsiSnapshot`, `run.json`, parallel runs: what makes a sweep cheap.
- `plans/system_resources.md` -- the inventory and the records the harness reads and writes beside.
- `plans/paper_cg_dse_vision.md` -- per-block calibration over each block's own parameters is this plan's
  Stage 3; its composition claim is D8.
- `plans/pipelined_ops.md` -- timed phases, the calibration they make generated, and the migration this
  plan's harness enables (see above).
- `plans/multi_agent_flows.md`, `plans/mcp_fir.md` -- Part B.
