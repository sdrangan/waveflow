---
name: fir-dse
description: Explore bounded FIR designs using live context, staged evidence, and durable evaluation results.
---

# FIR design-space exploration

Use the six `dse_*` tools when available. Otherwise use the host-installed Python
CLI: `python -m examples.dse_fir context`, `schemas`, or
`call TOOL --args JSON`. Host configuration selects the interpreter and durable
root; do not invent a new root or change policy to escape a limit.

1. Call `dse_get_dse_context` first. Read the current experiment, legal candidate
   schema, objective, constraints, available backends, and remaining budgets.
   Construct parameters from this schema, not from remembered candidate fields.
2. Read `dse_get_results` before spending work; page with `offset` and `limit`.
   Context and results are live. Re-read after calls, reconnects, or cancellations.
3. Use `dse_pysim` for functional/quality evidence and
   `dse_predict_resource` for inexpensive resource estimates. Both take
   `{"params": {...}}`. Prefer a small, interpretable exploration over blind sweeps.
4. Use `dse_synth` and `dse_rtlsim` selectively, with
   `{"params": [{...}, {...}]}`. These are bounded batches, not arbitrary command
   execution. Compare requested and returned counts and inspect every row's status.
5. Report the best *verified feasible* candidate according to the live service,
   its candidate/evaluation IDs, objective value and constraint evidence. If none
   is feasible, say so; do not promote an unverified prediction to a winner.

## Interpret evidence, not just numbers

For an objective to minimize, candidate A dominates B only when its objective is
no worse and every compared constrained resource is no worse, with at least one
strict improvement, using comparable successful evidence. Reverse the objective
comparison for maximization. Feasibility requires *all* required constraints and
required evidence levels, not merely a favorable scalar score.

Predictions are estimates; Python simulation is not physical timing validation.
Synthesis, RTL simulation, and implementation answer different questions. Read
`evidence_kind`, provenance, metric definitions, and units. Never relabel synthetic
or fixture-backed results as vendor-tool execution. For paired prediction p and
measurement m, a useful signed discrepancy is m - p; relative discrepancy
(m - p)/p is undefined when p = 0. State the denominator and evidence sources.

Distinguish cached evidence from newly purchased evaluations. A cache hit does not
create an independent sample. Failed, unavailable, cancelled, or rejected rows
are not successful measurements and must not silently disappear from a report.
Do not infer confidence intervals from a single deterministic result.

## Authority boundary

This skill supplies search guidance and interpretation, **not enforcement**.
Candidate validation, frozen experiment configuration, capability availability,
budget accounting, cache identity, concurrency, and durable evidence belong to
the Python service. Tool arguments cannot select executable code, paths, backend
commands, or policy overrides. Do not use unrelated shell/file tools to bypass a
service refusal. Ask the host to establish a separately authorized experiment
when policy must change. Pi itself may expose other tools: the extension is not a
sandbox for the entire agent.
