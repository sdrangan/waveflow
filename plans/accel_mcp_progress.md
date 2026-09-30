# accel_mcp: progress

Working branch: `accel-mcp` (branched from `main` at `19758f8`). Nothing pushed, no PR.
Plan: [accel_mcp.md](accel_mcp.md). Work order: [accel_mcp_overnight_prompt.md](accel_mcp_overnight_prompt.md).

**Read this section first if you read nothing else.** Stages 1 to 4 of the work
order are done and the tree is green — the same 6 pre-existing failures as
`main`, no new ones. Item 5 (the `new-accel` scaffold) and item 6 (the 89 doc
summaries) are the remaining work. Two things in the plan turned out to be
wrong and one decision is waiting for you; both are below.

## Baseline (before any change)

`../pysilicon-venv/Scripts/python.exe -m pytest -m "not vitis and not xsi" -q -p no:cacheprovider`

6 failures, all pre-existing on `main` and all outside the MCP area. None were
touched, and the branch adds none.

```
tests/hw/test_dataschema_poly.py::test_poly_notebook_flow_generates_headers_vectors_and_expected_outputs
tests/poly/test_timing_analysis.py::TestCommandHeader::test_tx_id
tests/poly/test_timing_analysis.py::TestCommandHeader::test_nsamp
tests/poly/test_timing_analysis.py::TestInputSamples::test_x_first_value
tests/poly/test_timing_analysis.py::TestInputSamples::test_x_last_value
tests/poly/test_timing_analysis.py::TestOutputSamples::test_y_values
```

## Status by stage

| Work-order item | State | Commit |
| --- | --- | --- |
| Plans committed | done | `e1d28d8` |
| Baseline + D5 list | done | `371a115` |
| 1 — Stage 1: search and read | **done** | `8e74fe5` |
| 2 — D1 removal (OpenAI RAG, committed corpus) | **done** | `HASH_234` |
| 3 — Stage 2: wire the server | **done** | `HASH_234` |
| 4 — Stage 3: frames, process, D2 removal | **done** | `HASH_234` |
| 5 — Stage 4: `new-accel` scaffold | **not started** | |
| 6 — doc summaries (89 pages) | **not started** | |

Items 2 to 4 are **one commit, not three**, against the work order's
"one logical step per commit". They share `registry.py` (the RAG tool leaves
and the frame tools arrive), `pyproject.toml` (one entry point out, one
package-data glob in) and `docs/guide/ai_tooling/` (one page replaces two).
Splitting them would have produced intermediate commits that do not import —
a registry registering `waveflow_get_process` before `waveflow/mcp/frames/`
is tracked — and the work order also asks that every step leave the tree
green. Green won. The commit message separates the three.

### What exists now

`waveflow/mcp/knowledge/` — `roots.py`, `corpus.py`, `bm25.py`, `usage.py`,
`examples.py`, `index.py`, `tools.py`. Eight tools registered in both
profiles:

| Tool | |
| --- | --- |
| `waveflow_browse` | doc tree with titles and summaries |
| `waveflow_search` | BM25 over heading-sized chunks |
| `waveflow_find_usage` | every use of a symbol, grouped by example |
| `waveflow_list_examples` | the 14 cards |
| `waveflow_get_example` | a card, or one whole file |
| `waveflow_get_doc` | a whole page, or one section |
| `waveflow_list_frames` | the architectures on offer |
| `waveflow_get_process` | the ordered steps for one |

All of them are also CLI: `waveflow kb <cmd>`, `waveflow frames`,
`waveflow process <frame>` (new `waveflow` console script). JSON by default,
`--text` for a readable rendering.

`waveflow/mcp/frames/stream_inband/` — `frame.md` (copied from the plan),
`process.md` (new), `prompts/01..03`, `frame.toml`. Shipped as package data.
`template/` is **not** there yet; it belongs to item 5.

### Numbers

- index: 268 doc pages, 3,116 chunks, 14 examples, ~600 indexed symbols
- build: **1.4 – 1.9 s** warm (budget 3 s, gated); ~13 s cold first read
- query: about 1 ms

