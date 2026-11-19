# Plan: agent skills -- the remaining skill groups

Hand-off plan, written 2026-11-19 at the end of the session that built the first two skills (PRs
#316-#319). Start here; everything below is in the repo.

## Goal

A student points **any** coding agent (Claude Code, Codex, Gemini CLI, Copilot ...) at Waveflow and it
follows Waveflow's patterns. Skills are one means. **A skill that turns out to be redundant is fine** --
if the MCP server and the guide already get an agent there, say so in the results and move on. The
class lets students choose their model; test on **Sonnet 5.5 and Opus 5.5**. (Haiku 4.5 was set aside as
too weak; Haiku 5.5 is untested.)

## Where things are

| what | where |
|---|---|
| shipped skills | `waveflow/skills/<name>/SKILL.md` (package data; `waveflow skills list` / `install`) |
| the method, and one page per skill | `docs/evaluation/agent_skills/` (`index.md`, `schemas.md`, `hls_body.md`) |
| the guide page for users | `docs/guide/ai_tooling/skills.md` (a table row per skill) |
| tests: specs, review sheets, references | `evaluation/skill_tests/<skill>/` (README there) |
| results, one file per batch | `evaluation/skill_tests/results/` |
| the harness | `waveflow blind-test` (`waveflow/mcp/blind_test.py`; `--skills`, `--files`, `--model`) |
| the skill tests' own tests | `tests/mcp/test_skills.py` (every page a skill names resolves; references run) |

## Done

| skill | covers | result |
|---|---|---|
| `waveflow-schema` | DataSchema, fields, enums, fixed point, tagged unions, HLS serialize, array utils, header generation | Opus/Sonnet at 51-54 of 54 in both arms; the skill removed defensive casts and cut tokens 31-53% |
| `waveflow-hls-body` | kernel body shape, the lane loop, pragmas, state across commands and reset, deadlock rules | all runs at the ceiling (43-44 of 44) in both arms; tokens -30..41% |

## To build, in this order

Each is the same recipe (below). Scope is a starting point; adjust after reading the guide.

### 1. `waveflow-module` -- an HwModule and its pysim twin
- **Covers:** `HwModule` ports and directions, `Interface` wiring, the pysim behaviour (`run_proc`, slave
  methods), `HostActivated` vs `FreeRunMod`, the command / response / status protocol, the module in
  its unit harness.
- **Guide:** `systems/hwmodule.md`, `systems/pysim.md`, `systems/command_response.md`,
  `systems/streams.md`, `flows/modules.md`, `flows/units.md`, `flows/units_harness.md`,
  `interface/overview.md`, `interface/primitive/stream.md`.
- **Test idea:** a small module (command in, results + `Status` out, a fatal error that closes the
  output) written as an `HwModule` with its pysim behaviour and a unit-harness test that **asserts
  counts**. Traps: an error path that must stop taking input; a golden compared by count, not just
  "it finished".

### 2. `waveflow-memory` -- memory-mapped data
- **Covers:** AXI-MM masters and slaves, `MemRStream` / `MemWStream`, BRAM, `MemoryMod`, register maps,
  credit streams, `hwstate`.
- **Guide:** `memory/index.md`, `memory/memorymod.md`, `memory/memmgr.md`, `memory/hwstate.md`,
  `interface/axi_mm/*.md` (master, slave, regmap, credit_streams, credit_streams_hls),
  `interface/primitive/bram.md`, `interface/primitive/sob.md`.
- **Test idea:** a kernel that reads a buffer through `m_axi`, processes it, and writes a result
  buffer, with a register map. Traps: `m_axi` on an `hls::task` needs `offset=off` + `stable`
  (memory note: `stable` alone deadlocks); a credit window (`wait_room` before writing); a request and
  its reply on two streams (poll with `read_nb`). These are also the traps the HLS-body test lacked:
  this test can double as `hls_body` test 02.

### 3. `waveflow-system` -- composing modules
- **Covers:** `HwSystem(sim, dut)`, the system flow and its DAG, connecting modules, the system
  testbench, `run_dag_cli`, what the flow generates.
- **Guide:** `systems/index.md` (Flow Overview), `systems/recipe.md`, `systems/dut.md`,
  `systems/rtl.md`, `flows/concurrent*.md`, `flows/units_composing.md`.
- **Test idea:** two given modules wired into a system with a host, built through the flow as far as
  the generated sources (no Vitis). Trap: an un-paced free-running pipeline (forward a per-job token
  through every stage).

### 4. `waveflow-verify` -- proving it works
- **Covers:** pysim tests, csim / cosim, the XSI gates, asserting **counts** so a deadlock cannot look
  like success, timing calibration at a glance.
- **Guide:** `systems/verification.md`, `systems/calibration.md`, `flows/units_hostile.md`,
  `flows/units_recordings.md`, `sim/`, `timing/`.
- **Test idea:** given a module and a broken variant (a deadlock, a dropped word), write the tests;
  score whether they catch both. This one can run its tests (pysim only, no Vitis).

## The recipe for one skill

1. **Search before designing**: `waveflow kb search`, read the guide pages above. The skill points to
   pages; it never restates them (it would drift).
2. **Write `waveflow/skills/<name>/SKILL.md`**: front matter (`name` = folder name; a description
   saying what and when, in the user's words), the rules that always apply, a "page before step" table,
   one short pattern, a self-check. Copy the shape of `waveflow-hls-body`.
3. **Write the test** in `evaluation/skill_tests/<name>/`: a spec (`01_<task>.md`), a review sheet
   (`answers/01_<task>.review.md`, items scored 0/1/2), a reference answer (`answers/ref_01/`), trigger
   prompts. Given files go through `--files`. If correctness can be checked cheaply without Vitis,
   add a checker like `hls_body/check_body.py` (one sheet item, not the score).
4. **Check the reference**: it must run; if it has HLS, compile it (plain g++ via the checker) and,
   once, csynth it to confirm II / shape. Add it to `tests/mcp/test_skills.py`.
5. **Run four blind runs**: Opus and Sonnet x MCP / MCP + skill, the same message, `--no-approve`,
   folders `../waveflow_blind_tests/<test>_<arm>_<model>`. Command template:
   `docs/evaluation/agent_skills/hls_body.md` ("How a run is made").
6. **Score** against the sheet; record per item in `evaluation/skill_tests/results/<test>_<date>.md`;
   write `docs/evaluation/agent_skills/<name>.md` (shape of `hls_body.md`); add a row to
   `agent_skills/index.md` and to `docs/guide/ai_tooling/skills.md`.
7. **Fix gaps the test finds in Waveflow** when small; record them otherwise.

## Lessons from the first two

- **A wrong line in a skill is followed.** The first `hls-body` skill said cross-command state needs no
  reset pragma; both models followed it and got reset wrong, while the MCP-only runs, copying the
  guide, got it right. Check every rule against the guide **and** the toolchain before running. Don't
  trust a project note: verify it (the reset note had misread a csynth warning).
- **Never let a skill's examples use the test's own values** (formats, names): it leaks the answer.
  Two runs were discarded for that.
- **Easy tasks hit the ceiling.** When the MCP server hands out a near-identical worked example, both
  arms score full marks. Put a trap in each task that no worked example shows.
- **Small models may not load the skill at all** (Haiku 4.5 never did).
- **`--setting-sources project` does not hide built-in or account-plugin skills**; both arms see the
  same set, so the comparison holds.
- No wall-clock times on this laptop (it is in use): report turns and tokens.

## Open items carried over

- **Trigger prompts** for both skills: `evaluation/skill_tests/{schema,hls_body}/trigger_prompts.md`,
  not run yet. Run on Sonnet.
- **Haiku 5.5**: one run of each test, to see whether it belongs with the models students use.
- **A second reviewer** for the scores (all scored by one Claude session so far).
- **Waveflow gaps found, not fixed:**
  - `FixedField` formats that differ only in rounding / overflow mode get the same class name, so their
    `ArrayUtilsStep`s collide. A fix renames existing designs' array-utils headers: plan it.
  - `DataArray` arithmetic takes no scalar operand (a header field times an array needs a one-element
    array).
- **RTL staleness:** PR #319 changed comments in `waveflow/build/rf_tx_player_task.h` and
  `rf_circ_play_task.h`; designs that copy them (rf_repeat_play, rf_circ_play) need a csynth before
  their `-m xsi` gates.

## Working rules (from `CLAUDE.md`, `CLAUDE.local.md` and the project memory)

- venv: `../pysilicon-venv/Scripts/python.exe -m pytest ...` (a bare `pytest` may run nothing).
- One PR, many commits; branch first; `git push` and `gh pr merge` each as its own command; fetch
  `origin` before pushing (other sessions work in parallel).
- Commit messages from a file (`git commit -F <file>`), not a PowerShell here-string.
- Don't commit others' untracked work (`examples/_archive/`, `examples/test/`, `plans/planning_test_cases/`).
- Long csynth paths fail silently on Windows: build under `C:\wfsc\...`.
- Name the tool version (Vitis 2025.1) beside any measured number.
