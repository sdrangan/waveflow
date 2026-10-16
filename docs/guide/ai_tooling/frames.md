---
title: Frames
parent: AI Tooling
nav_order: 2.5
has_children: false
summary: "How Waveflow guides an AI agent that is asked to build an accelerator. A frame is a design pattern realized in one system shape and one verification flow, with a reference example that proves it -- stream_inband (one host-launched kernel), freerun_pipeline (a composite of free-running tasks) and bus_system (kernels and a host on a crossbar). The agent chooses a frame from the spec, then follows its process: a specification stage that stops for review, then the build against that frozen specification. Covers the menu, what is in a frame, the shared process, the frame lint, and how to add a frame."
---

# Frames

An agent asked to build an accelerator has two problems before it writes a line: it does not
know Waveflow, and it has to decide what kind of system the spec describes. Search answers the
first, one name at a time. A **frame** answers the second: it is a worked architecture the agent
can choose, with the steps, rules and reference design that go with it.

## What a frame is

A frame is a [design pattern](../patterns/index.md) realized in one **system shape** and one
**verification flow**, with a reference example that proves it works:

| axis | sets | values |
| --- | --- | --- |
| **pattern** | the contract between host and kernel | command-response, continuous stream, configured stream, one-shot |
| **shape** | the structure, and the reference to copy | one kernel; a pipeline of free-running tasks; several kernels on a bus |
| **flow** | the steps and the gates | host-launched (csim / cosim); free-running (XSI from the testbench graph); system (the system DAG) |

Waveflow has three frames:

| frame | shape | verified by | references | scaffold |
| --- | --- | --- | --- | --- |
| `stream_inband` | one host-launched kernel, configuration in each command header | C++ testbench, Vitis csim and cosim | [stream_inband](../../examples/stream_inband/index.md) | yes |
| `freerun_pipeline` | a composite of free-running tasks, memory through `MemRStream` / `MemWStream` | the XSI testbench generated from the testbench graph, an exact cycle gate | [memcpy](../../examples/memcpy/index.md), [interleaver](../../examples/interleaver/index.md) | no |
| `bus_system` | several free-running kernels and an on-chip memory on one crossbar, a software host | the [system DAG](../build/xsi_system.md): the host's bus traces identical at RTL and in pysim | [markov](../../examples/markov/index.md), [mm_fir](../../examples/mm_fir/index.md) | no |

All three realize **command-response**: a command carrying `n` and a `tx_id`, the work on `n`
elements, a response that echoes them. A spec in another pattern, such as a continuous stream,
is reached through the example cards instead.

## Choose, then follow

The server's MCP `instructions` reach the agent before it has called anything. They tell it to
choose the architecture before writing anything:

1. **If the spec names a frame**, call `waveflow_get_process(frame)` and follow it. A lab
   prompt that says "use the stream_inband example" goes straight there.
2. **Otherwise**, call `waveflow_list_frames()`, the architecture menu. Each entry gives the
   frame's pattern, shape and flow, a one-line "choose it when", its references and whether it
   has a scaffold. Pick the frame that matches the spec on all three axes.
3. **If none fits**, call `waveflow_list_examples()`, pick the closest reference design from
   its cards (each card names the frames that use it), and follow the generic process:
   `waveflow_get_process()` with no frame.

No frame is a default, and the instructions name no example. Choosing is part of the task.

In blind tests, both new frames were chosen correctly from specs with the frame name removed,
and each design went on to pass its RTL gate.

## What the agent gets

`waveflow_get_process(frame)` returns two documents:

- **The process**: the ordered steps, which tool to use at each one, and the rules. The scaffold
  writes the same text into a new project as its `AGENTS.md`, so the tool and the file cannot
  drift apart.
- **The specification** (`frame.md`). For `stream_inband` it is the whole protocol, the error
  rules and the report format, so a prompt need only give a function. For the other frames it
  lists **what the frame fixes** (the rules every design in it follows) and **what the spec must
  decide** (the checklist the specification stage answers).

### Two stages, and a stop between them

Every process has the same shape:

- **The specification stage** writes down what the accelerator must do, in executable form: the
  schemas, a golden model pinned by worked examples, the scenarios and their expected outputs,
  and the frame's decisions. Then the agent **stops** for review.
- **The build stage** builds against that frozen specification. The agent may not edit the
  specification to make something pass. If it believes the specification is wrong, it stops and
  says so.

A design checked against a model written at the same time proves little. The stop is what gives
the build something to be wrong against. The process says the stop holds in a non-interactive
session too: ending the turn *is* the stop. Agents in early blind tests skipped it, reasoning
that nobody was there to approve.

### The scaffold

`waveflow_new_accel_project(name, frame)` copies the frame's reference design into a new
project, renamed, with the compute stubbed to an identity, so it builds and passes before
anything is edited. Only `stream_inband` has one: it reduces to "replace one function". A bus
system or a pipeline does not reduce that far, so for those the agent reads its references, and
the scaffold refuses with a pointer to them.

## Inside a frame

A frame is a directory of text under `waveflow/mcp/frames/`, with no code of its own:

```text
waveflow/mcp/frames/
  _common/
    process.md      the part every frame shares; a <!-- FRAME --> line marks where a frame's own steps go
    unframed.md     what fills that line when no frame is named: how to choose, the menu, the generic steps
  bus_system/
    frame.toml      name, synopsis, pattern / shape / flow / choose_when, references, prompts, [template]
    frame.md        what the frame fixes, and what a spec must decide
    process.md      the frame's own steps and traps
    prompts/        example specs
```

The shared process holds what is true everywhere: assume you do not know the API, the tool
table, the two stages, never hand-pack words, never edit a generated file, stop and report what
the machinery cannot express. Each frame's `process.md` holds only its own steps, and the two are
joined when the process is read. A rule fixed once is fixed for every frame.

### The frame lint

A frame is text that names code: example files to read, doc pages, classes and functions. When
the code moves and the frame does not, the agent goes looking for something that is no longer
there, and believes the frame over what it finds. `tests/mcp/test_frames.py` checks every frame's
text against the tree:

- every `waveflow_get_example(name, file=...)` resolves;
- every repository path exists;
- every tool named is registered;
- every backticked identifier occurs in the tracked `waveflow/` or `examples/` code.

Names a frame asks a design to *create*, such as a new error code, are listed in `frame.toml`
under `introduces`.

## Adding a frame

1. **Find the reference first.** A frame is only as good as the example that proves it: one of
   the curated examples, built and gated end to end.
2. **Write `frame.toml`**: the three axes, a one-line `choose_when` that tells this frame
   apart from its neighbours, and the references, primary first. Add a `[template]` only if
   the design reduces to stubbing a function.
3. **Write `frame.md`**: what the frame fixes, then what a spec must decide.
4. **Write `process.md`**: which reference pages to read and in what order, the specification
   stage ending in its stop, the build stage with its gates, and the traps. Each trap should
   have cost someone a debugging session.
5. **Write two or three prompts**, and check by hand that each can be built within what the
   frame fixes.
6. **Run the lint**, then [blind-test](./blind.md) a prompt with the frame name removed, to see
   whether an agent chooses it, and how far it gets.