### Test results

`tests/mcp/` — 87 passed, 5 xfail, 1 xpass. Covering:

| File | |
| --- | --- |
| `test_retrieval_eval.py` | 25 queries in Waveflow's words + scope/generated behaviour |
| `test_paraphrase.py` | the 6 newcomer-phrased queries, `xfail(strict=False)` |
| `test_usage_index.py` | hand-checked index for `regmap`, `stream_inband`, `vecmult` |
| `test_knowledge_corpus.py` | the D5 gate, the cards, browse/get_doc, the 3 s budget |
| `test_kb_cli.py` | every subcommand, and CLI output == tool output |
| `test_server_smoke.py` | stdio: start the server, list the tools, call each one |
| `test_frames.py` | the frame parts, the process rules, the package-data glob |

## D5: the example list

The 14 TOC examples, each `docs/examples/<doc>/index.md` now carrying an
`example_dir:`. Three doc names differ from their directory (marked).

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

Files a TOC page links to outside its own directory come along, **derived
from the pages** rather than hand-listed: `corpus._linked_example_paths`
reads markdown links and inline-code `examples/...` paths off every page
under `docs/examples/<doc>/`. Today that finds exactly the two the plan
names, and there is a test asserting both are present:

- `examples/schemas/fixedpoint/` — from `basic_vec`
- `examples/interface/aximm_queue_demo.py` — from `mmqueue`

**Excluded** — not searchable, not listed, not fetchable: `_archive`,
`block_scale`, `bram_toy`, `dse_fir`, `memory`, `rf_blk_delay`,
`rf_relayout`, `rf_repeat_play`, `rf_samp_buf_rx`, `rf_samp_buf_tx`,
`state_toy`, `test`, `timing`, `toy`, `vecunit`, `vscode`, plus the parts of
`examples/interface/` and `examples/schemas/` no TOC page links to. There is
a test asserting a sample of these never reach the index.

## Decisions needed

**1. Two search-ranking judgement calls I made, both reversible.**

- *A guide-over-source prior.* `waveflow_search` multiplies scores by 1.15
  for `docs/guide/`, 1.0 for example docs, 0.8 for example source
  (`tools._prior`). Without it, "BuildDag build steps" returned six example
  `*_build.py` files — each literally defining a `build_<name>_dag` — and
  none of `docs/guide/build/`. The reasoning: the guide is the reference,
  and `waveflow_find_usage` already exists for "show me X in use". If you
  would rather search led with real code, set the three constants to 1.0;
  the retrieval eval still passes 24 of 25 (the BuildDag query is the one
  that then fails).
- *At most 2 sections per file in one result set* (`tools._MAX_PER_PATH`).
  Without it a single long page filled all five slots and the second-best
  *page* never appeared.

**2. The example-card `hook_files` heuristic is good, not perfect.**
See "Wrong in the plan" item 2. `firblock` shows 1 of its 3 hand-written
bodies because the other two are named only in a docstring, and
`rf_shot_{rx,tx,loopback}` show none. If the card should list every
hand-written C++ file, say so and I will widen the rule — but it will then
also list generated schema headers for the examples that commit them.

**3. `openai.md` is gone; the OpenAI key note moved nowhere.**
I deleted `docs/guide/ai_tooling/openai.md` and folded `rag.md` into
`docs/guide/ai_tooling/search.md` (the work order allowed either). But
`headless.py` still uses an OpenAI client, so `OPENAI_API_KEY` is still
needed to run the eval harness, and that page was the only place the key
was documented. `docs/guide/developer/headless.md` is where it belongs.
I did not write it, because how you want the key described is your call
and Stage 6 (D4) may change the client anyway. **This is the one gap I
left deliberately.**

## Wrong in the plan (or in the tree)

