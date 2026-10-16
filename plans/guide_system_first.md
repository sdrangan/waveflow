# Plan: system first -- the guide and the MCP frames, restructured around the system, led by evidence

**Status:** drafted 2026-10-16, before any change.  The guide is built bottom-up (schema -> interfaces ->
simulation -> flows -> codegen), and the system arrives halfway through, three levels deep.  This plan
turns it around -- *Waveflow models concurrent systems* -- and restructures the MCP frames on the same
model, because **most users of Waveflow are now agents**, and an agent meets the frames first and the
guide second.  The order of work is set by evidence: a **planning-only blind test** is run on the
unchanged tree first (the baseline), and again after the frames and the slice of the guide they point
to are rewritten.

## Motivation

Three things have moved ahead of the docs:

1. **The system flow is the only flow** (`plans/system_model.md`).  The guide still splits on the DUT
   -- *Sequential (host-activated)* and *Concurrent (free-running)* under *Hardware modules and Flows*
   -- and the system is `flows/concurrent_system.md` at nav order 0.5 under *Concurrent*.
2. **The unit harness is complete** (`plans/unit_harness.md`, PRs #277-#283).  Any module in the DUT
   tree can be run alone (`harness(module)`), calibrated alone, and composed into its parent.  It is
   documented on one page under *Flows* (`flows/units.md`) and is in no frame's process.
3. **`HwSystem(sim, dut)` (unit_harness D12)** is the form a system should be written in.  Only
   `stream_kernel_mm` uses it; markov and mm_fir subclass `HwSystem`; fourteen examples use
   `GraphSystem`.  The guide (`build/xsi_system.md`, `flows/concurrent_system.md`) teaches the older two.

And the MCP frames (`waveflow/mcp/frames/`, `plans/mcp_frames.md`) are built on the same split:

| frame | shape | flow | reference | scaffold |
|---|---|---|---|---|
| `stream_inband` | one host-launched kernel | `SeqTB`, Vitis csim / cosim | stream_inband | **the only one** |
| `freerun_pipeline` | a composite of tasks, `MemRStream` / `MemWStream` | the testbench graph, generated XSI testbench | memcpy, interleaver | no |
| `bus_system` | several kernels, a memory, a host on a crossbar | the system DAG | markov, mm_fir | no |

- The simplest spec -- *a function on a stream* -- goes to the legacy flow, with the only scaffold.
- The **flow** axis no longer separates anything (one flow), and the **pattern** axis is
  command-response in every frame: frames differ only in **shape**.
- No process verifies a module alone before the system is assembled: in `bus_system` the first RTL
  gate is the whole system, so "say which layer failed" has nothing per-module to go on.
- The processes teach the subclass and `GraphSystem` forms.
- `bus_system`'s process lists fifteen `get_doc` / `get_example` calls for markov -- a reading order
  that duplicates what a guide recipe would say, and will drift from it.

## Decisions

Proposed 2026-10-16; each is confirmed or changed with the user before the stage that depends on it.

- **D1 -- the guide leads with the system.**  The lead section is the system figure
  (`flows/figures/system_cut.svg`) walked top-down -- the system, its DUT tree, the devices, the
  modules, the interfaces, the schemas -- with forward links to the sections that define each.  The
  sections that define them (modules, interfaces, schemas, simulation, codegen, hooks, build) become
  reference, in that order.
- **D2 -- one flow; the *sequential Vitis flow* is legacy, host-activated *modules* are not.**  `SeqTB`,
  the generated Vitis testbench, cosim as the gate (`flows/sequential*.md`,
  `comp_codegen/testbench.md`, `comp_codegen/host_launch.md`) move to a Legacy section.  An
  `ap_ctrl_hs` kernel behind `s_axilite` stays a kind of module and of device (`comp_codegen/hostactivated.md`,
  register maps) -- the XRT target needs it (`project-xrt-target`).
- **D3 -- one system form is taught: `HwSystem(sim, dut)`.**  markov and mm_fir move to it before the
  pages that quote them are rewritten; `GraphSystem` is documented as the older form until its
  examples move (a follow-on, not this plan).
- **D4 -- the vocabulary is unit_harness's:** a system is flat (a testbench and a DUT); the DUT is a
  tree of modules; any module in it is a **unit** (the docs say "unit", not `SynthUnit`, as
  `flows/units.md` already does); a **device** is a composite packaged as one IP; a **harness** is an
  `HwSystem` whose DUT is one module; the **cut** is a build choice.
- **D5 -- one process, frames are shapes.**  The shared process is the recipe: the specification (stop
  for review), each module in its harness, the system, and timing when the spec asks for cycles.  A
  frame supplies only its shape: references, what it fixes, what a spec must decide, its traps, an
  optional scaffold.  The frames form a ladder -- one kernel on streams, one kernel behind a bus with a
  host, a pipeline with memory, several devices on a bus -- and `stream_inband` becomes the legacy
  `host_launched` frame, chosen only when a spec requires host launch.
- **D6 -- one source for the steps.**  The guide's recipe page holds the steps; `_common/process.md`
  keeps what only agents need (the stop, the tool table, never edit a generated file) and names the
  recipe's steps by anchor.  The frame lint checks every anchor resolves, and a test pins the frame
  menu and the guide's ladder table to the same frames.
- **D7 -- evidence first.**  The guide's order beyond the slice the frames point to is decided from
  what blind-test agents read, not from a guess.  A plan-only blind test is cheap (no csynth, no XSI)
  and is run before any change and after the frames are rewritten.
- **D8 -- reorder, do not regroup.**  Sections move by `nav_order`; no new parent pages above existing
  sections (Just the Docs binds `parent:` by title string, `reference-docs-nav-title-collision`).

## The target

### The guide

| # | section | from |
|---|---|---|
| 0 | Installation | unchanged |
| 1 | **Systems** (new lead): the figure walked top-down; **A system in code** (`stream_kernel_mm`'s `HwSystem(sim, dut)` in full, markov as the larger case); **The system flow** (csynth, system_rtl, scenario, pysim, system_xsi, compare -- named and linked); **Building a system** (the recipe: decompose top-down, write bottom-up, each module in its harness, then the system) | `flows/concurrent_system.md`, `flows/concurrent_flowsteps.md`, the concept half of `build/xsi_system.md` |
| 2 | Design patterns | unchanged |
| 3 | **Modules**: the formal definition; free-running leaf and composite; host-activated; RTL modules; testbench participants (`SwHost`, drivers and sinks, `bfm_model`); roles, not classes; parameterization | `flows/modules.md`, `flows/concurrent_python.md`, the module half of `flows/sequential_python.md`, `flows/parametrization.md` |
| 4 | Interfaces | unchanged; each page's summary says where it sits in a system |
| 5 | Data schemas, Vectorization | unchanged; the same |
| 6 | Simulation | reframed as the pysim engine an `HwSystem` holds |
| 7 | Module code generation, Custom hooks, Build system | `build/xsi_system.md` keeps the `SystemTopSpec` reference and freshness |
| 8 | **Developing module by module**: calibrating a unit, composing a parent, hostile stand-ins, incremental builds | `flows/units.md`, grown |
| 9 | Timing models, Calibration, Timing / Resource tools, Resource models | unchanged |
| 10 | Libraries: memory, RF, DSP, Vitis L1, linear algebra, CPU, platforms | unchanged |
| 11 | Toward the board | `flows/bitstream_ipi.md`, `build/ipi.md` |
| 12 | **Legacy: the sequential Vitis flow** | `flows/sequential*.md`, `comp_codegen/testbench.md`, `comp_codegen/host_launch.md` |
| 13 | AI tooling, Developers | unchanged |

`docs/guide/flows/` is dissolved into 1, 3, 8, 11 and 12.  The guide index's *model it / generate it /
hand-write it* framing moves to the Modules section, as the relation between 3 and 7.

### The frames

| frame | DUT | testbench | references | scaffold |
|---|---|---|---|---|
| `stream_kernel` | one free-running kernel | stream stand-ins (its harness) | stream_kernel | yes: stub the arithmetic in the golden and the HLS body |
| `bus_system` | one or more devices, an optional memory, a crossbar | a `SwHost` | stream_kernel_mm (one device), markov (several), mm_fir (a register bank) | open (Q2) |
| `freerun_pipeline` | a composite of tasks, memory through the stream adaptors | stand-ins and a memory | memcpy, interleaver | no |
| `host_launched` (legacy) | one `ap_ctrl_hs` kernel | `SeqTB` | stream_inband | yes, as today |

## Stages

**Stage 0 -- the test cases.**  Planning-only prompts and their scoring, so Stages 1 and 5 measure the
same thing.  Deliverables, in `examples/mcp_test/plan_only/`:

- **Four specs**, naming no frame and no example, one per rung of the ladder, each small enough that
  its plan fits on a page:

  | spec | the system | expected frame (after Stage 4) | expected references |
  |---|---|---|---|
  | `gain_clip_stream` | a function on a stream: a command (`tx_id`, `n`, a gain), `n` int16 samples, scaled, rounded and saturated; a response echoing the command with a clip count; a short burst answered, not halted on | `stream_kernel` | stream_kernel |
  | `gain_clip_device` | the same kernel reached by a host over AXI-MM: queues in and out, the host on interrupts, two jobs in flight, no polling | `bus_system` | stream_kernel_mm |
  | `scale_copy` | read a vector from on-chip memory, scale it, write it back, started by a command stream; no host program on a bus | `freerun_pipeline` | memcpy |
  | `abc_system` | kernels A (scale), B (a running sum per block) and C (a histogram into a shared on-chip memory), A -> B direct, B -> C over the bus with credit, a host that sends jobs and reads the histogram back on an interrupt | `bus_system` | markov |

- **One deliverable format, stated in every spec:** the agent writes no code; it writes `plan.md` --
  (a) the frame or reference it chose and why; (b) the DUT tree and the testbench; (c) the build as
  ordered steps, each naming the Waveflow API it uses and the **exact snippet it would start from**, as
  `example:file::symbol` or `docs/...md#anchor`; (d) the gates, and what each checks; (e) what it could
  not find.  Then it stops.
- **A reference plan per spec** (`<spec>.reference.md`), written by hand from the tree as it will be after
  Stage 4: the frame, the DUT tree, the steps, the snippets.  It is the answer key, not shown to agents.
- **A scorer** (`plan_grader.py`, beside `examples/mcp_test/grader/rotate_grader.py`), reading the trial
  folder's `plan.md` and the run's `summary.md`:

  | measure | how |
  |---|---|
  | citations resolve | every `example:file::symbol` and doc anchor exists -- the frame lint's checks (`tests/mcp/test_frames.py`), factored into a function both call |
  | frame | chosen vs expected |
  | system form | the form its system step cites: `HwSystem(sim, dut)`, an `HwSystem` subclass, `GraphSystem`, `SeqTB` |
  | per-module verification | a step that runs a module alone (`harness`) before the system |
  | steps covered | the reference plan's steps the agent's plan has, by the API each names |
  | what it read | guide pages and example files read, from `summary.md`; pages never reached |
  | cost | tokens and tool calls, from `summary.md` |

  The last two are what Stage 7 is ordered by.  A manual rating (would this plan build?) is added by
  hand, in three levels.

Gate: one spec run end to end through `waveflow blind-test --prompt ...` and scored; the scorer's output
checked by hand against the plan it scored.

**Stage 1 -- the baseline.**  The four specs on the unchanged tree (`main` at the start of the plan),
each run twice (agents vary), scored.  Recorded below: the frame chosen, the system form, whether a
module was verified alone, the pages read, tokens.  Expected, and to be confirmed or refuted:
`gain_clip_stream` goes to `stream_inband`; no plan verifies a module alone; the system steps cite
subclasses or `GraphSystem`.

**Stage 2 -- the references.**  markov and mm_fir to `HwSystem(sim, dut)` (D3), their gates unchanged
(markov 1870 cycles at RTL, pysim -1.6%); their example pages updated.  `stream_kernel` checked for
the stub anchors a scaffold needs.

**Stage 3 -- the guide slice the frames point to.**  The Systems section of the target: the lead page,
*A system in code*, *The system flow*, *Building a system* -- written new, on D4's vocabulary, every
step named and linked.  The pages they replace stay in place until Stage 7 moves them; each gets a line
pointing to its successor.

**Stage 4 -- the frames.**  The shared process rewritten on the recipe (D5, D6); `stream_kernel` added
with its scaffold; `bus_system` re-referenced (stream_kernel_mm first); `freerun_pipeline` on
`HwSystem(sim, dut)`; `stream_inband` renamed `host_launched`, its `choose_when` narrowed to specs that
require host launch.  The frame lint gains the anchor check; the menu-vs-guide test added;
`docs/guide/ai_tooling/frames.md` rewritten; `server.py`'s `INSTRUCTIONS` checked for a stale frame
name.  Gate: the frame and MCP tests (`tests/mcp/`), the scaffold building a fresh `stream_kernel`
project through pysim.

**Stage 5 -- the planning-only test again.**  Stage 1 repeated on the new tree, the same specs and
scorer.  Gate: every spec chooses its expected frame in both runs; every plan verifies modules alone
before the system and cites `HwSystem(sim, dut)`; citations resolve; whatever regressed is explained.

**Stage 6 -- a full build.**  Two specs built for real, through the RTL gates: `gain_clip_stream` and
`abc_system` (the simplest and the hardest).  A plan can cite every right snippet and still produce
parts that do not fit together; only a build shows that.  Gate: both reach their RTL gate; every failure
on the way is a finding, fixed in the frame or the guide, or recorded.

**Stage 7 -- the rest of the guide.**  The target's remaining sections, in an order the read logs of
Stages 1, 5 and 6 justify: Modules; `flows/` dissolved; Legacy; `nav_order`; a "where this sits in a
system" line in each reference section's page summaries; the guide index; CLAUDE.md's *Designing an
accelerator* pointer to the recipe.  Gate: the docs tests (snippets, links, the knowledge corpus) and a
`waveflow kb search` for "build a system" whose top hit is the recipe.

**Follow-on, not this plan:** the `GraphSystem` examples to `HwSystem(sim, dut)`, one at a time, with an
"older form" banner on each example page until it moves; what becomes of `basic_vec` and `regmap`.

## Open questions

- **Q1 -- the Legacy section's scope**: the flow only, or the three host-activated examples too
  (`basic_vec`, `regmap`, `stream_inband`)?
- **Q2 -- the `bus_system` scaffold**: from `stream_kernel_mm` (one device and its host, the
  arithmetic stubbed), or none?
- **Q3 -- timing in acceptance**: every frame's last stage, or only when a spec gives cycle targets?
- **Q4 -- the plan-only cost**: is two runs per spec enough to tell a change from noise?  Stage 1's
  spread decides.

## Relationship to other plans

- `plans/unit_harness.md` -- the harness, the unit vocabulary (D1, D2, D11, D12) and the first example
  (`stream_kernel`, its Stage 7) this plan builds on.
- `plans/mcp_frames.md` -- the frames, the shared process, the frame lint and the blind tests this plan
  restructures.
- `plans/system_model.md` -- the system flow as the flow; its migration of the examples is the
  follow-on above.
