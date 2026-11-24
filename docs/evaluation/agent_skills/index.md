---
title: Agent skills
parent: Evaluation
nav_order: 2
has_children: true
summary: "Do agent skills -- short SKILL.md instruction folders an AI coding agent loads when a task matches -- help an agent that has never seen Waveflow follow its patterns? The question, the method every skill is tested by (blind runs with and without the skill that write code, reviewed by eye against a per-pattern sheet), and the results so far: six design skills, each with one run per arm on Opus and Sonnet. Most tasks are at or near the ceiling in both arms, because the MCP server's frames and worked examples already carry the agent; the skill's effect shows on the weaker model (devices: Sonnet 44 to 50 of 50), on details no example shows, and on the cost (turns down by up to 55%)."
---

# Agent skills

> **Status: first results for every design skill.** Six skills have one run per arm on Opus 5.5 and
> Sonnet 5.5 ([data schemas](schemas.md), [HLS kernel bodies](kernel_hls.md), [kernels](kernel.md),
> [devices](device.md), [pipelines](pipeline.md), [systems](system.md)). The entry point,
> `waveflow-start`, is checked by [onboarding](onboarding.md). Every score is from one reviewer. This
> page records the method so the numbers can be checked.

## The results at a glance

Each test is a design task in a student's words with traps placed in it. The traps are what the skill's
rules are about, and an agent that guesses is likely to fall into them. *Score* is the review sheet's
total; the skill arm is the skill as it now is.

| skill | what the test asks | the traps | score, MCP / MCP + skill (Opus; Sonnet) | turns, skill against MCP |
|---|---|---|---|---|
| [`waveflow-schema`](schemas.md) | messages for a pulse report, in Python and HLS | hand-packing a declared schema | 51 / 53; 52 / 54 (of 54) | -39% / -22% |
| [`waveflow-kernel-hls`](kernel_hls.md) | a gain stage's HLS body, a count kept across commands | the lane loop, reset of state | 44 / 44; 43 / 43 (of 44) | -10% / +11% (tokens in -30% / -41%) |
| [`waveflow-kernel`](kernel.md) | three blocks described by what they talk to | an open protocol choice; a request Waveflow cannot meet | 51 / 52; 49 / 49 (of 52) | -13% / -44% |
| [`waveflow-device`](device.md) | a DDR offload a host drives over AXI | a status register with an interrupt (not buildable); done means stored; no zero-length read | 49 / 50; 44 / 50 (of 50) | -33% / even |
| [`waveflow-pipeline`](pipeline.md) | a peak picker as two concurrent stages fed by a broadcast | the broadcast deadlock pysim cannot show; a token per job | 44 / 46; 44 / 46 (of 46) | -55% / -56% |
| [`waveflow-system`](system.md) | two given devices, a memory and a host: the system, its DAG, its tests | tests that would pass a device that deadlocks or drops a word | 48 / 48; 48 / 48 (of 48) | -8% / +24% |

What the six show together:

- **The MCP server already carries both large models most of the way.** Its frames process and worked
  examples (`block_eq`, `markov`, `memcpy`, `mm_fir`) are what the agents copy, and the traps that a
  worked example shows are caught in both arms: every pipeline run avoided the broadcast deadlock,
  every system run's tests caught both broken devices.
- **Where the skill changes the code**, it is through a rule no example shows, or on the weaker model:
  devices on Sonnet (44 to 50: word counts from the schema, no zero-length read, the harness), stating
  an open choice with its alternative (kernels), checking a token's id and costing the alternative
  (pipelines, after a fix).
- **The skill usually cuts the cost**, by up to 55% of the turns and 73% of the input tokens
  (pipelines), but not on every task (systems: +24% on Sonnet, whose skill run wrote more tests).
- **The skill arms skip the frames process's review stop.** In the device and pipeline tests the MCP
  runs stopped after their first stage for review and needed one "Approved. Continue."; no skill run
  stopped.
- **Every run found something to fix in a skill.** A wrong rule is followed (kernel bodies, reset). A
  pointer into the folder that holds the answers leaks them (devices: two runs discarded, now
  guarded by a test). A point that every run misses belongs in the skill (pipelines). After the fixes,
  every skill arm is at or within a point of the ceiling.

