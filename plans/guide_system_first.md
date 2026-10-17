# Plan: system first -- the guide and the MCP frames, restructured around the system, led by evidence

**Status:** drafted 2026-10-16, before any change; D9 (the protocol pattern) and Stage 0a added the same day, with the user.  The guide is built bottom-up (schema -> interfaces ->
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
- **D9 -- DECIDED 2026-10-16 (the user): the default command-response protocol has a data plane and a
  status plane, and an error is fatal.**  Three streams:

      s_in       : Cmd(tx_id, nsamp, ...) | data
      m_out      : Resp(tx_id, nsamp)     | data          framed as s_in: the header a burst, the data a burst
      m_status   : Status(tx_id, status)                  one per command, after its data

  - **Normal:** the data, then `Status(tx_id, OK)`.  The status is *the last command seen*: on a bus it
    maps to a status register, and on real hardware it is how one sees the kernel is alive.
  - **Error** (an early TLAST, a bad parameter): the kernel closes `m_out` with TLAST, possibly before
    the data is complete, writes `Status(tx_id, <error>)`, and **stops taking `s_in`**.  Only a reset
    recovers.  An error is a sender's bug, not an operating condition.
  - **`nsamp` in the `Resp` header** lets any consumer size the data from the stream itself -- needed
    behind the bus, where the memory-mapped output queue drops TLAST (`slave_views.md`, *Queue out*).
    In the error case the data is short, but the error is fatal anyway.
  - **On the bus:** `m_status` -> a status register **with an interrupt** on an error; the host waits
    on data and on that interrupt, never polls, and keeps a timeout as the backstop against a hang
    with no status written, after which it reads the register and resets.
  - **Variable-length output** (the count is unknown until the end, e.g. detections): `m_out` is a
    tagged union with an in-band end -- `START(tx_id) | item ... | END(tx_id, count)` in one burst --
    and `m_status` is unchanged.  There is no way to avoid the in-band `END`.

  It replaces poly's "errors are answered, not halted on" (`stream_kernel` today: results, then a
  response carrying the status; the kernel takes the next command).  Stage 0a moves the pattern page
  and poly to it before any spec cites them.

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

**Stage 0a -- the pattern, then its reference (D9).**  Before any spec cites them:

1. **The pattern page** (`docs/guide/patterns/command_response.md`): the three streams; the header with
   `nsamp`; the status as the last command seen; an error closes the data, reports, and stops; how
   each stream maps onto the bus (queues for data, a status register with an interrupt); the
   variable-length variant (a tagged union ending in `END`); and why -- the counting a consumer can and
   cannot do, read order across streams, zero-length outputs.
2. **poly (`examples/stream_kernel`)** moved to it: `Resp(tx_id, nsamp)` heads the results, `Status` on
   its own stream, an early TLAST fatal (the kernel stops taking `s_in`).  The golden, `run_iter`, the
   HLS body, `PolyHost` (it sizes the results from the header, and waits on the status interrupt),
   the tests, the protocol pages.  Re-checked: the RTL gate, the calibration (it should not move:
   fill depends on `nwords`), the composition behind the bus (+1.0%) and the device alone (+3.2%), and
   Stage 5's hostile policies, which assume the kernel goes on after an error.
3. **Open, to check first:** how a halted kernel is gated (the error command last; the gate asserts
   the kernel consumed nothing after it), and whether a reset in the middle of a run can be exercised at
   RTL at all (`reference-hls-task-reset-trap`).

Gate: the pattern page and `stream_kernel` agree; every `stream_kernel` gate green with its numbers
re-recorded.

