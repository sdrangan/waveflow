---
title: Installing the MCP Server
parent: AI Tooling
nav_order: 1
has_children: false
summary: "Setting up the Waveflow MCP server: which assistants are supported (VS Code chat with any model it offers, recommended; or Claude Code), why the server needs a clone of the Waveflow repository with an editable install rather than a plain pip install, how `waveflow init` sets a project up for every assistant (MCP server config, AGENTS.md, skills), what a claude.ai/code cloud session gets, and how to check that the connection works."
---

# Installing the MCP Server

Setting up has two parts. First, [install Waveflow](#install-waveflow) so the
server can find the guide and the examples. Then, run
[`waveflow init`](#set-up-your-project-waveflow-init) in your project folder, which
connects every assistant to the server and gives it the Waveflow instructions
and skills.

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

## Set up your project: `waveflow init`

The folders end up like this:

```text
waveflow/           the clone (git clone); the guide and the examples live here
waveflow-venv/      the virtual environment; pip install -e points it at the clone
my_project/         your project: its own folder, its own git repository
  AGENTS.md         written by waveflow init (a Waveflow section; yours is kept)
  CLAUDE.md
  .claude/skills/
  .agents/skills/
  .mcp.json         on this machine only (gitignored)
  .vscode/mcp.json  on this machine only (gitignored)
```

The virtual environment can also live inside `my_project/` (then add it to
`.gitignore`). Nothing from Waveflow is copied into your project except the
skills and the Waveflow section of `AGENTS.md`.

**Activate** the virtual environment, go to your own project folder, and run:

```bash
cd /path/to/my_project
waveflow init
```

This sets the folder up for every assistant at once:

| file | what it is | commit it? |
| --- | --- | --- |
| `.mcp.json` | registers the MCP server for Claude Code | no (`init` adds it to `.gitignore`) |
| `.vscode/mcp.json` | registers the MCP server for VS Code | no (likewise) |
| `.claude/skills/`, `.agents/skills/` | the [agent skills](./skills.md) | yes |
| `AGENTS.md` | instructions for any agent: what Waveflow is, where to look things up, what to do when Waveflow is not installed | yes |
| `CLAUDE.md` | imports `AGENTS.md` (Claude Code reads `CLAUDE.md`) | yes |

The two server files name the interpreter you ran `init` with, which is why
they stay on your machine:

```json
{
  "mcpServers": {
    "waveflow": {
      "type": "stdio",
      "command": "/path/to/waveflow-venv/bin/python",
      "args": ["-m", "waveflow.mcp.server"]
    }
  }
}
```

`init` never replaces your own content. An existing `AGENTS.md` or `CLAUDE.md`
gets the Waveflow part appended, an existing `.mcp.json` gets a `waveflow`
entry beside its other servers, and an installed skill is left alone. Run it
again after upgrading Waveflow (with `--force-skills` to refresh the skills) or
after moving the virtual environment.

**Then open the project in your assistant:**

- **VS Code**: open the folder (`code .`), open the chat view, and switch it
  to **Agent** mode; tools are only offered there. Run `MCP: List Servers`
  from the Command Palette: `waveflow` should be listed and running. From
  there you can also restart the server, for example after updating Waveflow.
- **Claude Code**: start `claude` in the folder. It asks once whether to use
  the project's MCP servers: approve `waveflow`. `/mcp` should list it as
  connected.

**Working on a remote server** (for example over Remote-SSH): install Waveflow
and run `waveflow init` **on the remote machine**, in the remote project
folder. The server then runs there, next to the clone and the Vitis tools.

**Registering the server by hand** (for every folder, say) still works:
`claude mcp add --scope user waveflow -- <venv python> -m waveflow.mcp.server`.
Do not register it both ways, or Claude Code starts two copies.

## Cloud sessions (claude.ai/code)

A cloud session works from a clone of your project's repository, on a machine
that has neither your virtual environment nor the MCP server. The committed
files still reach it: the skills, and `AGENTS.md`, which tells the agent to
clone Waveflow beside the project and install it editable when `waveflow` is
missing. From there the agent uses the same lookups through the shell
(`waveflow kb search`, `waveflow kb doc`, ...), which return what the MCP
tools would.

A session opened on the **Waveflow repository itself** needs none of this: the
guide and the examples are in the clone, and the repository commits its own
`AGENTS.md` and skills.

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
| The server does not appear in VS Code or Claude Code | `.vscode/mcp.json` / `.mcp.json` is in the wrong folder (it must be in the folder you opened), or it names an interpreter that has moved. Re-run `waveflow init` there with the right environment active. |
| The tools are listed but the assistant never uses them | Make sure VS Code chat is in Agent mode. Otherwise, name them in your request ("use the Waveflow tools to …"). |
