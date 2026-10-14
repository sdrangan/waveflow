# Plan: a unit harness per test subsystem -- verification, timing and calibration, one module at a time

**Status:** drafted 2026-10-12; revised 2026-10-13 (Stage 0); paused 2026-10-14 for
`plans/pipelined_ops.md`, which gave modules a way to state their timing ([timed
phases](../docs/guide/timing_model/timed_phases.md), merged PRs #274, #275).  **Revised again
2026-10-14, with the user, before any code:** the unit is a **test subsystem**, not only a device;
**D9 is decided**; the first vehicle is a **free-running poly** -- first as a stream-only kernel, then
as a memory-mapped device -- and markov follows as the first multi-kernel system.  That example also
becomes the docs' first example, the start of retiring the host-activated examples.  The original Stage
0 (markov and mm_fir, port by port) is kept as a record at the end: it is the inventory the markov stage
needs.

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

## What a test subsystem is

A **test subsystem** is a subgraph of a system that admits a harness (the user's definition,
2026-10-14).  It must satisfy:

1. **Its function and its timing are observable at its boundary** -- the events of its timed phases are
   on its own ports.
2. **It is hierarchical** -- a test subsystem is made of test subsystems.
3. **Its resources and timing are either calibrated from its boundary and its synthesis reports (a
   leaf), or composed from its children's (a composite)** -- leaves are calibrated, composites are
   composed and then *verified* by property 1, never fitted.

It is independent of packaging: a kernel is a test subsystem but rarely an IP; a device is both.  The
crossbar is a leaf of its own, calibrated once per configuration and composed into its parents.  A
child's boundary is observed **in place** whenever its parent runs (its declared events are logged at
any level); only a test subsystem that needs **control** -- a workload sweep, a hostile environment,
development before its parent exists -- needs a harness of its own.

For one test subsystem, the harness gives:

| | how |
| --- | --- |
| **function** | its golden model and scenarios, bit-exact in pysim and at RTL (XSI) |
| **timing** | its timed phases, measured at its own ports (RTL transfers paired with pysim's records), fitted, filed under its identity |
| **resources** | read, not measured: a leaf's csynth report or a device's out-of-context record, under the same identity |
| **robustness** | hostile environments on demand: a neighbour that stalls, starves credit, or answers late |

## Decisions

- **D1 -- the unit is a test subsystem** (was: the device).  A device of `ip_packaging_routes.md` is one
  level of the hierarchy; the kernel inside it is the leaf below, the system the composite above.  One
  identity keys a test subsystem's verification, timing records and resource record.
- **D2 -- a harness is a cut, not a new kind of testbench.**  Build it by drawing the cut around the test
  subsystem in a system that holds it (often a minimal one), and generate the rest with the machinery
  that already lowers a system: `system_top_spec`, the XSI workspace, the host's C++ twin.  No second
  testbench framework.
- **D3 -- generic stand-ins for cut-away neighbours.**  A unit harness should not need a hand-written
  model of every neighbour.  The framework provides generic stand-ins: stream drivers and sinks (exist),
  a remote queue that accepts a writer's bursts and returns credit by a policy, a memory, an interrupt
  sink.  A neighbour's own `bfm_model()` may be used instead.
- **D4 -- the environment is a parameter.**  A stand-in's policy (always ready, a fixed latency, a stall
  pattern, credit starved) is part of the scenario (`run.json`), so one compiled harness serves
  calibration (contention-free) and robustness (hostile) alike.
- **D5 -- timing is measured at the ports, as declared phases.**  A test subsystem's timing is its timed
  phases; their events are its ports' first and last beats; the RTL side reads them from the harness's
  port trace or its bus models' timestamps, pysim logs them itself.  No internal net, no manifest.  (Was:
  "cycles per firing from port timestamps" -- the phases are what made that precise.)
- **D6 -- the harness does not re-measure resources.**  It reads the record a leaf's csynth or a
  device's out-of-context synthesis already filed.
