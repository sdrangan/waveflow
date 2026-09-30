You are working unattended overnight on the Waveflow repo. There is no human to answer questions until morning. Get as far as you can through the plan below, and leave a clear record of what you did.

## Read first

1. `plans/accel_mcp.md`: the plan. You are implementing it. Decisions D1, D2 and D3 are APPROVED.
2. `plans/example_stream_prompts/` (the README and `frame.md`): what the tools are for.
3. `CLAUDE.md`, and `waveflow/mcp/` (`server.py`, `registry.py`, `schema_tools.py`, `example_rag.py`, `headless.py`, `build_corpus.py`).

## Setup

- The Python venv is `../pysilicon-venv` (a sibling of the repo). Run everything with `../pysilicon-venv/Scripts/python.exe` (`-m pytest`, and so on). A bare `pytest` or `python` may pick up the wrong interpreter and report "0 failed" because nothing ran.
- Create the branch `accel-mcp` from `main`. The untracked files `plans/accel_mcp.md`, `plans/accel_mcp_overnight_prompt.md` and `plans/example_stream_prompts/` come along. Make them your first commit. **Do not commit `plans/waveflow_issues.md`**; it belongs to the user.
- Before changing anything, record the baseline: `../pysilicon-venv/Scripts/python.exe -m pytest -m "not vitis and not xsi" -q -p no:cacheprovider 2>&1 | tail -30`. A few failures already exist; a note says about 6. Write the baseline failing test IDs into the progress file. Your job is to add no *new* failures. Do not fix unrelated existing ones.
- Commit often, one logical step per commit, on this one branch. **Do not push and do not open a PR.** End every commit message with:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`

## Work order (stop wherever the night ends; each item should leave the tree green)

1. **Stage 1: search and read** (`waveflow/mcp/knowledge/`). The corpus, an in-house BM25 with a code-aware tokenizer, the usage index (Python AST + C++ pragmas / `ns::fn` calls / generated-header includes), the example cards, and the tools `waveflow_browse`, `waveflow_search`, `waveflow_find_usage`, `waveflow_list_examples`, `waveflow_get_example`, `waveflow_get_doc`. Each tool is a plain function with a `waveflow kb <cmd>` CLI entry; register them in `registry.py`.
   - **Which examples (D5, DECIDED): only the examples in the docs TOC.** An example is in the corpus if and only if it has a page under `docs/examples/<doc>/index.md`. Some `examples/` directories are older and must NOT be offered to the agent as models. Rules:
     - Each `docs/examples/*/index.md` gets a new front-matter key `example_dir:` naming its source directory. Three doc names differ from their directories: `memcpy` → `examples/mem_copy`, `firblock` → `examples/fir_block`, `mmqueue` → `examples/vmac`. The other 11 have the same name.
     - Add a test that fails if a TOC page lacks `example_dir:` or names a directory that does not exist.
     - Individual `examples/...` files that a TOC page links to directly are included even when they are outside that directory. Today these are `examples/schemas/fixedpoint/` from `basic_vec` and `examples/interface/aximm_queue_demo.py` from `mmqueue`.
     - Everything else under `examples/` is out of the corpus: not searchable, not in `waveflow_list_examples`, and not fetchable by `waveflow_get_example`.
     - `docs/guide/` is indexed in full.
     - Put the resulting 14-example list in the progress file.
   - **Tests** (non-toolchain, fast):
     - the retrieval eval, about 25 `(query, must-be-in-top-5)` pairs;
     - the paraphrase set, the six queries in the plan, marked `xfail(strict=False)` with the expected page and the expected path (BM25 or browse);
     - usage-index accuracy on 3 examples;
     - the card/synopsis test;
     - index build time under 3 s.

     A 60-line prototype already got the right page for queries phrased in Waveflow's own words, so a correct implementation should too.
2. **D1 removal.** Delete `waveflow_rag_search_examples`, `example_rag.py`, `cli_build_example_rag.py`, `build_corpus.py`, the committed `waveflow/mcp/corpus/`, and their `pyproject.toml` entry points. Fix every reference: tests, `headless.py`, docs. `grep` the repo with `git ls-files | xargs grep -a -l` (plain `grep -I` is unreliable here). Rewrite `docs/guide/ai_tooling/rag.md` and `openai.md` to describe the local search, or fold them into one page. Keep the docs front-matter conventions (`title`, `parent`, `nav_order`, `summary`); a docs test checks nav titles.
3. **Stage 2: wire the server.** FastMCP `instructions` text, all tools registered in both profiles, and a smoke test that starts the server over stdio, lists the tools, and calls each Stage 1 tool once.
4. **Stage 3 and the frames layout.** Create `waveflow/mcp/frames/stream_inband/`:
   - `frame.md` (copy from `plans/example_stream_prompts/frame.md`);
   - `process.md` (the ordered steps and which tool to use at each; Stage 1 = the frozen spec, Stage 2 = the implementation; the rules);
   - `prompts/` (01 to 03);
   - `frame.toml`.

   Add `waveflow_list_frames` and `waveflow_get_process(frame)`. Then remove `waveflow_get_schema_draft_plan` (D2), fixing its references. Make sure `pyproject.toml` package-data ships the frames directory.
5. **Stage 4 scaffold, only if time remains.** `waveflow new-accel <name> --frame stream_inband`: copy `examples/stream_inband`, rename it, stub the compute to identity, add `spec/` stubs, `AGENTS.md` (= `process.md`), and one-line `CLAUDE.md` / `GEMINI.md` pointers.
   - The non-Vitis gate: the scaffolded project's pysim runs.
   - Vitis HLS 2025.1 is installed, so also try `--through validate_csim` (it takes minutes; run it in the background).
   - If the scaffold cannot be made to work cleanly, stop, and describe exactly why in the progress file.
6. **Doc summaries, last, in their own commit(s).** Add a `summary:` front-matter line to docs pages under `docs/guide` and `docs/examples` that lack one (about 89). Each is one or two factual sentences drawn from the page's own content, in the style of the existing summaries. Do not change page bodies. The user reviews docs closely, so keep these commits separate and easy to revert.

## Rules

- Stay inside this scope. Do not refactor unrelated code, touch `examples/` sources (except reading them), or "fix" existing test failures outside the MCP area.
- **If you hit a decision that is genuinely the user's,** write it in the progress file under "Decisions needed", choose the most reversible option, and continue with independent work. Never stop and wait.
- If a step fails repeatedly, do not loop. Record what you tried and the exact error, then move to the next independent item.
- Do not run `-m xsi`. Run `-m vitis` only for the item 5 scaffold check.
- Before each commit, run the fast suite (`-m "not vitis and not xsi"`) and compare it with the baseline.

## Progress file

Keep `plans/accel_mcp_progress.md` updated as you go, and commit it with each step. It should contain:

- the baseline failures;
- per stage: done / partial / not started, with commit hashes;
- test results;
- the proposed D5 exclude list;
- the "Decisions needed" list;
- anything in the plan that turned out to be wrong;
- the next step.

The user will read this file first in the morning. Also update the status line at the top of `plans/accel_mcp.md`.
