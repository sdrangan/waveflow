---
title: AI Tooling
parent: Guide
nav_order: 15
has_children: true
summary: "The optional AI-assistant integrations: a VS Code extension, an MCP server exposing Waveflow's hardware-design tools to agents, OpenAI-backed semantic search over the example corpus, and the keys and environment each needs. None of them are required — the core package runs standalone — and this is an evolving area whose pages can lag the code, so treat them as intent rather than exact steps."
---

# AI Tooling

Waveflow includes **optional** AI-assistant integrations: a VS Code extension, an MCP
server that exposes Waveflow's hardware-design tools to agents (Claude Code, VS Code),
OpenAI-backed semantic example search (RAG), and the keys/setup they need. **None of
these are required** to use or develop Waveflow — the [core package](../installation/)
runs standalone. Reach for them only when you want AI-assisted design.

> **Heads-up:** this is an evolving area and these pages can lag the code. If a step
> doesn't match the current tooling, treat the page as intent rather than exact steps.

- [VS Code extension](./vscode.md) — the Waveflow IDE extension.
- [MCP server](./mcp_setup.md) — expose Waveflow's tools to agentic assistants.
- [OpenAI setup](./openai.md) — API key + environment variables for semantic search.
- [Semantic example search (RAG)](./rag.md) — build and manage the example-search stores.
