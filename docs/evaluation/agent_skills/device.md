---
title: "Agent skills: devices"
parent: Agent skills
nav_order: 4
summary: "The waveflow-device skill: kernels joined to memory and to a host's bus through adaptors (MemRStream / MemWStream in-band, the writer's echo as done, queue views and their interrupts, build_mm_device). Its test is a gain-and-shift offload on DDR buffers that a host drives over AXI, asking for a status register with an interrupt, which Waveflow cannot build. One run per arm on Opus and Sonnet: Opus 49 without the skill, Sonnet 44; with the skill as fixed, both 50 of 50. The first skill version pointed into the folder that holds the answers, and its runs were discarded."
---

# Agent skills: devices

> **Status: first results.** One run per arm on two models, scored by one reviewer, with the skill in
> its third version (each fix below came from a run).

## The skill

`waveflow-device` ([`waveflow/skills/waveflow-device/SKILL.md`](https://github.com/sdrangan/waveflow/tree/main/waveflow/skills/waveflow-device/SKILL.md))
covers what sits between a kernel and everything that is not a stream
([Devices and the DUT](../../guide/systems/devices.md)). Its twelve rules include:

- the kernel stays streams-only, and the composite holding the memory reader and writer is a
  **device**;
- memory through the framework's in-band `MemRStream` / `MemWStream`, with addresses and lengths in
  bus words counted by the schema, and never a zero-length read;
- **"done" means stored**: the status rides behind the write and comes back on the writer's echo;
- an issue kernel that asks and a compute kernel that takes, never one kernel that asks and then
  blocks on the reply;
- the host's views (`QueueIn`, `QueueOut`, `RegBank` ...) declared on the class, the host woken only
  by a queue's interrupt, and **the register bank raises no interrupt**: say so.

## The test

`evaluation/skill_tests/device/01_scale_offload.md`: an ARM host keeps 16-bit sample buffers in DDR and
offloads `y = sat16((x * gain) >> shift)`. It sends jobs over AXI (several in flight), wants a status
"it can read from a register" and an interrupt when a job's output is in DDR, and a fatal error for
`n > 2048`. The agent writes the device, a bus system with a host, and pysim tests.

| trap | what is right |
|---|---|
| a status register with an interrupt | not buildable; a queue out with its interrupt, and say so |
| "done" | the status through the writer's echo, so it means stored |
| the error | answered through the writer with no read issued, then stop |
| a length not a multiple of four | the last word is written whole; state it |

The review sheet has 25 items (50 points). The reference answer passes in pysim.

## Results

| model | arm | score (of 50) | turns | tokens in / out |
|---|---|---|---|---|
| Opus 5.5 | MCP | 49 | 80 | 7.6M / 55k |
| Opus 5.5 | MCP + skill | **50** | 54 | 5.2M / 37k |
| Sonnet 5.5 | MCP | 44 | 56 | 4.3M / 39k |
| Sonnet 5.5 | MCP + skill | **50** | 57 | 4.7M / 39k |

Per item, the earlier skill versions and the commands:
[`evaluation/skill_tests/results/device01_2026-11-21.md`](https://github.com/sdrangan/waveflow/tree/main/evaluation/skill_tests/results/device01_2026-11-21.md).
The MCP runs stopped after their first stage for review, as the `bus_system` frame says, and were sent
one "Approved. Continue."; their figures add both parts.

- **Every run caught the trap**: a queue out with its interrupt for "done", and the register bank's
  missing interrupt said in so many words. Every run sent the status through the writer.
- **The skill made the difference on Sonnet** (44 to 50). The MCP run counted words by hand, sent a
  one-word read for a failed job (the reader refuses a zero-length one), built its test bench by hand
  rather than with the harness, and kept the output's spare lanes by a read-modify-write of the last
  word. Each of these is a line in the skill.
- **Opus without the skill reached 49**, by redesigning when pysim refused its zero-length read.
- **Cost:** Opus -33% turns and -32% tokens; Sonnet about even.

**Three versions of the skill were run:**

1. The committed skill pointed to a worked trial under `evaluation/skill_tests/results/`. Both runs
   followed it into the checkout; one listed `evaluation/skill_tests/`, so it saw the reference answer's
   file names. Neither opened the answers. **Both runs were discarded**, the pointer replaced by the
   `memcpy` example, and a test now fails if any skill names `evaluation/`.
2. Both runs scored 48: each built a single command stream and never said so, and left the host's C++
   twin out of the "what RTL still needs" list.
3. Two lines added to the self-check (state the protocol; name what RTL still needs): both 50.

## What next

- A second test where no frame carries the MCP arm: two inputs from memory (`z = f(x, y)`, two
  readers), or a credit stream across the bus.
- The trigger prompts (`device/trigger_prompts.md`), on Sonnet.
- A second reviewer.
