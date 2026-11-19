---
title: "Agent skills: data schemas"
parent: Agent skills
nav_order: 1
summary: "The first skill, waveflow-schema: defining a message as a DataSchema and moving it in Python and in Vitis HLS without packing anything by hand. What it contains, its test (a pulse detector's messages, chosen so the defaults are wrong and hand packing is tempting), and the first results, one run per arm on Opus, Sonnet and Haiku: on Opus and Sonnet both arms follow the main patterns and the skill adds the last few points at about half the tokens; Haiku never loaded the skill, and without it hand-packed. The test also found a codegen bug."
---

# Agent skills: data schemas

> **Status: first results.** One run per arm on each of three models, scored by one reviewer. Too few
> runs to estimate an effect; enough to see where to look next.

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

Six runs on 2026-11-19, one per arm on each of three Claude models, all in Claude Code. The class
this skill is for lets students choose their own model, so the test spans a large, a middle and a
small one. Every run used the same spec and the same first message. The per-item scores, what decided
each one, and the commands are in
[`evaluation/skill_tests/results/schema01_2026-11-19.md`](https://github.com/sdrangan/waveflow/tree/main/evaluation/skill_tests/results/schema01_2026-11-19.md).
The scores are by one reviewer, by eye, against the review sheet. A second reviewer has not checked
them.

| model | arm | skill loaded | score (of 54) | hand packing | turns | tokens in / out |
|---|---|---|---|---|---|---|
| Opus 5.5 | MCP | -- | 51 | no | 62 | 3.36M / 25.7k |
| Opus 5.5 | MCP + skill | 2nd tool call, before any write | 53 | no | 38 | 1.59M / 17.3k |
| Sonnet 5.5 | MCP | -- | 52 | no | 46 | 2.02M / 14.1k |
| Sonnet 5.5 | MCP + skill | 2nd tool call, before any write | 54 | no | 36 | 1.39M / 12.2k |
| Haiku 4.5 | MCP | -- | 27 | **yes** | 53 | 3.83M / 21.6k |
| Haiku 4.5 | MCP + skill | **never** | 20 | no (its C++ reads and writes nothing) | 26 | 1.22M / 22.7k |

Wall-clock times are left out: the runs were made on a laptop in use, and four of them ran at once.
Tokens in are mostly cache reads (over 90% in every run).

**Opus and Sonnet follow the main patterns with or without the skill.** All four runs:

- write every message as a schema, with 8-bit `EnumField` codes and both fixed-point formats;
- build the tagged union with a tag schema;
- declare the sample type with the `include_dir` its array header needs;
- read and write every message with the generated methods, and the samples with the generated lane
  routine.

None packs anything by hand, and every layout in their notes matches `serialize`.

The skill adds the last few points:

- **Defensive casts.** Both MCP-only runs wrap field reads in `int()` and `np.asarray()`
  (`Op(int(cmd.op)) == Op.END`), 10 and 13 times. Both skill runs keep values as they come, as the
  skill's rule 7 says.
- **State between commands.** The Opus MCP-only kernel keeps a `static` flag across firings, which
  the spec rules out.

**With the skill, these models also take less work:** 22% (Sonnet) to 39% (Opus) fewer turns, and 31%
(Sonnet) to 53% (Opus) fewer tokens in. One run per arm cannot separate that from run-to-run variation, but both
models point the same way.

**Haiku is a different story, and the skill did not get a chance.**

- **With the skill installed, Haiku never loaded it.** It saw the skill's description among the
  others and went on without it. Its work stopped short:
  - a header script that imports modules that do not exist and was never run;
  - a model that never decodes its input;
  - a C++ body whose reads and writes are comments.
- **Without the skill, Haiku hand-packed in both languages:**
  - extracting samples by bit offset, at the wrong number per word;
  - calling C++ methods that do not exist;
  - packing its own 64-bit test vectors with shifts and masks.

So for a small model the first problem is getting the skill loaded at all, not what the skill says.

**The test also found Waveflow defects.** The Opus MCP-only agent reported that the generated C++
`get_nwords<W>(n)` counts densely packed elements, which is short when an element's width does not
divide the word's (five 12-bit samples need 3 words at 32 bits, not 2). Two smaller gaps came up
too:

- `from_real` returned an array of a cached format class, which `write_uint32_file` then refused;
- Python had no way to compare fixed-point values, so a signed sample against an unsigned threshold
  had to be improvised (floats, or shifted stored integers).

All three are fixed. The fixed-point page now has
[exact comparisons](../../guide/schema/python/fixpoint.md#comparison), and the skill points to them.
The runs above used the skill as it was before that line was added.

**The smoke run** that checked the harness before these, on trigger prompt 1 with Opus, loaded the
skill at its first tool call and wrote a correct `DataList`.

## What next

- **Get the skill loaded on small models.** Options to test, smallest first:
  - a line in the project's `AGENTS.md` / `CLAUDE.md` (which `waveflow skills install` could write)
    saying to load the `waveflow-*` skill before writing Waveflow code;
  - the same sentence in the MCP server's instructions;
  - a sharper description.
  Then rerun Haiku.
- **More runs per arm** on Sonnet and Haiku, the models a student is most likely to pick on a budget,
  to tell an effect from noise.
- **The trigger prompts**, on Haiku first: a small model that does not load the skill on the task is
  unlikely to load it on a one-line request.

## What is not covered yet

- **One task.** A second task, from a different part of the schema guide (a `VarDataArray`, or a
  message moved over `m_axi` memory), would show whether the effect depends on the task.
- **Python alone.** Every line of the task touches both Python and C++. A Python-only task would show
  whether the skill helps where the agent has the least to guess.
