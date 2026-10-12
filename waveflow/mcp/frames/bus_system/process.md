## The frame: `bus_system`

You are building in the `bus_system` frame: several free-running kernels and
an on-chip memory on one AXI-MM crossbar, with a software host that never
polls.  `frame.md` (returned with this process as `specification`) says what
the frame fixes and what your spec must decide.  There is no scaffold: you
build from the references, so read them first.

## The reference design

**Your primary reference is `markov`**: two kernels, a credit link routed
over the bus, results in shared memory, a two-thread host.  Read its pages in
the order they run:

```
waveflow_get_example("markov")                               # the card: modules, ports, hooks
waveflow_get_doc("docs/examples/markov/index.md")
waveflow_get_doc("docs/examples/markov/protocol.md")         # every message, in order
waveflow_get_doc("docs/examples/markov/python.md")           # schemas, golden model, the modules
waveflow_get_doc("docs/examples/markov/host.md")             # the host, and a checklist for writing one
waveflow_get_doc("docs/examples/markov/system.md")           # the system class: devices, crossbar, IRQs
waveflow_get_doc("docs/examples/markov/credit_link.md")      # the routed link and its sizing
waveflow_get_doc("docs/examples/markov/pysim.md")            # both wirings, the gates
waveflow_get_doc("docs/examples/markov/build.md")            # the build DAG and its CLI
waveflow_get_doc("docs/examples/markov/codegen.md")          # the tops and the hand-written bodies
waveflow_get_doc("docs/examples/markov/synth.md")
waveflow_get_doc("docs/examples/markov/xsi.md")              # the host's C++ twin, the harness
waveflow_get_doc("docs/examples/markov/rtlsim.md")           # the RTL run and its gates
waveflow_get_example("markov", file="markov.py")             # schemas, modules, host, system
waveflow_get_example("markov", file="markov_build.py")       # codegen + add_system_steps
waveflow_get_example("markov", file="markov_host.h")         # the host's C++ twin
waveflow_get_example("markov", file="src/markov_gen_task.h") # a hand-written kernel body
```

**For a kernel the host reaches over the bus** -- a register bank, a
configuration changed mid-stream -- read `mm_fir` the same way, starting at
`docs/examples/mm_fir/protocol.md` and `docs/examples/mm_fir/slave_adaptor.md`,
with `waveflow_get_example("mm_fir", file="mm_fir.py")` and
`waveflow_get_example("mm_fir", file="mm_fir_host.h")`.

Then the pattern pages: `docs/guide/patterns/command_response.md` and
`docs/guide/patterns/stream_only.md`.

---

## Stage 1: the specification

1. **The schemas** of every message: commands, forwarded data, responses,
   any configuration and status.  `DataList` / `EnumField`, validated with
   `waveflow_validate_schema`.
2. **The golden model**: every job as a pure function of its command (and
   configuration), independent of any module.  Pin it with worked examples
   in a test.
3. **The scenario**: the jobs the host runs, from fixed seeds, and their
   golden outputs computed from the golden model.
4. **The block diagram**: kernels, links (direct or routed), views, the
   memory and its regions, the host -- in `design.md`, with one line for
   each item of `frame.md`'s "Decided by the spec".

Run the worked examples and show the output.

**Stop here.** Summarize Stage 1 and end your turn -- even in a
non-interactive session; the approval is the next message. Do not write the
system yet.

---

## Stage 2: the system

The Stage 1 artifacts are now frozen.

1. **The kernels**: one `FreeRunMod` each, stream ports only, its views in
   `mm_views`, its Python behavior and timing.  Read
   `docs/guide/vectorization/hls/loop_optimization.md` before shaping any
   loop.
2. **The system class**: each kernel through `build_mm_device`; the
   `MemoryMod`; the links (`StreamIF` / `CreditStreamIF` direct,
   `MmCreditStreamIF` routed, with `.place(...)`); one `AXIMMCrossBarIF`;
   `assign_address_ranges`; an `IrqIF` from each view the host waits on.
   - `waveflow_find_usage("build_mm_device")`, `waveflow_find_usage("assign_address_ranges")`
3. **The host**: a `SwHost` with `add_bus_master` and `add_irq`, its threads
   started with `start`, its jobs in flight bounded by a `SwSemaphore`, and
   `scenario_bursts()` for the RTL run.  It never polls.
   - `waveflow_get_doc("docs/examples/markov/host.md")` has the checklist.
4. **Gate: pysim bit-exact** against the golden outputs.
5. **Code generation**: the generated headers and kernel tops, a
   hand-written body per kernel (`KernelTask`, using the generated lane
   routines -- never pack a word by hand), and `write_writer_project` for
   each routed writer.
6. **The build DAG**: the example's `codegen` step, then `add_system_steps`,
   on `run_dag_cli` -- as `markov_build.py` does.
7. **The host's C++ twin** (`cpp_model`, `cpp_header`): the same jobs over
   the same bus, the addresses from the generated headers.
8. **Gate: the DAG through `compare`** (`--through compare --synth check`).
   Read the run back with `load_run`: `trace_mismatches` empty, the cycle
   count, `polls` zero, and pysim's cycles within 5% of the RTL.
9. **`results/report.md`**: each gate, its number, and the step that
   produced it; anything the machinery could not express.

## Rules for this frame

Each of these has cost a debugging session here:

- **A write, then a blocking read on another stream, deadlocks** when the
  two depend on each other: use `read_nb` (`docs/guide/patterns/stream_only.md`).
- **An un-paced free-running pipeline deadlocks** at RTL: every stage waits
  for a command or a credit (`docs/guide/patterns/command_response.md`).
- **An `hls::task` that writes before it reads counts as in reset**
  (`docs/guide/rf/rfshotbuf/tx_internal.md`, "The reset trap").
- **A deadlock at RTL looks like a hang, or like success**: every gate
  asserts counts -- jobs answered, words written -- not just "it finished".
- **One body edit re-synthesizes every top** that includes it; batch edits.
- **A deep Windows path fails csynth with no error text**: keep the work
  directory shallow.
