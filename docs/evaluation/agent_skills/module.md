---
title: "Agent skills: modules"
parent: Agent skills
nav_order: 3
summary: "The third skill, waveflow-module: writing a free-running module from the interfaces its user describes -- each mapped to a stream port and the adaptor outside it, the protocol chosen and stated when the user leaves it open, run_iter, and a harness test that counts. Its test is three blocks in a student's words (the protocol left open; an ADC and a host on the bus, with a request Waveflow cannot meet; two streams the user asked for). First results, one run per arm on Opus and Sonnet: all four at 49-52 of 52, because the frames process and the mm_fir example already carry the mapping; the skill cut turns by 13-44% and was the only arm to state the open choice with its alternative. The runs found a timing window the reference had missed."
---

# Agent skills: modules

> **Status: first results.** One run per arm on two models, scored by one reviewer. All four runs are
> at or near the ceiling.

## The skill

`waveflow-module` ([`waveflow/skills/waveflow-module/SKILL.md`](https://github.com/sdrangan/waveflow/tree/main/waveflow/skills/waveflow-module/SKILL.md))
covers one step: the user names what a block talks to ("the host sets a threshold", "results go to
the host", "samples from the ADC"); the module turns each into a **stream port**, and whatever is not
a stream (a bus, registers, an interrupt, memory) into an **adaptor** outside its logic
([Stream patterns](../../guide/systems/streams.md)). It holds:

- **The rules**: free-running unless the spec asks for a launch; the logic on streams only;
  command-response with a status stream; errors fatal; an id between two input streams; tests that
  count; say so when Waveflow cannot do something.
- **The open choice.** When the user does not say how commands and data travel: one input stream (the
  command header, then its data) and two outputs, chosen without asking and **stated** with the reason
  and the alternative. When to depart from it: different sources, a configuration too large to repeat,
  data in memory, no jobs.
- **The mapping table**: what the user says, what the module declares, what sits outside it
  (`QueueIn`, `QueueOut` and its `IrqIF`, `RegBank`, `MemRStream` / `MemWStream`, `BramIF`, credit).
- A page per step, a skeleton class, a self-check.

Building the bus around the module is the system step (`waveflow-system`, planned); memory adaptors in
detail are `waveflow-memory`.

## The test

`evaluation/skill_tests/module/01_three_blocks.md`: three blocks, described as a student would, by
what each talks to. Python modules in pysim with their tests; no Vitis.

| block | what it tests |
|---|---|
| **A** contrast stretch: rows with an offset and a gain, from one block to another | the open choice: one stream or two, and is it stated |
| **B** energy monitor: frames from the ADC; a host on the bus sets a threshold at any time, takes reports without polling, reads a status; and asks for AMD-style interrupt-enable and clear-on-write registers | "host" without a launch; two sources; a register bank, a queue and its interrupt; a setting from the next frame; a request Waveflow cannot meet |
| **C** correlator: templates and jobs on two streams, as the user asks | two streams kept, paired by an id rather than a count |

The review sheet has 26 items (52 points); `check_blocks.py` runs the agent's tests and lists each
module's kind, ports and views. The reference answer passes in pysim.

## Results

| model | arm | score (of 52) | turns | tokens out |
|---|---|---|---|---|
| Opus 5.5 | MCP | 51 | 81 | 70.6k |
| Opus 5.5 | MCP + skill | 52 | 70 | 63.9k |
| Sonnet 5.5 | MCP | 49 | 77 | 63.4k |
| Sonnet 5.5 | MCP + skill | 49 | 43 | 52.2k |

Per item: `evaluation/skill_tests/results/module01_2026-11-20.md`.

- **Every run got the mapping right.** All three blocks free-running (the word "host" pulled none to
  `HostActivated`), stream-only, B's settings and status in a register bank and its reports in a queue
  with an interrupt, the interrupt registers **said not built** with the guide's own sentence, C's
  streams paired by an id. The MCP-only runs got there through the frames process and the `mm_fir`
  example.
- **What the skill changed:** fewer turns (-13% Opus, -44% Sonnet) and tokens, and the open choice in
  block A stated with its alternative -- both skill runs did, neither MCP run did.
- **A window the reference missed.** "From the next frame that starts after they are written": a
  check made before waiting for the next frame lets a setting written during the wait apply a frame
  late. Two runs closed it (a loop over both streams; the frame's first word), one stated it, one
  closed it through pysim internals. The skill, the sheet and the reference were corrected.
- **A gap in Waveflow:** pysim has no documented way to act at a burst's first word
  (`begin_read` exists, undocumented).

## What next

- **The trigger prompts** (`module/trigger_prompts.md`), on Sonnet.
- **A second task where the MCP server has no near copy**: a module whose data is in memory (a
  composite with `MemRStream`), or a mid-stream setting whose exact frame matters.
- A second reviewer.
