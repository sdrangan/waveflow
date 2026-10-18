## The frame: `stream_pipeline`

You are building in the `stream_pipeline` frame: a composite of free-running stages joined by internal
streams.  `frame.md` (returned with this process as `specification`) says what the frame fixes and what
your spec must decide.  There is no scaffold: you build from the references, so read them first.

## The reference designs

**Your primary reference is `block_eq`**: a `StreamBroadcast` and two stages at different rates, a link
between them, each stage verified alone and then the composite.  Read its pages in order, and its files:

```
waveflow_get_example("block_eq")                                  # the card
waveflow_get_doc("docs/examples/block_eq/index.md")
waveflow_get_doc("docs/examples/block_eq/protocol.md")            # one input, three bursts; errors
waveflow_get_doc("docs/examples/block_eq/stages.md")              # the stages, the bodies, the composite
waveflow_get_doc("docs/examples/block_eq/system_flow.md")         # each stage alone, then the composite
waveflow_get_example("block_eq", file="block_eq.py")              # schemas, golden, stages, composite
waveflow_get_example("block_eq", file="block_eq_build.py")        # codegen, the tops, the systems
waveflow_get_example("block_eq", file="src/eq_apply_task.h")      # a hand-written stage body
```

**For one stage -- the protocol, the harness, the timing**, `stream_kernel`
(`docs/examples/stream_kernel/index.md`).  Then `docs/guide/comp_codegen/freerunning_override.md` (*One
body, wherever it sits*), `docs/guide/streams/broadcast.md` if two stages share an input, and
`docs/guide/vectorization/hls/loop_optimization.md` before you shape any loop.

---

## Stage 1: the specification

1. **The schemas** of every message, the internal links' included.  Validate each with
   `waveflow_validate_schema`.
2. **The golden model**: the operation as pure functions, independent of the stages, bit-exact.  Pin it
   with the spec's worked examples in a test.
3. **The scenarios**: commands from fixed seeds, as intents; the expected outputs computed from the
   intents -- and, for each internal link, the messages it should carry (they drive the stages' own
   harnesses).
4. **`design.md`**: the stages and why the split is there; each link; one line for each item of
   `frame.md`'s "Decided by the spec".

Run the worked examples and show the output.

**Stop here.** Summarize Stage 1 and end your turn -- even in a non-interactive session; the approval
is the next message. Do not write the stages yet.

---

## Stage 2: the stages, then the composite

The Stage 1 artifacts are now frozen.

1. **Each stage**: a `FreeRunMod` with framed ports, its `run_iter` one command, its `workload(kind)`.
   **Gate:** pysim bit-exact in its own harness, driven by the messages the golden says its neighbour
   sends.
2. **The composite**: the children, the links (`StreamIF(framed=True, depth=...)`), the boundary.
   **Gate:** pysim bit-exact in its harness.
3. **Code generation**: the schemas with framed readers and writers (`DataSchemaStep(..., framed=True)`),
   the lane routines, a top for the composite and one for each stage, and a hand-written body per stage
   behind `kernel_task()` with `framed=`.
4. **The build DAG**: `add_system_steps` for each stage's harness and the composite's, on `run_dag_cli`.
   **Gate:** each through `compare`.
5. **`results/report.md`**: each gate, its number, and the step that produced it; anything the machinery
   could not express.

## Rules for this frame

- **A stage that waits on another must first consume its own copy of anything queued ahead** -- a
  broadcast's outputs move in lockstep.
- **A link's depth is explicit**, sized against what can pile up in it: a burst of results while the
  next stage is busy.
- **Every gate asserts counts** -- bursts recorded, statuses written -- not just "it finished".
- **One Python `write` is one burst**: a burst of several messages is collected and written once.
