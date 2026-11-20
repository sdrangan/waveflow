# Agent instructions: the Waveflow repository

Waveflow is a Python-native hardware design platform: one Python source is the single source of
truth for the simulation, the Vitis HLS kernel, its testbench, the firmware and the docs. It is
recent and specialised, so assume you do not know its API: look every name up before using it.

This file is for any coding agent opened on this repository, whether the task is to *use* Waveflow
(try it, answer a question about it, build a design) or to *develop* it. Claude Code reads it
through `CLAUDE.md`, which adds the developer notes (architecture, test markers, toolchain); other
agents should read `CLAUDE.md` too when changing Waveflow itself.

## Set up

Python 3.10+. In a virtual environment, from the repository root:

```bash
pip install -e ".[dev]"
waveflow kb --text browse          # checks the install: lists the guide and the examples
```

The install must be editable (`-e`): the search tools read the guide and the examples from this
checkout. Vitis HLS / Vivado (2025.1) are needed only for synthesis and RTL simulation; Python
simulation and code generation need neither.

## Where things are

- **The guide**: `docs/guide/`. The design flow starts at `docs/guide/systems/index.md` (*Flow
  Overview*); the default protocol is `docs/guide/systems/command_response.md`; HLS kernel bodies
  follow `docs/guide/vectorization/hls/loop_optimization.md`.
- **The reference examples**: the curated ones each have a page `docs/examples/<name>/index.md`
  naming their folder under `examples/`. Others in `examples/` are older work: prefer the curated.
- **Search** rather than grep when you do not know Waveflow's word for something:
  `waveflow kb --text search "<query>"`, `waveflow kb --text browse [section]`,
  `waveflow kb --text examples`, `waveflow kb --text usage <Symbol>` (every place a name is used).
  The Waveflow MCP server (`python -m waveflow.mcp.server`; setup in
  `docs/guide/ai_tooling/mcp_setup.md`) offers the same as `waveflow_*` tools.
- **Skills**: `.claude/skills/` and `.agents/skills/` hold the `waveflow-*` skills: `waveflow-start`
  (looking Waveflow up, beginning a design), then data schemas and HLS kernel bodies. Load the
  matching one before that step. They are copies: the source is
  `waveflow/skills/`.

## Rules

- To build an accelerator, choose the architecture, then follow its process (`waveflow frames
  --text`, `waveflow process <frame> --text`) before writing anything. Copy the closest reference
  example's patterns rather than inventing your own.
- Anything Waveflow generates is never hand-edited. Messages and sample bursts are serialized through
  their schema or the generated array utilities, never packed into words by hand.
- If Waveflow cannot express something, stop and say so rather than working around it.
- A design of your own belongs in its own folder outside this repository; `waveflow init` in that
  folder sets it up for agents (skills, `AGENTS.md`, the MCP server).

## Tests

```bash
pytest -m "not vitis and not xsi and not gem5 and not sionna"     # no vendor tools needed
```
