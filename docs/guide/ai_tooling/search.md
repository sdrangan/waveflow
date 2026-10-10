---
title: Searching the Guide and Examples
parent: AI Tooling
nav_order: 2
has_children: false
summary: "How an AI assistant finds things in Waveflow: six local tools over the guide and the reference examples, with no API key, no service and no corpus to rebuild. The index is built in memory from your checkout each time the server starts, so it is never out of date, and every tool is also a waveflow kb subcommand for agents that cannot speak MCP."
---

# Searching the Guide and Examples

An assistant that has never seen Waveflow does not know `DataList`,
`HostActivated`, hooks, `SeqTB` or `BuildDag`. These six tools let it find
them — in the guide, and in working code.

Everything here is **local**. There is no API key, nothing is billed, and
nothing leaves your machine. The index is built in memory from your checkout
when the MCP server starts, in about a second and a half, so it cannot be out
of date and there is no corpus to rebuild.

> Replaces the earlier OpenAI-backed example search. That path needed a
> per-user key, a hosted vector store and a `--build-rag` step, and the
> committed corpus it searched drifted from the tree. Both are gone.

## The tools

| Tool | What it answers |
| --- | --- |
| `waveflow_browse(section)` | "What is there?" — the doc tree with every page's title and summary, so the model matches on meaning |
| `waveflow_search(query, scope, k)` | "Where is this mentioned?" — ranked pointers: path, heading, line range, snippet |
| `waveflow_find_usage(symbol)` | "Show me this in use" — every place a name appears in the examples, grouped by example |
| `waveflow_list_examples()` | "Which example should I copy?" — a card per reference design |
| `waveflow_get_example(name, file)` | The card, or one whole file from it |
| `waveflow_get_doc(path, heading)` | A whole page, or one section |

**Two ways to find things, and you want both.** `waveflow_search` is keyword
matching, and it is excellent the moment a query uses Waveflow's own words —
`TLAST`, `VitisRegMap`, `cosim`. It is poor at a query that avoids them.
`waveflow_browse` covers that case by handing the model each page's summary
and letting it do the matching. Ask a question in Waveflow's vocabulary and
search it; ask "where do I put my own C++ for the math part" and browse.

**Find in chunks, read whole.** Search returns pointers into files. Reading
returns whole files. Working from a snippet is how an assistant ends up
inventing the rest.

## Asking for an accelerator

Two more tools sit above search, for the case where the assistant is not
looking something up but building something:

| Tool | What it answers |
| --- | --- |
| `waveflow_list_frames()` | the architecture menu: each frame's pattern, shape, flow, references, and when to choose it |
| `waveflow_get_process(frame)` | the ordered steps for one frame, which tool to use at each, and the rules; with no frame, the generic process |

A **frame** is a [design pattern](../patterns/index.md) realized in one
system shape and one verification flow, with a reference example that proves
it:

| Frame | Shape | Flow | References |
| --- | --- | --- | --- |
| `stream_inband` | one host-launched kernel, in-band commands | csim / cosim | [stream_inband](../../examples/stream_inband/index.md) |
| `freerun_pipeline` | a composite of free-running tasks, memory through stream adaptors | XSI testbench from the testbench graph, exact cycles | [memcpy](../../examples/memcpy/index.md), [interleaver](../../examples/interleaver/index.md) |
| `bus_system` | several free-running kernels and a memory on one crossbar, a software host | the system DAG, the host's traces at RTL | [markov](../../examples/markov/index.md), [mm_fir](../../examples/mm_fir/index.md) |

**Choose, then follow.** The server's MCP `instructions` — which most clients
inject before the model has called anything — tell the assistant to choose
the architecture before writing anything. A spec that names a frame is
followed straight away. One that does not is matched against the menu on all
three axes. If no frame fits, the assistant picks the closest reference from
the example cards (each card names the frames that use it) and follows the
generic process, `waveflow_get_process()` with no frame.

`waveflow_get_process(frame)` returns the process text and the frame's
specification (`frame.md`: what the frame fixes, and what a spec must
decide). The part of the process every frame shares is one file, joined to
each frame's own steps when it is read, so the frames cannot drift apart; the
scaffold writes the same text as the project's `AGENTS.md`.

## From the command line

Every tool is a plain function with a `waveflow kb` subcommand, so an
assistant with no MCP support — or you, checking why a query missed — can
reach all of it from a terminal:

```bash
waveflow kb browse guide/custom_hooks
waveflow kb search "error when TLAST arrives early"
waveflow kb usage HostActivated
waveflow kb examples
waveflow kb example stream_inband --file poly.py
waveflow kb doc docs/guide/custom_hooks/writing.md
waveflow frames
waveflow process                 # the generic process: choose a frame
waveflow process stream_inband
```

Output is JSON, the same payload the MCP tool returns. Add `--text` for a
readable rendering.

## What is in the index

| Source | Notes |
| --- | --- |
| `docs/guide/**` | indexed in full |
| `docs/examples/**` | the per-example tutorials |
| the example source | **only the examples in the docs table of contents** |

That last row is deliberate. An example offered to an assistant is one it may
copy, and some directories under `examples/` are older work that should not
be. The table of contents is the curation that already exists, so each
`docs/examples/<name>/index.md` names its source directory in an
`example_dir:` front-matter key, and those fourteen are the whole list. A
directory with no page is not searchable, not listed, and not fetchable.

Generated code and other build output — `examples/*/gen/`, copied support
headers — is indexed but kept out of search results unless you ask for it,
and every hit and fetch of it is tagged. An assistant may read it to see what
code generation produces; it must never edit it.

## Requirements

An editable install (`pip install -e .`), which is how the course venv and the
lab machines install Waveflow. The index reads the `docs/` and `examples/`
trees directly, so a non-editable wheel has nothing to index.

## See also

- [Installing the MCP Server](./mcp_setup.md) — pointing VS Code at the server.
- [Headless evaluation](../developer/headless.md) — driving the tools with no
  human, for testing the agent surface.
