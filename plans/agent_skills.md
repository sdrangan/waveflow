# Plan: agent skills -- the remaining skill groups

Hand-off plan. First written 2026-11-19 (PRs #316-#319); **rewritten 2026-11-20** after the guide's
systems section was restructured around kernels, adaptors and devices (branch `docs/glossary`). The
skill list below follows the new guide. Start here; everything below is in the repo.

## Goal

A student points **any** coding agent (Claude Code, Codex, Gemini CLI, Copilot ...) at Waveflow and it
follows Waveflow's patterns. Skills are one means. **A skill that turns out to be redundant is fine** --
if the MCP server and the guide already get an agent there, say so in the results and move on. The
class lets students choose their model; test on **Sonnet 5.5 and Opus 5.5**. (Haiku 4.5 was set aside as
too weak; Haiku 5.5 is untested.)

## The guide the skills point to (new, 2026-11-20)

The systems section now leads with four concept pages, then the flow. A skill points to these pages and
never restates them.

| page | what it settles |
|---|---|
| `systems/index.md` -- *Waveflow Architecture* | what Waveflow is; three levels of simulation (golden, pysim, RTL; two descriptions, not three: `run_iter` calls the golden, the HLS body is the second); the layer around the DUT (`Simulation` vs `HwSystem`) |
| `systems/kernels.md` -- *Kernels* | **a kernel is a `FreeRunMod` whose interfaces are all streams**, yours or the library's; leaf (`run_iter`, one task) or composite (child kernels only); the stream for each thing a kernel talks to (one table); every stream typed; read order; BRAM the one exception |
| `systems/devices.md` -- *Devices and the DUT* | **an adaptor** has streams on one side and not a stream on the other (`MemRStream`, `MemWStream`, a bus writer, a slave adaptor's front and views, the RFDC); **a device** is kernels + their adaptors (with none, just its kernels); its **HLS top** (kernels + HLS adaptors) and RTL parts in one **sub-top**, one IP; the DUT = devices, memories, interconnect; what you write vs what Waveflow provides |
| `systems/command_response.md` | the default protocol; memory reached the same way (`MemRCmd`, `MemWCmd`, the echo on `m_done` = done) |
| `systems/flow.md` -- *Flow Overview* | the steps: `dut.md` (designing the DUT: golden, kernels, what each talks to, messages, one firing), `streams.md` (stream detail), `hwmodule.md` (a leaf kernel in Python and HLS), pysim, rtl, verification, calibration, dse |
| `systems/glossary.md` | every term with its class and page |

Words that matter: **kernel** (streams only) vs **adaptor** vs **device**; a composite with a
`MemRStream` child is a device, not a kernel. The code's `MmSlaveDevice` covers only the bus case.

## The skills

| skill | covers | status |
|---|---|---|
| `waveflow-start` | looking things up (MCP or shell), installing, the concept pages, which skill covers which piece | done (PR #322); concept pages and skill table added (PR #326) |
| `waveflow-schema` | DataSchema, fields, fixed point, tagged unions, HLS serialize, array utils, header generation | done (PRs #316-#317); 51-54 of 54 both arms |
| `waveflow-kernel` | a kernel in Python: its streams and the stream for each interface (point at Kernels' table), command-response, `run_iter`, timed phases, the harness test that asserts counts | renamed from `waveflow-module` (PR #326; 49-52 of 52) |
| `waveflow-kernel-hls` | a leaf kernel's HLS body: loop shapes, the lane loop, pragmas, state and reset, deadlocks | renamed from `waveflow-hls-body` (PR #326; 43-44 of 44) |
| `waveflow-device` | adaptors and devices: memory readers and writers, slave adaptors (queues, register bank, interrupts), credit streams, BRAM, `MemoryMod`; building them in pysim (`build_mm_device`) and RTL (`render_adaptor_slot`, the "not built yet" list); the HLS top and sub-top | written, test and reference ready, not run blind (PR #326) |
| `waveflow-pipeline` | composite kernels: when to split, internal stream depths and widths, `StreamBroadcast` (drain your own copy), a token per job, the command travelling with the data, credit across a bus | written, test and reference ready, not run blind (PR #326) |
| `waveflow-system` | `HwSystem(sim, dut)`, the harness and the cut, the build DAG (`add_system_steps`, `run_dag_cli`), csynth / XSI / `compare`, asserting counts so a deadlock cannot pass | written, test and reference ready, not run blind (PR #326) |

## Step 1: migrate the old skills (do this first)

The renames touch the files below. Do them in one PR, with the link sweep (step 2).

1. **Rename the source folders** with `git mv`: `waveflow/skills/waveflow-module` ->
   `waveflow-kernel`, `waveflow/skills/waveflow-hls-body` -> `waveflow-kernel-hls`. Change each
   `SKILL.md`'s `name:` to the folder name (the format requires it), and its description.
2. **Trim `waveflow-kernel`** to the new pages: its mapping table duplicates Kernels' stream table --
   replace it with a pointer to `systems/kernels.md` ("The stream for each thing a kernel talks to")
   and keep only what a page does not say (choose-and-state for control and data; the B5 window line;
   the self-check). Its row "the module reads or writes a buffer in memory" now belongs to
   `waveflow-device`: point there. Use the words kernel / adaptor / device as `glossary.md` defines them.
3. **The installer leaves stale skills behind.** `waveflow skills install --force`
   (`waveflow/mcp/skills.py::install_skills`) replaces the skills it ships but never removes one it no
   longer ships, so a project that installed `waveflow-module` would load it beside `waveflow-kernel`.
   Add removal of known retired names (a `RETIRED = {"waveflow-module": "waveflow-kernel",
   "waveflow-hls-body": "waveflow-kernel-hls"}` map; remove only folders whose `SKILL.md` matches a
   shipped version, so a project's own edits are never deleted silently -- or report them), with a test.
4. **Refresh the repo's committed copies**: `waveflow skills install --dir . --force`, and `git rm` the
   old `.claude/skills/waveflow-{module,hls-body}` and `.agents/skills/...` folders
   (`test_the_repos_committed_skills_match_the_shipped_ones` enforces the sync). Never commit the rest of
   `.claude/`.
5. **Rename the tests**: `evaluation/skill_tests/module` -> `kernel`, `hls_body` -> `kernel_hls`
   (`git mv`), and every path inside (`check_blocks.py`, `check_body.py`, the review sheets,
   `trigger_prompts.md`, `evaluation/skill_tests/README.md`). **Keep the results files' names**
   (`results/module01_2026-11-20.md`, `hls_body01_2026-11-19.md`): they record what was run; add one line
   at the top of each, "the skill was then named `waveflow-module`, now `waveflow-kernel`".
6. **Docs**: `docs/evaluation/agent_skills/{module,hls_body}.md` -> `kernel.md`, `kernel_hls.md` with the
   same note; their rows in `agent_skills/index.md`; the table in `docs/guide/ai_tooling/skills.md`.
7. **Code and tests that name skills**: `tests/mcp/test_skills.py`, `tests/mcp/test_init_project.py`
   (asserts `waveflow-hls-body` is installed), `waveflow/mcp/init_project.py` (names `waveflow-start`
   only -- check).
8. **`waveflow-start`**: add the concept pages (Architecture, Kernels, Devices, Command-response, then
   the Flow Overview; Glossary for terms) and the skill table above. Skills not yet built are named with
   the guide page to read instead (`devices.md`, `rtl.md`).

Find every mention with `git ls-files | xargs grep -al "waveflow-hls-body\|waveflow-module\|skill_tests/module\|skill_tests/hls_body"`
(the list on 2026-11-20 is 23 files, all named above).

## Step 2: the link sweep for the new guide pages

Every place that says the guide starts at the *Flow Overview* (`systems/index.md`) or calls `dut.md`
*The DUT and two levels of simulation*: `docs/guide/index.md` (its "how this guide is organized"),
`AGENTS.md`, `CLAUDE.md` ("Designing an accelerator"), `waveflow/mcp/frames/_common/process.md`,
`docs/overview/{index,flow}.md`, `docs/guide/flows/concurrent.md`, `docs/guide/systems/recipe.md`,
`tests/mcp/test_retrieval_eval.py` (a retrieval expectation may name a page title),
`plans/next_docs_examples.md`. `systems/index.md` still resolves -- it is now the architecture page --
so most links only need their words changed; the ones that mean "the step table" go to `flow.md`.
About 67 files mention it: check each, do not pattern-replace. Run `pytest tests/docs tests/mcp`.

**A trap**: the user's editor formats Markdown on save and strips `{#...}` heading attributes. Prefer
links to the anchor a heading generates; `tests/docs` catches a broken one.

## Step 3 onward: build the new skills, in this order

### `waveflow-device`
- **Guide:** `systems/devices.md`, `systems/command_response.md` (memory section),
  `interface/axi_mm/{master,slave,slave_views,regmap,credit_streams,credit_streams_hls}.md`,
  `interface/primitive/{bram,sob}.md`, `memory/{index,memorymod,memmgr,hwstate}.md`,
  `comp_codegen/rtl_module.md`. Examples: `memcpy`, `stream_kernel_mm`, `mm_fir`, `markov`, `interleaver`.
- **Test idea:** a host on the bus sends a command through a queue in; the device reads a buffer through
  `MemRStream`, computes, writes the result through `MemWStream`; the status goes to a register bank and
  an interrupt tells the host the command is done -- through the system flow as far as the generated
  sources, no Vitis. Traps: `m_axi` on an `hls::task` needs `offset=off` + `stable` (`stable` alone
  deadlocks); a credit window (`wait_room` before writing); a request and its reply on two streams (poll
  with `read_nb`); the register bank raises no interrupt (a Waveflow gap the agent must state, not work
  around); the RTL adaptor is assembled by hand (`slave.md` "What is not built yet").

### `waveflow-pipeline`
- **Guide:** `systems/kernels.md` (composite), `systems/streams.md` ("Between kernels"),
  `systems/command_response.md` ("Through a pipeline"), `streams/broadcast.md`,
  `flows/concurrent_python.md`, `flows/units_composing.md`. Examples: `block_eq`, `interleaver`,
  `memcpy`.
- **Test idea:** a two- or three-stage composite with a broadcast; traps: an un-paced free-running
  pipeline deadlocks (forward a token per job through every stage); a stage waiting on another must first
  drain its own broadcast copy; an internal depth too small for a burst.

### `waveflow-system`
- **Guide:** `systems/rtl.md`, `systems/pysim.md` (the cut), `systems/verification.md`,
  `systems/calibration.md` (at a glance), `build/xsi_system.md`, `build/bfm.md`, `build/sw_threads.md`,
  `flows/units_harness.md`, `flows/units_hostile.md`.
- **Test idea:** given two devices and a host, wire the `HwSystem` and its DAG as far as the generated
  sources, and write the tests; a given broken variant (a deadlock, a dropped word) must fail them --
  scored on whether the tests catch both (pysim only, no Vitis).

## The recipe for one skill

1. **Search before designing**: `waveflow kb search`, read the guide pages above. The skill points to
   pages; it never restates them (it would drift).
2. **Write `waveflow/skills/<name>/SKILL.md`**: front matter (`name` = folder name; a description
   saying what and when, in the user's words), the rules that always apply, a "page before step" table,
   one short pattern, a self-check. Copy the shape of `waveflow-kernel-hls`. Then refresh the repo's
   committed copies, `waveflow skills install --dir . --force`, and commit `.claude/skills/` and
   `.agents/skills/` with it. Never edit the copies; never commit the rest of `.claude/`.
3. **Write the test** in `evaluation/skill_tests/<name>/`: a spec (`01_<task>.md`), a review sheet
   (`answers/01_<task>.review.md`, items scored 0/1/2), a reference answer (`answers/ref_01/`), trigger
   prompts. Given files go through `--files`. If correctness can be checked cheaply without Vitis,
   add a checker like `kernel/check_blocks.py` (one sheet item, not the score).
4. **Check the reference**: it must run; if it has HLS, compile it (plain g++ via the checker) and,
   once, csynth it to confirm II / shape. Add it to `tests/mcp/test_skills.py`.
5. **Run four blind runs**: Opus and Sonnet x MCP / MCP + skill, the same message, `--no-approve`,
   folders `../waveflow_blind_tests/<test>_<arm>_<model>`. Command template:
   `docs/evaluation/agent_skills/kernel_hls.md` ("How a run is made"); the module run used
   `--message "Write the code specified in <spec>, in this folder, with Waveflow: its MCP server is available."`
6. **Score** against the sheet; record per item in `evaluation/skill_tests/results/<test>_<date>.md`;
   write `docs/evaluation/agent_skills/<name>.md`; add a row to `agent_skills/index.md` and to
   `docs/guide/ai_tooling/skills.md`.
7. **Fix gaps the test finds in Waveflow** when small; record them otherwise.

## Lessons so far

- **A wrong line in a skill is followed.** The first `hls-body` skill said cross-command state needs no
  reset pragma; both models followed it. Check every rule against the guide **and** the toolchain.
- **Never let a skill's examples use the test's own values**: it leaks the answer (two runs discarded).
- **Easy tasks hit the ceiling.** All three tested skills scored at or near full marks in both arms: the
  MCP server's frames process and worked examples carry the agent. The skill's measured effect is cost
  (turns -13..44%, tokens -9..53%) and the few items no example shows (stating an open choice). Put a trap
  in each task that no worked example shows.
- **The runs can beat the reference** (module B5: a setting checked before a blocking wait applies a
  frame late). Score against what is right, and fix the reference and the skill.
- **Small models may not load the skill at all** (Haiku 4.5 never did).
- **`--setting-sources project` does not hide built-in or account-plugin skills**; both arms see the
  same set, so the comparison holds.
- No wall-clock times on this laptop (it is in use): report turns and tokens.

## Open items carried over

- **Trigger prompts** for every skill (`evaluation/skill_tests/*/trigger_prompts.md`), not run yet.
  Run on Sonnet.
- **Haiku 5.5**: one run of each test, to see whether it belongs with the models students use.
- **A second reviewer** for the scores (all scored by one Claude session so far).
- **Waveflow gaps found, not fixed:**
  - pysim has no documented first-word check: `begin_read(pending_ok=True)` exists but no page names
    it; document it as the twin of `!empty()` or add `empty()` (module test, B5).
  - The register bank raises no interrupt, so command-response's "status register with an interrupt on
    an error" cannot be built (`slave.md`).
  - `FixedField` formats that differ only in rounding / overflow mode get the same class name, so their
    `ArrayUtilsStep`s collide. A fix renames existing designs' array-utils headers: plan it.
  - `DataArray` arithmetic takes no scalar operand.
  - The code's device class `MmSlaveDevice` covers only the bus case; the guide's device is broader.
- **RTL staleness:** PR #319 changed comments in `waveflow/build/rf_tx_player_task.h` and
  `rf_circ_play_task.h`; designs that copy them (rf_repeat_play, rf_circ_play) need a csynth before
  their `-m xsi` gates.

## Working rules (from `CLAUDE.md`, `CLAUDE.local.md` and the project memory)

- venv: `../pysilicon-venv/Scripts/python.exe -m pytest ...` (a bare `pytest` may run nothing).
- One PR, many commits; branch first; `git push` and `gh pr merge` each as its own command; fetch
  `origin` before pushing (other sessions work in parallel).
- Commit messages from a file (`git commit -F <file>`), not a PowerShell here-string.
- Don't commit others' untracked work (`examples/_archive/`, `examples/test/`, `plans/planning_test_cases/`)
  nor `docs/evaluation/sim_speed/systolic.md`'s local modification.
- Long csynth paths fail silently on Windows: build under `C:\wfsc\...`.
- Name the tool version (Vitis 2025.1) beside any measured number.
