# Plan: agent skills -- the remaining skill groups

Hand-off plan. First written 2026-11-19 (PRs #316-#319); **rewritten 2026-11-20** (updated 2026-11-21: migration, link sweep and the three new skills
done in PRs #326-#327) after the guide's
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

## The skills (state on 2026-11-21)

| skill | covers | state |
|---|---|---|
| `waveflow-start` | looking things up (MCP or shell), installing, the concept pages, which skill covers which piece | done (PR #322; concept pages added in #326); onboarding verified in one smoke run |
| `waveflow-schema` | DataSchema, fields, fixed point, tagged unions, HLS serialize, array utils, header generation | done; blind-tested (51-54 of 54 both arms) |
| `waveflow-kernel` | a kernel in Python: the stream for each thing it talks to, command-response, `run_iter`, the harness test that asserts counts | done (renamed from `waveflow-module`, #326); blind-tested as the module skill (49-52 of 52) |
| `waveflow-kernel-hls` | a leaf kernel's HLS body: loop shapes, the lane loop, state and reset, deadlocks | done (renamed from `waveflow-hls-body`, #326); blind-tested (43-44 of 44) |
| `waveflow-device` | adaptors and devices: memory readers and writers, slave adaptors, credit, BRAM, `MemoryMod`, the HLS top and sub-top | done; blind-tested 2026-11-21 (MCP 49 / 44, skill 50 / 50 of 50 after two fixes) |
| `waveflow-pipeline` | composite kernels: stages, internal depths, `StreamBroadcast`, a token per job, the last stage answers | done; blind-tested 2026-11-21 (MCP 44 / 44, skill 46 / 46 of 46 after one fix) |
| `waveflow-system` | `HwSystem`, the harness and the cut, the build DAG, tests that count | done; blind-tested 2026-11-21 (all four 48 of 48, both hidden faults caught by every run; redundant for this task) |
| `waveflow-architecture` | the step before all the others: from an algorithm in a paper to requirements, a HW / SW split, blocks and interfaces, a static latency / II / utilization budget, and a spec per block | **planned** -- see [The architecture skill](#the-architecture-skill-planned-2026-11-21) |

**Done since the last rewrite:** the migration (renames, the installer's `RETIRED` map removing
unchanged retired copies, tests and docs renamed, result files annotated) and the link sweep for the
concept pages (`AGENTS.md`, `CLAUDE.md`, the guide index, the frames process text, `docs/overview`) --
PR #326. A trial building a vector-multiply device from the skills alone, with fixes to five skills --
PR #327 (`evaluation/skill_tests/results/vmul_2026-11-21/REPORT.md`). The front matter of
`waveflow-kernel` and `waveflow-device` did not parse as YAML (an unquoted `: ` in the description, so an
agent's loader dropped the description -- the text that decides when a skill loads); fixed, and
`test_every_skills_front_matter_is_valid_yaml` now guards every skill -- PR on branch
`fix/skill-front-matter`. **The module skill's four blind runs used that broken description**: it
loaded anyway (on its name, at the second call), but its trigger was never tested.

**Blind runs for device, pipeline and system (2026-11-21):** four each plus reruns of the skill arms
after fixes; results in `evaluation/skill_tests/results/{device01,pipeline01,system01}_2026-11-21.md`
and `docs/evaluation/agent_skills/{device,pipeline,system}.md`. The device skill's pointer into
`evaluation/` leaked the answers' file names (two runs discarded;
`test_no_skill_points_into_the_evaluation_folder` now guards it); the device self-check gained the protocol and "what RTL still
needs"; the pipeline skill gained the token-id check and the cost of each broadcast option. The MCP
arms of device and pipeline stop at the frame's Stage 1 review and need one `--resume --message
"Approved. Continue."`; the skill arms never stop (an open question for the frames process).

## Next

1. **Harder second tests** where no worked example carries the MCP arm (device: two readers or a
   credit stream; pipeline: three stages sized together; system: an overlapping host window).
2. **Trigger prompts** for every skill (`evaluation/skill_tests/*/trigger_prompts.md`), never run.
   Now that the descriptions parse, this is the first real test of whether each skill loads when it
   should. Run on Sonnet.
3. **The vmul trial's unfixed findings** (REPORT.md, "Where the skills were unclear"): 3, enum headers
   need their own `DataSchemaStep` and nothing reports a missing one (make `DataSchemaStep` emit nested
   enum headers, or refuse); 4, `memcpy`, `interleaver` and `interface/axi_mm/master.md` hand-serialize
   (`write(np.asarray(m.serialize(...)))` where `write(m)` does it) -- fix the references, an agent copies
   them; 6, `MemStreamStep` copies other examples' bodies into a user's `include/`; 8, the smaller
   points (the generated top's `m_done` has no TLAST with `done_framed=True`; no frame for "one kernel
   plus memory"; `vecmult` misleads by its name).
4. **The architecture skill** (below): write it, its test and reference, and run it blind.

## The architecture skill (planned, 2026-11-21)

**The gap.** Students take an algorithm from a communications-theory paper and ask an agent to "make it
hardware". Unprompted, a frontier model goes straight to code: one kernel that mirrors the paper's
equations, float or arbitrary fixed-point widths, and no question about the rate, the deadline or the
part. It can do each piece of architectural reasoning when asked; it does not start there. Nothing in
Waveflow starts there either: every skill above begins from a decided architecture (`waveflow-kernel`
"from the interfaces a user describes"), and the frames' Stage 1 assumes the frame -- the architecture --
is already chosen. This skill comes first, and its output is what Stage 1 takes.

It is not a licence to vibe-code: it makes the agent show the rate and resource arithmetic that a design
review asks for, and stop for the student to check it before any code exists.

**The steps** (the skill's body; each names the guide page it leans on, never restates it):

1. **Requirements.** Derive them from the application: sample rate, block size, throughput, latency
   budget, antennas / users / layers, target part and clock (for NR, from the numerology). State each as
   a number with its assumption, and ask the user to confirm before going on.
2. **Operation count.** Ops per sample or per block for each step of the algorithm, the memory each
   holds, the precision each needs (where fixed point is safe, where it is not). This is where "the matrix
   inverse is 90% of the work" shows.
3. **HW / SW split.** Rate-critical inner loops to kernels; control, configuration and rare paths to
   software threads (`SwFunction` on `Processor`, priced by the A53 platform's models -- `docs/guide/cpu/`).
4. **Blocks and interfaces.** Kernels, adaptors and devices in the concept pages' words
   (`systems/kernels.md`, `devices.md`); command-response by default (`command_response.md`); a schema
   sketch per stream with its width set by the rate (`waveflow-schema` does the detail later).
5. **Static timing, no simulation.** Per block: II and latency, then the bottleneck, then the pipelining or
   replication that meets the rate and the utilization that results. Per-block resources likewise.
6. **A spec per block** -- its ports, messages, rate, II and latency budget, resources -- handed to the
   matching frame's Stage 1. Then **stop for review**.

The output is one architecture document with tables: requirements, op counts, the per-block budget.

**The rule that makes it Waveflow's: every number names its source** -- a calibrated model
(`waveflow/linalg/cost.py` for the systolic engine, `waveflow/vecunit/calib.py`, the CPU platform's
`SwFunction` models, the platform bus calibration, the resource model `docs/guide/resource_model/`), a
formula from `docs/guide/vectorization/hls/loop_optimization.md` (the lane loop: words per iteration,
II=1), or a stated assumption. A block no model covers is **"unknown -- needs a csynth probe"**, never a
guess. Confident made-up numbers are the failure this rule exists for. Step 5 is a point of
`systems/dse.md` ("models price a point without building it"); a sweep is the later step.

**The test** (the recipe below): two or three algorithms from papers -- e.g. MMSE MIMO detection, OFDM
least-squares channel estimation, an LDPC decoder (no calibrated model: tests the "unknown" rule).
Each spec gives the paper's algorithm and the application, not the numbers. Review-sheet items: states
requirements with assumptions and asks; counts the ops and finds the real bottleneck; splits HW / SW with
a reason; names a source for every number; per-block II within ~2x of csynth (Vitis 2025.1) on the
reference; marks the uncovered block unknown; stops before code. Run without the skill as well -- the
point is whether the MCP arm does this unprompted (the expectation: it does not).

**Open questions.** Whether this is a skill or a frame (`waveflow frames`: an "architecture" frame whose
process ends where the others begin); whether the requirements step needs a small table of standard
numerologies to keep agents from inventing them.

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
- **A skill's own example can contradict another skill.** The device skill's first shape sent a read
  command and blocked on the reply in one firing, which `waveflow-kernel-hls` forbids; pysim passed it
  (vmul trial). Read a new skill's examples against the other skills' rules.
- **Front matter must be valid YAML.** An unquoted `: ` in a description drops it in the agent's
  loader while Waveflow's own line parser reads it fine; `tests/mcp/test_skills.py` now checks.
- **Small models may not load the skill at all** (Haiku 4.5 never did).
- **Never point a skill into `evaluation/`**: the answers live there, and an agent that follows the
  pointer lists the folder (device v1, two runs discarded). Point to the guide and curated examples.
- **A point every run misses is the skill's to give** (pipeline B4 / D2: all four runs, both arms).
- **Check the transcript for reads of the checkout** (`evaluation/`, `answers/`) before scoring a run.
- **`--setting-sources project` does not hide built-in or account-plugin skills**; both arms see the
  same set, so the comparison holds.
- No wall-clock times on this laptop (it is in use): report turns and tokens.

## Open items carried over

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
