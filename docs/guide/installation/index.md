---
title: Installation
parent: Guide
nav_order: 1
has_children: true
summary: "Two paths, and which you want depends on whether you are using Waveflow or changing it: a pip install from GitHub into a virtual environment for building designs with it as a library, or a clone plus an editable install with the development tools for modifying it and running the bundled examples and tests. Python 3.10 or newer either way; the AI-assistant integrations are optional and covered separately."
---

# Installing Waveflow

Waveflow requires **Python 3.10 or newer**. Pick the path that fits you:

- **[User Setup](./users.md)** — *use* Waveflow as a library in your own project.
  `pip install` from GitHub into a virtual environment; no clone needed. Start here if
  you just want to build designs with Waveflow.
- **[Developer Setup](./developers.md)** — *modify* Waveflow, contribute changes, or
  run the bundled examples and tests. Clone the repository and install in editable mode
  with the development tools.

The optional AI-assistant integrations — MCP server, OpenAI-backed semantic example
search, and the VS Code extension — are covered separately under
[AI Tooling](../ai_tooling/). None of them are required to use or develop Waveflow.