1. **`docs/guide/build/` was invisible to the first index I wrote.** My
   artefact-directory skip list held `build`, `data`, `results`, `figures` —
   sensible under `examples/`, and wrong under `docs/`, where they are
   subject names. A whole chapter of the guide was unsearchable. The skip
   list is now scoped to source roots only (`corpus._SKIP_IN_SOURCE` vs
   `_SKIP_ALWAYS`). Worth knowing because the symptom was silent: search
   just never returned those pages.

2. **"`examples/*/gen/` is the generated code" is not the whole line.** The
   plan treats `gen/` as the generated tree, but an example's `include/` is
   equally generated (schema headers) or copied (support headers), some
   examples *commit* their generated headers and others ignore them, and
   `examples/mem_copy/include/` is deliberately half and half. Rather than
   re-derive that, the corpus asks **git**: a file under an example that is
   not tracked is tagged and kept out of search by default. `.gitignore`
   already draws this line and draws it carefully.

   The same problem hit the card's `hook_files`: by extension alone,
   `stream_inband` had "38 hooks", all generated headers. It is now derived
   from the Python — `@synthesizable` methods imply
   `<component>_<method>_impl.*`, and `KernelTask("x", "x.h", ...)` names
   its body outright.

3. **Six examples carry a byte-identical `streamutils_hls.h`.** A query
   about stream framing returned the same file five times and nothing else.
   The corpus now de-duplicates by content hash and reports the copies as
   `also_at` on the hit. 95 duplicate groups collapsed, 3,842 chunks → 3,116.

4. **The plan's probe found 2 of 6 paraphrase queries; this implementation
   finds 1.** Which one the probe also got is not recorded. I checked
   whether the guide-over-source prior was the cause — it is not; forcing it
   to 1.0 still gives 1 of 6. The plan's conclusion is unchanged and if
   anything firmer.

5. **FastMCP builds each tool's schema from the function signature, not from
   the registry's `parameters` dict.** Two consequences, both found by the
   stdio smoke test and both fixed:
   - a `root` argument I had on the knowledge tools for testing was exposed
     to the model, letting it repoint the index at any directory. The tools
     now take no such argument; `WAVEFLOW_KB_ROOT` selects a tree instead.
   - `waveflow_get_process(frame: str = "stream_inband")` **rejected
     `null`** — and the registry marks these schemas `strict`, which makes
     every property required, so a client wanting the default sends null.
     Every optional argument is now `X | None`. Worth a look if any future
     tool is added: the registry `parameters` are used only by
     `tool_schemas()` for the headless harness, so the two can disagree
     silently.

6. **`waveflow process --text` crashed on a default Windows console** —
   cp1252 cannot encode the `→` in `process.md`. `cli._utf8_stdout`
   reconfigures stdout/stderr to UTF-8 with `errors="replace"`.

## What I changed outside the MCP area

Kept to the minimum the removals forced:

- `CLAUDE.md` — the `waveflow/mcp/` and `examples/` bullets described the
  removed corpus and a list of examples that no longer exist (`conv2d/`,
  `histogram/`, `poly/`).
- `tests/docs/test_markdown_integrity.py`, `tests/docs/test_documented_numbers.py`
  — each had `waveflow/mcp/corpus/` in a skip list; the tree is gone.
- `tests/examples/test_mcp_setup.py` — every test in it was about
  `--build-rag`. Rewritten to cover what the command still does, including
  the overwrite refusal, which had no coverage of its own.
- `waveflow/scripts/waveflow_mcp_setup.py` — `--build-rag` and the
  vector-store plumbing removed.
- `docs/guide/developer/headless.md` — two sentences that said "the RAG
  tools".

## Next step

Item 5, the scaffold: `waveflow new-accel <name> --frame stream_inband`.
It copies `examples/stream_inband`, renames it, stubs the compute to
identity, adds the `spec/` stubs, writes `process.md` as `AGENTS.md` plus
one-line `CLAUDE.md` / `GEMINI.md` pointers, and copies `frame.md`. Gates:
the scaffolded project's pysim runs (non-Vitis), and `--through
validate_csim` under `-m vitis`, run in the background because it takes
minutes.

Then item 6, the 89 doc summaries, in their own commits so they are easy to
revert.
