# Review sheet: pipeline/01_peak_picker

For the reviewer, not the agent (no spec links this file). Fill one sheet per run. First run:

- `python -m pytest <trial folder> -q`: the trial's own tests (G1).
- `python evaluation/skill_tests/lint_schema_use.py <trial folder>`: hand-packing lines (G2).

**Read the code for B1 and B2: pysim does not show them.** pysim admits a burst longer than a link's
depth once the link has room for part of it, so a stage that waits on its sibling with a whole job's
samples still queued in its copy passes every pysim test, and hangs at RTL
(`ref_01/test_peaks.py`, the strict-xfail test). Score from the read order and the depths.

Run: ____________  arm: [ ] MCP only  [ ] MCP + skill   model: ________  tokens in/out: ________
Skill loaded (from summary.md): [ ] yes, at tool call # ___   [ ] no

Score each item **2** (as the guide does it), **1** (partly), **0** (not done / done otherwise).
The reference is `ref_01/` (`peaks.py`'s docstring and `DESIGN.md` state every choice below).

## A. The composite (`systems/kernels.md`, `flows/concurrent_python.md`)

| # | item | the pattern | score |
|---|---|---|---|
| A1 | a **composite kernel**: a `FreeRunMod` with children (`add_comp`), internal links (`add_if`), a named `boundary`, and no body of its own | a single leaf doing both, or a composite with a `run_iter`, is 0 | |
| A2 | both stages get the samples from the framework's **`StreamBroadcast`** | a hand-written splitter is 1; one stage forwarding the samples to the other (no concurrency on the same job, but valid) is 1 | |
| A3 | each stage a leaf `FreeRunMod`, one firing per job, streams only | | |
| A4 | every internal link a **framed** `StreamIF` with a **stated depth** | the default depth everywhere, unstated, is 0 | |

## B. The traps (`streams/broadcast.md`, `systems/streams.md` *Between kernels*)

| # | item | the pattern | score |
|---|---|---|---|
| B1 | the stage that needs `T` **drains its own copy of the samples before it waits for `T`** (into a local buffer of at most 1024), **or** its copy's link is deep enough for a whole job (512 words + the command), the depth tied to that reason | waiting for `T` first with a shallow copy (it hangs at RTL for the 1024-sample job) is 0, even if pysim passes | |
| B2 | the reason for B1 is **stated** (the broadcast's outputs move in lockstep) | | |
| B3 | **a token per job**: the stage that computes `T` sends one message per job, including the bad request and the short burst, so the last stage is never left waiting | no message on an error (the last stage hangs) is 0 | |
| B4 | the token carries the job id (or the command travels with it) and the last stage checks or uses it | | |
| B5 | **the last stage answers**: the count, the indices and the status come from the stage that picks the indices | | |

## C. The protocol (`systems/command_response.md`)

| # | item | the pattern | score |
|---|---|---|---|
| C1 | command (`tx_id`, `n`, `mult`) on the same stream as its samples, two bursts | | |
| C2 | a response heading the indices (`tx_id`, the count); the zero-count case **stated** (no index burst, or a tagged END) | a zero-length burst sent, or the count missing, is 0 | |
| C3 | one status per job on its own stream | | |
| C4 | errors fatal: `n` out of range and a short burst each reported, then nothing more answered | | |

## D. Choices stated (`DESIGN.md`)

| # | item | score |
|---|---|---|
| D1 | every link and its depth, with why | |
| D2 | B1's choice and its alternative (drain into a buffer, or a deep link: the cost of each) | |
| D3 | the zero-index case and the error tokens | |

## E. Tests (`systems/verification.md`, `flows/units_harness.md`)

| # | item | the pattern | score |
|---|---|---|---|
| E1 | each stage alone in its **harness** (`harness(Stage, workload=...)`), then the composite | | |
| E2 | the **number** of bursts on every output asserted | | |
| E3 | the 1024-sample job and a zero-index job in the scenario | | |
| E4 | each error placed mid-scenario, jobs after it, nothing after it answered | | |

## G. Over all

| # | item | score |
|---|---|---|
| G1 | the trial's tests pass | |
| G2 | every message by its schema; no hand packing (the lint) | |
| G3 | process: the guide pages (or the skill) read before the first module was written; API names not guessed (count corrections: ___ ) | |

**Total:** ____ / 46.

Free notes -- what it got wrong and which line of the skill (or which missing line) would have
prevented it:
