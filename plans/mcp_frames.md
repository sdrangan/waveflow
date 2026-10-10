# Plan: frames for the key architectures -- and an agent that chooses before it follows

**Status:** drafted 2026-10-09.  The MCP server was built to push an agent into exactly one
architecture, `stream_inband`; this plan makes it **choose** an architecture from the spec it was given,
adds two frames -- a **bus system** and a **free-running pipeline** -- and keeps the `stream_inband`
lab flow working exactly as it does when a prompt names its frame.

## Motivation: an audit of what the agent is given

Every layer of the chain an agent meets defaults to `stream_inband`:

| # | Where | Today | Effect |
|---|---|---|---|
| 1 | `waveflow/mcp/server.py`, `INSTRUCTIONS` -- the one text every client injects | "Asked to build an accelerator? Call `waveflow_get_process` first, **before reading source or writing anything**." | the process, before the agent has looked at one example |
| 2 | `waveflow_get_process(frame=None)` | `DEFAULT_FRAME = "stream_inband"` ("the lab needs exactly one") | no frame -> stream_inband's process and its 219-line protocol |
| 3 | `frames/stream_inband/process.md` | "**Your reference design is `stream_inband`.**" | named, whatever the spec |
| 4 | `waveflow_list_frames` | one frame | nothing to choose between |
| 5 | `waveflow_new_accel_project(frame=None)` | stream_inband, renamed, compute stubbed, its process as `AGENTS.md` | the project *is* a stream_inband design |
| 6 | `blind_test.py`, `WAVEFLOW_FIRST` | "...its stream_inband example is the reference design to follow." | named again in the first message |

Two smaller nudges: `waveflow_get_example`'s description uses `'stream_inband'` as its example, and the
blind-test summary leads with "`get_process` / `new_accel_project` never called".

What works already: `waveflow_list_examples`.  Its cards say what each design is for (markov: "two
free-running kernels that talk over a shared bus ... four bus masters on one crossbar"), and its
description says to call it "before starting a new design to choose the closest reference".  Layer 1
sends the agent elsewhere first.

The frame mechanism is right for the task it was built for -- **"put this function into this
architecture"**: a lab prompt gives a function, and `frame.md` supplies the protocol, the errors and the
report.  It is wrong for **"build a system from this spec"**, where choosing the architecture is the
first part of the task.  Both tasks stay; the chain learns to tell them apart.

## What a frame is

A **frame** is a design pattern realized in one system shape and one realization flow, with a reference
example that proves it:

| axis | values | what it sets in the frame |
|---|---|---|
| **pattern** (`docs/guide/patterns/`) | command-response, continuous stream, configured stream, one-shot | the contract the spec must settle |
| **shape** | one kernel; a pipeline of free-running tasks; several kernels on a bus | the structure, and the reference to copy |
| **flow** | host-launched (csim / cosim); free-running (XSI testbench from the testbench graph); system (the system DAG, `system_xsi`, the trace gate) | the steps and the gates |

The frames this plan ends with:

| frame | pattern | shape | flow | references | scaffold |
|---|---|---|---|---|---|
| `stream_inband` (exists) | command-response, in-band | one host-launched kernel | csim / cosim (`SeqTB`) | `stream_inband` | yes |
| `freerun_pipeline` (new) | command-response through a pipeline | a composite of free-running tasks, memory through `MemRStream` / `MemWStream` | XSI testbench from the testbench graph, exact cycles | `mem_copy`; `interleaver` for a compute stage and on-chip random access | no |
| `bus_system` (new) | command-response, routed | several free-running kernels and an on-chip memory on one crossbar, a `SwHost` | the system DAG (`add_system_steps`), the trace gate | `markov`; `mm_fir` for a kernel reached over the bus | no |

A pattern the guide names but no frame covers yet (the continuous stream: `rf_loopback`) is reached the
way any uncovered spec is: through the example cards.

## The chain after the change: choose, then follow

1. **Instructions** (`INSTRUCTIONS`): read the spec; if it names a frame, `waveflow_get_process(frame)`
   and follow it; otherwise `waveflow_list_frames()` and pick the frame whose pattern, shape and flow
   match the spec; if none does, `waveflow_list_examples()` and pick the closest reference, then
   `waveflow_get_process()` for the generic process.  The rules that hold everywhere stay (generated
   files, no hand-packing, stop and report what the machinery cannot express).
2. **`waveflow_list_frames()`** is the architecture menu: each frame with its pattern, shape, flow,
   references, a one-line "choose it when", and whether it has a scaffold.
3. **`waveflow_get_process(frame=None)`** returns the **generic process** -- the frame-independent part
   -- and the menu; with a frame, the generic part followed by the frame's own.  `DEFAULT_FRAME` goes.
4. **`waveflow_new_accel_project(name, frame)`**: `frame` is required in effect (null -> an error naming
   the frames that have a scaffold); a frame without a `[template]` is refused with a pointer to its
   reference's build page.
5. **`waveflow_list_examples` / cards**: unchanged; they are the fallback, and each card gains the
   frame(s) that use it, if any.
6. **Blind test**: the default first message names no example; `--message` still can.

## Decisions

* **D1 -- the generic process is one file, composed at load time.**
  `frames/_common/process.md` holds what every frame shares: the tool table, "assume you do not know the
  API", choosing a reference, the two-stage freeze (write the spec artifacts, stop for review, then
  build), generated files, no hand-packing, report what cannot be expressed.  A frame's `process.md`
  holds only its own steps.  `Frame.process` returns the two joined, so `get_process` and the scaffold's
  `AGENTS.md` still read one text, and the shared part cannot drift between frames.
  `stream_inband/process.md` is split accordingly; its rendered text stays equivalent (a test compares
  the sections).
