# Verification ledger

## Current upstream reconciliation

Integrated `main` at `c10da3002a5e51cd56bbfc9538f017d0c849a6c2` without rewriting
branch history. Targeted analysis covered the overlapping FIR quantizer, manifest,
package dependencies, shared MCP registry/server, and the reused MemCopy build/simulator.
Upstream hardware examples and framework code are accepted unchanged; no vendor tools ran.

- Reproduced the SDK migration failure before repairing it: removed `FastMCP` imports
  prevented CLI/MCP startup under upstream's `mcp==2.3.0`. Both surfaces and the shared
  DSE server factory now use `MCPServer`; wire tests retain strict rejection and durable
  attempt assertions. Added a regression for the separate shared-server factory path.
- Regenerated the development environment with `uv sync --extra dev`.
- Targeted gate: **370 passed, 9 skipped, 18 deselected**:

  ```sh
  uv run --no-sync pytest examples/dse_fir/tests \
    tests/examples/test_mcp_tools.py tests/examples/test_mcp_setup.py tests/docs \
    tests/examples/test_mem_copy*.py tests/examples/test_fir_block*.py \
    tests/hw/test_fixpoint*.py -m 'not vitis and not xsi' -o addopts='' -q
  ```

- Actual Chrome visualizer suite: **6 passed** separately. Scoped DSE Ruff passes.
- Post-merge fresh demo at `f4fe5b0`: **14 attempts**, with all statuses, parameters,
  metrics and checks identical to the original study (wall times and IDs are fresh).
  Both experiment identities changed as required. The current study/review exports
  contain these new runs; the pre-integration study remains at `71727ec` in Git.
- Rebuilt wheel includes execution modules and all presentation/handoff artifacts.
  Its non-editable installation outside the checkout completed the real 14-attempt
  demo with `passed: true`. No editable-install fallback was used.
- Final transport/docs check after the shared-registry annotation repair:
  **103 passed, 2 skipped, 1 deselected**. Undefined-name checks on both shared MCP
  files and diff checks relative to `main` pass. Upstream's existing SVG whitespace
  is retained rather than rewritten as part of this merge.
- Broader MCP run (DSE + `tests/mcp` + existing MCP/examples/docs): **407 passed,
  3 failed, 8 skipped, 5 deselected, 8 xfailed, 1 xpassed**. All three failures
  reproduce on an untouched detached worktree of `c10da30` using the same environment:
  - `tests/mcp/test_knowledge_corpus.py::test_index_builds_in_under_three_seconds`
    exceeded the machine-dependent budget (4.03 s integrated; 3.37 s upstream).
  - `tests/mcp/test_retrieval_eval.py::test_generated_code_is_excluded_by_default`
    found no generated files with the opt-in flag.
  - `tests/mcp/test_usage_index.py::test_generated_files_are_excluded_from_usage_by_default`
    found identical counts with/without generated files.

These are scoped compatibility checks, not an all-repository green claim. No upstream
test was suppressed or relaxed. Historical verification below describes earlier revisions.

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

## Checkpoint and session recovery extension

- **138 Python tests pass:** FIR, MCP and documentation integration. Ruff and diff checks pass.
- **9 Pi tests pass** against SDK 0.86.1; TypeScript passes. The SDK test uses
  `createAgentSession`, extension events and persisted session restoration with
  the real Python CLI. Its offline assistant fixture triggers session persistence;
  no provider request occurs.
- `recovery_probe` passes through a new CLI process in both scripted arms.
  Checkpoints match; each is **2080 canonical bytes**. Each arm retains one charged
  replay query; the verification retry is cached. Evidence:
  [recovery_probe.json](evidence/recovery_probe.json).
- Wheel build and non-editable installation outside the checkout pass the same
  recovery probe. Existing setuptools archive/package warnings remain.
- Integration defect reproduced and fixed: legacy evidence compaction treated
  checkpoint observation counts as dictionaries. Content-addressed checkpoints
  now pass through unchanged; a regression checks projection/hash parity.
- Independent review reproduced JavaScript numeric reserialization invalidating
  checkpoint hashes. The real Pi SDK regression failed before the fix and passes
  after it: Python recomputes hashes from injected and restored canonical text,
  including the exact constraint `max_top_lut=9007199254740993`. Wire context uses
  `checkpoint_json` without a duplicate parsed checkpoint; Pi preserves tool stdout.

Limits: no new model trial, statistical comparison, live synthesis or RTL run.
Exposure entries establish adapter submission, not provider receipt. Historical
DeepSeek trials above predate this extension. Earlier full-suite failures remain
documented; this revision uses the focused integration gate.

## Shared execution slice — working review

Environment regenerated with `uv sync --extra dev`. The `.venv` directory is
disposable; recreation is part of the documented workflow.

- `uv run --no-sync pytest examples/dse_fir/tests tests/examples/test_mcp_tools.py tests/examples/test_mcp_setup.py tests/docs -m 'not vitis and not xsi' -o addopts='' -q`: **212 passed, 8 skipped**. Six skips are optional browser tests, executed separately below.
- `uv run --no-project --with playwright python examples/dse_fir/tests/test_execution_visualizer.py`: **6 passed**, with actual Chrome. Covers import rejection, safe text, filters, review roundtrip, chat bridge/fallback and 1200/700/390 px layouts.
- New execution service/adapter subset: **42 passed**, including real FIR and MemCopy through subprocess CLI and actual stdio MCP. The demo adds a separate end-to-end export test.
- Scoped Ruff and `git diff --check` pass. The complete repository suite was not rerun; historical upstream failures above are not claimed resolved.
- `uv build` produced a wheel containing all execution modules and both HTML files. Running the demo from the installed wheel outside the checkout returned `passed: true` with 14 attempts.
- Independent read-only review found no material issues in the CPU-only slice; its focused run passed 43 tests with 6 optional browser skips. It also checked source identities and reproduced the recorded non-timing outcomes with a fresh demo.
- [Recorded study](evidence/execution_study.json): **14 attempts** — 6 successful Python simulations, 2 discovery observations, 4 deliberately invalid requests, 2 unavailable synthesis requests. Every result comes from the actual service; no hardware replay is substituted.
- FIR: 8/32 taps plus a repeated 32-tap point. MemCopy: 32/64-bit bus plus a repeated 64-bit point, each copying 2,048 bytes across four jobs. Successful repeats retain request identity and receive distinct attempt identities.
- The [interactive review](execution/review.html) loads the recorded study, filters actual attempts and preserves review notes. Actual-data browser checks confirm all 14 rows, six successful Python rows, and three MemCopy rows under the corresponding filters, without horizontal overflow at the tested widths.

Limits: serial local execution only; source/condition changes require new roots.
Synthesis and RTL execution are not integrated. MemCopy cycles denote modeled
simulation end, not RTL last completion. Cold imports are included in wall time;
this demo does not establish performance differences or agent-search superiority.
The legacy Pi adapter remains on the six-tool FIR interface. Presentation judgments
remain open for team review.
