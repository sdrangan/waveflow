---
title: Installing the MCP Server
parent: AI Tooling
nav_order: 2
has_children: false
summary: "Pointing VS Code at the Waveflow MCP server: install the package into a virtual environment, then run waveflow_mcp_setup --workspace . from the repository you want to work in, which writes .vscode/mcp.json. Written for VS Code; the same configuration adapts to other hosts such as Claude Code."
---

# Setting up The MCP Server

## Overview

Waveflow is being designed with agentic assistance to help build and simulate modules with the package. To use this facility, you will have to set up the **model context protocol** or MCP server. For now the setup is designed for users in VS Code. The process can be adapted for other IDEs such as Claude Code.

## Set-Up

To set up the MCP server on VS Code, first follow the [instructions](../installation/) to create and activate a virtual environment, then install `waveflow` into that environment. You can install from a cloned `waveflow` repository or from a published package source, as long as the environment you activate contains `waveflow`.

Independent of where the virtual environment is installed, navigate to the root folder of the repository where you wish to work:

- If you are an user working in a repository with own project, say `my_hw_project`, navigate to that repository;
- If you are a Waveflow developer working on the `waveflow` repository itself, navigate to the root of the `waveflow` repository.

From there run:

```bash
waveflow_mcp_setup --workspace .
```

The function will copy a file, `.vscode/mcp.json` to the repostitory root that VS Code uses a configuration file for the MCP server.  If `.vscode/mcp.json` already exists, rerun the command with `--force` to replace it:

```bash
waveflow_mcp_setup --workspace . --force
```

The setup command discovers its own interpreter path, verifies that `waveflow.mcp.server` can be imported from that interpreter, and then writes `.vscode/mcp.json` for the workspace.

In most cases this command only needs to be run once per workspace. If you later recreate or move the virtual environment, rerun `waveflow_mcp_setup --workspace .` so `.vscode/mcp.json` points to the new interpreter path.

After the command is run, you can launch VS Code from the command line:

```bash
code .
```

## Testing the MCP Server is Running

The simplest way to confirm that the MCP server is running is:

1. In VS Code, Open the Command Palette.
2. Run `MCP: List Servers`.
3. Select `waveflow`.
4. Choose `Restart`, or `Stop` and then `Start`.

After changing `mcp.json` or reinstalling packages, you can also use the above procedure to restart the server in VS Code:

You usually do not need to close and reopen VS Code unless you changed the environment after VS Code was already open.

---

## Searching the Guide and Examples

The server's search and read tools — how an assistant finds a page, a symbol
in use, or a whole example — are documented in
[Searching the Guide and Examples](./search.md). They are local and need no
key or setup beyond an editable install.
