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
difference explained; no example-specific calibration wrapper.  Its two loose ends are closed in the
Stage 2a record below.

**Stage 2 -- the composite: the same kernel behind AXI-MM.**  First D11: the `SynthUnit` tree and
harnesses generated from a node, Stage 1's harness re-expressed as the leaf node's.  Then `link="mm"`:
queue views (command/sample queue in, response/result queue out) on one front, a `SwHost` of two threads.
The composite (kernel + views + front, packaged as one IP) is the harness's DUT under D9: its sub-top as
the top, the host bound straight to its slave group, no crossbar.  Gate: bit-exact; the composite's
timing **composed** from the kernel leaf's phases and the views' with no fitting, checked against its RTL
(D8); then the root (the composite and the crossbar leaf, `AxiXbarConfig` records) composed and checked
against its RTL.

**Stage 3 -- calibration as a step, records under identity.  DONE (record below).**  The framework calibration step of Stage 1
generalized: one `XsiSnapshot` per configuration, the workload hook's scenario in `run.json`, the fit
filed under the `SynthUnit`'s identity (D7), a repeated calibration with no change running no
simulation.  Grid discipline: at least three values a feature, interior points held out.  Gate: a
repeat is a cache hit; a held-out point within tolerance; the filed record found from the identity alone.

**Stage 4 -- markov: the first multi-kernel system.  DONE: -1.6% from composition (Stage 4d record).**  Declare phases on markov's two stream kernels (the
generator and the chain's core), replacing their hand-fitted per-chunk overheads (7 and 15); calibrate
each as a leaf; the composites with the stand-ins of the Stage 0 record (the remote queue, the credit loop);
the composition check against markov's RTL (1870 cycles), with no system-level fitting.  Gate: within the
existing 5% from composition alone -- or a written account of what the decomposition missed.

**Stage 5 -- robustness.  DONE (record below).**  Hostile stand-in policies (stall, starved credit, late answers) swept per
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

## Record: Stage 2a (2026-10-14), the harness factory

