---
title: Blind Testing the MCP Server
parent: AI Tooling
nav_order: 3
has_children: false
summary: "How to test the MCP server the way a new user meets it: waveflow blind-test gives a fresh AI agent a spec and nothing but the Waveflow tools, runs it unattended to completion, and writes a report of which tools it called, what it read and wrote, what it was denied and how much it used. Currently built on the Claude Code command line."
---

# Blind Testing the MCP Server

> Like the rest of the MCP server, blind testing is experimental.

## What is blind testing, and why?

The MCP server exists for one situation: an AI assistant that has **never seen
Waveflow** is asked to build something with it. The only way to know whether
the server works is to recreate that situation and watch what happens.

That is harder than it sounds. If you test by asking your usual assistant in
your usual folder, it is not blind. It may load project notes (`CLAUDE.md`
files), its own saved memory of the project, your saved permissions, or files
in the Waveflow repository that spell out what the tools were designed to do.
It then succeeds for reasons a new user will never have.

A **blind test** removes those advantages. A fresh agent starts in an empty
folder holding only a specification, with the Waveflow tools as its only
source of Waveflow knowledge. It runs to completion without anyone helping.
Afterwards a report shows what it actually did, including:

- whether it found the build process (`waveflow_get_process`) and the
  scaffold (`waveflow_new_accel_project`) on its own;
- which guide pages and examples it read, and whether it wandered into parts
  of the repository a user should not depend on;
- whether it hand-edited generated files;
- where it was stuck, and what it was refused.

Each problem it hits is a gap in the tools, the process text or the guide. A
fix to any of them can then be tested against the same specification.

## Supported agents

`waveflow blind-test` currently drives **Claude Code**, Anthropic's
command-line agent (`claude`), which must be installed and logged in. Claude
Code can run non-interactively, and its options control exactly what the
agent is given. Other command-line agents, such as Gemini CLI and Codex CLI,
may be supported in the future.

Because the test uses one agent, it tests the server with one family of
models. A spec that works here should also be tried by hand with other models,
for example from the VS Code chat (see
[Installing the MCP Server](./mcp_setup.md)).

## Running a blind test

You need Waveflow installed as described in
[Installing the MCP Server](./mcp_setup.md), and the `claude` command on your
`PATH`. With the virtual environment active:

```bash
waveflow blind-test --prompt path/to/spec.md
```

`--prompt` is the specification. Any Markdown files it links to (such as a
`frame.md` it tells the agent to read first) are copied along with it.

The agent works in a new folder **beside** the Waveflow clone, never inside
it: by default `waveflow_blind_tests/<spec name>/`, next to the clone
directory. The reason is that Claude Code loads every `CLAUDE.md` in the folder
it starts in *and in every folder above it*. An agent working inside the
clone would read the repository's own `CLAUDE.md` before the spec, and the
repository's plans would be a few directories away. The default location is
also outside git, so nothing it produces needs ignoring.