* **D2 -- `frame.toml` gains the axes**: `pattern` (a guide pattern page), `shape`, `flow`,
  `choose_when` (one line), `reference_examples` (the first is the primary), and the existing optional
  `[template]`.  `list_frames` reports them.  `_`-prefixed directories are not frames.
* **D3 -- `frame.md` is the contract for a scaffolded frame, the decisions for the others.**
  `stream_inband`'s fixes the protocol so a prompt can be a function.  A new frame's lists **what the
  frame fixes** (rules a design in it must follow) and **what a spec must decide** (the checklist Stage 1
  answers) -- the role `docs/examples/stream_inband/decisions.md` plays today.
* **D4 -- no scaffold for the new frames at first.**  `stream_inband` reduces to "stub one function"; a
  bus system (two kernel bodies, a host's C++ twin, the wiring) and a pipeline (several task bodies, the
  testbench graph) do not.  Add one when a blind test shows agents copying the reference badly.
* **D5 -- every frame is linted.**  A test reads each frame's `process.md` and `frame.md` and checks that
  every `waveflow_get_example(name, file=...)` names a real file, every doc path exists, every
  backticked Waveflow name exists (`waveflow_find_usage` finds it, or the package exports it), and every
  reference example is in the curated list.  Frames are text that names code; the lint is what keeps
  them true as the code moves.
* **D6 -- the lab flow does not change.**  The lab's prompts and the existing blind-test specs name
  `stream_inband` in their own text (`examples/mcp_test/rotate.md`: "Use the Waveflow stream_inband
  example"), and `waveflow new-accel ... --frame stream_inband` is the CLI's documented form.  Removing
  the default changes nothing for a prompt that names its frame; a lab prompt that does not must say so
  (one line), and the plan checks the lab's prompts in Stage 1.

## The two new frames

### `bus_system`

`frame.toml`: pattern `command_response`; shape "several free-running kernels and an on-chip memory on
one crossbar, a software host"; flow "system DAG"; references `markov`, `mm_fir`; choose when "the
design has more than one kernel, or a host that reaches a kernel over a bus, or kernels that share a
memory"; no template.

`frame.md` -- **fixed by the frame:**
1. every kernel is a `FreeRunMod` whose logic reads and writes only streams
   (`docs/guide/patterns/stream_only.md`); bus access is through `build_mm_device` views: queue in,
   queue out, register bank, credit-in;
2. every job is a command carrying `n` and a `tx_id`, and its response echoes them; through a pipeline,
   the command travels with the data and the last stage answers;
3. one crossbar; the host is a `SwHost` with one bus master (`add_bus_master`) and **never polls** --
   every wait is on an `IrqIF` from a view;
4. kernels joined directly use a `StreamIF` / `CreditStreamIF`; a kernel writing another kernel's queue
   over the bus uses an `MmCreditStreamIF`, placed with `.place(...)`, its credit window covering the
   link's round trip (`docs/guide/interface/axi_mm/credit_streams.md`);
5. memory is on-chip (a `MemoryMod` in the cut); off-chip memory is refused until
   `plans/offchip_memory.md` lands;
6. addresses come from `assign_address_ranges`, never typed;
7. verification: a golden model; pysim bit-exact; the DAG through `compare` with `synth="check"`; the
   host's traces byte-identical between pysim and RTL; the RTL cycle count recorded and pysim within 5%.

**decided by the spec:** the kernels and each one's job and schemas; each link (direct or routed, depth,
credit window); each kernel's views and their order (the address layout); the memory and the regions
the host hands out; the host's threads, jobs in flight and what it reads back; the scenario and its
golden outputs.

