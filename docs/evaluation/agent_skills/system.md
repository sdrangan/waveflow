---
title: "Agent skills: systems"
parent: Agent skills
nav_order: 6
summary: "The waveflow-system skill: HwSystem(sim, dut), each module alone in its harness, one build DAG with a prefix per system, and tests that count so a deadlock or a dropped word cannot pass. Its test gives two finished devices and asks for the system, its DAG and its tests; the reviewer then runs those tests against a device that stops after its second job and one that writes a word short. One run per arm on Opus and Sonnet: all four at 48 of 48, and every one caught both faults. The MCP server alone carries this task; the skill is redundant here."
---

# Agent skills: systems

> **Status: first results.** One run per arm on two models, scored by one reviewer. All four runs are
> at the ceiling.

## The skill

`waveflow-system` ([`waveflow/skills/waveflow-system/SKILL.md`](https://github.com/sdrangan/waveflow/tree/main/waveflow/skills/waveflow-system/SKILL.md))
covers the system around the devices ([Verification](../../guide/systems/verification.md)): bottom-up,
a system per level; the DUT named and the test bench derived; only what has an RTL twin in an
`HwSystem`; a golden from the intents; **every test counts first**; one DAG with a prefix per system,
the steps that need no toolchain run and the rest named; errors mid-scenario; scenarios at the edges.

## The test

`evaluation/skill_tests/system/01_two_devices.md` gives two devices, finished and tested: **ScaleDev**
(a gain-and-shift offload on memory buffers, behind a command and a status queue) and **Checksum**
(samples in, a weighted sum out). An ARM host, both devices and one DDR share a 64-bit bus. Per job the
host has ScaleDev scale a buffer, reads it back and checksums it. The agent writes the system, a build
script with each device alone and the whole on one DAG, and tests that "would catch a device that stops
answering partway through, or one that writes a wrong or missing word".

**The check is by running code.** `check_variants.py` runs the trial's tests as given, then against a
scaler that stops after its second job (`deadlock`) and one that writes one word fewer per output
(`drop`). The wanted result is PASS, FAIL, FAIL. The agent sees only the hook in `devices.py` that
makes the swap possible, never the broken devices.

The review sheet has 24 items (48 points).

## Results

| model | arm | score (of 48) | given / deadlock / drop | turns | tokens in / out |
|---|---|---|---|---|---|
| Opus 5.5 | MCP | 48 | PASS / FAIL / FAIL | 92 | 11.8M / 64k |
| Opus 5.5 | MCP + skill | 48 | PASS / FAIL / FAIL | 85 | 8.1M / 59k |
| Sonnet 5.5 | MCP | 48 | PASS / FAIL / FAIL | 68 | 5.8M / 58k |
| Sonnet 5.5 | MCP + skill | 48 | PASS / FAIL / FAIL | 84 | 8.0M / 71k |

Per item and the commands:
[`evaluation/skill_tests/results/system01_2026-11-21.md`](https://github.com/sdrangan/waveflow/tree/main/evaluation/skill_tests/results/system01_2026-11-21.md).

- **Every run built what the guide describes**: an `HwSystem` with a `DutTop` of the two devices, the
  memory and the crossbar; a `SwHost` waiting on four interrupts, with `scenario_bursts()` and its
  C++ twin said not written; four or five systems on one DAG, a prefix each, through `run_dag_cli`;
  `scenario`, `pysim` and `golden` run, and `csynth` to `compare` named with the reason they were not.
- **Every run's tests caught both faults**, and every run, unasked, wrote 5 to 10 broken devices of its
  own to show its tests fail on them.
- **The skill changed neither the score nor, consistently, the cost**: Opus -8% turns, Sonnet +24%
  (its skill run wrote more tests). The `bus_system` frame, the `markov` example and the verification
  page already carry this task. **For this task the skill is redundant.**

## What next

- Keep the skill: it costs little, and it is the only place the system rules are gathered. A test
  that could show its effect needs what no worked example shows: a host overlapping two devices with a
  window that can deadlock, or a DAG run through `compare` at RTL.
- The trigger prompts, on Sonnet.
