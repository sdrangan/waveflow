# FIR experiment-and-evidence interface

A bounded FIR DSE service with six typed tools, durable evidence, a JSON/CSV CLI,
stdio MCP, and an optional Pi extension. Ordinary Python owns the semantics;
agent hosts are replaceable. See [design](DESIGN.md) and [verification](VERIFICATION.md).
Module diagram: [REVIEW.html](REVIEW.html).

**Available now:** real fixed-point Python evaluation, canonical resource prediction,
and replay of the committed 24-point HLS corpus. `synth` returns explicitly labeled
replay evidence; `rtlsim` returns an unavailable observation. Neither runs vendor tools.
Feasibility means the declared Python-quality/HLS-estimate constraints—not board validation.

## Quick start

From the repository, `uv sync --extra dev` installs the Python environment. From a
built wheel, use its installed Python in place of `uv run python` below.

```sh
uv run python -m examples.dse_fir --root ./fir-run context
uv run python -m examples.dse_fir schemas
uv run python -m examples.dse_fir --root ./fir-run call dse_pysim \
  --args '{"params":{"ntap":32,"samp_w":16,"samp_i":2,"unroll_lane":false,"mem_dwidth":32}}'
uv run python -m examples.dse_fir --root ./fir-run call dse_predict_resource \
  --args '{"params":{"ntap":32,"samp_w":16}}'
uv run python -m examples.dse_fir --root ./fir-run call dse_synth \
  --args '{"params":[{"ntap":32,"samp_w":16}]}'
uv run python -m examples.dse_fir --root ./fir-run results
uv run python -m examples.dse_fir --root ./fir-run results --format csv
```

Omitted candidate fields use the defaults published in context. Host-owned
`--config experiment.json` can set evaluation, constraints and budgets **when creating
a new root**; subsequent calls use the frozen policy. `WAVEFLOW_DSE_ROOT` is the
alternative root selector. Do not share an experiment between incompatible source
versions: source/backend changes deliberately fail closed. Use a new root instead.

Results preserve per-operation status, provenance, cost and IDs. Repeated identical
calls return cached evidence without spending twice. Each uncached batch member
consumes its declared operation unit, including unavailable/failed evaluations.
The CLI reserves stdout for JSON/CSV (diagnostics go to stderr), and returns nonzero
for failed calls. Page observation rows with `results --offset N --limit N`.
Transport views are compact and page at most five observations per call, even
when a larger limit is requested; follow `next_offset`. Candidate summaries cover
that page. Full numerical diagnostics and all candidates remain available through
the direct Python service and its durable SQLite store.

## MCP and other hosts

Configure an MCP host to run the installed interpreter with these arguments:

```text
-m examples.dse_fir --root /absolute/path/to/fir-run serve
```

The stdio server exposes exactly `dse_get_dse_context`, `dse_pysim`,
`dse_predict_resource`, `dse_synth`, `dse_rtlsim`, and `dse_get_results`.
The existing Waveflow entry point also supports `build_mcp(mode="dse", work_dir=...)`.
No authoring, arbitrary shell or file tools are added to this profile.

For direct Python, import `DseService` from `examples.dse_fir.service` and call
`invoke(tool_name, arguments)`, `context()`, or `results()`. The portable procedure
is [skills/fir-dse/SKILL.md](skills/fir-dse/SKILL.md).

## Pi extension

`pi/` uses the upstream `@earendil-works/pi-coding-agent` extension API. Point
`WAVEFLOW_DSE_PYTHON` at the absolute installed Python interpreter, set
`WAVEFLOW_DSE_ROOT`, and load `pi/index.ts` using Pi's extension loader. The adapter
obtains schemas from Python, passes JSON as an argv value, and preserves cancellation.
It does not disable other Pi tools or claim to sandbox the host.

`WAVEFLOW_DSE_CONTEXT_POLICY=pull` (default) retains explicit context retrieval.
`resume` restores the same checkpoint at session start and submits it before the
next agent run. Session entries record experiment binding and submitted evidence;
they do not prove provider receipt. Mismatched experiment bindings fail closed.

Tool context includes `checkpoint_json`: canonical JSON text containing experiment
ID, observation revision, content hash, constraints, budgets, best evidenced candidate
and bounded references. Direct Python returns the corresponding `checkpoint` object.
Pi preserves this text without numeric reserialization; tool details retain `raw_json`.
Projection reads spend no evaluation units. Session state never overrides SQLite.

Run npm installation/tests in a scratch copy, not this checkout: dependency trees
interfere with repository-wide documentation/package checks. Copy both `pi/` and
`skills/` as siblings; run `npm ci`, `npm run check`, `npm test` in the copied `pi/`.
Set `PYTHONPATH` to the repository root when testing from that scratch copy.
`npm pack` includes the prepared portable skill in the Pi package.

## Reproduce the benchmark

Deterministic recovery check, without model calls:

```sh
uv run python -m examples.dse_fir.recovery_probe --root ./new-recovery-run
```

The probe discards a committed replay response, reopens through a new CLI process,
and checks checkpoint parity, citations and cache accounting in scripted pull/resume
arms. Outputs: `report.json` and per-arm `exposure.json`. It tests recovery contracts,
not Pi lifecycle execution or model performance. Existing output roots are rejected.

The scorer's exhaustive reference is never exposed to the model:

```sh
uv run python -m examples.dse_fir.benchmark \
  --root /new/oracle-root --output /path/to/oracle.json
uv run python -m examples.dse_fir.run_agent \
  --home /new/isolated-hermes-home --root /new/trial-root \
  --output /new/trial-output --port 8647
```

The second command is a **no-inference preflight** by default. Add `--paid` only
to authorize a real trial. It requires a local Hermes installation and configured
DeepSeek credentials, launches a separate loopback HTTP gateway, verifies exactly
six MCP tools, locks `deepseek-flash` / `deepseek`, captures SSE/runtime evidence,
and terminates its owned gateway. It does not modify the personal gateway.
Use new directories for every trial; the optional `--turns` cannot exceed 16.

Score persisted observations independently:

```sh
uv run python -m examples.dse_fir.benchmark --root /path/to/trial-root \
  --reference /path/to/oracle.json --output /path/to/score.json
```

This score is the **best evidenced candidate found**, not automatically the final
candidate recommended by the model. The trial scorer validates that recommendation
and exits nonzero on failure, including unsupported feasibility or invented IDs:

```sh
uv run python -m examples.dse_fir.score_trial --root /path/to/trial-root \
  --trace /path/to/trial-output --reference /path/to/oracle.json \
  --output /path/to/verdict.json
```

`selection_score` reports final-recommendation quality, the gap from the best
observed candidate, and reference regret when an oracle is supplied. Invalid
recommendations retain failure status and null quality/regret.

The [first two real trials](evidence/first_trials.json) are retained as offline
regression evidence: fail before compact views, pass after. Paid inference is
opt-in and is never invoked by pytest. Random/grid-prefix baselines match expensive
query allowances; the full grid is only an oracle, and cheap screening is an
important non-agent comparator. This small public replay grid cannot establish
out-of-distribution optimization performance.

## Lean development gate

```sh
uv run pytest examples/dse_fir/tests -q
uv run ruff check examples/dse_fir
uv run pytest -m 'not vitis and not xsi'
```

Known upstream failures, exact benchmark outcomes and limits are recorded in
[VERIFICATION.md](VERIFICATION.md). No Vitis, Vivado, xsim or physical-board claims
are made by this pilot.
