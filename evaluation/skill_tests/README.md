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

For the trigger prompts, run each with the stub spec, which only says the request is in the first
message: `--prompt evaluation/skill_tests/trigger_stub.md --message "<prompt>" --no-approve
--timeout 0.1` (six minutes is enough to see whether the skill loads).

## Rules (from `evaluation/planning_tests/`)

- A spec is outside `docs/` and `examples/`, so the knowledge index never serves it.
- A spec that becomes a worked example retires: an agent could copy it.
- Same model, same message, in both arms.  The arm is set only by the skill folder.
- When a run goes wrong, fix the **skill** and run again; do not soften the spec.
