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
written but not run in Vitis, each run reviewed by eye. This section summarizes the test; the full
spec, review sheet, reference answer and prompts are in `evaluation/skill_tests/schema/`.

### The task: a pulse detector's messages

The agent gets a one-page spec for a free-running kernel on two AXI4-Streams of 32- or 64-bit words.
For each command it reads a burst of samples and reports every sample above a threshold.

| stream | message | fields |
|---|---|---|
| `s_in` | command | `op` (uint8: `DETECT`, `END`), `tx_id` (uint16), `nsamp` (uint16), `thresh` (`ap_ufixed<16, 4>`) |
| `s_in` | samples | `nsamp` values, each `ap_fixed<12, 1>` |
| `m_out` | `START` | `kind` (uint8), `tx_id` |
| `m_out` | `PEAK` | `kind`, `index` (uint16), `value` (`ap_fixed<12, 1>`) |
| `m_out` | `DONE` | `kind`, `tx_id`, `count` (uint16); TLAST on its last word only |

The agent writes five files:

1. `pulse_msgs.py`: every message as Python;
2. `gen_headers.py`: generates the C++ headers for both word widths, and is run;
3. `pulse_model.py`: the detector as a function from input words to output words, with a check on a
   worked example at both widths, and is run;
4. `pulse_det_body.cpp`: the HLS kernel body, written as for synthesis but not compiled;
5. `NOTES.md`: each message's word layout at 32 bits, and anything Waveflow could not do.

The task is built to catch the usual mistakes:

- **The defaults are wrong.** An `EnumField`'s default width is the minimum (1 or 2 bits here); the
  spec says 8.
- **Hand packing is tempting.** Two 12-bit samples fit in a 32-bit word, five in a 64-bit word, and
  neither divides evenly.
- **Two fixed-point formats meet.** A signed sample is compared with an unsigned threshold.
- **Several kinds of message share one stream**, so the agent needs a tag schema rather than a test
  on raw bits.

A reference answer generates its headers and passes its own check at 32 and 64 bits.

### How a run is made

Each run is one `waveflow blind-test` in a fresh folder outside the clone, with the same first message
in both arms:

```bash
waveflow blind-test --prompt evaluation/skill_tests/schema/01_pulse_report.md \
    --folder ../waveflow_blind_tests/schema01_skill_sonnet --model sonnet --no-approve \
    --message "Write the code specified in 01_pulse_report.md, in this folder, with Waveflow: its MCP server is available." \
    --skills waveflow/skills/waveflow-schema      # the skill arm only
```

The harness writes a `summary.md` beside the agent's files: every tool call, which guide pages the
agent read and when, whether the skill was loaded and at which call, edits to generated files, and
the token counts.

### How a run is scored

The reviewer first runs `lint_schema_use.py` on the folder, which lists lines worth reading (shifts,
masks, `.range()`, `int.from_bytes`, defensive casts). It does not score anything. The reviewer then
reads the code against the review sheet: 27 items, each scored **2** (as the guide does it), **1**
(partly) or **0** (not done, or done by hand), 54 in all.

| group | items | what is checked |
|---|---|---|
| A. Python schemas | 6 | the command is a `DataList`; `op` and `kind` are 8-bit `EnumField`s; both `FixedField` formats; the samples as an element array; one schema per output kind plus a tag schema; every width as specified |
| B. Header generation | 4 | `StreamUtilsStep` and a `DataSchemaStep` per schema; `ArrayUtilsStep` for the samples; the script ran; nothing generated was edited or written by hand |
| C. Python model | 7 | the command by `deserialize`; the samples by `read_array`; outputs built as schema instances and serialized; word counts from the schema; the threshold compared exactly; the worked example asserted at both widths and passing; no defensive `int()` / `np.asarray()` |
| D. HLS body | 7 | the command by `read_axi4_stream<W>`; the samples by the generated lane routine; outputs by `write_axi4_stream<W>` with TLAST on `DONE` only; the generated enum, not an integer; the compare in `ap_fixed`; a loop per command and a straight-line loop over the samples; plausibly compiles |
| E. Process | 3 | read the skill or the schema pages before writing; no API names guessed and then corrected; the layout in `NOTES.md` matches `serialize` |

A run that packs or unpacks by hand anywhere (a 0 on any of C1-C3 or D1-D3) is flagged separately,
whatever its total.

### Triggering

Fifteen one-line prompts check whether the skill loads when it should and stays out when it should
not. Each is a short blind run with the skill installed, stopped after its first few tool calls;
`summary.md` says whether the skill loaded.

- **Ten should load it**, for example "Using Waveflow, define the command header for a kernel: an
  opcode (READ, WRITE, RESET), a 16-bit transaction id, a 32-bit address and a 12-bit length", or
  "With Waveflow, how should I pack four signed 10-bit samples into a 64-bit stream word?"
- **Five should not**, for example "Explain what TLAST means on an AXI4-Stream". Two of the five are
  near misses: they mention Waveflow but are not about data types (a simulation clock, a register
  map). A load there means the description is too broad.

The trigger prompts have not been run yet.

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

- **Haiku is set aside.** It is well below the models students use, so getting it to load the skill
  is not pursued; later runs use Sonnet and Opus.
- **More runs per arm** on Sonnet, the model a student is most likely to pick on a budget, to tell an
  effect from noise.
- **The trigger prompts**, on Sonnet.
- **The next skill**: writing HLS kernel bodies (the lane loop, a straight-line loop per message, no
  state across firings), the area where the one structural mistake on a strong model came from.

## What is not covered yet

- **One task.** A second task, from a different part of the schema guide (a `VarDataArray`, or a
  message moved over `m_axi` memory), would show whether the effect depends on the task.
- **Python alone.** Every line of the task touches both Python and C++. A Python-only task would show
  whether the skill helps where the agent has the least to guess.
