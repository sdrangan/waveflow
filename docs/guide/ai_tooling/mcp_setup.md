---
title: Installing the MCP Server
parent: AI Tooling
nav_order: 1
has_children: false
summary: "Setting up the Waveflow MCP server: which assistants are supported (VS Code chat with any model it offers, recommended; or Claude Code), why the server needs a clone of the Waveflow repository with an editable install rather than a plain pip install, and how to connect each assistant and check that the connection works."
---

# Installing the MCP Server

Setting up has two parts. First, install Waveflow so the server can find the
guide and the examples. Then, connect your assistant to the server. The first
part is the same for every assistant.

> The MCP server is experimental; see the [AI Tooling](./index.md) overview.

## Requirements

**An assistant that speaks MCP.** Two setups are supported:

| Setup | What it is | Status |
| --- | --- | --- |
| **VS Code chat** (recommended) | The chat view built into VS Code, in **Agent** mode. The model picker offers Claude, GPT and Gemini models, and the Waveflow tools work with whichever you choose. Needs a GitHub account with Copilot; a free tier exists. | supported |
| **Claude Code** | Anthropic's command-line agent (`claude`). The same setup also covers the Claude Code extension for VS Code. | supported |
| Gemini CLI, Codex CLI | Both can connect to MCP servers, and should work with Waveflow's. | **not yet tested or documented** |

**Everything else:**

- Python 3.10 or newer, and `git`.
- **A clone of the Waveflow repository, installed in editable mode.** See the
  next section for why.
- For the build steps past Python simulation (C simulation, synthesis,
  co-simulation): Vitis HLS, connected as described in
  [Connecting Vitis and Vivado](../installation/vitis.md). The search tools and
  the Python simulation do not need it.

## Do you need to clone the repository?

**Yes, for now.** The [user install](../installation/users.md)
(`pip install git+https://…`) gives you the Waveflow *package*: everything
needed to build designs. The MCP server needs more than the package. It reads
the **guide** (`docs/guide/`) and the **reference examples** (`examples/`), and
builds its search index from them each time it starts. Those directories are
part of the repository, not the package. The scaffold tool needs them too,
because it copies a reference example to start your project.

The server finds them by looking next to the installed package. With an
editable install (`pip install -e`) from a clone, the package *is* the clone,
so the directories are right there. With a plain install they are not, and
every search tool stops with an error saying so.

If you already have a plain install and a clone somewhere else, you can point
the server at the clone instead, by setting an environment variable before
starting your assistant:

```bash
export WAVEFLOW_KB_ROOT=/path/to/waveflow          # Linux / macOS
$env:WAVEFLOW_KB_ROOT = "C:\path\to\waveflow"      # Windows PowerShell
```

For a new setup, the editable install below is simpler.

**Your own project does not live in the clone.** The clone only needs to be
installed. Your designs can be in any folder, and that folder is what you open
in VS Code or start Claude Code in.

## Install Waveflow

This follows [Developer Setup](../installation/developers.md). In short:

```bash
git clone https://github.com/sdrangan/waveflow.git
cd waveflow
python -m venv ../waveflow-venv
source ../waveflow-venv/bin/activate          # Linux / macOS
..\waveflow-venv\Scripts\Activate.ps1         # Windows PowerShell
pip install -e ".[dev]"
```

The virtual environment can live anywhere. Note where it is, because the
connection steps below need the path of its Python interpreter:

| System | Interpreter |
| --- | --- |
| Linux / macOS | `<venv>/bin/python` |
| Windows | `<venv>\Scripts\python.exe` |

**Check** that the server can see the guide:

```bash
waveflow kb --text browse
```

This should list the top of the guide and the examples. If `waveflow` is not
found, the environment is not active, or it was installed before the command
existed. Re-run `pip install -e ".[dev]"`; do the same after any update that
adds new commands.

## Connect VS Code (recommended)

1. **Activate** the virtual environment, then go to the folder you will work
   in, which is your own project folder:

   ```bash
   cd /path/to/my_project
   waveflow_mcp_setup --workspace .
   ```

   This writes `.vscode/mcp.json`, which tells VS Code how to start the server
   with the interpreter you just ran:

   ```json
   {
     "servers": {
       "waveflow": {
         "type": "stdio",
         "command": "/path/to/waveflow-venv/bin/python",
         "args": ["-m", "waveflow.mcp.server"]
       }
     }
   }
   ```

   If the file already exists, add `--force` to replace it. Run the command
   again whenever you move or recreate the virtual environment.

2. **Open** the folder in VS Code (`code .`), open the chat view, and switch
   it to **Agent** mode. Tools are only offered in Agent mode.

3. **Confirm** the server is running. Run `MCP: List Servers` from the Command
   Palette: `waveflow` should be listed and running. You can also check the
   chat's tools button, where the `waveflow_*` tools should appear. From
   `MCP: List Servers` you can also restart the server, for example after
   updating Waveflow.

**Working on a remote server** (for example over Remote-SSH): install Waveflow
and run `waveflow_mcp_setup` **on the remote machine**, in the remote project
folder. The server then runs there, next to the clone and the Vitis tools.

## Connect Claude Code

Register the server once, giving the full path to the virtual environment's
interpreter:

```bash
# Linux / macOS
claude mcp add waveflow -- /path/to/waveflow-venv/bin/python -m waveflow.mcp.server

# Windows
claude mcp add waveflow -- C:\path\to\waveflow-venv\Scripts\python.exe -m waveflow.mcp.server
```

By default this registers the server for the **current folder** only, so run
it from your project folder. To make it available in every folder, add
`--scope user` after `add`.

**Confirm** it with `claude mcp list`, or with `/mcp` inside a Claude Code
session, where `waveflow` should be listed as connected.

## Check that it works

In a new chat or session, in your project folder, ask:

> Use the Waveflow tools to list the reference examples.

The assistant should call `waveflow_list_examples` and answer with the
examples listed under [Examples](../../examples/). If it answers from general
knowledge instead, or says it has no such tool, the server is not connected.
Recheck the connection steps for your assistant.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| A search tool fails with **"no Waveflow checkout found"** | Waveflow was installed without a clone. Use the editable install above, or set `WAVEFLOW_KB_ROOT`. |
| **`waveflow: command not found`** | The virtual environment is not active, or it predates the command. Activate it; re-run `pip install -e ".[dev]"`. |
| The server does not appear in VS Code | `.vscode/mcp.json` is in the wrong folder (it must be in the folder you opened), or it names an interpreter that has moved. Re-run `waveflow_mcp_setup --workspace . --force`. |
| The tools are listed but the assistant never uses them | Make sure VS Code chat is in Agent mode. Otherwise, name them in your request ("use the Waveflow tools to …"). |
