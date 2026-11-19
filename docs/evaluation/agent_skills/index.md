---
title: Agent skills
parent: Evaluation
nav_order: 2
has_children: false
summary: "Do agent skills -- short SKILL.md instruction folders an AI coding agent loads when a task matches -- help an agent that has never seen Waveflow follow its patterns? Just started: the first skill covers data schemas (DataList, fixed-point fields, serialization in Python and HLS), tested by blind runs with and without it that write code and are reviewed by eye against a per-pattern sheet. No results yet; this page says what is measured and how."
---

# Agent skills

> **Status: just started.** One skill is written and one test is ready to run. There are no results
> yet. This page records the method so the numbers, when they come, can be checked.

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

## The first skill: data schemas

`waveflow-schema` covers defining a message as a `DataSchema` and moving it in Python and in Vitis
HLS. It contains:

- eight rules: every message is a schema, every field is sized, codes are `EnumField`s, nothing is
  packed by hand, generated files are never edited, headers come from the build steps, field values are
  used as they come, and word counts come from the schema;
- a table naming the guide page to read before each step;
- two short patterns (a command header, several kinds of message on one stream), and a checklist the
  agent runs on its own code before finishing.

It points to the guide and does not repeat it, so it stays correct as the guide changes.

## How it is tested

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

Each run is then **reviewed by eye** against a sheet with one line per pattern, scored 0, 1 or 2: is the
command a `DataList`? Is the opcode an 8-bit `EnumField`? Does the C++ read the header with the
schema's reader, and the samples with the generated lane routine? A small script lists the lines worth
reading first (shifts, masks, `.range()`, defensive casts) but does not score them. Its hits are places
to look, not verdicts.

Two further checks run beside the task:

- **Triggering:** fifteen one-line prompts, ten that should load the skill and five that should not
  (two of them near misses: Waveflow, but not data types).
- **Process:** from the blind-test report, whether the agent read the skill or the schema pages before
  writing its first schema, and how many API names it guessed wrong.

The first task is a pulse detector's messages: a command with an 8-bit opcode and an unsigned
fixed-point threshold, 12-bit fixed-point samples, and three kinds of report on one stream. It was
chosen so that the defaults are wrong (an enum's minimum width is 1 bit, not 8) and hand packing is
tempting (two 12-bit samples per 32-bit word). A reference answer generates its headers and passes its
own check at 32 and 64 bits.

## Results

No task runs yet. One smoke run (2026-11-19, Claude Code with `claude-opus-5-5`) checked that the
harness works: given trigger prompt 1 ("define the command header for a kernel: an opcode, a 16-bit
transaction id, ..."), the agent loaded `waveflow-schema` at its first tool call, before writing
anything, read the fields page the skill points to, and wrote a `DataList` with an `EnumField` opcode
and sized fields. One run of one prompt shows the mechanism works, not that the skill helps. (The
command is the one in `evaluation/skill_tests/schema/trigger_prompts.md`.)

| run | arm | model | tokens in / out | review score (of 54) | hand packing | skill loaded at |
|---|---|---|---|---|---|---|
| | | | | | | |

## What is not covered yet

- **One skill, one task.** The guide suggests about twenty skills. Others follow only if this one
  shows an effect.
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