`--folder` chooses a different location. Wherever it is, it must be new or
empty, outside the clone, and outside any folder holding a `CLAUDE.md` or
`AGENTS.md`. The command refuses folders that break these rules. To start the
same spec over, delete the folder and its `.blindtest` folder, or pass a new
`--folder`. To continue an unfinished run, see
[Interrupted runs](#interrupted-runs).

### What the command controls

| The agent gets | The agent does not get |
| --- | --- |
| the spec, and the files it links to | project notes (`CLAUDE.md`) or saved memory |
| the Waveflow MCP server, and no other MCP servers | your saved permissions and settings |
| the Waveflow virtual environment first on its `PATH`, so `python` is the one with Waveflow | anyone to answer its questions |
| an allowlist of tools a student would reasonably approve: the Waveflow tools, reading and editing files, `python`, `waveflow`, and basic read-only shell commands, in both Bash and PowerShell | anything outside the allowlist, which is **denied and recorded** rather than left waiting |

The agent can still read files in the Waveflow clone, as a real user's
assistant can. The report shows which ones it read.

The agent is also told a few facts about its **environment**, never about
Waveflow. Nobody will reply. The session ends when its turn ends, and a
background command is stopped at that point unless a `Monitor` is watching
it. So for long runs such as synthesis it should wait with `Monitor`.
`ScheduleWakeup` is removed, because in this mode nothing would ever wake the
agent. If a turn still ends with a background command stopped, the command
continues the session automatically (up to three times) and tells the agent
to rerun the command and wait for it.

A command that chains several (`python --version; which vitis-run`) is
allowed only if every part is. A denial is not fatal: the agent sees the
refusal and usually finds another way. If the same harmless command is denied
run after run, add it with `--allow`.

### Review stops

A specification may tell the agent to stop for review, for example after the
specification artifacts and before the design. The agent's run then ends.
By default the command resumes the same session once with the reply
`Approved. Continue.`, so the run reaches the end of the second stage:

| Option | Effect |
| --- | --- |
| `--approve "…"` | the reply to send at each review stop |
| `--rounds N` | how many replies to send (default 1) |
| `--no-approve` | stop at the first review stop |

A fixed reply keeps the run blind, and tests whether the agent's first stage
was good enough *without* a knowledgeable reviewer. To test with a real
review, run with `--no-approve`, read the agent's work, and continue by hand.

### Other options

| Option | Effect |
| --- | --- |
| `--message "…"` | the first message (default: *Build the accelerator specified in `<spec>`, in this folder.*) |
| `--model <name>` | the Claude model (default: Claude Code's default) |
| `--allow "<tool>"` | add a tool to the allowlist, e.g. `--allow "Bash(make:*)"`; repeatable |
| `--timeout <hours>` | limit on each run (default 4) |
| `--silent` | do not print the agent's activity while it runs |
| `--resume` | continue an unfinished run; see [Interrupted runs](#interrupted-runs) |
| `--force` | start a new run in a non-empty folder, replacing the earlier report. The old partial work stays in the folder, so the run is not blind; prefer deleting the folder |

### Usage

The agent runs under whatever login `claude` has:

- **A claude.ai subscription** (Pro, Max, and similar), which is the usual
  case. Nothing is charged per run. The run counts against your plan's usage
  limits, like any other Claude Code session.
- **An API key**, used if `ANTHROPIC_API_KEY` is set in the shell you run the
  command from. Then every token is billed.

`summary.md` states which login was used. For each run, the log and the
report show the **model** and the **tokens**: tokens in, and how many of those
were read from cache, and tokens out. Input dominates because the whole
conversation so far is re-read on every turn, mostly from cache. The token
counts are the measure to compare runs by.

A full accelerator build is long, and can use a large share of a usage
window. Start with `--no-approve` to see the first stage before running the
second.

### Interrupted runs

A full build takes hours, and a run can be cut short: you press Ctrl-C, the
time limit runs out, or the laptop sleeps or shuts down. Pressing Ctrl-C stops
the agent and everything it started, writes the summary so far, and prints
the command to continue. In every case, continue with:

```bash
waveflow blind-test --resume --prompt path/to/spec.md
```

or `--resume --folder <folder>` if the test used a non-default folder.
Resuming continues the **same Claude Code session**, so the agent remembers
what it did. It is told it was interrupted, and finds its files where it left
them. It runs with the same settings. Options given with `--resume` change
them: `--allow` adds to the allowlist, and `--model`, `--timeout` and
`--permission-mode` replace the saved values. Any approvals still owed are
sent afterwards, and `summary.md` covers every run, before and after the
interruption.

If the run was stopped before Claude Code had started its session (start-up
takes a few seconds and sometimes much longer), there is nothing to continue.
The agent had done nothing, so `--resume` starts that run over with the
original message.

Do not use `--force` to continue: it starts a new agent in a folder already
holding the old one's partial work, so the test is no longer blind.

### The results

While the test runs, the agent's activity is printed as it happens: its text,
each tool call with its main argument, and a short line for each result.

Everything is also saved in a folder **beside** the trial folder, named
`<folder>.blindtest/`, where the agent cannot read it:

| File | Contents |
| --- | --- |
| `summary.md` | **Read this first.** Turns, time, model and tokens per run, and which login was used. The Waveflow tools called, in order. What it read outside its folder, with reads of the repository's `plans/` flagged as leaks and examples outside the [Examples](../../examples/) list flagged too. What it wrote, flagging generated files it edited. Every command, every denial, and the agent's final message at each stop. |
| `transcript-N.jsonl` | the complete record of run *N*, one event per line |
| `stderr-N.txt` | anything Claude Code printed to its error stream |
| `mcp.json` | the server configuration the agent was given |
| `config.json`, `phases.json` | the run's settings, and each run's status (running, done, timed out, interrupted). Both are written as the test goes, which is what `--resume` reads |

The trial folder itself holds what the agent built.

## Example

The repository keeps its blind-test specifications in
[`examples/mcp_test/`](../../../examples/mcp_test/). The smallest,
`tiny_test.md`, checks the harness itself rather than the tools: it links to a
second file and stops for review halfway.

```markdown
# Tiny spec

Read [notes.md](notes.md) first.

Stage 1: use the Waveflow tools to find which accelerator frames exist, and
write their names to `frames.txt` in this folder. Then STOP and wait for approval.

Stage 2 (after approval): append the line `DONE` to `frames.txt`.
```

Run it:

```bash
waveflow blind-test --prompt examples/mcp_test/tiny_test.md --text
```

The command creates `waveflow_blind_tests/tiny_test/` beside the clone, copies
`tiny_test.md` and `notes.md` into it, and starts the agent. The live log:

```text
=== phase 1: Build the accelerator specified in tiny_test.md, in this folder.
  [init] model claude-opus-5-5, MCP [{'name': 'waveflow', 'status': 'connected', ...}], 11 Waveflow tools
  -> Read  ./tiny_test.md
  -> ToolSearch  select:mcp__waveflow__waveflow_get_process,mcp__waveflow__waveflow_list_frames,...
  -> Read  ./notes.md
  -> waveflow_list_frames
  <- {"frames":[{"name":"stream_inband","synopsis":"A host-launched streaming accelerator: ...
  -> Write  ./frames.txt
  Stage 1 is finished. The Waveflow tools list one accelerator frame, `stream_inband`, and I
  wrote that name to `frames.txt`. ... As the spec says, I've stopped here.
=== phase 1 done: success, 6 turns, 0.2 min, claude-opus-5-5, 100.8k tokens in (96.6k cached), 774 out

=== phase 2: Approved. Continue.
  -> Edit  ./frames.txt
  Stage 2 is finished: I added the line `DONE` to `frames.txt` ...
=== phase 2 done: success, 2 turns, 0.1 min, claude-opus-5-5, 53.7k tokens in (53.2k cached), 256 out
2 run(s), claude-opus-5-5, 154,519 tokens in (149,866 cached), 1,030 out
  the agent's work: .../repos/waveflow_blind_tests/tiny_test
  read first:       .../repos/waveflow_blind_tests/tiny_test.blindtest/summary.md
```

`ToolSearch` is Claude Code loading the Waveflow tools' definitions before
their first use. The run stopped at the review point as instructed, and the
approval resumed the same session.

The start of `summary.md`:

```markdown
| Phase | Message | Turns | Wall | Model | Tokens in (cached) | Tokens out | Outcome |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Build the accelerator specified in tiny_test.md, … | 6 | 0.2 min | claude-opus-5-5 | 100.8k (96.6k) | 774 | success |
| 2 | Approved. Continue. | 2 | 0.1 min | claude-opus-5-5 | 53.7k (53.2k) | 256 | success |

## Waveflow tools

First Waveflow call at tool call #4 of 6.
- `waveflow_get_process`: **never called**
- `waveflow_new_accel_project`: **never called**
```

Here "never called" is correct: this specification asks for a frame list, not
an accelerator. For a real accelerator specification, the same two lines are
the first thing to check. If the agent never asks for the process or the
scaffold, it has built something without Waveflow's guidance, and the rest of
the report shows where it went instead.
