# Free-running pipeline frame (read this before the spec)

A **free-running pipeline** is one accelerator built as a composite of
free-running tasks that stream to each other, reading and writing memory
through the framework's stream adaptors.  This file says what the frame
**fixes** -- the rules every design in it follows -- and what a spec **must
decide**, which is the checklist Stage 1 answers.  Where the spec and this
frame disagree, the spec wins; say so in Stage 1.

The pattern is command-response through a pipeline
(`docs/guide/patterns/command_response.md`); the rule under every
free-running module is stream-only logic behind adaptors
(`docs/guide/patterns/stream_only.md`).  The references are `memcpy` (the
whole flow, with no compute) and `interleaver` (compute stages, and on-chip
random access through a stream of blocks).

## Fixed by the frame

1. **The accelerator is a composite `FreeRunMod`**: its stages are children
   added with `add_comp`, joined by `StreamIF` edges (`framed=True` inside
   the composite) or, for on-chip blocks, `StreamOfBlocksIF`.  Each stage is a
   task (`kernel_task()`, a `KernelTask` naming its hand-written body), and
   its logic reads and writes only streams.
2. **Memory is reached only through `MemRStream` / `MemWStream`**
   (`inband=True` for the framed form the pipeline uses), whose timing models
   ship pre-fit.  No stage holds a bus master of its own.
3. **The command travels with the data**: the first stage reads it, every
   stage forwards what the next needs, the last stage answers.  Nothing is
   decided from a side channel.
4. **Every stage is paced** by a command or by its input; there is no
   un-paced free-running loop -- it deadlocks at RTL
   (`docs/guide/comp_codegen/freerunning_composite.md`).
5. **A word is packed and unpacked only with the generated lane routines**
   (`<elem>_array_utils`: `read_framed_stream_lane`,
   `write_framed_stream_lane`; `docs/guide/vectorization/hls/arrayutils.md`).
6. **Boundary streams carry TLAST from `ap_axis`; internal edges carry a
   framed word** -- the codegen decides this from the composite's `boundary`;
   never hand-write a top.
7. **Verification**, each against the golden outputs of Stage 1:
   - a golden model of every job, independent of the modules;
   - pysim of the testbench graph (a `MemoryMod` with its `MemSeg` loads and
     dumps, a `StreamDriver` for the commands, a `StreamSink` for the
     responses), bit-exact;
   - the XSI testbench generated from the **same** testbench graph, bit-exact
     at RTL;
   - the RTL cycle count recorded as an exact gate, and pysim's per-job
     period within the reference's tolerance (`memcpy`: 3%).
   Vitis cannot co-simulate free-running tasks, so XSI is the only RTL check.

## Decided by the spec

Stage 1 answers each in one line, or in the stage diagram:

- the stages and the job each does;
- the messages between stages: what each forwards, and the command and
  response schemas;
- the memory regions and their element types;
- the parallelism: elements per word, and the bus width;
- whether a stage needs on-chip random access (then the `interleaver`'s
  stream-of-blocks pattern) or state across firings (`add_state`, as
  `firblock` uses it; `docs/guide/memory/hwstate.md`);
- the scenario -- the commands, the memory images -- and its golden outputs;
- the acceptance numbers the spec adds (cycles, resources), if any.
