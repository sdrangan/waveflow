---
title: Agent skills
parent: Evaluation
nav_order: 2
has_children: true
summary: "Do agent skills -- short SKILL.md instruction folders an AI coding agent loads when a task matches -- help an agent that has never seen Waveflow follow its patterns? The question, and the method every skill is tested by: blind runs with and without the skill that write code, reviewed by eye against a per-pattern sheet. Just started: two skills (data schemas, HLS kernel bodies), each with its own page and first results."
---

# Agent skills

> **Status: just started.** Two skills are written and have first results, one run per arm (see
> [data schemas](schemas.md) and [HLS kernel bodies](hls_body.md)). A third, `waveflow-start`, is
> the entry point; [onboarding](onboarding.md) lists what is verified about it and what is not. This page records the method so the
> numbers can be checked.

## The question

Waveflow's patterns -- every message is a schema, nothing is packed by hand, a kernel body is a
straight-line loop per message -- are spread over the [guide](../../guide/). An agent with the
[MCP server](../../guide/ai_tooling/) can search the guide, but it only finds a pattern if it thinks to
look for it at the right moment. Agents new to Waveflow often don't: the most common failure is to
declare a `DataList` and then decode its words by hand anyway.

An **agent skill** is a folder with a `SKILL.md` file: a name, a one-line description, and short
instructions. The format is an open standard that Claude Code, Codex, Gemini CLI, Copilot and other
agents read. An agent sees only the skill's description until a task matches it. It then loads the
instructions, and reads the files they point to only when it needs them. A skill can therefore hold
the rules that always apply and say which guide page to read before which step, without filling the
agent's context when the task is about something else.

The question is whether that changes what the agent writes.

## The skills

Each skill has its own page: what it contains, its test, and its results.

| skill | covers | status |
|---|---|---|
| [`waveflow-start`](onboarding.md) | the entry point: finding Waveflow (MCP or shell), installing it when a session lacks it; set up by `waveflow init` | verified locally in one smoke run; the cloud case and agents without MCP are untested (the page says how to test them) |
| [`waveflow-schema`](schemas.md) | data schemas: defining a message and moving it in Python and in Vitis HLS | one run per arm on Opus, Sonnet and Haiku |
| [`waveflow-hls-body`](hls_body.md) | Vitis HLS kernel bodies: loop shape, the lane loop, state across commands | one run per arm on Opus and Sonnet; both arms at the ceiling |
| `waveflow-module` | an `HwModule`, its ports and pysim behaviour, the command / response protocol, its unit harness | planned |
| `waveflow-memory` | AXI-MM, memory streams, BRAM, register maps, credit streams | planned |
| `waveflow-system` | composing modules with `HwSystem` and the system flow | planned |
| `waveflow-verify` | pysim, csim / cosim and XSI tests that assert counts, so a deadlock cannot pass | planned |

The skills are grouped by step in the design flow, about six in all rather than one per guide page:
sub-tasks that happen together share a skill, and fewer descriptions are easier for an agent to tell
apart. The plan for the remaining four is
[`plans/agent_skills.md`](https://github.com/sdrangan/waveflow/tree/main/plans/agent_skills.md).

Each `SKILL.md` points to the guide and does not repeat it, so it stays correct as the guide changes.

## How a skill is tested

Each test is a [blind test](../../guide/ai_tooling/blind.md): a fresh agent in an empty folder, with a
spec and nothing else. The two arms differ only in the skill:

| arm | the agent gets |
|---|---|
| MCP | the spec and the Waveflow MCP server -- what a student has today |
| MCP + skill | the same, plus the skill |

**The agent writes code; it does not run Vitis.** C simulation would show whether the code is
functionally correct, but not whether it follows the patterns, and patterns are what a skill is for.
Leaving Vitis out also makes a run a fraction as long. Python that needs no Vitis -- generating the
headers, running the model on a worked example -- is run, so a reviewer starts from code that executes.

Each run is then **reviewed by eye** against the skill's review sheet, one line per pattern, scored 0,
1 or 2. A small script lists the lines worth reading first (shifts, masks, `.range()`, defensive casts)
but does not score them. Its hits are places to look, not verdicts.

Two further checks run beside the task:

- **Triggering:** one-line prompts, some that should load the skill and some that should not,
  including near misses (about Waveflow, but not what the skill covers).
- **Process:** from the blind-test report, whether the agent loaded the skill, or read the pages it
  points to, before writing its first file, and how many API names it guessed wrong.

The specs, review sheets, reference answers and trigger prompts are in `evaluation/skill_tests/`, one
folder per skill.

## What is not covered yet

These limits apply to every skill; each skill's page lists its own.

- **Two skills so far.** The guide suggests about twenty. On the large models tested, both show their
  clearest effect on the tokens and on details; the next tests are built where the guide gives an agent
  less to copy.
- **One agent.** The blind-test harness drives Claude Code. The skill format is shared, but other
  agents are untested.
- **Other skills are present.** Claude Code offers its built-in skills and those of the operator's
  plugins in every run (19 in the smoke run), so neither arm sees Waveflow's skill alone. Both arms
  see the same set, so the comparison holds, but a skill that competes for the same tasks would blur
  it. A run on an account without plugins would remove that doubt.
- **Scoring by eye.** The review sheet is explicit, but a reviewer still judges it. Two reviewers per
  run would show how much that judgment varies.
- **Functional correctness** is not checked here. The [blind tests](../../guide/ai_tooling/blind.md)
  that build through C simulation and RTL still do that.
