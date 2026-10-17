## The frame: `stream_kernel`

You are building in the `stream_kernel` frame: one free-running kernel on framed streams, the
command-response protocol with a status stream, an error fatal.  `frame.md` (returned with this process
as `specification`) says what the frame fixes and what your spec must decide.  There is no scaffold:
you build from the reference, so read it first.

## The reference design

**Your reference is `stream_kernel`.**  Read its pages in the order they run, and its files:

```
waveflow_get_example("stream_kernel")                                 # the card
waveflow_get_doc("docs/examples/stream_kernel/index.md")
waveflow_get_doc("docs/examples/stream_kernel/protocol.md")           # three streams, why a status stream, the fatal error
waveflow_get_doc("docs/examples/stream_kernel/golden.md")             # schemas, the golden, intents -> stimulus / expected
waveflow_get_doc("docs/examples/stream_kernel/kernel.md")             # run_iter, the HLS body, the generated top
waveflow_get_doc("docs/examples/stream_kernel/system_flow.md")        # the harness, the system steps, compare
waveflow_get_doc("docs/examples/stream_kernel/timing.md")             # timed phases, calibrated in the harness
waveflow_get_example("stream_kernel", file="stream_kernel.py")        # schemas, golden, the kernel
waveflow_get_example("stream_kernel", file="stream_kernel_build.py")  # codegen + add_system_steps
waveflow_get_example("stream_kernel", file="src/poly_kernel_task.h")  # the hand-written HLS body
```

Then the pattern page, `docs/guide/patterns/command_response.md`, and before you shape any loop,
`docs/guide/vectorization/hls/loop_optimization.md`.  For the data's types:
`docs/guide/schema/python/fixpoint.md` (`FixedField`, its rounding and overflow modes) and
`docs/guide/schema/python/complex.md` (`ComplexField`).

---

## Stage 1: the specification

1. **The schemas** of every message -- the command, the results' header, the status -- and the data's
   element types.  Validate each with `waveflow_validate_schema`.
2. **The golden model**: the operation as pure functions, independent of the kernel, bit-exact (the
   spec's operation order, every rounding).  Pin it with the spec's worked examples in a test.
3. **The scenarios**: commands from fixed seeds, as intents; `stimulus(cmds)` and `expected(cmds)`
   computed from the intents -- not from the stimulus -- including what an error leaves and the
   silence after it.
4. **`design.md`**: one line for each item of `frame.md`'s "Decided by the spec".

Run the worked examples and show the output.

**Stop here.** Summarize Stage 1 and end your turn -- even in a non-interactive session; the approval
is the next message. Do not write the kernel yet.

---

## Stage 2: the kernel

The Stage 1 artifacts are now frozen.

1. **The kernel**: a `FreeRunMod` with framed ports, its `run_iter` one command -- the command read,
   the `Resp` header written, the data through a pipelined `TimedPhase` (`load=get_array`,
   `store=write_array`, the compute on the whole array at once), the status, `yield self.event()`
   after an error -- and `workload(kind)` returning the scenarios.
2. **Gate: pysim bit-exact** in the kernel's harness, `harness(Kernel, workload=...)`, every
   recorded burst against `expected`.
3. **Code generation**: `DataSchemaStep` per schema and `ArrayUtilsStep` per element type, at every
   word width; the top from `composite_top_spec`; the hand-written HLS body behind `kernel_task()`,
   with the generated lane routines and a `static bool halted`.
4. **The build DAG**: the example's `codegen` step, then `add_system_steps` on the harness, on
   `run_dag_cli` -- as `stream_kernel_build.py` does.
5. **Gate: the DAG through `compare`** (`--through compare --synth check`): RTL against pysim, every
   word and TLAST, on every scenario.  An error scenario must show nothing after the error's status.
6. **Timing, if the spec asks for numbers**: the cycle count from the RTL run, or the timed phases
   calibrated in the harness (`add_calib_steps`, a `"calib"` workload).
7. **`results/report.md`**: each gate, its number, and the step that produced it; anything the
   machinery could not express.

## Rules for this frame

Each of these has cost a debugging session here:

- **One Python `write` is one burst.**  A header and its data are two writes, so two bursts -- which is
  why the protocol frames them that way.  A burst built of several messages is collected and written
  once.
- **A stream word cannot say that only some of its lanes are data** (no lane count travels with it).
  An early TLAST ends a whole word; the sender pads its last word with zeros.
- **Several word widths from one class**: the width is a build parameter, and each width is its own
  top with its own `cpp_kernel_name`.
- **Wider than 64 bits**, a word travels in pysim and in the testbench as 64-bit chunks
  (`docs/guide/build/bfm.md`).
- **Float arithmetic**: every operation its own statement, in the spec's order -- a fused multiply-add
  rounds once and no longer matches the golden.
- **A deadlock at RTL looks like a hang, or like success**: every gate asserts counts -- bursts
  recorded, statuses written -- not just "it finished".
- **One body edit re-synthesizes every top** that includes it; batch edits.
