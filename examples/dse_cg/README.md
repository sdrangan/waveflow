# dse_cg: does pysim speed up design-space exploration?

**Status: skeleton.** The interfaces are settled; the bodies raise `NotImplementedError` naming the
phase of [`plans/dse_cg.md`](../../plans/dse_cg.md) that writes them. The plan says *why*; this
directory says *what*.

## The experiment in one paragraph

An agent explores the CG detector's design space to meet an objective (for example, the fewest LUTs
at a given accuracy and cycle budget) within a budget of tool time. It runs twice, in two **arms**
that are identical except for one function. The **traditional** arm has what a systems team and a
hardware team have: an untimed bit-exact simulation (`golden`), synthesis (`synth`) and RTL
simulation (`rtlsim`). The **Waveflow** arm also has `pysim`: bit-exact, with calibrated cycles,
per-block activity and resources. Both must submit a design they have synthesized and RTL-simulated.
The score is how much tool time each arm needs to reach a design close to the true optimum.

## Using it yourself

The agent works the way a user of Waveflow would. The instructions come in three layers:

| layer | what | file |
|---|---|---|
| **skill** | how to explore a design space with the `dselib` library: the evaluations, the rules, the report. Nothing about CG. | [`skill/dse/SKILL.md`](skill/dse/SKILL.md), [`skill/dse/dselib/`](skill/dse/dselib/__init__.py) |
| **context** | one design: the system, its modules, parameters, workloads, metrics, and what each evaluation costs | `DSE.md`, generated from [`DSE.md.tmpl`](DSE.md.tmpl) and the adaptor |
| **prompt** | one task: the objective | typed by you |

In your own project, with your own `CLAUDE.md` and session:

1. Install the skill in the project: `brief.install_skill(Path(".claude/skills"))`.
2. Generate the context: `brief.write_context(CgDetectorAdaptor(), Path("examples/dse_cg/DSE.md"))`.
3. Start the service on the live backend (`python -m examples.dse_cg.service --live`) and set
   `DSE_SERVICE` to its address.
4. Ask:

   ```
   /dse examples/dse_cg/DSE.md Minimize LUTs subject to MSE <= -20 dB at SNR 10 dB and <= 4,000 cycles per solve.
   ```

(Steps 1–3 are not written yet: plan phases P1b and P2.)

## A run is that flow in a clean sandbox

```
 run sandbox (generated per run by brief.py)          outside the sandbox
 ┌──────────────────────────────────────────┐        ┌──────────────────────────────────────┐
 │ .claude/skills/dse/  the skill + dselib ─┼─calls─►│ service.py   ledger, cache, budget,  │
 │ DSE.md       the design's context        │        │              k worker slots, arm check│
 │ docs/, src/  the system's pages and code │        │   └─ ReplayBackend: ground truth (G1) │
 │ (the agent's own scripts and notes)      │        │ ledger file ──► score.py              │
 └──────────────────────────────────────────┘        └──────────────────────────────────────┘
   prompt: /dse DSE.md <objective from prereg.py>
```

The same skill, the same context and the same prompt, in a new session with no context of its own:
no `CLAUDE.md`, no user settings or memory, not this repository, not the calibration records, not
the ground truth. The two arms differ only in the `arm:waveflow` blocks of `SKILL.md`, `dselib` and
`DSE.md`. The one difference from the user flow is the backend: a run replays recorded results and
costs, so it finishes in minutes and every run sees the same numbers.

## Files

| file | for | what |
|---|---|---|
| [`skill/dse/SKILL.md`](skill/dse/SKILL.md) | the agent | the skill: the method, with `arm:waveflow` blocks |
| [`skill/dse/dselib/__init__.py`](skill/dse/dselib/__init__.py) | the agent | the library; its docstrings are the API reference, with an example result for each function |
| [`DSE.md.tmpl`](DSE.md.tmpl) | the agent | one design's context, with `{{blocks}}` the generator fills |
| [`adaptor.py`](adaptor.py) | the harness | `DseAdaptor`, the base class a design subclasses; `defaults`, `fill` and `legal` are written |
| [`cg_detector.py`](cg_detector.py) | the harness | `CgDetectorAdaptor`, the CG detector (V1) |
| [`brief.py`](brief.py) | the harness | renders the skill and `DSE.md` for a user or a sandbox; `select_arm`, `render` and `prompt` are written |
| [`service.py`](service.py) | the harness | charges, caches, schedules and answers every `dselib` call; owns the ledger; replay and live backends |
| [`prereg.py`](prereg.py) | the harness | what is fixed before any scored run: objectives (the prompts), budget, ε, workers, runs, model |
| [`policies.py`](policies.py) | the harness | the scripted explorers, one per arm: the agent-free comparison |
| [`run_agent.py`](run_agent.py) | the harness | one agent run, end to end |
| [`score.py`](score.py) | the analysis | ledgers → anytime curves, time to target, report checks |

The skill, `dselib`, `DseAdaptor` and the generator know nothing about CG. They stay here until a
second design uses them, then move into the `waveflow` package; `cg_detector.py` stays.

## Order of work

Follows the plan's V1 phases:

1. **P0, P1** (lab machine): the units recalibrated at 2025.1 (`plans/calib_2025_1.md`), then the
   ground-truth grid G1 measured, which is `LiveBackend` driven over a pre-registered grid, and the
   system-level predictions scored on it. This directory waits on G1 for real data, but not for its
   code.
2. **P1b**: `service.py` and the `dselib` transport, with `CgDetectorAdaptor.golden`, `.pysim`,
   `.build_system` and the base class's `identities` and `synth`, against a few G1 points.
3. **P2**: `brief.py`, the rest of the adaptor, and a test that the two arms' sandboxes differ only
   in the `arm:waveflow` blocks. The user flow works from here.
4. **P3**: `policies.py` and `score.py`: the first result, with no agent.
5. **P4**: `run_agent.py`, pilot runs, pre-registration (`prereg.py`), the scored runs.

V2 (the descriptor-ring system) adds a second adaptor, and nothing else here changes.