**A different kind of test: a held-out design.** [Shared kernels](shared_kernel.md) asks a larger question
than one skill's -- can an agent design and build a whole accelerator whose hard part is sharing one kernel,
and how long does it take -- on a problem kept outside the repository, before and after the framework
work it needed. Before, agents with and without Waveflow had the architecture on paper; after, every chat
run used the new blocks correctly, and one build run reached passing pysim in 14 minutes and found four
framework gaps.

**Held-out problems are not in this repository.** Their specifications, rubrics, run transcripts and
scores are kept in a separate, private evaluation repository (`sdrangan/waveflow-eval`), so that no agent working in this
repository -- an agent under test, or one developing Waveflow -- can read them, and the problems stay
unseen for later runs. This repository holds only the method and the results in outline. Researchers
who want the full material for a held-out study can ask the maintainer for read access (open an
issue on the Waveflow repository). A problem is released publicly once it is retired as a test.

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
| [`waveflow-kernel-hls`](kernel_hls.md) | Vitis HLS kernel bodies: loop shape, the lane loop, state across commands | one run per arm on Opus and Sonnet; both arms at the ceiling |
| [`waveflow-kernel`](kernel.md) | a module from the interfaces its user describes: streams and adaptors, how commands and data travel, `run_iter`, its harness test | one run per arm on Opus and Sonnet; all at 49-52 of 52; turns -13..44% |
| [`waveflow-device`](device.md) | adaptors and devices: memory readers and writers, the host's queues, register bank and interrupts, credit across a bus, BRAM, the HLS top and sub-top | one run per arm on Opus and Sonnet; skill arm 50 of 50 on both after two fixes; Sonnet without it 44 |
| [`waveflow-pipeline`](pipeline.md) | composite kernels: stages, internal streams and their depths, broadcast (drain your own copy), a token per job, the last stage answers | one run per arm on Opus and Sonnet; MCP 44, skill 46 of 46 after one fix; turns -55% |
| [`waveflow-system`](system.md) | `HwSystem(sim, dut)`, the harness and the cut, the build DAG, tests that count so a deadlock cannot pass | one run per arm on Opus and Sonnet; all four 48 of 48, every one catching both hidden faults; redundant for this task |
| [`waveflow-architecture`, `waveflow-shared-kernel`](shared_kernel.md) | the map of a design; one kernel shared by several clients, a cycle in a composite | a held-out design, before and after the shared-kernel work: four chat runs (Opus, Sonnet) all right afterwards; a build run from an empty folder to passing pysim in 14 minutes, which found four framework gaps |

The skills are grouped by step in the design flow, about six in all rather than one per guide page:
sub-tasks that happen together share a skill, and fewer descriptions are easier for an agent to tell
apart. The plan they were built from is
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

- **One task per skill, one run per arm.** Most tasks are at the ceiling, so they cannot rank the
  arms; the next tests are built where no worked example gives the agent something to copy.
- **Triggering is untested.** Each skill's trigger prompts (`evaluation/skill_tests/*/trigger_prompts.md`)
  have not been run, so whether a skill loads when it should, and only then, is not yet known. In
  every run here the skill was installed alone and loaded at the second or third tool call.
- **One agent.** The blind-test harness drives Claude Code. The skill format is shared, but other
  agents are untested.
- **The checkout is reachable.** The Waveflow install is editable, so an agent can read the clone,
  including `evaluation/skill_tests/`, where the reference answers live. Each run's transcript is
  checked for reads there; a skill must never point there (`tests/mcp/test_skills.py` checks).
- **Other skills are present.** Claude Code offers its built-in skills and those of the operator's
  plugins in every run (19 in the smoke run), so neither arm sees Waveflow's skill alone. Both arms
  see the same set, so the comparison holds, but a skill that competes for the same tasks would blur
  it. A run on an account without plugins would remove that doubt.
- **Scoring by eye.** The review sheet is explicit, but a reviewer still judges it. Two reviewers per
  run would show how much that judgment varies.
- **Functional correctness** is not checked here. The [blind tests](../../guide/ai_tooling/blind.md)
  that build through C simulation and RTL still do that.
