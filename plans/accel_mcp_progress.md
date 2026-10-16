# accel_mcp: progress

Working branch: `accel-mcp` (branched from `main` at `19758f8`). Nothing pushed, no PR.
Plan: [accel_mcp.md](accel_mcp.md). Work order: [PR #209](https://github.com/sdrangan/waveflow/pull/209).

---

**Read this first.** All six items of the work order are done. The tree is
green: the same 6 pre-existing failures as `main`, no new ones. Both Stage 4
gates pass, including `--through validate_csim` on real Vitis HLS 2025.1.

Three things want your attention, in order:

1. **The 87 doc summaries are two commits of their own** (`fd1536d` guide,
   `dfc96fc` examples), one added front-matter line per file, no page body
   touched. Revert either independently.
2. **Three decisions are waiting for you** — see [Decisions needed](#decisions-needed).
   None blocked anything; each has the more reversible option in place.
3. **Seven things in the plan or the tree turned out to be wrong** — see
   [Wrong in the plan](#wrong-in-the-plan-or-in-the-tree). Two of them were
   silent failures that only a test caught.

---

## Baseline (before any change)

`../pysilicon-venv/Scripts/python.exe -m pytest -m "not vitis and not xsi" -q -p no:cacheprovider`

6 failures, all pre-existing on `main`, all outside the MCP area. None were
touched.

```
tests/hw/test_dataschema_poly.py::test_poly_notebook_flow_generates_headers_vectors_and_expected_outputs
tests/poly/test_timing_analysis.py::TestCommandHeader::test_tx_id
tests/poly/test_timing_analysis.py::TestCommandHeader::test_nsamp
tests/poly/test_timing_analysis.py::TestInputSamples::test_x_first_value
tests/poly/test_timing_analysis.py::TestInputSamples::test_x_last_value
tests/poly/test_timing_analysis.py::TestOutputSamples::test_y_values
```

## Status

| Work-order item | State | Commit |
| --- | --- | --- |
| Plans committed | done | `e1d28d8` |
| Baseline + D5 list | done | `371a115` |
| 1 — Stage 1: search and read | **done** | `8e74fe5` |
| 2 — D1 removal (OpenAI RAG, committed corpus) | **done** | `fd84023` |
| 3 — Stage 2: wire the server | **done** | `fd84023` |
| 4 — Stage 3: frames, process, D2 removal | **done** | `fd84023` |
| 5 — Stage 4: `new-accel` scaffold | **done** | `50760a9` |
| 6 — doc summaries (87 pages) | **done** | `fd1536d`, `dfc96fc` |

Items 2 to 4 are one commit rather than three. They share `registry.py` (the
RAG tool leaves and the frame tools arrive), `pyproject.toml` (one entry point
out, one package-data glob in) and `docs/guide/ai_tooling/` (one page replaces
two), so splitting them would have produced intermediate commits that do not
import — and the work order also asks that every step leave the tree green.
The commit message separates the three.

## What exists now

**`waveflow/mcp/knowledge/`** — `roots.py`, `corpus.py`, `bm25.py`, `usage.py`,
`examples.py`, `index.py`, `tools.py`.

**`waveflow/mcp/frames/stream_inband/`** — `frame.md` (copied from the plan),
`process.md` (new), `prompts/01..03`, `frame.toml`. Shipped as package data.
There is no `template/`: see "wrong in the plan" item 6.

**`waveflow/mcp/scaffold.py`** — `new_accel`, driven by the frame's
`[template]` declaration.

Eleven tools, all registered in both profiles and all reachable from a
terminal:

| Tool | CLI |
| --- | --- |
| `waveflow_browse` | `waveflow kb browse [section]` |
| `waveflow_search` | `waveflow kb search <query>` |
| `waveflow_find_usage` | `waveflow kb usage <symbol>` |
| `waveflow_list_examples` | `waveflow kb examples` |
| `waveflow_get_example` | `waveflow kb example <name> [--file F]` |
| `waveflow_get_doc` | `waveflow kb doc <path> [--heading H]` |
| `waveflow_list_frames` | `waveflow frames` |
| `waveflow_get_process` | `waveflow process [frame]` |
| `waveflow_new_accel_project` | `waveflow new-accel <name>` |
| `waveflow_validate_schema` | (unchanged) |
| `waveflow_get_components` | (unchanged) |

`waveflow` is a new console script. Output is JSON — the same payload the MCP
tool returns, which a test asserts — with `--text` for a readable rendering.

### Numbers

- index: 267 doc pages, 3,211 chunks, 14 examples, 595 indexed symbols
- build: **1.4 – 1.9 s** warm (budget 3 s, gated); about 13 s on a cold read
- query: about 1 ms

### Test results

`tests/mcp/` — 108 passed, 5 xfail, 1 xpass, 1 vitis-marked.

| File | |
| --- | --- |
| `test_retrieval_eval.py` | 25 queries in Waveflow's words, plus scope and generated-file behaviour |
| `test_paraphrase.py` | the 6 newcomer-phrased queries, `xfail(strict=False)` |
| `test_usage_index.py` | hand-checked index for `regmap`, `stream_inband`, `vecmult` |
| `test_knowledge_corpus.py` | the D5 gate, the cards, browse/get_doc, the 3 s budget |
| `test_kb_cli.py` | every subcommand; CLI output equals tool output |
| `test_server_smoke.py` | stdio: start the server, list the tools, call each one |
| `test_frames.py` | the frame parts, the process rules, the package-data glob |
| `test_scaffold.py` | the scaffolded project's structure, rename, stubs — and that its pysim runs |

**Stage 4's two gates, both met:**

- non-Vitis: `python gain_clip_build.py --through py_sim` passes on a freshly
  scaffolded project, unmodified. Gated by `test_scaffolded_project_runs_pysim`.
- Vitis HLS 2025.1: `--through validate_csim` passes, unmodified — csim,
  csynth, estimated Fmax 137.36 MHz, "All loop constraints were satisfied".
  Gated by `test_scaffolded_project_runs_csim`, marked `vitis`.

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

Files a TOC page links to outside its own directory come along, **derived from
the pages** rather than hand-listed: `corpus._linked_example_paths` reads
markdown links and inline-code `examples/...` paths off every page under
`docs/examples/<doc>/`. Today that finds exactly the two the plan names, and a
test asserts both are present:

- `examples/schemas/fixedpoint/` — from `basic_vec`
- `examples/interface/aximm_queue_demo.py` — from `mmqueue`

**Excluded** — not searchable, not listed, not fetchable: `_archive`,
`block_scale`, `bram_toy`, `dse_fir`, `memory`, `rf_blk_delay`, `rf_relayout`,
`rf_repeat_play`, `rf_samp_buf_rx`, `rf_samp_buf_tx`, `state_toy`, `test`,
`timing`, `toy`, `vecunit`, `vscode`, plus the parts of `examples/interface/`
and `examples/schemas/` no TOC page links to. A test asserts a sample of these
never reach the index.

## Decisions needed

**1. Two search-ranking judgement calls, both one constant away from reversed.**

- *A guide-over-source prior.* `waveflow_search` multiplies scores by 1.15 for
  `docs/guide/`, 1.0 for example docs, 0.8 for example source
  (`knowledge/tools.py::_prior`). Measured three ways on the 25-query eval,
  **all three score 23/25** — the prior trades one miss for another rather
  than winning outright. With it, "BuildDag build steps run_dag_cli" reaches
  `docs/guide/build/`; without it, six example `*_build.py` files fill the
  result and the guide page never appears. I kept it because the guide is the
  reference and `waveflow_find_usage` already exists for "show me X in use" —
  but it is a judgement about what an agent should see first, which is yours.
  Set the three constants to 1.0 to remove it; one eval expectation
  ("halted error tx_id status after a failure") then wants narrowing again.
- *At most two sections per file in one result set* (`_MAX_PER_PATH`). Without
  it a single long page filled all five slots and the second-best *page* never
  appeared. I do not think this one is contentious.

**2. The card's `hook_files` is derived, and deliberately under-collects.**

Extension alone is useless — by that rule `stream_inband` had "38 hooks", all
generated schema headers. It is now derived from the Python: a
`@synthesizable` method implies `<component>_<method>_impl.*`, and
`KernelTask("x", "x.h", ...)` names its body outright. That gives the right
answer for 10 of the 14 examples. `firblock` shows 1 of its 3, because the
other two are named only in a docstring; `rf_shot_{rx,tx,loopback}` show none.
Widening the rule would also start listing generated headers for the examples
that commit them. **Tell me which way you want that traded** — under-listing a
real hook, or listing files the agent must not edit.

**3. `openai.md` is gone and its content moved nowhere. This is the one gap I
left on purpose.**

I deleted `docs/guide/ai_tooling/openai.md` and folded `rag.md` into
`docs/guide/ai_tooling/search.md` (the work order allowed either). But
`headless.py` still uses an OpenAI client, so `OPENAI_API_KEY` is still needed
to run the eval harness, and that page was the only place the key was
documented. It belongs in `docs/guide/developer/headless.md`. I did not write
it because how you want the key described is your call, and Stage 6 / D4 may
replace the client anyway.

## Wrong in the plan (or in the tree)

1. **`docs/guide/build/` was invisible to the first index I wrote.** My
   artefact-directory skip list held `build`, `data`, `results`, `figures` —
   sensible under `examples/`, wrong under `docs/`, where they are subject
   names. A whole chapter of the guide was unsearchable and the symptom was
   silent: search simply never returned those pages. The skip list is now
   scoped to source roots (`_SKIP_IN_SOURCE` vs `_SKIP_ALWAYS`).

2. **"`examples/*/gen/` is the generated code" is not the whole line.** An
   example's `include/` is equally generated (schema headers) or copied
   (support headers); some examples commit their generated headers and others
   ignore them; `examples/mem_copy/include/` is deliberately half and half.
   Rather than re-derive that, the corpus asks **git**: a file under an example
   that is not tracked is indexed, tagged, and kept out of search by default.
   `.gitignore` already draws this line and draws it carefully.

3. **Six examples carry a byte-identical `streamutils_hls.h`.** A query about
   stream framing returned the same file five times and nothing else. The
   corpus now de-duplicates by content hash and reports the copies as `also_at`
   on the hit: 95 duplicate groups collapsed, 3,842 chunks down to 3,211.

4. **The plan's probe found 2 of 6 paraphrase queries; this implementation
   finds 1.** Which one the probe also got is not recorded. I checked whether
   the guide-over-source prior was the cause — it is not; forcing it to 1.0
   still gives 1 of 6. The plan's conclusion is unchanged and if anything
   firmer: BM25 alone does not answer a query that avoids Waveflow's words.

5. **FastMCP builds each tool's schema from the function signature, not from
   the registry's `parameters` dict.** Two bugs followed, both found by the
   stdio smoke test and both fixed:
   - a `root` argument the knowledge tools carried for testing was **exposed to
     the model**, letting it repoint the index at any directory on the machine.
     The tools now take no such argument; `WAVEFLOW_KB_ROOT` selects a tree.
   - `waveflow_get_process(frame: str = "stream_inband")` **rejected `null`**,
     which is exactly what a client sends for an optional argument under a
     `strict` schema. Every optional argument is now `X | None`.

   Worth knowing before the next tool is added: the registry `parameters` are
   used only by `tool_schemas()` for the headless harness, so the two
   descriptions of a tool can disagree silently. The smoke test is the thing
   that notices.

6. **The plan's frame layout has a `template/` directory; there isn't one.**
   A committed template is a 1,300-line duplicate of `examples/stream_inband`
   that rots the first time the example changes, against the plan's own first
   principle. Instead `frame.toml` has a `[template]` section naming the source
   example, the name token, and the compute regions to stub — and the scaffold
   copies the live example and renames it. The cost is that scaffolding needs a
   checkout, the same condition the index already has. **If you want a
   shippable template for the no-checkout case, this is where that decision
   goes.**

   Related, and the nastiest bug of the night: the first version found the
   compute with a **regex over arbitrary example source**. It silently matched
   nothing in the Python and produced a project that claimed to be stubbed and
   still contained the reference Horner evaluation. The regions are now
   declared by anchor in `frame.toml`, and a stub that fails to land is a hard
   error that writes nothing.

7. **Two smaller ones.** `waveflow process --text` died on a default Windows
   console, because cp1252 cannot encode the arrow in `process.md`
   (`cli._utf8_stdout`). And writing a `summary:` **replaces** the
   first-paragraph fallback in the index, so a summary that drops a word the
   page is known by makes the page *harder* to find — it cost
   `docs/guide/timing/vcd.md` the query "cycle count from a VCD waveform" until
   the summary said "waveform", which the page says twice.

## What I changed outside the MCP area

Kept to the minimum the removals forced:

- `CLAUDE.md` — the `waveflow/mcp/` and `examples/` bullets described the
  removed corpus and a list of examples that no longer exist (`conv2d/`,
  `histogram/`, `poly/`).
- `tests/docs/test_markdown_integrity.py`, `tests/docs/test_documented_numbers.py`
  — each had `waveflow/mcp/corpus/` in a skip list; the tree is gone.
- `tests/examples/test_mcp_setup.py` — every test in it was about
  `--build-rag`. Rewritten to cover what the command still does, including the
  overwrite refusal, which had no coverage of its own.
- `tests/examples/test_mcp_tools.py` — dropped the RAG section and the four
  `get_schema_draft_plan` tests; the removals are now asserted as removals.
- `waveflow/scripts/waveflow_mcp_setup.py` — `--build-rag` and the vector-store
  plumbing.
- `docs/guide/developer/headless.md` — two sentences that said "the RAG tools".

No `examples/` source was modified. `plans/waveflow_issues.md` (since deleted; fixed in PR #207) and
`plans/stream_array_alignment.md` are untouched and uncommitted.

## Next step

Nothing in the work order is left. From the plan itself, the next stages are:

- **Stage 0**, which was to run in parallel and did not: run `01_gain_clip.md`
  through Claude Code on a throwaway worktree with the tools that now exist,
  and record where it gets stuck. That is also the only way to measure the
  `browse` path in the paraphrase set, which is what decides Stage 1b.
- **Stage 5**, the action tools (`freeze_spec`, `run_build`, `synth_summary`,
  `vcd_measure`, `acceptance_report`) — the plan says Stage 0 confirms the list.
- **Stage 7**, the `freerun_bfm` frame, which is what will show what Stages 3
  to 5 quietly assumed about `stream_inband`. The scaffold already reads its
  template entirely from `frame.toml`, so a second frame should need no code.