*Record (2026-10-16):* done.  `m_out` frames as `s_in` does -- the `Resp` header its own burst, then
the data -- not one burst: in pysim one write is one burst, and nothing in D9 needs a single one (behind
the bus TLAST is gone; the count is in the header).  The stop is a `static bool halted` under `#pragma
HLS reset` in the task body (an early return per firing; precedent `mm_fir`'s per-firing statics) and
`yield self.event()` in `run_iter`; the harness's `n_cycles` bound ends the run, and the gate is the
recordings: at RTL, four commands answered, the fourth short (6 of 20), the fifth silent -- identical to
pysim.  Re-measured (Vitis/Vivado 2025.1): calibration latency 29 (was 30), fill `nwords - 1`, status 1
(the old response 4), held-out exact, record `poly_kernel-062ade1d` (the old key removed); behind the
crossbar 392 pysim / 385 RTL, +1.8% (was +1.0%); the device alone 383 / 370, +3.5% (was +3.2%); host
traces identical, zero polls.  The host runs `good_cmds()`: the bus-side error path (a status register
with an interrupt, a reset) needs a status-only register view -- the register bank requires a config
port and raises no interrupt -- so `m_status` maps to a queue for now.  Found on the way: `run_dag_cli`
exits 0 when a step fails (a stale `report.json` then reads as a result).

**Stage 0b -- the test cases.**  Planning-only prompts and their scoring, so Stages 1 and 5 measure the
same thing.  Deliverables, in `evaluation/planning_tests/` (outside `docs/` and `examples/`, so the
knowledge index never serves them):

- **A ladder of specs** from communications, the target application, each rung adding one difficulty
  and reusing the one below.  Each spec gives the boundary exactly (fields, widths, order, framing,
  errors -- the packing is the agent's), the architecture in words, the operation with worked examples,
  and the test plan.  Agreed with the user 2026-10-16:

  | # | spec | what it adds | expected reference |
  |---|---|---|---|
  | 1 | `01_qam16_llr`: a 16-QAM max-log soft demapper, fixed point, `WORD_BW` 32/64/128 at `PF` samples a cycle | the flow itself, in `stream_kernel`'s shape | stream_kernel |
  | 2 | block equalizer: `g[i] = conj(h[i]) / (|h[i]|^2 + N[i])`, then `z[i,j] = g[i] r[i,j]` | two input streams, two stages at fixed rates, a division | stream_kernel, interleaver |
  | 3 | `03_peak_detect`: peaks of a correlation metric, two stages (`detect`, `serialize`) | a rate that depends on the data, a wide link serialized, a tagged union ending in `END`, a search over `PF` lanes | stream_kernel, interleaver |
  | 3b | the peak detector with the architecture left open | does the agent find the split?  (after the baseline) | -- |
  | 4 | FFT matched-filter detector | a pipeline around the framework FFT, rung 3's detector at its end | ssr_fft, interleaver |
  | 5 | packet capture | rung 4's detector, a capture kernel, a shared memory, a host | markov |

- **One deliverable format** (`deliverable.md`, linked by every spec, so `blind-test` copies it): the
  agent writes no code; it writes `plan.md` -- the approach and its reference, the modules, the
  interfaces and messages with each one's word layout, the verification mapped onto Waveflow, the
  gaps -- each item citing the snippet it starts from (`example:<name>/<file>::<symbol>`,
  `docs/...md#anchor`, `waveflow.<module>.<symbol>`).  Then it stops.
- **A reference plan per spec** (`answers/<spec>.reference.md`, linked by no spec): what a good plan
  holds, what may differ, what counts against it, and the probes recorded but not scored.  It is the
  answer key, never shown to agents.
- **A scorer** (`plan_grader.py`, in `evaluation/planning_tests/`), reading the trial
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

**Stage 1 -- the baseline.**  The specs on the tree after Stage 0a and before any frame or guide
change, each run twice (agents vary), scored.  Recorded below: the frame chosen, the system form,
whether a module was verified alone, the pages read, tokens.  Expected, and to be confirmed or
refuted: rung 1 goes to the `stream_inband` frame (the only one-kernel frame); no plan verifies a
module alone; the system steps cite subclasses or `GraphSystem`.

*Record (2026-10-17):* one run each of rungs 1-3 (`evaluation/planning_tests/results/baseline_2026-10-17.md`).
Every plan would build with small fixes: the agents found `stream_kernel` from the example cards with no
frame (the menu offered three wrong ones), followed it closely, used `harness` -- per stage, at rungs 2
and 3 -- and found four errors in the specs (fixed) and a dozen real framework gaps (below).  **The user
then changed the method:** do not baseline the complex rungs further -- the docs and frames must be
rewritten regardless, so the delta is not the point.  **Write and test in tandem:** each concept the
guide or a frame explains gets a blind test when it is written; rungs that need it get new examples.

**Framework items the baseline surfaced** (each recorded with its evidence in the results file):

- **`DataUnion` is not a `DataSchema`** (the user, 2026-10-17: it should derive from the base class).
  It has its own serializer and C++ struct, so none of the base's generated readers and writers --
  array, stream, AXI4-Stream, framed -- reach it, and a stream's `write` takes it only as words.
  Deriving it from `DataSchema` gives all of them through one hook (`_gen_write_recursive` /
  `_gen_read_recursive`: the header's fields, then a `switch` on `schema_id`).  **To decide first:**
  fixed size (padded to the largest payload, as today -- `nwords_per_inst` constant, the base machinery
  as is) or variable length (a reader reads the tag, then the variant's own words -- what a
  variable-length output wants, a larger change to the base).  **DECIDED (the user, 2026-10-17): fixed
  length**, and the documentation says so (`schema/python/dataunion.md`, *Fixed length, by design*).
  The refactor to derive from `DataSchema` is still to do.
- A Python framed `write` is one burst: no way to write a burst in pieces (TLAST low).  **Deliberate**
  (the user, 2026-10-17): a write in pieces would invite serializing and deserializing by hand.  The
  cost -- data-dependent structures (a variable number of records, a `DataUnion` per record) cannot be
  written as they are made -- is acknowledged, to be addressed later.
- ~~A stage's hand-written body cannot be RTL-harnessed alone~~ -- **WRONG, corrected 2026-10-17**:
  `KernelTask(framed=(...))` makes each listed port's word type a template parameter the generator
  supplies (`framed_word` inside a composite, `axi4s_word` at a top), "so one body serves a composite
  and the kernel's own harness" (`waveflow/hw/mem_stream.py`; markov uses it).  The real finding is a
  **doc** one: a careful agent concluded it did not exist.  The guide must show it.
  **Done 2026-10-17:** `docs/guide/comp_codegen/freerunning_override.md#framed`, and the helpers a body
  needs to never name a word type -- `streamutils::read_msg` (beside `write_msg`) and every element's
  `read_boundary_lane` / `write_boundary_lane`.  Both headers are copied into every example: a full
  re-synthesis precedes the next full `-m xsi` run.  **Done 2026-10-17:** `WAVEFLOW_RESYNTH=1 pytest -m xsi` -- every example
  re-synthesized, **221 passed**, 0 failed, 37 min (Vitis/Vivado 2025.1).
- No TKEEP / lane count through the lane routines (a partial last word is invisible).
- `harness` around a composite: in no example.  One class at several widths (`param_supports`): in no
  example.  `ComplexField` in a `DataList`, and lane routines for a `DataList` element: in no example.
- No timed-phase pattern for a data-dependent rate.  ~~`run_dag_cli` exits 0 when a step fails~~ -- **fixed**
  2026-10-17 (the build test, below).
- **Fixed 2026-10-17:** a framed internal link wider than the boundary was declared at the boundary's
  width (`FramedEdge`); it now carries its own, as `StreamEdge` did.  `write_msg` takes no TLAST argument:
  to do after the re-synthesis run (it is in `streamutils_hls.h`).
- A queue-in packet longer than the queue cannot be written on the interrupt (the waiting `write` needs
  room for all of it; splitting puts TLAST on each piece) -- the input queue must hold the largest
  command.  A harness run with no host reports no cycle count (`cycles: -1`).

*Record (2026-10-17, overnight):* written and tested in tandem -- the `stream_kernel` frame (rung 1: chosen
from the menu, -36% tokens, every probe), `docs/guide/systems/` (the lead page and the recipe), `bus_system`
pointed at `stream_kernel_mm` for one device and new test `01b` (one kernel on a bus: every must-have).
Results in `evaluation/planning_tests/results/baseline_2026-10-17.md`.

**Stage 2 -- the references.**  markov and mm_fir to `HwSystem(sim, dut)` (D3), their gates unchanged
(markov 1870 cycles at RTL, pysim -1.6%); their example pages updated.  `stream_kernel` checked for
the stub anchors a scaffold needs.

*Record (2026-10-17):* **done** -- `markov_system()` and `mm_fir_system()` return `HwSystem(sim, dut)`
(the subclasses removed; pysim cycles unchanged, 1682 / 1823 and 608); pages and `xsi_system.md` lead
with the direct form.  Then `write_msg(..., last)` and a `WAVEFLOW_RESYNTH=1 pytest -m xsi` run: 218 of
221 at once, the 3 others two csynth failures that passed on a rerun (the mimo_cg unit bench's m_axi
lowering, which its message says to re-run, and markov's core unit, both while a blind build ran Vitis
beside them) -- **221 passed**, markov and mm_fir gates included (Vitis/Vivado 2025.1).

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

**Stage 6 -- a full build.**  Two specs built for real, through the RTL gates: rung 1 (`01_qam16_llr`) and
rung 5, packet capture (the simplest and the hardest).  A plan can cite every right snippet and still produce
parts that do not fit together; only a build shows that.  Gate: both reach their RTL gate; every failure
on the way is a finding, fixed in the frame or the guide, or recorded.

*Record (2026-10-17), rung 1:* built blind from the `stream_kernel` frame, 200 turns, 31 min, 31.5M tokens
in (99% cached), 96k out.  32 and 64 bits pass every gate at RTL (9/9 scenarios each; 4107 / 2059 cycles
for 4096 samples, PF samples per cycle); 128 bits was blocked by a framework defect (the harness wrote
>64-bit words flat) -- fixed, with four more the build found (csynth freshness ignored the `.tcl`;
`--force-step csynth` did nothing; a failed step exited 0; `validate_schema` failed on a `@dataclass`).
`evaluation/planning_tests/results/build_01_2026-10-17.md`.

*Record (2026-10-17), rung 5:* built blind from the `bus_system` frame (markov as reference), 285 turns,
52 min, 66.9M tokens in (99% cached), 215k out.  Every functional gate passes at RTL (each kernel alone,
the system's host traces identical to pysim's and the golden, zero polls); composed timing -6.4% of RTL
against the spec's 5% (the memory writer's model under contention).  Fixed from it: a credit write
reordered ahead of `wait_room` deadlocks at RTL (the guard); `SwHost`'s master serializes as the RTL
host does; `wf_cint.h` written by `ArrayUtilsStep`.
`evaluation/planning_tests/results/build_05_2026-10-17.md`.  **Stage 6 gate met: both reach their RTL gate.**

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
