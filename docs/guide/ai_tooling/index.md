---
title: AI Tooling
parent: Guide
nav_order: 15
has_children: true
summary: "The Waveflow MCP server: a small local program your AI assistant launches, which gives it Waveflow's guide and reference examples, the step-by-step process for building an accelerator, and a scaffold that starts every project from a working design. Nothing leaves your machine and no API key is needed. Optional, and experimental."
---

# AI Tooling

> **Experimental.** The MCP server is new and changing quickly. Tool names,
> arguments and the build process it describes may change between versions,
> and so may these pages. Report anything that does not match what you see.

None of this is required. Waveflow runs standalone; see
[Installing Waveflow](../installation/). Use these tools when you want an AI
assistant to help you build designs with Waveflow.

## What is MCP?

The **Model Context Protocol** (MCP) is an open standard for connecting an AI
assistant to outside tools. An *MCP server* is a program that offers a set of
named tools, each with a description and typed arguments. The assistant's host
(VS Code, Claude Code and others) starts the server, shows its tools to the
model, and runs a tool whenever the model decides to call one. The model then
reads the result and continues.

The **Waveflow MCP server** is one such program. It runs on your own machine,
inside the Python environment where Waveflow is installed. Your assistant
starts it for you; you never run it by hand.

## Why use it

A general-purpose AI assistant knows C++, Python and a good deal of Vitis HLS,
but it has never seen Waveflow. Asked to build an accelerator, it does not know
that the design starts from a `DataList` schema, that the kernel and testbench
are *generated* rather than written, or that a `BuildDag` runs the flow. Left
alone, it falls back on hand-written HLS, which is exactly what Waveflow is
meant to replace.

The MCP server gives the assistant what it is missing:

| Tools | What the assistant gets |
| --- | --- |
| `waveflow_browse`, `waveflow_search`, `waveflow_get_doc` | **The guide.** It can browse the page tree by title and summary, search it, and read any page or section in full. |
| `waveflow_list_examples`, `waveflow_get_example`, `waveflow_find_usage` | **The reference examples**, meaning only those listed under [Examples](../../examples/). It can find every place a Waveflow class or function is actually used, then read the whole file. |
| `waveflow_list_frames`, `waveflow_get_process` | **The architecture menu, then the process.** Each *frame* is a design pattern in one system shape and one verification flow -- one host-launched kernel (`stream_inband`), a pipeline of free-running tasks (`freerun_pipeline`), several kernels on a bus (`bus_system`). The assistant chooses the frame that matches the spec, then follows its ordered steps and rules. The main rule: freeze the specification and the tests before writing the design. |
| `waveflow_new_accel_project` | **A starting point**, for a frame that has a scaffold (today `stream_inband`): a new project copied from the frame's reference design, which builds and passes C simulation *before* you change anything. |
| `waveflow_validate_schema`, `waveflow_get_components` | **Checks** on the schemas it writes. |

Three properties matter in practice:

- **Local.** Everything runs on your machine. There is no API key, no cloud
  service, and nothing is uploaded. The only AI involved is the assistant you
  already use.
- **Always current.** The search index is built from your Waveflow clone each
  time the server starts, which takes about a second. When the guide or an
  example changes, the next session sees the change.
- **Also a command line.** Every tool is also a `waveflow` subcommand (for
  example `waveflow kb search "…"`). You can see exactly what the assistant
  sees, and an assistant without MCP support can still use the tools from a
  terminal.

## In this section

- [Installing the MCP Server](./mcp_setup.md): what you need, installing
  Waveflow so the server can find the guide, and connecting VS Code or Claude
  Code.
- [Searching the Guide and Examples](./search.md): the search and read tools
  in detail, and the `waveflow kb` command line.
- [Blind Testing the MCP Server](./blind.md): run a fresh agent on a spec
  with nothing but the Waveflow tools, and get a report of what it did.
- [VS Code Extension](./vscode.md): building the Waveflow VS Code extension.
  Only for contributors to the extension; not needed to use the MCP server.
