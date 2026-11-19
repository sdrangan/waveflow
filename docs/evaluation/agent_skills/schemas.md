---
title: "Agent skills: data schemas"
parent: Agent skills
nav_order: 1
summary: "The first skill, waveflow-schema: defining a message as a DataSchema and moving it in Python and in Vitis HLS without packing anything by hand. What it contains, its test (a pulse detector's messages, chosen so the defaults are wrong and hand packing is tempting), and the results: a smoke run shows the agent loads the skill first and follows it to the right guide page; the A/B task runs are still to do."
---

# Agent skills: data schemas

> **Status: just started.** The skill is written and its test is ready to run. One smoke run, no task
> results yet.

## The skill

`waveflow-schema` ([`waveflow/skills/waveflow-schema/SKILL.md`](https://github.com/sdrangan/waveflow/tree/main/waveflow/skills/waveflow-schema/SKILL.md))
covers defining a message as a `DataSchema` and moving it in Python and in Vitis HLS. It contains:

- eight rules: every message is a schema, every field is sized, codes are `EnumField`s, nothing is
  packed by hand, generated files are never edited, headers come from the build steps, field values are
  used as they come, and word counts come from the schema;
- a table naming the guide page to read before each step;
- two short patterns (a command header, several kinds of message on one stream), and a checklist the
  agent runs on its own code before finishing.

It points to the guide and does not repeat it, so it stays correct as the guide changes.

## The test

The method is the one on [Agent skills](index.md): two blind runs that differ only in the skill, code
written but not run in Vitis, each run reviewed by eye. The files are in `evaluation/skill_tests/schema/`.

**The task** is a pulse detector's messages: a command with an 8-bit opcode and an unsigned
fixed-point threshold, 12-bit fixed-point samples, and three kinds of report on one stream. The agent
writes the schemas, a script that generates the C++ headers, a Python model of the detector, and the
HLS body. The task was chosen so that the defaults are wrong (an enum's minimum width is 1 bit, not 8)
and hand packing is tempting (two 12-bit samples per 32-bit word). A reference answer generates its
headers and passes its own check at 32 and 64 bits.

**The review sheet** has 27 lines, each scored 0, 1 or 2 (54 in all), in five groups: the Python
schemas, the header generation, the model, the HLS body, and the process. For example: is the command
a `DataList`? Is the opcode an 8-bit `EnumField`? Does the C++ read the header with the schema's
reader, and the samples with the generated lane routine? A run that hand-packs anywhere is marked
separately, whatever its total.

**Triggering:** fifteen one-line prompts, ten that should load the skill and five that should not. Two
of the five are near misses: they mention Waveflow but are not about data types (a simulation clock, a
register map).

## Results

No task runs yet. One smoke run (2026-11-19, Claude Code with `claude-opus-5-5`) checked that the
harness works. Given trigger prompt 1 ("define the command header for a kernel: an opcode, a 16-bit
transaction id, ..."), the agent:

- loaded `waveflow-schema` at its first tool call, before writing anything;
- read the fields page the skill points to;
- wrote a `DataList` with an `EnumField` opcode and sized fields.

One run of one prompt shows the mechanism works, not that the skill helps. The command is the one in
`evaluation/skill_tests/schema/trigger_prompts.md`.

| run | arm | model | tokens in / out | review score (of 54) | hand packing | skill loaded at |
|---|---|---|---|---|---|---|
| | | | | | | |

## What is not covered yet

- **One task.** A second task, from a different part of the schema guide (a `VarDataArray`, or a
  message moved over `m_axi` memory), would show whether the effect depends on the task.
- **Python alone.** Every line of the task touches both Python and C++. A Python-only task would show
  whether the skill helps where the agent has the least to guess.