**D12 in code** (`waveflow/hw/hw_system.py`): `HwSystem(sim, dut, *, n_cycles, inputs, check, run_until,
fresh, calibration)` beside the subclass form (the existing systems unchanged).  The constructor refuses
a DUT not in the simulation, a DUT that is not a root, a testbench participant with no lowering
(`bfm_model()` / `xsi_model()` / a `SwHost`), and an unbound DUT boundary port; `participants()` is the
derived testbench; `fresh()` an unrun copy (the calibration's pysim pass, replacing
`dataclasses.replace`).  `cut()` now **refuses an empty cut**, and `dut_members` treats a DUT that is
itself a member (a csynth'd kernel, a memory, one device) as its own cut.

**`harness(module, *, workload, n_cycles)`** (`waveflow/hw/harness.py`): a fresh copy of the module (its
class and `HwParam`s, as `system_top` rebuilds one for csynth) on a new simulation; a `StreamDriver` per
stream input, a `StreamSink` per stream output; the stimulus from `module.workload(kind)` or given
directly; the calibration workload from `module.workload("calib")` and `module.calib_holdout` (D10, now
on the module).  Other port kinds raise -- Stage 2b's.  The free-running poly lost its testbench graph
and system class: `stream_kernel_build.system()` is `harness(PolyKernel, workload="test")`.

**Stage 1's loose ends.**  (a) `calib_phases` now consumes the calibration run's `compare`.  Its first
run failed, rightly: with the harness's DUT being the kernel itself, the old `dut_members` (which only
looked at a DUT's *children*) gave an empty cut, and the RTL run had **no design in it** -- csynth,
`system_rtl` and XSI all "passed", the sinks recorded nothing, and only the comparison saw it.  Hence the
empty-cut refusal above.  (b) The one latency cycle (the free-running port latency is depth + 6, poly's
depth + 5): the level-1 trace cannot split it -- the task's `s_in` `TREADY` is already high when the
first sample arrives and the register slices look transparent at the ports, so all 30 cycles fall
"inside the task"; separating loop entry from the slices needs the slices' task-side `TVALID`, not
dumped.  Recorded as a measured one-cycle residual, consistent over every command.

## Record: Stage 2b (2026-10-15), the kernel behind AXI-MM, and the composition check

`examples/stream_kernel/stream_kernel_mm.py`: the **same** `PolyKernel` (same class, same csynth'd RTL)
behind the queue views its type now declares (`mm_views`: a queue in for `s_in`, depth 256 so the
largest command fits; a queue out for `m_out`, depth 256; one for `m_resp`, depth 16), per-view on a
1x3 crossbar, driven by `PolyHost` -- two threads (a writer: each command's header and samples, a
packet each; a reader: each command's results, then its response), interrupt-driven, never polling.
The host knows how many results each command gives (one per sample sent), so it drains them before
the response and the results queue never fills behind an unread response.  Its C++ twin is
`poly_kernel_host.h`.  Built the D12 way (`mm_system()`: modules on one simulation, then
`HwSystem(sim, dut)` -- the first system with a software host so built), in the same build DAG as the
stream harness (`mm_*` steps sharing `codegen` and `csynth`; `--through mm_compare`).

**One adaptation, in the framework.**  The kernel's outputs are framed (TLAST, which the stream harness
and an early end need); a queue-out view's input was declared unframed.  The RTL queue accepts TLAST
and drops it and the view pops whole waiting bursts by count, so `build_mm_device` now declares the
view's input with the producer's framing: same RTL, honest pysim channel; unframed producers (mm_fir,
markov) unchanged.

**Result.**  Bit-exact first time: pysim against the golden, and the host's three endpoint traces RTL
against pysim (`mm_compare`).

**The composition check (D8) found a modelling gap, and closing it closed the check.**  pysim's timing
here is composed from parts calibrated elsewhere -- the kernel's Stage 1 phases, the queue views' and
crossbar's models -- with nothing fitted to this system.  First run: **466 cycles against the RTL's 392,
19% slow**.  Tracing the kernel's ports at RTL against pysim's logs, command by command: command 0's 100
samples enter the kernel at cycles 46-145 at RTL, its results begin at 71, overlapped; in pysim the
results began at 132, *after the last sample* -- the overlap lost, every later command offset by it.
Cause: the queue-in view delivered each packet with `write_pipelined` and a **backdated** start, because
the crossbar hands a slave a write burst only when its last word lands -- the case `pipelined_ops.md`
named ("a backdated write cannot take a contended bus in the past"; Stage 6 there).  Fix: an optional
**cut-through hook** on a bus slave, `rx_write_begin(words, local_addr, word_period)`, which the
crossbar calls when a FULL burst's first data word lands (then `rx_write_proc` at its last, as before);
the queue-in view uses it to *begin* each packet the burst holds whole as its first word lands and *end*
it as its last does, and the bus write still waits for that delivery, so a packet with too little room
stalls the bus as in RTL (the first version did not wait, and the queue-view gates caught it).  A
consumer that reads with a plain `get` sees the packet at its last word, as before.  After: **396
against 392, within 1%**, nothing fitted.  Gates: `tests/examples/test_stream_kernel_mm.py` (pysim
golden, never polls, the D12 shape; at RTL the traces and the composition within 5%).

**The device's own harness (D9), with no crossbar.**  `mm_system(direct=True)`: the views behind one
front, the host bound straight to its slave group.  Before this a system without a crossbar rendered no
views and a 1x1 crossbar was refused, so the link needed a realization of its own: a **direct link** --
the pysim crossbar model with one master and one slave, marked `direct` (`AxiXbarConfig.direct`), which
skips the IP (no `axi_crossbar` generated, no 1x1 refusal, no stub padding) and is rendered in the system
top as `assign`s from the host's port group to the one slave slot.  `discover` and the testbench
generation are unchanged (they find the host on the link's master port).  Bit-exact at RTL
(`dev_compare`); **408 cycles at RTL, 421 in pysim (+3.2%)**, nothing fitted.  The one number chosen is
the link's latency, and it is physics: a wire has none (2 cycles, the first guess, gave +6.1%); the
front's registers are in the adaptor's model.  Synthesis and packaging of a direct link are not wired
(`generate_axi_xbar` refuses it with a message); they are opt-in steps.

| system | RTL cycles | pysim (composed, nothing fitted) |
|---|---|---|
| poly behind AXI-MM, per-view on a 1x3 crossbar | 392 | 396 (+1.0%) |
| the device alone, one front, direct link (D9) | 408 | 421 (+3.2%) |

**Open from Stage 2:** the device harness is hand-wired in the example (`mm_system(direct=True)`), not
yet produced by `harness(device)`: a generic host stand-in generated from a device's views (the "unit
host" of Open questions) is the missing piece -- the stream harness's drivers and sinks have no
memory-mapped counterpart yet.

## Record: Stage 3 (2026-10-15), calibration records under identity

**Filed in the module store.**  `add_calib_steps(..., store=ModuleStore(...))`: `calib_phases` files one
`timing` record per calibrated module in the existing record store (`waveflow/calib/record_store.py`,
`modules/<key>/timing/records.jsonl`), source `xsi`, payload `{kind: "timed_phases", workload: <digest>,
phases: {phase: {span: params}}, max_abs_residual}`, provenance (structure signature, the part csynth
used, the clock, the tool).  A `TimedPhase` whose `calib_dir` is a store root (it holds `modules/`)
loads its spans from the newest such record under its owner's identity -- found from the identity
alone, verified against it (a record of different hardware raises `StaleRecordError`) -- else, as
before, its per-span files or the seed.  stream_kernel moved: its `calib/samples/*.json` and
`calib/resp/*.json` are now one record, `calib/modules/poly_kernel-53a32b7b/`.

**A repeat runs no simulation.**  The DAG decides freshness per step after its upstream has run, so a
fresh last step does not spare the XSI run before it.  The decision is therefore taken when the DAG is
built: `add_calib_steps` asks the store first, and when every calibrated module has a record measured on
this workload (`scenario_digest`: the bytes the workload's testbench plays, hashed without simulating)
it adds one `PhaseCalibCachedStep` and nothing else.  Measured: the repeat ran `calib_phases` alone,
0.0 s; a changed workload or an empty store brings the chain back.

**A finding: the identity was not context-free.**  The point of D7 is that a module is calibrated once
and its record found wherever it sits.  The kernel's `identify_instance` key differed between its
stream harness (`3e5b096b`) and behind AXI-MM (`6b469304`), so the memory-mapped system silently fell
back to the seed (latency 34, response 1).  Cause: `identify_instance`'s `exempt` only skips the
boundary in the bound check; the digest still hashes each boundary port's `queue_size` -- the depth of
whatever channel it is bound to (2 in the harness, 256 behind a queue view).  `module_key`'s own
docstring says a boundary port's depth "is set by whatever encloses the top, not by the top".  Fixed
for timed-phase records (`waveflow.calib.phases.module_identity` blanks the boundary depths for the
hash): one key in all three systems, and the memory-mapped system now loads the harness's calibration
(396 cycles, as before the move).  **Open:** the same fix in `identify_instance` itself, which moves
every existing key with a boundary port (resource records, the calibrated platforms) -- a key
migration (`waveflow/calib/migrate_keys.py`), not done unattended.

**Not done:** grid discipline as a check (at least three values per feature, interior points held out
-- stream_kernel's workload satisfies it, nothing enforces it); `cost_seconds` counts the fit step only,
not the XSI run before it; the record's `tool` names Vitis without its version when the build config
does not carry one.

## Record: Stage 4, part 1 (2026-10-15), markov's kernels alone -- credit stand-ins

**Generic credit-stream stand-ins (D3).**  `waveflow/simulation/credit_tb.py`: `CreditSink` (faces a
kernel that produces into a credit stream: always ready, records, reports the cumulative count every
`crd_every` words) and `CreditSource` (faces a kernel that consumes one: plays its bursts, each once the
window has room for all of it), with C++ twins of the same names in `xsi_bfm.h` (a credit stream is two
AXIS ports at RTL; the models take them as one port group `{data, credit}`; the rules are
`credit_stream_hls.h`'s).  `harness()` gives each credit port its stand-in on a `CreditStreamIF` at the
window the kernel class declares (`credit_links = {port: {"depth", "crd_every"}}`: a producer has its
consumer's depth compiled in, so the harness refuses to guess it).

**markov's kernels** declare `credit_links` and a `workload(kind)` (`gen_stimulus` / `chain_stimulus`,
`workload_jobs("test" | "calib")`); markov's DAG builds the chain's core as a top of its own (codegen,
not in `top_names()`) and adds both kernels' harnesses (`ugen_*`, `ucore_*`, traced).  In pysim both
run, and the generator's output, as its credit sink takes it, is word for word what the core's harness
plays.  At RTL **the generator alone is bit-exact** against the C++ `CreditSink` (`ugen_compare`; gate
`tests/examples/test_markov_units.py`).

**Was blocked (resolved in part 2, option A): the chain core alone did not csynth.**  Its body writes `m_x` as
`hls::stream<framed_word<64>>` -- the internal framed type, because inside `MarkovChain` the channel to
the memory writer is declared `framed=True`; at a top that port must be `axi4s_word` (Vitis allows
`ap_axis` only on interface ports) and the generator renders it `ap_uint` (it reads the endpoint, and
the framing was the channel's).  A port's C++ type is decided by its context, which a harness does not
have.  Options, for the user: (A) the kernel declares its framed ports and the body takes their word
type as a template parameter -- `framed_word` inside a composite, `axi4s_word` at a top
(`streamutils`' `write_boundary_word<WordT>` already serves both); (B) the generated top inserts a
framed_word -> axi4s adapter task at such a port (body untouched, but the harness RTL gains a cycle
the system lacks); (C) harness `MarkovChain` (core + writer) as the leaf, which needs a memory stand-in
on its `m_axi`.  Recommended: A.

**Was not started (done in part 2): the generator's timed phases.**  Its RTL pattern (from the harness trace): the command
forwarded in 3 back-to-back words, then each 64-draw chunk as 16 words over **61 cycles** (a word every
4 draws), **~12 cycles** between chunks, the job's last chunk (11 words) over 41.  A chunk is a write
**paced by its producer** -- which no timed-phase pattern expresses yet (Pattern 1 waits; Pattern 2 is
load -> store).  Needed: a third pattern, `phase.produce(data, store, **feats)` (begin the store's burst,
wait the declared fill span from its first beat, end it); begin/end on a credit master (the room wait,
then the forward half's begin/end); and events and RTL port names for a credit stream's halves
(`m_u_fwd`).  The hand-fitted overheads (7 and 15) stand until both kernels can be calibrated, since the
Stage 4 gate is markov's composition from both.

## Record: Stage 4, part 2 (2026-10-15), both kernels calibrated alone -- the gate not yet met

**Option A, done.**  `KernelTask.framed` names a kernel's packet-carrying ports; the body takes each
one's word type as a template parameter and the composite generator supplies it -- `framed_word` on an
internal FIFO, `axi4s_word` (a TLAST port) at a top.  `streamutils::write_msg<W>(msg, s)` picks the
schema's framed or AXI-Stream writer by the stream's type.  The chain core csynths as its own top and
matches pysim at RTL (`test_rtl_core_alone_matches_pysim`); `MarkovChain` instantiates it with
`framed_word` and its RTL gates are unchanged (1870 cycles, all ten markov XSI gates).

**Pattern 3, `phase.produce(ep, data, **feats)`** (`timed_phase.py`): a write paced by its producer.  Two
spans, a *lead* from any event of the module's (the latest transfer there) to the write's `first`, and a
*fill* from its `first` to its `last`; an occurrence the environment delayed (begun late for want of room
or input, ended late for want of a reader) is marked stalled.  A credit master has `begin` / `end` (the
room wait, then the forward half's), its halves' events are the module's own, and their RTL ports are
`<attr>_fwd` / `<attr>_crd` (`timed_ports`).  Tests: `TestPattern3`.

**Both kernels' phases, fitted alone** (`ugen_calib_*`, `ucore_calib_*`; records in
`examples/markov/calib/modules/`): the generator's chunk -- lead 12.8 (residual <= 0.9), fill `c - 4`
(exact); its command forward -- lead 16.5, fill 2.  The core, all exact: a chunk's MemWCmd lead 8 (7 for
a job's first, from the previous response, reading the command between), fill 1; its states lead 15,
fill `c - 8`; the job's closing MemWCmd lead 8, the response lead 1.  The hand-fitted
`chunk_overhead`s (7, 15) are gone.

**Identity, three leaks fixed** (each moved a kernel's key between where it was calibrated and where it
is used): a credit link's run-time counters (`written`, `consumed`, ...) are context (`_CONTEXT_ATTRS`);
`module_identity` blanks every own port's depth, halves included -- a module's ports are its boundary
wherever it sits (the generator's `s_cmd`: 2 in its harness, 16 in the system); a simpy subclass is
run-time (a queue view swaps in a notifying `Store` at `pre_sim`).  And a phase loads its model before
`pre_sim`, a record is filed under an **un-run** copy's identity: a module's own state (a job counter)
cannot decide where its calibration lives.  Tests: `test_a_kernel_is_filed_and_found_under_one_identity`.

**The gate: not met -- pysim 1705 cycles against RTL 1870 (-8.8%)**, `test_markov_pysim_tracks_rtl`
fails.  The kernels compose right (the core is 80 cycles a chunk to RTL's 79; the generator matches its
RTL probes to a constant offset), and what is missing is **the MM link between them**: at RTL the core
starves twice a job, ~28 cycles each (probes: `u` reads idle 496 -> 525, 617 -> 645), while pysim's never
does.  The forward queue writer issues its burst ~29 cycles after a chunk's last word and adds a second
(pointer) write (`ufwd_last` 103 -> `wr1_aw` 132 -> `wr1_b` 154); pysim's bursts at once (88 -> 109).  The
hand-fitted overheads had absorbed this, which is why they composed to +3%: they charged the kernels for
the link.  Delaying the pysim writer alone barely moves the total (+29 for 29 cycles), so the starvation
is the link's whole path -- writer, crossbar, queue-in reader, credit return -- and **the next step is to
calibrate the link's parts as units of their own** (`MmStreamWriter`, the queue-in view), which needs a
memory stand-in for an `m_axi` port (D3) and AXI-MM events for a timed phase.  Until then the gate is red,
on purpose: re-fitting the kernels to hide the link is what the old overheads did.

## Record: Stage 4c (2026-10-15), the memory as a calibrated unit -- the gate still red

**The diagnosis of part 2 was wrong.**  Word by word (probes `ufwd` against `u`), every draw is in the
chain's queue 90-200 cycles before the core reads it: the core does not starve for input.  It reads
slowly at job boundaries because its output cannot drain: the chain's memory writes stall (30 cycles
for one that takes 10) behind the **host's result reads of the same memory** -- 38 words in 80 cycles at
RTL, 42 in pysim.  `mm_bram_port.v` says why: a read word costs `LAT + 1` cycles, and the front serves one
AXI transaction at a time.  pysim's memory was full-duplex at a cycle a word.

**The memory's harness** (`waveflow.hw.harness.memory_harness`): the memory's slot behind a direct link,
the DUT; the testbench a scripted master, `MmScript` (`waveflow/simulation/mm_tb.py`, C++ twin of the
same name over `AxiMmMaster(overlap_rw)`) -- the system's host by role, so it keeps a host's contract
(script under `scenario`, read data in `trace_dir` compared RTL against pysim, each op's
`[t_start, t_end]` beside it in `<trace_dir>_timing`).  A model deriving from another model is now a
model (`header_model_classes` follows derivation).  markov: `umemcal_*` (calibration script: every length
1-32, reads and writes started together) and `umem_*` (the check).

**Measured, and fitted exactly** (`waveflow.calib.mem_timing`, step `MemCalibStep`, record in
`examples/markov/calib/modules/memory_mod-*`): over a zero-latency link a read adds `1 + n` cycles, a write
`1`; and a read and a write that meet are served one after the other, write first (`serialize`).  With
the record loaded (`MemoryMod.calib_dir`; the slave then serves one transaction at a time,
`serialize_transactions` on one channel), every op of the check script matches RTL to the cycle -- gate
`test_rtl_memory_alone_is_timed_exactly_by_its_calibration`.  Collisions land within 2-3 cycles (a
turnaround between transactions at the front, not modeled).  Identity: a memory's backing store,
allocation, back door (`__wf_identity_exclude__`) and a slave's address placement are context.

**The gate: pysim 1740 against RTL 1870 (-7.0%)**, from -8.8%.  The host's result read now takes 81
cycles to RTL's 80.  What remains, from the chain's memory writes job by job:

* **startup, ~44 cycles**: the core gets its first chunk 26 cycles later at RTL -- the forward queue
  writer issues its burst ~29 cycles after a packet's last word (`ufwd_last` 103 -> `wr1_aw` 132), pysim's
  at once -- and then, starved, it needs a pipeline latency from the draws' arrival to its first state,
  which Pattern 3 does not charge (a late start begins the write as soon as the input is in hand).  The
  core's `x` is a pipelined load -> compute -> store: **Pattern 2**, with the load on a credit stream.
* **per job, ~23 cycles** (414 against 391): each job's first chunk write meets the host's result read
  at RTL and its stall propagates to the core's next chunk (99 cycles, not 80).  pysim now serializes
  the memory, but its offsets differ (the startup above), so the two rarely meet.

Next: the core's `x` as Pattern 2 (a credit-stream `begin_read`), and the forward writer as a unit (its
harness: a stream in, `MemoryMod` as the `m_axi` stand-in).

## Record: Stage 4d (2026-10-15), the core's input lead -- the gate met: -1.6%

**The gate: markov composed from its calibrated parts is 1841 cycles against RTL 1870 (-1.6%)**, no
system-level fitting; `test_markov_pysim_tracks_rtl` is out of quarantine.  The forward writer was not
needed: once the core waited for its input, the rest followed.

**The core's states need two things** -- their turn (after the chunk's MemWCmd) and their draws (the
chunk's first word in).  Timed from the input alone, the RTL rows came in two clean values, 19 and 34
cycles: the input port's handshake is when a word enters the kernel's input FIFO, which is early when
the stand-in has it ready and is the core's own read when it arrives late.  So the RTL is
`x.first = max(MemWCmd.last + 15, s_u_fwd.first + 19)`, Pattern 2's latency rule.  `produce` takes
several leads to the write's first beat and starts it when every one allows; leads to one event are
**lower bounds** (`TimedPhase.lower_bounds`), fitted to the lower envelope of their rows (`fit_phase`):
the turn 15 on all 26 chunks, the input 19 for a full chunk on the 10 where it held, both exact.  (The
shorter last chunks' input lead is the less certain: in the harness their input was always early.)

**Ports with no TLAST** (a credit stream's data half, `s_u_fwd`) are framed at RTL by the word counts
pysim logged on the same endpoint (`rtl_xfers`, `_frame_by_counts`) -- legal for the reason the whole
join is: both runs carry the same words; a different total refuses.

**A record now states its phases** (`phase_declaration`: each span's events by RTL port, the features),
and `stored_phases` counts only a record of the phases as declared.  Found the hard way: changing the
core's spans gave a calibration cache hit (phases are context, so the identity did not move) and would
have loaded numbers fitted to other spans.  All records were refitted.

## Record: Stage 5 (2026-10-15), robustness -- no findings

**Stage 4 closed on its gate's second clause** (superseded: Stage 4d met the gate itself, -1.6%) ("or a written account of what the decomposition
missed"): the Stage 4c record is that account, and `test_markov_pysim_tracks_rtl` is quarantined until
the forward writer and the core's input lead are calibrated.

**Policies** (`waveflow/simulation/policy.py`): a `StandInPolicy` -- `stall_pct` (a stream sink drops
TREADY; a stream driver or credit source holds off its next word, never withdrawing one it presented),
`answer_pct` (a scripted AXI-MM master drops RREADY / BREADY: late answers; a stand-in says it takes
answers with `policy_role = "answers"`), `credit_delay` (a credit sink holds each report: starved
credit), `seed`.  At RTL the stall is cycle by cycle, `policy_stalled` (C++) / `stalled` (Python): one
seeded hash of the cycle number.  pysim, the functional reference, approximates each policy per burst.
The knobs are `DynParam`s on the stand-ins, so the generated testbench carries them; at their defaults
nothing is emitted and every existing gate is unchanged.  `harness(..., policy=)` and
`memory_harness(..., policy=)` apply one and scale the RTL bound (`StandInPolicy.bound`).

**The sweep** (`waveflow.build.robustness.add_robustness_steps`): each policy that touches a harness's
stand-ins adds it again as a system of its own (`<prefix><policy>_*`, the same top).  stream_kernel's
kernel (`rob_stall_`), markov's generator (`ugen_rob_stall_`, `ugen_rob_starved_credit_`), chain core
(`ucore_rob_stall_`) and memory (`umem_rob_late_answers_`).  Gates: `tests/examples/test_robustness_xsi.py`.

**First cut too gentle -- measured.**  At 50% a sink never back-pressured the generator (a word every four
cycles: 1449 cycles against 1436 polite), and credit held 300 cycles never emptied its 128-word window
(1430).  A policy that changes nothing finds nothing, so the sweep is 90% stall, 2000-cycle credit, 90%
late answers.  Then each bites: generator 1436 -> 3098 (stall) and 4633 (starved credit); core 1561 ->
3285; stream_kernel 326 -> 2029; the memory's 38-word read 77 -> 209.

**Findings: none.**  Every run finishes with exactly pysim's words.  Not covered: the device harness
(`dev_`), whose testbench is a software host with its own C++ model -- a host policy is that model's
to implement.

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
