# Plan: a unit harness per device -- verification, timing and calibration, one component at a time

**Status:** drafted 2026-10-12.  **Starts after `plans/ip_packaging_routes.md`**, whose Stage 0 (devices
derived from the inventory, rendered as sub-tops) and Stages 1-2 (per-device out-of-context synthesis,
identity, tie-offs as parameters) this plan builds on.  Part A is staged work; Part B is a sketch that
connects it to `plans/multi_agent_flows.md`.

## Motivation

Calibrating a module's timing today means running the **whole system** and extracting the module's
behaviour from a trace: `AddVcdTopStep` dumps a VCD, `TraceManifestStep` names the nets, and
`waveflow/utils/trace.py` binds firings to them (the memcpy arc, `project-memcpy-timing-instrumentation`).
It works, but:

- **the system must be arranged so the right signals arrive at the right time** -- the module's timing is
  only as measurable as the scenario around it makes it;
- **every calibration point is a system run**, so a module's parameter sweep pays for its neighbours;
- **a module cannot be developed alone**: no place to verify and calibrate it before the system exists.

The better design was always one harness per module: functional verification, timing and resources,
for that module alone.  It was not built because **Vitis cosim cannot unit-test a free-running task with
an `m_axi` port** (`ap_ctrl_none` with a non-stream port is refused; `ap_ctrl_hs` cannot drive a detached
task -- `reference-hls-task-no-maxi`).  Nothing was wrong with the idea; there was nowhere to run it.

That wall is gone:

- **XSI runs free-running modules with `m_axi` ports**, through the framework's bus models.  The
  `mem_r_stream` (158) and `mem_w_stream` (176) gates are already unit harnesses, for two framework blocks.
- **The cut is a build choice.**  `add_system_steps(..., inside=...)` overrides which modules are RTL;
  outside it, a module is its `bfm_model()` and an edge with both endpoints outside is its
  `xsi_model()` (`waveflow/hw/interface.py`, `project-design-cut`, `project-behavioral-edges`).  A
  unit harness is a system with the cut drawn around one device.
- **Re-runs are nearly free** (`plans/incremental_xsi.md`, done): a no-change build 6 ms, a workload point
  ~0.15 s plus its simulation, scenario sizes from `run.json`, parallel runs on one snapshot safe.
- **The calibration doctrine asks for it.**  Two-level calibration (`project-two-level-calibration`):
  characterize the bus once per platform, fit only each kernel's own compute, contention-free.  memcpy
  showed the loaded system then comes out right with no end-to-end fitting (0.0%), because back-pressure
  emerges from the FIFO depths.
- **Resources are per device already**, once `ip_packaging_routes.md` route B lands: a device's
  out-of-context synthesis *is* its resource measurement, keyed by its identity.

## What a device harness is

The unit is the **device** of `ip_packaging_routes.md`: a kernel, its memory-mapped views, its AXI front
and the bus writers that act for it.  (A framework leaf -- a mem stream, a queue -- can have one too; the
existing gates are examples.)  For one device, the harness gives:

| | how |
| --- | --- |
| **function** | its golden model and scenarios, bit-exact in pysim and at RTL (XSI of the device sub-top) |
| **timing** | cycles per job, per firing, measured at the device's ports from the bus models' timestamps -- no VCD, no system arrangement |
| **resources** | read, not measured: the device's route-B out-of-context record, under the same identity |
| **calibration** | sweeps over workloads (`run.json`) and device parameters; the kernel's timing model fitted; records filed under the device's identity |
| **robustness** | hostile environments on demand: a neighbour that stalls, starves credit, or answers late |

## Decisions

- **D1 -- the unit is the device**, as `ip_packaging_routes.md` decided, so one identity keys its
  verification, its timing records, its resource record and its IP.
- **D2 -- a harness is a cut, not a new kind of testbench.**  Build the device's harness by drawing the cut
  around it in the system it belongs to (or in a minimal system that holds only it), and generate the
  rest with the machinery that already lowers a system: the inventory, `system_top_spec`, the XSI
  workspace, the host's C++ twin.  No second testbench framework.
- **D3 -- generic stand-ins for cut-away neighbours.**  `bfm_model()` is hand-written per module, by
  design ("declared, never derived").  A unit harness should not require a hand-written model of every
  neighbour.  The framework provides **generic stand-ins at the bus level**: a remote queue that accepts
  a writer's bursts and returns credit by a policy; a memory; an interrupt sink.  A neighbour with its
  own `bfm_model()` may be used instead, when someone has written one.
- **D4 -- the environment is a parameter.**  A stand-in's policy (always ready, a fixed latency, a stall
  pattern, credit starved) is part of the scenario, written in `run.json`, so one compiled harness serves
  calibration (contention-free) and robustness (hostile) alike.  The baseline blind-test agent's
  95%-stall test found a Vitis bug; this makes that test routine.