- **D7 -- records under identity, library first.**  Fitted phases file in the calibration store under the
  test subsystem's identity; a measurement is content-addressed (a repeat is a cache hit); failures are
  rows; batches go through `SweepRunner`.
- **D8 -- the composition check is part of the plan.**  A composite's pysim, run on its children's
  calibrated phases with no fitting of its own, must reproduce its RTL run within tolerance; when it
  does not, the difference is undeclared time or a wrong decomposition, and that is the finding (poly
  showed both kinds: `plans/pipelined_ops.md`, the Stages 2-5 record).
- **D9 -- DECIDED 2026-10-14: a harness's boundary is the test subsystem's own boundary.**  For a
  device, its sub-top's ports -- its AXI4 slave groups, master groups and interrupt lines, the ports its
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

**Stage 1 -- the leaf: free-running poly on streams.**  The example with `link="direct"`: golden,
kernel (`run_iter` + its HLS task body), its system (`GraphSystem`: a stream driver, the kernel, a sink),
its build DAG and XSI gate.  The leaf harness: run the kernel alone at RTL with its ports traced (the
system flow's traced run, `SystemXsiStep(trace=True)`), decode its declared ports' transfers, pair with
pysim, fit -- through a framework calibration step (D10), the workload hook being "commands of these
lengths".  Gate: bit-exact in pysim and at XSI on every scenario (errors included); the phases fitted in
the harness with held-out lengths exact; the fit compared with the host-activated cosim fit, every
difference explained; no example-specific calibration wrapper.

**Stage 2 -- the device: the same kernel behind AXI-MM.**  `link="mm"`: queue views (command/sample
queue in, response/result queue out) on one front, a `SwHost` of two threads.  The device harness under
D9: the device's sub-top as the top, the host bound straight to its slave group, no crossbar.  Gate:
bit-exact; the device's timing **composed** from the kernel's phases and the views' with no fitting,
checked against its RTL (D8); then the system run with a crossbar composed from the device and the
crossbar leaf (`AxiXbarConfig` records), checked against its RTL.

**Stage 3 -- calibration as a step, records under identity.**  The framework calibration step of Stage 1
generalized: one `XsiSnapshot` per configuration, the workload hook's scenario in `run.json`, the fit
filed under the test subsystem's identity (D7), a repeated calibration with no change running no
simulation.  Grid discipline: at least three values a feature, interior points held out.  Gate: a
repeat is a cache hit; a held-out point within tolerance; the filed record found from the identity alone.

**Stage 4 -- markov: the first multi-kernel system.**  Declare phases on markov's two stream kernels (the
generator and the chain's core), replacing their hand-fitted per-chunk overheads (7 and 15); calibrate
each as a leaf; the devices with the stand-ins of the Stage 0 record (the remote queue, the credit loop);
the composition check against markov's RTL (1870 cycles), with no system-level fitting.  Gate: within the
existing 5% from composition alone -- or a written account of what the decomposition missed.

**Stage 5 -- robustness.**  Hostile stand-in policies (stall, starved credit, late answers) swept per
harness; any hang or mismatch is a finding.  Gate: the policies run on every harness; what they find is
fixed or recorded with a reproducer.

**Stage 6 -- incremental, end to end.**  Change one test subsystem's parameter: only its harness rebuilds,
only its synthesis re-runs, its parents' estimates update from records.  Gate: the events show no work
for unchanged subsystems.

**Stage 7 -- docs.**  The free-running poly becomes **the first example in the docs** (the user,
2026-10-14: the host-activated examples are to be retired over time).  It teaches, in order: the
command-response protocol, the golden, the kernel and its HLS body, the system flow, timed phases, the
harness and calibration, then the memory-mapped device.  A guide page on module-level development: test
subsystems, harnesses, stand-ins and policies, calibrating a leaf, composing and verifying a parent.
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

## Part B -- the test subsystem as an agent boundary (sketch)

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
