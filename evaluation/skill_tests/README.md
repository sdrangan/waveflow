# Skill tests

Do agent skills (`SKILL.md` folders, the open Agent Skills format) help an agent that has never seen
Waveflow follow its patterns?  Each test is a **blind run** (`waveflow blind-test`, see
`docs/guide/ai_tooling/blind.md`) in two arms that differ only in the skill:

| arm | the agent gets |
|---|---|
| **MCP** | the spec and the Waveflow MCP server -- what a student has today |
| **MCP + skill** | the same, plus the skill folders in the trial folder's `.claude/skills/` |

## Code only, reviewed by eye

The runs write code but do **not** synthesize or simulate it in Vitis.  C simulation would say whether
the code is functionally right, but not whether it follows the patterns -- which is what a skill is
for -- and it would make each run several times longer.  Instead each run is scored on a **review
sheet** (`<skill>/answers/*.review.md`): one line per pattern, 0 / 1 / 2.  `lint_schema_use.py` lists
the lines worth reading first (hand packing, defensive casts, hand-computed word counts); it points,
it does not score.  Python that needs no Vitis -- header generation, the model on a worked example --
is run by the agent, so the review starts from code that at least executes.

## Layout

```text
skill_tests/
  lint_schema_use.py
  schema/                         the waveflow-schema skill
    01_pulse_report.md            a task spec: messages, model, HLS body
    trigger_prompts.md            does the skill load when it should, and only then
    answers/                      linked by no spec, so never copied to an agent
      01_pulse_report.review.md   the review sheet
      ref_01/                     a reference answer: runs, generates its headers, passes its check
  hls_body/                       the waveflow-hls-body skill
    01_gain_stats.md              a task spec: the kernel body only
    given_01/                     the schemas, header script and golden the agent is given (--files)
    check_body.py                 compiles a trial's body (plain g++) and compares it with the golden
    trigger_prompts.md
    answers/
      01_gain_stats.review.md
      ref_01/                     the reference body (II=1 at csynth) and the driver check_body.py uses
  module/                         the waveflow-module skill
    01_three_blocks.md            three blocks described by what they talk to: Python modules + tests
    check_blocks.py               runs a trial's tests; lists each module's kind, ports and mm_views
    trigger_prompts.md
    answers/
      01_three_blocks.review.md
      ref_01/                     the reference: three blocks, their harness tests, DESIGN.md
  onboarding/                     does an agent get oriented on a researcher's project (not design quality)
    01_energy.md                  the request, in a researcher's words
    given_01/                     the lab's own AGENTS.md and CLAUDE.md (--files)
    README.md                     the arms, the commands, what to record, the cloud case by hand
  results/                        one file per batch of runs: arm, model, tokens, sheet totals
```

## Running

```bash
# arm 1: MCP only
waveflow blind-test --prompt evaluation/skill_tests/schema/01_pulse_report.md \
    --folder ../waveflow_blind_tests/schema01_mcp_a \
    --message "Write the code specified in 01_pulse_report.md, in this folder, with Waveflow: its MCP server is available."

# arm 2: MCP + skill -- the same, plus --skills
waveflow blind-test --prompt evaluation/skill_tests/schema/01_pulse_report.md \
    --folder ../waveflow_blind_tests/schema01_skill_a --skills waveflow/skills/waveflow-schema \
    --message "Write the code specified in 01_pulse_report.md, in this folder, with Waveflow: its MCP server is available."

python evaluation/skill_tests/lint_schema_use.py ../waveflow_blind_tests/schema01_skill_a
```

Two or three runs per arm: runs vary, and one run in each arm proves little.  The message names no
skill: whether the agent finds it is part of the test.

`--skills` copies the skill into the trial folder's `.claude/skills/` and allows the `Skill` tool.
`summary.md` then opens with a **Skills** section: what was installed, what Claude Code offered at
start-up (anything there that the harness did not install is listed, so both arms can be checked for
the same set), which skill was loaded at which tool call, before or after the first write, and which
of its files were read.

A test that starts from given code (`hls_body/`: the agent writes a body against schemas and a golden
it must not change) passes each given file with `--files`, which copies it beside the spec:
`--files evaluation/skill_tests/hls_body/given_01/gain_msgs.py` (and `gen_headers.py`,
`gain_model.py`).  Its reviewer also runs `hls_body/check_body.py <trial folder>`: a plain-C++
compile of the body against the golden, no Vitis -- a correctness item on the sheet, not a pattern score.

For the trigger prompts, run each with the stub spec, which only says the request is in the first
message: `--prompt evaluation/skill_tests/trigger_stub.md --message "<prompt>" --no-approve
--timeout 0.1` (six minutes is enough to see whether the skill loads).

The repo itself commits copies of the shipped skills in `.claude/skills/` and `.agents/skills/` (so an
agent working on Waveflow is offered them). The trial folders are outside the clone, so neither arm
sees those copies: the MCP-only arm stays without the skill.

## Rules (from `evaluation/planning_tests/`)

- A spec is outside `docs/` and `examples/`, so the knowledge index never serves it.
- A spec that becomes a worked example retires: an agent could copy it.
- Same model, same message, in both arms.  The arm is set only by the skill folder.
- When a run goes wrong, fix the **skill** and run again; do not soften the spec.