- **D5 -- timing is measured at the ports.**  Bus models already timestamp transactions (`cycles.bin`
  per stream).  A firing's start and end come from the device's own port traffic, so no internal net,
  manifest or VCD is required.  The trace path stays for what ports cannot show (an internal FIFO's
  occupancy).
- **D6 -- the harness does not re-measure resources.**  It reads the device's route-B record.  One
  measurement, one source; a harness that synthesized would duplicate route B and drift from it.
- **D7 -- records under identity, library first.**  Timing records file in the calibration store keyed by
  the device's identity and the workload's features, beside the resource record.  The `mcp_fir.md`
  commitments hold: failures are rows, a measurement is content-addressed (a repeat is a cache hit), cost
  is known before the call (`waveflow.events`), batches go through `SweepRunner`.
- **D8 -- the composition check is part of the plan, not an afterthought.**  Unit-calibrated devices must
  reproduce the system: timing through the system's pysim against its XSI run, with no system-level
  fitting; resources through route B's sum plus link term.  If they do not, the decomposition is wrong,
  and that is the finding.

## Part A -- the unit harness

**Stage 0 -- inventory of what a device needs.**  For markov's two devices and mm_fir's: every port, and
the bus model or stand-in that would answer it (existing, or named as missing).  How the cut override
behaves today with one kernel inside and its neighbours outside -- what fails, and why.  Which host
programs can drive a device alone (markov's host drives both devices; a unit host drives one).  Written
down before code.  Gate: each port of the three devices mapped to an existing model or a named
missing one.

**Stage 1 -- one device, assembled by hand.**  markov's generator device inside the cut, the chain
replaced by a stand-in remote queue with immediate credit, a minimal host driving `qcmd`.  Function
bit-exact against the golden model in pysim and at XSI; cycles per job recorded from port timestamps.
Gate: bit-exact; and the device's per-job timing agrees with the same device's timing traced inside the
full markov run (the existing VCD path) -- the unit view and the system view of one device must not
disagree.

**Stage 2 -- generated.**  The harness derived from a device's ports and the stand-ins of D3, for any
device: markov's generator and chain, mm_fir's device (both topologies).  A stand-in's policy read from
`run.json` (D4).  Gate: the generated harness for Stage 1's device is equivalent to the hand-assembled
one; the other devices pass function and record timing.

**Stage 3 -- timing calibration.**  Sweeps over workloads and device parameters on one `XsiSnapshot` per
device configuration; the kernel's timing model fitted from contention-free runs; records under the
device's identity.  Held-out interior points as the check, per the grid discipline of
`project-cycle-model-training` (at least three sizes, no extrapolation).  Gate: held-out points within
tolerance; a repeated sweep with no change runs no simulation.

**Stage 4 -- the composition check (D8).**  markov and mm_fir's system pysim with unit-calibrated
parameters against their system XSI runs (1870; 618 / 611 cycles), within the existing 5% and with no
system-level fitting; resources from route B beside them.  Gate: both systems within tolerance -- or a
written explanation of what the unit view missed.

**Stage 5 -- robustness.**  Hostile stand-in policies (stall, starved credit, late answers) swept per
device; any hang or mismatch is a finding.  Gate: the policies run on every device harness; whatever
they find is either fixed or recorded with a reproducer.

**Stage 6 -- incremental, end to end.**  Change one device's parameter: only that device's harness
rebuilds, only its route-B synthesis re-runs, the system's estimate updates from records.  A small sweep
over one device parameter (a queue depth or credit window) as the demonstration, its per-point cost from
the events.  Gate: the events show no work for unchanged devices.

**Stage 7 -- docs.**  A guide page on module-level development: a device's harness, its stand-ins and
policies, calibrating one device, the composition check.  markov's docs gain a short section on its two
devices' harnesses beside the system flow.  Name steps and link them.

## Part B -- the device as an agent boundary (sketch)

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
  device harness then needs a credit-stream stand-in, and `ip_packaging_routes.md` leaves the same link
  open.
- **A unit host.**  Generated from the device's views, or a scenario language over them?  The system's
  `SwHost` and its C++ twin are the starting point.
- **Ports that cannot show a firing.**  Where a device's timing hides behind an internal FIFO, when is the
  trace path still needed?
- **How hostile is hostile?**  Which stall policies are worth running by default, and at what cost.
- **Devices with more than one kernel**, if a later grouping produces them.

## Relationship to other plans

- `plans/ip_packaging_routes.md` -- the device, its sub-top, its identity and its resource record. Prerequisite.
- `plans/incremental_xsi.md` -- `XsiSnapshot`, `run.json`, parallel runs: what makes a sweep cheap.
- `plans/system_resources.md` -- the inventory and the records the harness reads and writes beside.
- `plans/paper_cg_dse_vision.md` -- per-block calibration over each block's own parameters is this plan's
  Stage 3; its composition claim is this plan's Stage 4.
- `plans/multi_agent_flows.md`, `plans/mcp_fir.md` -- Part B.