`process.md` -- before you start: read `markov` in the order its pages run (system, host, credit link,
build flow, code generation, synthesis, XSI testbench), and `mm_fir` for a kernel reached over the bus.
**Stage 1** (then stop): schemas, the golden model, the scenario, a block diagram, the decisions above.
**Stage 2:** the Python modules and the system class (`build_mm_device`, the crossbar,
`assign_address_ranges`, `.place`, `IrqIF`s); the host and `scenario_bursts()`; gate: pysim bit-exact
(`--through pysim`); `codegen` (headers, kernel tops and hand-written bodies -- read
`loop_optimization.md` first, use the lane routines -- and `write_writer_project` for each writer);
the DAG (`codegen` + `add_system_steps` on `run_dag_cli`); the host's C++ twin and its `decode()`;
gate: the DAG through `compare`, read back with `load_run`, the cycle count and the pysim gap reported.
**Known traps** (each has cost a debugging session here): a write then a blocking read on two streams
deadlocks (use `read_nb`); an un-paced free-running pipeline deadlocks; an `hls::task` that writes
before it reads counts as in reset; a deadlock at RTL looks like a hang, so a gate asserts counts;
one body edit re-synthesizes every top; a deep Windows path fails csynth with no error text.

`prompts/`: `01_scale_sum.md` (two kernels, a job's samples scaled then summed; joined directly, then
routed over the bus); `02_threshold_cfg.md` (one kernel reached over the bus: a level detector whose
threshold the host changes mid-stream through a register bank -- the `mm_fir` shape);
`03_pipeline_histogram.md` (generate -> filter -> histogram into the shared memory, read back by the
host; credit on the routed link; two jobs in flight).

### `freerun_pipeline`

`frame.toml`: pattern `command_response` (through a pipeline); shape "a composite of free-running
tasks reading and writing memory through stream adaptors"; flow "XSI testbench generated from the
testbench graph; exact cycle gate"; references `mem_copy`, `interleaver`; choose when "one accelerator
built from several stages that stream to each other, reading and writing memory, with no host program
on a bus"; no template.

`frame.md` -- **fixed by the frame:**
1. the accelerator is a composite `FreeRunMod`; each stage a task (`kernel_task()`), stream-only; memory
   is reached only through `MemRStream` / `MemWStream` (and their framed variants), whose timing
   models ship pre-fit;
2. the command travels with the data: the first stage reads it, every stage forwards what the next
   needs, the last stage answers; nothing is decided from a side channel;
3. every stage is paced -- no un-paced free-running loop (it deadlocks at RTL);
4. a word is packed and unpacked only with the generated lane routines;
5. verification: a golden model; pysim bit-exact; the generated XSI testbench (the testbench graph:
   `FlatMemory`, the stream drivers and sinks) bit-exact at RTL; the cycle count recorded as an exact
   gate, and pysim's estimate within the reference's tolerance -- Vitis cannot co-simulate
   free-running tasks, so XSI is the only RTL check.

