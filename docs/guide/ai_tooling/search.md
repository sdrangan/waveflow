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
| `waveflow_list_frames()` | which accelerator architectures it can be asked to build in |
| `waveflow_get_process(frame)` | the ordered steps for one, which tool to use at each, and the rules |

A **frame** is one architecture — `stream_inband` is a host-launched
streaming kernel with its parameters in a register map and a persistent loop
over in-band commands. `waveflow_get_process` returns that frame's process
text and its specification, and the scaffold writes the same process text as
the project's `AGENTS.md`, so the tool and the file in the project cannot
drift apart.

The server's MCP `instructions` — which most clients inject before the model
has called anything — say to call `waveflow_get_process` first when asked for
an accelerator.

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
`example_dir:` front-matter key, and those fifteen are the whole list. A
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
