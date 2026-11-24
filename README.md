---
nav_exclude: true
---

# Waveflow

*A Python‑native hardware design platform that unifies architecture, simulation, synthesis, and AI‑guided tooling into a single, coherent workflow.*

## Why Waveflow exists

Modern AI models can generate individual hardware modules, but they struggle to maintain a consistent system‑level architecture. Real hardware development requires reproducibility, deterministic simulation, stable interfaces, and unified firmware—not just isolated RTL fragments. Waveflow provides the missing foundation: a disciplined, Python‑first environment where hardware objects, transactional semantics, simulation, synthesis, and firmware all come from one architectural model. This turns AI from a code generator into a reliable, end‑to‑end engineering toolchain.

## Documentation

The documentation is at <https://sdrangan.github.io/waveflow/>; its source is the `docs/` directory of this repository.

## Using Waveflow with an AI coding agent

Clone the repository, install it editable (`pip install -e ".[dev]"` in a virtual environment), then run `waveflow init` in your own project folder. That gives any agent (Claude Code, VS Code chat, Codex, ...) the Waveflow MCP server, the Waveflow skills and an `AGENTS.md` telling it how to look Waveflow up. See [Installing the MCP Server](docs/guide/ai_tooling/mcp_setup.md).