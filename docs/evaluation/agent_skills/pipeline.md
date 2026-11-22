---
title: "Agent skills: pipelines"
parent: Agent skills
nav_order: 5
summary: "The waveflow-pipeline skill: composite kernels of concurrent stages -- the graph, every link's depth, StreamBroadcast and its lockstep (drain your own copy before you wait on a sibling), a token per job, the last stage answering. Its test is an adaptive peak picker: one stage computes a threshold over the job, the other picks the indices, both need the samples. One run per arm on Opus and Sonnet: every run, with or without the skill, avoided the broadcast deadlock pysim cannot show (44 of 46); every run missed the same two points, which the skill now gives (46 of 46). The skill's effect is the cost: turns -55%, tokens in -70%."
---

# Agent skills: pipelines

> **Status: first results.** One run per arm on two models, scored by one reviewer, with the skill in
> its second version.

## The skill

`waveflow-pipeline` ([`waveflow/skills/waveflow-pipeline/SKILL.md`](https://github.com/sdrangan/waveflow/tree/main/waveflow/skills/waveflow-pipeline/SKILL.md))
covers composite kernels ([Kernels](../../guide/systems/kernels.md)): split only for concurrency;
declare the graph and nothing more; every link's depth stated; the command travels with the data and
the last stage answers; a token for every job; and the broadcast's lockstep. **A stage that waits on a
sibling must first drain its own copy, or that copy's link must hold the whole job.** pysim does not
show the hang (it admits a burst longer than a link once the link has room), so the skill says to check
the read order by reading the code.

## The test

`evaluation/skill_tests/pipeline/01_peak_picker.md`: jobs of up to 1024 signed samples, and for each
the indices whose magnitude exceeds `T = sum|x| * mult // (8n)`. The hardware lead asks for two
concurrent stages: one computes `T`, the other picks. Both need the samples, and `T` exists only after
the whole job. So a pick stage that waits for `T` with the samples still in its copy deadlocks at RTL
on the 1024-sample job, and passes pysim. Further traps: a job with no index (no empty burst), errors
that must still send a token, and the last stage answering.

The review sheet has 23 items (46 points). The reference holds a strict-xfail test recording the
pysim gap.

## Results

| model | arm | score (of 46) | turns | tokens in / out |
|---|---|---|---|---|
| Opus 5.5 | MCP | 44 | 78 | 5.1M / 48k |
| Opus 5.5 | MCP + skill | **46** | 35 | 1.6M / 24k |
| Sonnet 5.5 | MCP | 44 | 48 | 1.8M / 32k |
| Sonnet 5.5 | MCP + skill | **46** | 21 | 0.5M / 15k |

Per item, the first skill version and the commands:
[`evaluation/skill_tests/results/pipeline01_2026-11-21.md`](https://github.com/sdrangan/waveflow/tree/main/evaluation/skill_tests/results/pipeline01_2026-11-21.md).
The MCP runs stopped after their first stage for review and were sent one "Approved. Continue.".

- **Every run avoided the deadlock**, giving the lockstep as the reason: the pick stage reads its whole
  copy into a buffer before it waits. The MCP runs copied it from `block_eq` through the frames process.
  Every run also sent a token for every job, answered from the last stage, and sent no index burst for
  a count of zero.
- **Every run, in both arms, missed the same two points** with the first skill: no stage checked the id
  the token carries, and none gave the cost of the alternative (a buffer of the job in the stage
  against a FIFO of 514 words on the link). Two lines were added to the skill. Both reruns then
  checked the id (Sonnet also tested a mismatch) and gave both costs: 46 of 46.
- **The skill's measured effect is cost**: turns -55% (both models), tokens in -69% (Opus) and -73%
  (Sonnet).

## What next

- A pipeline task that `block_eq` does not show: a stage whose output is shorter than a job, credit
  between stages, or a three-stage chain whose depths must be sized together.
- The trigger prompts, on Sonnet.
