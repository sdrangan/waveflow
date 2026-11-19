---
title: Agent Skills
parent: AI Tooling
nav_order: 2.7
has_children: false
summary: "Short SKILL.md instruction folders an AI coding agent loads when a task matches: the rules Waveflow's patterns depend on, and which guide page to read before which step. Shipped with Waveflow and installed into a project with `waveflow skills install`, for Claude Code and for agents that read .agents/skills. One skill so far (data schemas); experimental, and being evaluated."
---

# Agent Skills

> **Experimental.** There is one skill so far, and whether it helps is still being measured: see
> [Evaluation: Agent skills](../../evaluation/agent_skills/index.md).

The [MCP server](./index.md) lets an assistant *search* the guide, but it only finds a pattern if it
thinks to look for it at the right moment. An assistant new to Waveflow often doesn't: it declares a
`DataList` and then unpacks the words by hand anyway. A **skill** fills that gap. It is a folder with
a `SKILL.md` file, holding a name, a one-line description and short instructions:

- The assistant sees only the **description** of each installed skill, about one line each.
- When a task matches a description, it loads that skill's **instructions**: the rules that always
  apply, and which guide page to read before which step.
- It reads the skill's other files, if any, only when the instructions point to them.

The format is an open standard (Agent Skills). Claude Code, Codex, Gemini CLI, GitHub Copilot and
other agents read it, each from its own folder.

## The skills

| skill | use it for |
|---|---|
| `waveflow-schema` | defining a message as a `DataSchema` (`DataList`, `EnumField`, `FixedField`, arrays) and moving it in Python and in Vitis HLS: serializing it, reading and writing it on a stream or memory, and generating its C++ header |
| `waveflow-hls-body` | writing a Vitis HLS kernel body: one firing per message, the lane loop over packed samples with the generated array utils, `PIPELINE II=1`, state kept across commands, fixed-point datapaths, and the deadlocks and reset traps C simulation does not show |

`waveflow skills list` prints each skill's description, which is what decides when an assistant loads
it.

## Installing them into a project

The skills ship with the Waveflow package, so a project gets the skills that match its version of
Waveflow. From the project folder, with the virtual environment active:

```bash
waveflow skills install
```

This copies every skill into two folders:

| folder | read by |
|---|---|
| `.claude/skills/` | Claude Code |
| `.agents/skills/` | the cross-agent location, read by Codex and others |

`--for claude` (or `--for agents`) installs into one folder only, and `--dir <path>` installs into
another project. A skill already installed is left alone, so your edits to it are never overwritten.
`--force` replaces it, for example after upgrading Waveflow. Commit the folders with the project, so
everyone working on it, and every assistant, gets the same skills.

Each agent also needs the [MCP server](./mcp_setup.md): a skill tells the assistant which page to read,
and the server is how it reads the page.

**On claude.ai/code**, a session works from a clone of your repository, so it is offered exactly the
skills committed there: run `waveflow skills install` once on your machine and commit both folders.

## In the Waveflow repository itself

The Waveflow repository commits the same copies (`.claude/skills/`, `.agents/skills/`), so an
assistant opened on the repository -- locally, or in a claude.ai/code chat -- is offered the skills
too. `waveflow/skills/` is the source: after editing a skill there, refresh the copies with
`waveflow skills install --dir . --force` and commit them. A test
(`tests/mcp/test_skills.py`) fails when a copy differs from its source.

## Testing a skill

A skill is tested the way the MCP server is: with a [blind test](./blind.md). Two runs on the same
spec differ only in `--skills <folder>`, which installs the skill into the trial folder. The report
says whether the agent loaded the skill, and whether that was before or after it wrote its first file.
The specs and the review sheets are in `evaluation/skill_tests/`.
