# Verification ledger

## Upstream regression baseline

Base: `2e77a43` (`origin/main` at branch creation).

Command: `uv run pytest tests -m 'not vitis and not xsi'`

Initial integrated run: **3307 passed, 37 skipped, 284 deselected, 10 failed**.
Two documentation failures came from temporary Pi `node_modules` inside the checkout;
these are installation-workspace pollution, not acceptable exclusions. Move the npm
workspace out of the repository and rerun the documentation gates.

The other **eight failures were reproduced on an untouched detached worktree of
`2e77a43`**, using the same Python environment. Baseline command:

```sh
python -m pytest \
  tests/build/test_rtl_module.py::test_shipped_memory_is_the_witness_plus_the_published_latency \
  tests/calib/test_harmonize_equivalence.py \
  tests/hw/test_dataschema_poly.py::test_poly_notebook_flow_generates_headers_vectors_and_expected_outputs \
  tests/poly/test_timing_analysis.py -m 'not vitis and not xsi'
```

Baseline result: **8 failed, 24 passed**:

- RTL witness test expects CRLF in a published-latency insertion.
- `test_predictions_are_unchanged[vecmult]`: fitted FF estimate differs by one in this environment.
- Poly notebook-flow test points to absent `examples/stream_inband/poly.hpp`.
- Five Poly timing fixture assertions: transaction ID, sample count, first/last input, output values.

Do not silently suppress these tests or describe the complete repository suite as green.
They are outside the FIR surface change. Final focused/integration gates and benchmark
results must be appended after the implementation is frozen.

## Packaging and host integration

- `uv build --wheel` succeeded. An isolated, non-editable wheel installation outside
  the checkout ran Python quality evaluation, canonical resource prediction, replayed
  synthesis and feasibility reduction successfully for the default 32-tap/16-bit
  serial candidate; all three observation statuses were `ok`.
- The wheel includes the FIR corpus (35 `records.jsonl` files) and portable skill;
  it contains no `node_modules`, `__pycache__`, or `.pyc` entries.
- In an independent scratch copy: `npm ci`, `npm run check`, `npm test` and
  `npm pack --dry-run` passed. The Pi integration test obtains the real Python
  schemas and checks all six tool registrations, argv transport and cancellation.
  The packed Pi package contains `index.ts`, `package.json`, and the portable skill.
- `uv run ruff check examples/dse_fir` passed. Existing lint warnings in
  `waveflow/mcp/registry.py` are outside the added DSE implementation.
- A full 24-point oracle completed without failed observations; 19 candidates
  satisfy the default constraints. This is replay-grounded evaluation, not new
  synthesis. Model trials and final regression results are reported separately.

## First real model trials (2026-09-21)

Two real HTTP sessions locked to `deepseek-flash` / `deepseek`, each with an
eight-turn limit and only the six Waveflow MCP tools. No personal gateway changes.
Structured snapshots, exact calls and trace hashes: [evidence/first_trials.json](evidence/first_trials.json).

| Trial | Verdict | Calls | Python / prediction / replay queries | Input / output tokens | Spillover |
|---|---|---:|---|---|---:|
| Original verbose tool results | **Fail** | 40 | 24 / 8 / 8 | 514369 / 3767 | 4 |
| Compact views + bounded pages | **Pass** | 22 | 9 / 9 / 4 | 117935 / 3157 | 0 |

The first run evaluated the grid optimum but recommended a different candidate
without successful synthesis evidence, misread 32 DSP as exceeding the 48-DSP
limit, and hit the turn cap. Large replies spilled into files unavailable to the
restricted agent. The second run finished in **24.41 seconds**, recommended the
32-tap / 16-bit / serial candidate, and cited valid Python, prediction and replay
evaluation IDs. Its rejection is **55.32082157 dB**, SNDR **55.95403762 dB**, and
replayed resources are **32 DSP / 8674 LUT**: zero regret against the grid oracle.

Fix: transport projections omit repeated diagnostics while retaining scalar
metrics, evidence kinds, source references and evaluation IDs. Results page at
most five observations, with explicit counts/cursors and corresponding candidate
summaries; full diagnostics remain unchanged in SQLite/direct Python. Failed
Hermes runs retain terminal events, usage and final answers for honest scoring.

The scorer checks completion, locked runtime, tool boundary, context discovery,
candidate/parameter identity, feasibility, cited evidence and objective agreement.
It distinguishes *best candidate evaluated* from *candidate actually recommended*.
`tests/test_trial_score.py` rechecks both real recommendations offline.

Baselines under an eight-replay-query allowance: grid-prefix regret **28.24071 dB**;
1000 seeded uniform-without-replacement trials found the optimum **34.6%** of the
time (mean regret **9.71107 dB**). Cheap quality/prediction screening also finds
the optimum. These are groundwork, **not evidence of an agent advantage**.

Limits: one trial per view, not a controlled statistical ablation (the rerun also
corrects prompt turn-budget wording). The passing model's narrative says 12
candidates; persisted evidence shows **9**, so narrative coverage claims are not
trusted by the scorer. No other models were tested. No live synthesis or RTL runs.

## Final local checks

- Full non-toolchain suite before the compact-view fix: **3359 passed, 37 skipped,
  8 failed**. The failures are exactly the eight reproduced upstream above;
  documentation pollution failures are resolved.
- After the compact-view and scoring additions: **52 FIR tests passed** and Ruff
  passed. The two real HTTP trials exercise the actual MCP/model integration.

## Review closure and final-code trial

The independent review exposed untracked Pi JSON manifests, custom-evaluator cache
identity ambiguity, and synthesis-slot predictions incorrectly establishing
feasibility. Manifests are now explicitly tracked. Custom evaluators require a
host-supplied versioned identity, frozen in the experiment; feasibility now also
checks the synthesis evidence kind. Both contract regressions were reproduced
with failing tests before the fix. **54 FIR tests now pass**, with Ruff clean;
the existing MCP/documentation gate adds **74 passing tests**.

A fresh oracle and [final-code DeepSeek trial](evidence/final_trial.json) were run
after these changes: **pass**, **22.20 seconds**, 20 tool calls, 10 Python evaluations,
7 predictions and 8 replay queries. The final recommendation again matches the
grid optimum with valid evidence citations, confirmed runtime lock and zero
spillover. Usage: 90712 input / 3179 output tokens. The original two snapshots
remain historical regression evidence; source identity deliberately prevents
resuming their stores under changed code.
