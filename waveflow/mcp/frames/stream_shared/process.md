## The frame: `stream_shared`

You are building in the `stream_shared` frame: one kernel shared by several clients, or fed back its own
output.  `frame.md` (returned with this process as `specification`) says what the frame fixes and what
your spec must decide.  There is no scaffold: you build from the references, so read them first.

## The pattern and the reference designs

**Read the pattern first, whole**: `waveflow_get_doc("docs/guide/systems/shared_kernels.md")` -- the
four questions, the six ways to share, the return path, the blocks, a cycle inside a composite.  Then
the blocks it uses: `docs/guide/streams/arbiter.md`, `docs/guide/streams/demux.md`, and for an FFT used
both ways `docs/guide/dsp/ssr_fft/parameters.md`.

**Your primary reference is `os_filterbank`**: three channels on one FFT, forward then inverse, an
arbiter with the feedback first, the direction from the arbiter's index, a tag beside the FFT, filters
in slots, credits, three demuxes.  Read its page, then its files:

```
waveflow_get_example("os_filterbank")                                       # the card
waveflow_get_doc("docs/examples/os_filterbank/index.md")
waveflow_get_example("os_filterbank", file="os_filterbank.py")              # schemas, golden, stages, composite
waveflow_get_example("os_filterbank", file="os_filterbank_build.py")        # codegen, the tops, the systems
waveflow_get_example("os_filterbank", file="src/os_front_task.h")           # a client: credit, the frame whole
waveflow_get_example("os_filterbank", file="src/os_post_task.h")            # after K: feedback, answer, credit
```

**For the composite, its links and each stage alone**, `block_eq`
(`docs/examples/block_eq/index.md`, its `stages.md` and `system_flow.md`).  Then
`docs/guide/vectorization/hls/loop_optimization.md` before you shape any loop.

---

## Stage 1: the specification

1. **The schemas** of every message: each client's boundary messages, the tag, the credit, the frames
   into the arbiter.  Validate each with `waveflow_validate_schema`.
2. **The golden model**: the operation as pure functions, per client, independent of the stages,
   bit-exact.  Pin it with the spec's worked examples in a test.
3. **The scenarios**: per client, commands from fixed seeds, as intents; more work than its credit
   admits; clients of different lengths.  The expected outputs per client, and the messages each
   internal link should carry (they drive the stages' own harnesses).
4. **`design.md`**: K and why it is shared; which way of the six; the stages; each link with its depth
   and the credit that bounds it; the tag; one line for each item of `frame.md`'s "Decided by the
   spec".

Run the worked examples and show the output.

**Stop here.** Summarize Stage 1 and end your turn -- even in a non-interactive session; the approval
is the next message. Do not write the stages yet.

---

## Stage 2: the stages, then the composite

The Stage 1 artifacts are now frozen.

1. **Each stage**: a `FreeRunMod` with framed ports, its `run_iter` one frame or one command, its
   `workload(kind)` and `golden(kind)`.  **Gate:** pysim bit-exact in its own harness.
2. **The composite**: the clients, `StreamArbiter`, the stages around K, K, the `StreamDemux` blocks,
   the links with their stated depths, the boundary.  **Gate:** pysim bit-exact in its harness, every
   client's responses and statuses counted; and the same composite with its credit made ineffective
   answers fewer -- while the run returns.
3. **Code generation**: the schemas with framed readers and writers (`DataSchemaStep(..., framed=True)`),
   the stream blocks' bodies (`StreamArbiterStep`, `StreamDemuxStep`), a top for the composite and one
   for each stage, a hand-written body per stage behind `kernel_task()` with `framed=`.  A stage whose
   ports differ in width sets `endpoint_port_widths`.
4. **The build DAG**: `add_system_steps` for each stage's harness and the composite's, on `run_dag_cli`;
   each system top named apart from its kernel (`<kernel>_top`).  **Gate:** each through `compare`.
5. **`results/report.md`**: each gate, its number, and the step that produced it; anything the machinery
   could not express.

## Rules for this frame

- **Feedback first, and a frame only when complete.**  The arbiter commits on a frame's first word.
- **The stage after K never blocks**: the credit bounds the loop; every loop link holds what it admits.
- **Each client's outputs on their own ports**, or the order on a shared one shown not to depend on
  timing.
- **Every gate asserts counts** -- a deadlock on a cycle looks like a clean finish.
- **A missing block is an extension, not a workaround**: propose it (what, where, its parts), following
  the closest existing block; outside the Waveflow repository, ask before changing Waveflow.
