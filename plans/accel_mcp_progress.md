# accel_mcp: progress

Working branch: `accel-mcp` (branched from `main` at `19758f8`). Nothing pushed, no PR.
Plan: [accel_mcp.md](accel_mcp.md). Work order: [accel_mcp_overnight_prompt.md](accel_mcp_overnight_prompt.md).

## Baseline (before any change)

`../pysilicon-venv/Scripts/python.exe -m pytest -m "not vitis and not xsi" -q -p no:cacheprovider`

6 failures, all pre-existing on `main` and all outside the MCP area. None were
touched.

```
tests/hw/test_dataschema_poly.py::test_poly_notebook_flow_generates_headers_vectors_and_expected_outputs
tests/poly/test_timing_analysis.py::TestCommandHeader::test_tx_id
tests/poly/test_timing_analysis.py::TestCommandHeader::test_nsamp
tests/poly/test_timing_analysis.py::TestInputSamples::test_x_first_value
tests/poly/test_timing_analysis.py::TestInputSamples::test_x_last_value
tests/poly/test_timing_analysis.py::TestOutputSamples::test_y_values
```

## Status by stage

| Stage | State | Commits |
| --- | --- | --- |
| Plans committed | done | `e1d28d8` |
| 1 — search and read (`waveflow/mcp/knowledge/`) | in progress | |
| 2 — D1 removal (OpenAI RAG, committed corpus) | not started | |
| 3 — wire the server | not started | |
| 4 — frames layout, `waveflow_get_process` | not started | |
| 5 — `new-accel` scaffold | not started | |
| 6 — doc summaries (89 pages) | not started | |

## D5: the example list

The 14 TOC examples, each `docs/examples/<doc>/index.md` with the `example_dir:`
it will carry. Three doc names differ from their directory (marked).

| Doc page | `example_dir:` |
| --- | --- |
| `basic_vec` | `examples/basic_vec` |
| `bram_access` | `examples/bram_access` |
| `firblock` | `examples/fir_block` **(differs)** |
| `interleaver` | `examples/interleaver` |
| `memcpy` | `examples/mem_copy` **(differs)** |
| `mmqueue` | `examples/vmac` **(differs)** |
| `regmap` | `examples/regmap` |
| `rf_loopback` | `examples/rf_loopback` |
| `rf_shot_loopback` | `examples/rf_shot_loopback` |
| `rf_shot_rx` | `examples/rf_shot_rx` |
| `rf_shot_tx` | `examples/rf_shot_tx` |
| `shared_mem` | `examples/shared_mem` |
| `stream_inband` | `examples/stream_inband` |
| `vecmult` | `examples/vecmult` |

Files a TOC page links to outside its own directory are pulled in as well.
These are **derived from the pages**, not hand-listed: `corpus._linked_example_paths`
reads both markdown links and inline-code `examples/...` paths off every page
under `docs/examples/<doc>/`. Today that finds exactly the two the plan names:

- `examples/schemas/fixedpoint/` — from `basic_vec`
- `examples/interface/aximm_queue_demo.py` — from `mmqueue`

**Excluded** (everything else under `examples/`, not searchable, not listed,
not fetchable): `_archive`, `block_scale`, `bram_toy`, `dse_fir`, `memory`,
`rf_blk_delay`, `rf_relayout`, `rf_repeat_play`, `rf_samp_buf_rx`,
`rf_samp_buf_tx`, `state_toy`, `test`, `timing`, `toy`, `vecunit`, `vscode`,
plus the parts of `examples/interface/` and `examples/schemas/` no TOC page
links to.

## Decisions needed

*(none yet)*

## Wrong in the plan

*(none yet)*

## Next step

Finish Stage 1: the usage index, the example cards, the six tools, the CLI, and
the test gates.
