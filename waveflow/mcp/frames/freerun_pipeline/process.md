## The frame: `freerun_pipeline`

You are building in the `freerun_pipeline` frame: one accelerator, a
composite of free-running tasks that stream to each other and reach memory
through `MemRStream` / `MemWStream`.  `frame.md` (returned with this process
as `specification`) says what the frame fixes and what your spec must
decide.  There is no scaffold: you build from the references, so read them
first.

## The reference design

**Your primary reference is `memcpy`**: a sequencer, a reader and a writer
-- the whole free-running flow with no compute in the way.  Read its pages
in the order they run:

```
waveflow_get_example("memcpy")                               # the card: modules, ports, hooks
waveflow_get_doc("docs/examples/memcpy/index.md")
waveflow_get_doc("docs/examples/memcpy/memcpy.md")           # what it does, and why
waveflow_get_doc("docs/examples/memcpy/python.md")           # the leaves, the composite, the schemas
waveflow_get_doc("docs/examples/memcpy/testbench.md")        # the testbench graph: pysim and XSI
waveflow_get_doc("docs/examples/memcpy/codegen_dut.md")      # the free-running top
waveflow_get_doc("docs/examples/memcpy/codegen_tb.md")       # the XSI testbench from the graph
waveflow_get_doc("docs/examples/memcpy/rtlsim.md")           # the RTL run, the exact cycles
waveflow_get_doc("docs/examples/memcpy/timing.md")
waveflow_get_example("memcpy", file="mem_copy.py")           # schemas, leaves, composite, codegen
waveflow_get_example("memcpy", file="mem_copy_sim.py")       # the testbench graph
waveflow_get_example("memcpy", file="mem_copy_build.py")     # the build DAG
waveflow_get_example("memcpy", file="include/mem_seq_framed_task.h")  # a hand-written task body
```

**For a compute stage, or on-chip random access**, read `interleaver` the
same way (`docs/examples/interleaver/interleaver.md`, then `python.md`,
`testbench.md`, `codegen_dut.md`, `codegen_tb.md`, `rtlsim.md`), and only
these of its files -- the directory also holds sandboxes, unit gates and
retired variants that are not models:

```
waveflow_get_example("interleaver", file="interleaver_inband.py")      # the stages, the composite, codegen
waveflow_get_example("interleaver", file="interleaver_inband_sim.py")  # the testbench graph
waveflow_get_example("interleaver", file="interleaver.py")             # the shared schemas
waveflow_get_example("interleaver", file="include/il_compute_inband_task.h")  # a compute body
```

Then the pattern pages: `docs/guide/patterns/command_response.md` (the
pipeline form) and `docs/guide/patterns/stream_only.md`, and
`docs/guide/comp_codegen/freerunning_composite.md`.

---

## Stage 1: the specification

1. **The schemas**: the command, the response, and every message between
   stages.  `DataList` / `EnumField`, validated with
   `waveflow_validate_schema`.
2. **The golden model**: each job as a pure function of its command and the
   input memory, independent of any module.  Pin it with worked examples in
   a test.
3. **The scenario**: the commands and the input memory image, from fixed
   seeds, and the golden outputs -- the output memory image and the
   responses -- computed from the golden model.
4. **The stage diagram** in `design.md`: the stages, what each forwards, the
   memory regions, with one line for each item of `frame.md`'s "Decided by
   the spec".

Run the worked examples and show the output.

**Stop here.** Summarize Stage 1 and wait for approval. Do not write the
pipeline yet.

---

## Stage 2: the pipeline

The Stage 1 artifacts are now frozen.

1. **The stages and the composite**: one `FreeRunMod` per stage with its
   Python behavior and `kernel_task()`; `MemRStream` / `MemWStream` for
   memory; the composite with `add_comp`, its edges and its `boundary`.
   Read `docs/guide/vectorization/hls/loop_optimization.md` before shaping
   any loop.
2. **The testbench graph**, as `mem_copy_sim.py` builds it: the
   accelerator, a `MemoryMod` with `MemSeg` loads and dumps, a
   `StreamDriver` and a `StreamSink`.
3. **Gate: pysim bit-exact** against the golden outputs.
4. **DUT codegen**: the free-running top from the composite, and the
   hand-written task bodies -- one per stage, using the generated lane
   routines, never packing a word by hand.
5. **Testbench codegen**: the XSI testbench from the same graph.
6. **csynth** of the top.
7. **Gate: the XSI run bit-exact**, its cycle count recorded as an exact
   gate, and pysim's per-job period compared against it.
8. **`results/report.md`**: each gate, its number, and the step that
   produced it; anything the machinery could not express.

## Rules for this frame

Each of these has cost a debugging session here:

- **An un-paced free-running pipeline deadlocks** at RTL: every stage waits
  for a command or its input (`docs/guide/comp_codegen/freerunning_composite.md`).
- **Boundary TLAST comes from `ap_axis`**; an internal edge is a framed word,
  and `ap_axis` on an internal FIFO is rejected by Vitis.
- **A write, then a blocking read on another stream, deadlocks** when the
  two depend on each other: use `read_nb` (`docs/guide/patterns/stream_only.md`).
- **An `hls::task` that writes before it reads counts as in reset**
  (`docs/guide/rf/rfshotbuf/tx_internal.md`, "The reset trap").
- **A deadlock at RTL looks like a hang, or like success**: the gate
  asserts counts -- responses received, words written -- not just "it
  finished".
- **One body edit re-synthesizes every top** that includes it; batch edits.
- **A deep Windows path fails csynth with no error text**: keep the work
  directory shallow.
