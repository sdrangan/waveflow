# dse_cg: does pysim speed up design-space exploration?

**Status: skeleton.** The interfaces are settled; the bodies raise `NotImplementedError` naming the
phase of [`plans/dse_cg.md`](../../plans/dse_cg.md) that writes them. The plan says *why*; this
directory says *what*.

## The experiment in one paragraph

An agent explores the design space of the LTBF receiver on a shared-memory accelerator
([`examples/ltbf_cg_mem_dse`](../ltbf_cg_mem_dse/hw/system.py)) to meet an objective (for example,
the fewest LUTs at a given accuracy and long-term update time) within a budget of tool time. It runs twice, in two **arms**
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
2. Generate the context: `brief.write_context(LtbfCgMemAdaptor(), Path("examples/dse_cg/DSE.md"))`.
3. Start the service on the live backend (`python -m examples.dse_cg.service --live`) and set
   `DSE_SERVICE` to its address.
4. Ask:

   ```
   /dse examples/dse_cg/DSE.md Minimize LUTs subject to median loss <= 0.1 dB at SINR targets [-6, 3] dB and T_LT <= 10 ms.
   ```

(Steps 1–3 are not written yet: plan Phase 3.)

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
| [`ltbf_cg_mem.py`](ltbf_cg_mem.py) | the harness | `LtbfCgMemAdaptor`, the LTBF system of `examples/ltbf_cg_mem_dse` (V1) |
| [`brief.py`](brief.py) | the harness | renders the skill and `DSE.md` for a user or a sandbox; `select_arm`, `render` and `prompt` are written |
| [`service.py`](service.py) | the harness | charges, caches, schedules and answers every `dselib` call; owns the ledger; replay and live backends |
| [`prereg.py`](prereg.py) | the harness | what is fixed before any scored run: objectives (the prompts), budget, ε, workers, runs, model |
| [`policies.py`](policies.py) | the harness | the scripted explorers, one per arm: the agent-free comparison |
| [`run_agent.py`](run_agent.py) | the harness | one agent run, end to end |
| [`score.py`](score.py) | the analysis | ledgers → anytime curves, time to target, report checks |

The skill, `dselib`, `DseAdaptor` and the generator know nothing about CG. They stay here until a
second design uses them, then move into the `waveflow` package; `ltbf_cg_mem.py` stays.

## Order of work

Follows [`plans/dse_cg.md`](../../plans/dse_cg.md). The stubs name the phase or step that writes
them.

1. **Phase 1: the LTBF system at any design point.** It already runs as an `HwSystem` through the
   system flow (at 2024.1). First and blocking: `build_system` at a design point, and its RTL gate at
   2025.1. Then synthesis and csynth through the system flow part by part, the three workloads, and
   pysim from the system.
2. **Phase 2: the adaptor, verified.** `adaptor.py`'s defaults and `ltbf_cg_mem.py`'s methods; fast
   tests with no toolchain; then five design points through every method, checked against each
   other.
3. **Phase 3: the service and `dselib` on the live backend**, and `DSE.md` generated. The user flow
   above works from here: **the hand-off point.**

After that: the ground-truth grid G1 and the validation of pysim's predictions (step 4), replay and
the sandbox (5), the scripted policies (6), the agents (7), and V2, the same system with its CG
chains sequenced on chip by descriptor passing, which adds a second adaptor and changes nothing else
here (8).
