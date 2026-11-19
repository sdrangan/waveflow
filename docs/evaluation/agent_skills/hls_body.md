---
title: "Agent skills: HLS kernel bodies"
parent: Agent skills
nav_order: 2
summary: "The second skill, waveflow-hls-body: writing a Vitis HLS kernel body in Waveflow's loop patterns. What it contains, its test (a gain stage that keeps a count across commands, written against given schemas and a golden, and checked by a plain-C++ compile against that golden), and the first results, one run per arm on Opus and Sonnet: every run, with or without the skill, wrote a correct body in the reference's shape, so the test is at the ceiling; the skill cut the tokens by a third and kept an ineffective reset pragma out. A harder test is next."
---

# Agent skills: HLS kernel bodies

> **Status: first results.** One run per arm on two models, scored by one reviewer. Both arms reached
> the ceiling, so this test cannot yet show what the skill does.

## The skill

`waveflow-hls-body` ([`waveflow/skills/waveflow-hls-body/SKILL.md`](https://github.com/sdrangan/waveflow/tree/main/waveflow/skills/waveflow-hls-body/SKILL.md))
covers writing the body of a Vitis HLS kernel. It contains:

- eight rules: one firing per message; packed samples through the lane loop, in the shape the
  throughput asks for; pragmas on the loop that does the work; messages by their schema; state across
  commands as a `static`, and only that, with the firing starting with a blocking read; decisions once
  per chunk, outside the loop; no blocking read of a reply just requested on another stream; arithmetic
  in the declared types;
- a table naming the guide page to read before each step;
- the shape of a body, and a checklist the agent runs on its own code before finishing.

Several of the rules are about failures that C simulation does not show: a body that deadlocks in RTL,
or a counter that moves during reset.

## The test

The method is the one on [Agent skills](index.md). The files are in `evaluation/skill_tests/hls_body/`.

### The task: a gain stage with statistics

Unlike the [schema test](schemas.md), the agent writes only the kernel body. It is **given** three
files, which it must not change:

- `gain_msgs.py`: the messages as schemas. A command (`tx_id`, `n`, a `gain` in `ap_fixed<16, 3>`), the
  samples (`ap_fixed<16, 1>`), the response, and a status (`tx_id`, `nsat`, `total`);
- `gen_headers.py`, which generates their C++ headers;
- `gain_model.py`, the golden: one command per `run_iter`.

The body is one firing of a free-running `hls::task` on three streams. For each command it:

1. writes the response, then each sample times the gain, rounded and saturated to
   `ap_fixed<16, 2, AP_RND, AP_SAT>`, with TLAST on the last result word only;
2. writes a status with how many results saturated, and `total`, the samples processed since reset.

`total` is the only state kept between commands. The sample loop must take **one input word per
clock** at 32 and 64 bits, which calls for the lane loop's word-per-iteration shape. The agent also
writes a `NOTES.md`: the shape and why, and roughly how many clocks a command takes.

The reference body matches the golden, and in a Vitis HLS 2025.1 csynth its sample loop schedules at
II=1.

### How a run is made

As for the schema test, with the given files passed by `--files`, which copies them beside the spec:

```bash
waveflow blind-test --prompt evaluation/skill_tests/hls_body/01_gain_stats.md \
    --files evaluation/skill_tests/hls_body/given_01/gain_msgs.py \
    --files evaluation/skill_tests/hls_body/given_01/gen_headers.py \
    --files evaluation/skill_tests/hls_body/given_01/gain_model.py \
    --folder ../waveflow_blind_tests/body01_skill_sonnet --model sonnet --no-approve \
    --message "Write the code specified in 01_gain_stats.md, in this folder, with Waveflow: its MCP server is available." \
    --skills waveflow/skills/waveflow-hls-body     # the skill arm only
```

### How a run is scored

The review sheet has 21 items, each 0, 1 or 2 (42 in all):

| group | items | what is checked |
|---|---|---|
| A. Shape | 5 | one command per call; one pipelined loop at II=1; a word per iteration with the lanes unrolled; `PF` from `lane_capacity`, lane arrays partitioned; no decisions in the loop |
| B. Moving the data | 4 | samples and results by the generated lane routines, with the tail count and TLAST; messages by their schema methods; nothing packed by hand |
| C. State and reset | 3 | `total` the only `static`; the firing starts with the blocking read; `total` updated once a command, wrapping at 32 bits |
| D. Arithmetic | 3 | the product at full precision, assigned to the output type; saturation counted as the golden counts it; no conversions the golden does not make |
| E. Correctness and notes | 3 | compiles and matches the golden; the clock estimate; the shape explained from the throughput |
| F. Process | 3 | read the loop-pattern page or the skill before writing; no API names guessed; ran the given scripts, changed none |

**One item is checked by running code.** `check_body.py` compiles the body as plain C++ with g++ and the
Vitis headers, without running Vitis, and feeds it four commands at both widths. It compares every
output word and TLAST with the golden, and checks that `total` carries across firings. That takes
seconds. It checks correctness, not patterns, so it is one item of 21.

**Triggering:** thirteen prompts, eight that should load the skill and five that should not (two near
misses: a schema header, a Python golden). Not run yet.

## Results

Four runs on 2026-11-19, one per arm on Opus 5.5 and Sonnet 5.5. The per-item notes and the commands
are in
[`evaluation/skill_tests/results/hls_body01_2026-11-19.md`](https://github.com/sdrangan/waveflow/tree/main/evaluation/skill_tests/results/hls_body01_2026-11-19.md).

| model | arm | skill loaded | score (of 42) | matches the golden | turns | tokens in / out |
|---|---|---|---|---|---|---|
| Opus 5.5 | MCP | -- | 42 | yes | 20 | 653k / 9.0k |
| Opus 5.5 | MCP + skill | 3rd tool call, before any write | 42 | yes | 17 | 440k / 6.1k |
| Sonnet 5.5 | MCP | -- | 41 | yes | 18 | 552k / 7.2k |
| Sonnet 5.5 | MCP + skill | 4th tool call, before any write | 42 | yes | 20 | 333k / 6.2k |

**Every run wrote a correct body in the reference's shape.** All four compile and match the golden word
for word at both widths. All four have the same structure: one command per call, a word per iteration
at II=1 with the lanes unrolled, the generated lane routines, the product at full precision, and
`total` the only `static`. The MCP-only runs found the loop-pattern page and a worked kernel body
through the MCP server, and followed them.

**So this test is at the ceiling, and cannot show the skill's effect on the code.** Two differences
remain:

- **Tokens.** With the skill, 33% (Opus) and 40% (Sonnet) fewer tokens in.
- **The reset pragma.** Both MCP-only runs put `total` under `#pragma HLS reset`, copied from the worked
  example, and one says this clears it on reset. The skill runs left it out and started the firing
  with the blocking read, as the skill's rule says. The guide disagrees with itself on whether the
  pragma alone is enough (below), so this is not scored.

**The task did not touch the traps the skill is for.** Its one piece of state is safe because the
firing starts with a read, and it has no reply to wait for, no credit window and no decision to make in
the loop. A second test should put one of those in the task.

**The test found three Waveflow gaps while it was written:**

- Two fixed-point formats that differ only in rounding or overflow mode get the same generated name,
  so their array utils collide. The test's output format was changed to avoid it. A fix renames
  existing designs' headers, so it needs a plan.
- `DataArray` arithmetic takes no scalar operand, so a gain from a header has to be wrapped in a
  one-element array.
- The guide disagrees with itself about `#pragma HLS reset`. Three pages teach a `static` flag under it
  as cleared by a reset. Two others say the pragma alone did not fix a reset problem under Vitis 2025.1
  and `config_rtl -reset state` did. Not yet checked in RTL.

## What next

- **A harder second task** with one of the traps: a task that writes before it reads, a request and its
  reply on two streams, or a credit stream. Those are where an agent without the skill has no worked
  example to copy.
- **The trigger prompts**, on Sonnet.
