---
title: "Agent skills: onboarding"
parent: Agent skills
nav_order: 0
summary: "Does an agent on a researcher's own project find Waveflow: load the waveflow-start skill and look Waveflow up before writing, instead of guessing its API? What waveflow init sets up for that, what has been verified (locally, with the MCP server: the skill loads at the first tool call), what has not (a claude.ai/code cloud session with no Waveflow installed; an agent without the MCP server; agents other than Claude), and how to test each."
---

# Agent skills: onboarding

> **Status: partly verified.** The local case works in one smoke run. The cloud case, the one the
> setup was built for, is untested. Each open case below says how to test it.

## The question

A researcher or student asks their agent to "try Waveflow" on their own project. Does the agent get
oriented: does it look Waveflow up before writing Waveflow code, rather than guessing an API it was
never trained on? The design's quality is a separate question, which the other pages answer skill by
skill. This one asks only whether the agent finds its way in.

[`waveflow init`](../../guide/ai_tooling/mcp_setup.md#set-up-your-project-waveflow-init), run once in
the project, sets three things up for that:

| what | where | for |
|---|---|---|
| a five-line Waveflow section | appended to the project's `AGENTS.md` (`CLAUDE.md` imports it) | every agent, every session: Waveflow exists, do not guess it, load `waveflow-start`, and how to install Waveflow if it is missing |
| the skills, `waveflow-start` first | `.claude/skills/`, `.agents/skills/` | looking Waveflow up (MCP tools, or the same commands in a shell), installing it, beginning a design |
| the MCP server's config | `.mcp.json`, `.vscode/mcp.json` (this machine only) | the `waveflow_*` tools |

## What is verified

| case | evidence |
|---|---|
| An agent with the MCP server uses it | every [blind test](../../guide/ai_tooling/blind.md) since the server was built |
| An installed skill loads when the task matches | the skill arms of the [schema](schemas.md) and [HLS-body](kernel_hls.md) tests |
| **The `init` setup, locally**: a lab's own `AGENTS.md` with the Waveflow section appended, the skills, the MCP server | one smoke run (Sonnet 5.5, 2026-11-19): `waveflow-start` loaded at the first tool call; the agent chose the architecture through the MCP tools and read the protocol page before writing anything; it still followed the lab's own conventions |

## What is not verified, and how to test it

### 1. A cloud session with no Waveflow (claude.ai/code)

**The case:** a researcher opens their own repository in claude.ai/code. The session has a clone of
that repository and nothing else: Waveflow is not installed, and there is no MCP server (its config
names a local interpreter, so `init` keeps it out of the repository). Only the committed `AGENTS.md`
section and the skills reach the agent. They tell it to clone Waveflow beside the project and install
it, after which `waveflow kb` gives the same lookups as the MCP tools. **This is the case `init` and
`waveflow-start` were built for, and the most important to check.**

**How to test it** (by hand; the blind-test harness runs locally, where Waveflow is installed):

1. Make a new GitHub repository (private is fine) holding the onboarding test's request and the
   lab's files, from `evaluation/skill_tests/onboarding/`: `01_energy.md`, `given_01/AGENTS.md` and
   `given_01/CLAUDE.md`, all at the repository's root.
2. Clone it locally and run `waveflow init --no-mcp` in the clone. Commit everything it wrote and push.
3. Open the repository in claude.ai/code and send: *"I'd like to try Waveflow on a small accelerator.
   What I want is in 01_energy.md; build it in this folder."*
4. Watch for, in order:
   - it loads `waveflow-start`;
   - it finds Waveflow missing, clones `github.com/sdrangan/waveflow` and runs `pip install -e`;
   - `waveflow kb --text browse` (or another lookup) works afterwards;
   - its first Waveflow file comes **after** a lookup, not before;
   - it does not write Waveflow code from memory while the lookups are broken.
5. Note where it went wrong, if anywhere, and keep the session's link. A failure is fixed in the
   `AGENTS.md` section (`waveflow/mcp/resources/agents_block.md`) or the skill
   (`waveflow/skills/waveflow-start/`), never by softening the request.

### 2. Waveflow installed, but no MCP server

**The case:** an agent whose MCP connection was never set up, or one that does not use MCP. The skill
says to use the same lookups through the shell (`waveflow kb search`, `waveflow kb doc`, ...). Does it?

**How to test it:** this needs a harness option that leaves the server out but keeps the `waveflow`
command allowed. The harness's `--no-waveflow` arm blocks both, so it is the wrong arm. With a
`--no-mcp` option added to `waveflow blind-test`, run the onboarding test's `init` arm with it
(commands in `evaluation/skill_tests/onboarding/README.md`), and check in `summary.md` that the
`waveflow kb` commands appear before the first write.

### 3. Agents other than Claude

**The case:** Codex, Gemini CLI or VS Code chat with Copilot, which read `AGENTS.md` and
`.agents/skills/` rather than `CLAUDE.md` and `.claude/skills/`.

**How to test it:** by hand, as in case 1, but locally. In a fresh project folder, run `waveflow init`,
open the folder in the other agent, and send the same message. Check the same list. The blind-test
harness drives only Claude Code.

## The test

`evaluation/skill_tests/onboarding/`: the request, written as a researcher would write it; the lab's
own `AGENTS.md` and `CLAUDE.md`, so the Waveflow section is appended to someone else's file as it will
be in practice; and the commands for the two local arms (the MCP server alone, and with `waveflow
init`), on Opus and Sonnet. A run stops at the first review the architecture's process asks for, which
is enough to see whether the agent got oriented. What to record comes from the run's `summary.md`:
when the skill loaded, the first lookup against the first write, any API it guessed, and whether the
lab's own rules were still followed.