**decided by the spec:** the stages and the job each does; the messages between stages (what each
forwards); the memory regions and element types; the parallelism (elements per word, `PF`); the
scenario and its golden outputs; whether any stage needs on-chip random access (then `interleaver`'s
BRAM pattern) or state across firings (`fir_block`'s `add_state`).

`process.md` -- before you start: read `mem_copy` (its pages: module, Python, testbench, DUT codegen,
testbench codegen, RTL simulation, timing) and, for a compute stage, `interleaver`.  **Stage 1** (stop):
schemas, the golden model, the scenario, the stage diagram, the decisions above.  **Stage 2:** the
stage modules and the composite; gate: pysim bit-exact; DUT codegen (the tops from the graph, the
hand-written task bodies -- `loop_optimization.md` first); testbench codegen; csynth; gate: the XSI run
bit-exact and its cycle count recorded; pysim against it.  **Known traps:** as for `bus_system`, minus
the bus ones, plus the free-run pacing rule and the boundary-TLAST rule (`ap_axis`).

`prompts/`: `01_scale_copy.md` (read a vector, scale it, write it -- `mem_copy` plus one compute
stage); `02_gather.md` (a permutation by an index vector, on-chip -- the `interleaver` shape with a
different access); `03_block_stats.md` (per block of `n`: min, max, sum -- a stage with state across a
block, results written to memory).

## Stages

Each stage keeps `pytest -m "not vitis and not xsi"` green.  The MCP tests are the gate for the code
stages; blind tests are the gate for the text.

**Stage 1 -- frames as a menu; the generic process.**  D1, D2: `frames/_common/process.md`, the
`stream_inband` split, `Frame.process` composed, the axes in `frame.toml` and in `list_frames`;
`get_process(None)` returns the generic process and the menu; `DEFAULT_FRAME` removed;
`new_accel_project` without a frame refused with the scaffoldable frames named.  Check the lab's prompts
and `examples/mcp_test/` name their frame (D6).  Update `tests/mcp/test_frames.py`,
`test_scaffold.py`, `test_server_smoke.py`, `test_tool_errors.py`, `tests/examples/test_mcp_tools.py`.
Gate: those tests; a scaffolded `stream_inband` project still passes `--through py_sim` unedited, and
its `AGENTS.md` carries the same sections as before.

**Stage 2 -- the instructions and the tool descriptions.**  `INSTRUCTIONS` as in the chain above;
`waveflow_get_process`'s and `waveflow_list_frames`'s descriptions; a neutral example in
`waveflow_get_example`'s.  Gate: `test_server_smoke.py` (the instructions reach a stdio client), and the
retrieval evals (`test_retrieval_eval.py`, `test_paraphrase.py`) unchanged -- this stage changes what
the agent is told to do first, not what search returns.

**Stage 3 -- the frame lint (D5).**  `tests/mcp/test_frames.py::test_frame_text_names_real_things`
over every frame; run against `stream_inband` first, which must pass as it stands (or the lint has
found real rot, which is fixed here).

**Stage 4 -- `bus_system`.**  The four files as above; the lint passes.  The prompts are checked by
hand against `markov` / `mm_fir`: each must be buildable with what the frame fixes (no off-chip memory,
no AXI-Lite).

**Stage 5 -- `freerun_pipeline`.**  The same for the pipeline.  Before writing it, settle which
`interleaver` files a frame may point to: the directory holds sandboxes, retired variants and logs
beside the model files, and the card's hook list is the place to start.

**Stage 6 -- the blind-test harness.**  `WAVEFLOW_FIRST` names no example; `summary.md` leads with
**choice**: was `waveflow_list_frames` or `waveflow_list_examples` called before the first write; which
frame was passed to `waveflow_get_process`; which references were read; was a scaffold requested, for
which frame.  `docs/guide/ai_tooling/blind.md`'s options table updated.  Gate: `test_blind_test.py`.

**Stage 7 -- blind tests (the real gate for Stages 1-6).**  With the neutral message, `--no-approve`
first (the specification stage only, cheap), then the full run for the ones that choose right:
* `bus_system/prompts/01_scale_sum.md` and `freerun_pipeline/prompts/01_scale_copy.md`, each with the
  frame name **removed** from the prompt -- does the agent pick the right frame from the spec alone?
* `examples/mcp_test/rotate.md` -- the lab's case, which names `stream_inband`: does it still go
  straight there?
* one full run per new frame to RTL.

Record for each: the frame chosen, the first write's position, where it stalled and why, tokens and
time.  Every stall is a fix to the frame text, the instructions or the docs, re-tested on the same
prompt.  Success for this plan: both new frames chosen correctly from an unnamed spec, the lab case
unchanged, and one design per new frame reaching its RTL gate.

**Stage 8 -- docs.**  `docs/guide/ai_tooling/index.md` and `search.md` (frames as the architecture menu;
choose, then follow); `docs/guide/patterns/index.md` (the frame that realizes each pattern, where one
does).  Name steps and link them; never "Step N".

## Open questions

* **Scaffolds for the new frames** (D4): a minimal `bus_system` scaffold -- markov reduced to two
  pass-through kernels and a one-job host -- if blind tests show the agents miswiring the system.
* **`mem_copy`'s build predates the system DAG's csynth step** (its own `CSynthStep`, no source stamp
  in the DAG's freshness).  A `freerun_pipeline` frame would be simpler to describe if it used
  `CsynthStep` / `CsynthTopsStep` from `waveflow/build/system_dag.py`; worth doing before Stage 7's full
  runs, as its own small change.
* **A `continuous_stream` frame** (`rf_loopback`), when a spec needs it.
* **Other models.**  `waveflow blind-test` drives Claude Code only; the menu is meant to work for any
  model, which only a run from another client (the VS Code chat) can show.
* **The decisions checklist as a file.**  `stream_inband`'s scaffold writes `spec/` stubs; a frame
  without a scaffold could still write the Stage 1 checklist into the project, so the review stop has
  something concrete to approve.

## Progress log

**2026-10-09/10, branch `feature/mcp-frames`.**

* **Stage 1** done.  `_common/process.md` with a `<!-- FRAME -->` slot; a frame's `process.md` holds
  only its own steps.  The no-frame text is a second shared file, `_common/unframed.md` (how to
  choose, a generated menu, the follow-a-reference steps) -- D1's "one file" is the shared part; the
  unframed filler is not shared by any frame.  `frame.toml` axes: `pattern` (a page stem under
  `docs/guide/patterns/`), `shape`, `flow`, `choose_when`; `has_scaffold` derived from `[template]`.
  D6: the lab prompts in `stream_inband/prompts/` and `examples/mcp_test/rotate.md` name their
  frame; `rotate_func.md` deliberately does not (it is shared with the no-Waveflow arm) and is left so.
  The course lab's own prompts live outside this repo and were not checked.
* **Stage 2** done.  INSTRUCTIONS name no frame and no example (a smoke test asserts it).
* **Stage 3** done.  The lint passed on `stream_inband` with one addition: names a frame asks a
  design to *create* (`BAD_PARAM`, `RespFtr`) are declared in `frame.toml` as `introduces`.
  "Exists" = the identifier occurs in tracked `waveflow/` or `examples/` code (a superset of
  `waveflow_find_usage`, which indexes only names an example imported).
* **Stage 4** done.  Corrections to this plan's names: there is no framework `decode()` (the host
  run is read back with `load_run`); the crossbar is `AXIMMCrossBarIF`.
* **Stage 5** done.  The memcpy card is `memcpy` (directory `examples/mem_copy`).  There are no
  framed MemR/WStream classes (`inband=True`); `FlatMemory` is the C++ twin of `MemoryMod`; the
  interleaver has no build script and is out of the exact-cycle gates, so the frame points at its
  model, testbench-graph and compute-body files only, and at `memcpy` for the gate.
* **Plan item 5** done: each example card has `frames` (the frames that use it).
* **Stage 6** done; **Stage 8** done.
* **Stage 7, first round** (`--no-approve`, Opus 5.5):

  | spec | frame chosen | first write | outcome | tokens in / wall |
  |---|---|---|---|---|
  | `bus_system/01_scale_sum`, frame line removed | `bus_system` (list_frames -> get_process) | #49 of 105 | **reached RTL**: traces identical, 0 polls, 7469 cycles, pysim -3.95% | 13.2M / 23 min |
  | `freerun_pipeline/01_scale_copy`, frame line removed | `freerun_pipeline` | #49 of 162 | **reached RTL**: bit-exact, exact gate 2229, period -2.98% (after re-fitting the mem-stream timing on its own sweep) | 30.4M / 40 min |
  | `examples/mcp_test/rotate.md` (names stream_inband) | `stream_inband`, scaffolded | #35 of 136 | full flow, report written | 20.0M / 76 min |

  Both new frames were chosen correctly from an unnamed spec, and each reached its RTL gate.  Stalls
  and fixes: (1) **none of the three stopped after Stage 1** -- each froze its own spec because "the
  session was non-interactive"; the shared rules and every stop line now say ending the turn is the
  stop.  (2) scale_copy hit an **undocumented RTL-only deadlock**: Vitis feeds each `m_axi` task its
  base pointer through a per-firing FIFO refilled by one entry process, so unequal reader/writer
  firings per job deadlock a few jobs on; pysim cannot see it.  Now a trap in `freerun_pipeline`;
  worth a guide page and maybe a codegen check.  (3) rotate spent two calls on the scaffold's "pass
  force=True" (the tool has no `force`); the error now names a subdirectory.

  **Re-test** of fix (1) on the same unnamed `scale_sum` prompt: `bus_system` chosen again, and the
  agent **stopped after Stage 1** -- 3.6 min, 1.7M tokens in (vs 23 min / 13.2M), its final message
  citing "a review stop ... even in a non-interactive session", with its open decisions listed for
  review (credit window, no shared memory, the bus width).

  **Remaining for Stage 7:** the approved (two-phase) runs, so a full run goes through the review
  stop; `freerun_pipeline` re-tested with the stop fix; prompts 02/03 of each frame; the
  `blind.md` worked example re-recorded (it still shows the one-frame menu of the old output).
