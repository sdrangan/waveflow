# Plan: the accelerator-lab agent surface: knowledge search, process, scaffold, and action tools

> **Status (2026-09-29): design plan, nothing built.** D1, D2 and D3 approved (all yes). Stage 1 is next.

## Motivation

A student writes a precise spec for an accelerator
([`example_stream_prompts/`](example_stream_prompts/)), and an AI tool the
student chooses (Claude, Codex or Gemini in VS Code, possibly over
Remote-SSH to NYU ECS) builds it **with the full Waveflow machinery**. The architecture comes from a
**frame** (see [Frames](#frames)). The first frame, and the only one the lab
needs, is `examples/stream_inband`'s. The tools themselves are not tied to
it. The lab teaches how to write a spec
and how to judge functional accuracy and timing. The same setup doubles as a
demo of the package.

An AI that has never seen Waveflow does not know `DataList`, `HostActivated`,
hooks, `SeqTB` or `BuildDag`. This plan gives it three things:

1. **The process.** What to do and in what order, delivered without relying
   on the model deciding to ask for it.
2. **Knowledge.** Search over the guide and the examples, exact lookup of
   "show me X in use", and whole examples to read.
3. **Actions.** Tools for the things an AI must not fake (measurements, the
   report) or reliably gets wrong (packing, layout).

## Principles

- **Derived, not hand-labeled.** No `teaches:` tags. Every piece of example
  metadata is computed from source (the usage index, module kinds, ports,
  hooks) or taken from text that already exists for humans (the example's
  docs `summary:` front-matter, or the module docstring). Hand labels go
  stale and answer only the questions their author anticipated.
- **Meaning comes from the guide; exact words come from BM25.** There are
  two ways to find things, and both are local, with no API key or service.
  - **Browsing** (`waveflow_browse`): the doc tree with every page's
    `title` + `summary`, so the *model* does the meaning-matching. The guide's
    summaries are the semantic index, and they are already written.
  - **BM25** over heading-sized chunks: for identifiers and Waveflow terms.

  A probe on 2026-09-29 showed why both are needed. BM25 found the right
  page for 2 of 6 queries phrased the way a newcomer would, avoiding
  Waveflow's words (for example "where do I put my own C++ for the math
  part" should find hooks). It was excellent whenever the query used
  Waveflow's words. A local embedding ranker (Stage 1b) is added **only if**
  the paraphrase tests still fail with browsing available.
- **Find in chunks, read whole.** Search returns pointers (file, heading,
  line range, snippet). Reading returns whole files.
- **No committed corpus copy.** The index is built in memory from the live
  source trees at server start. Nothing to go stale. Measured on 2026-09-29
  over the whole corpus (716 files, 5.4 MB, 2,623 chunks): 0.7 s with the
  files already cached, 4.7 s on a cold first read; a query takes about 1 ms. (The committed `waveflow/mcp/corpus/` already has.)
- **CLI first, MCP as a thin wrapper.** Every tool is a plain Python function
  with a CLI entry point. Any agent can reach it from a terminal, and MCP
  registration is one line on top.
- **Tools only where they earn it:** measurements the AI must not fake, and
  mechanical work it gets wrong. Writing C++ and Python is the AI's job.

## Frames

A **frame** is the unit of extension: one architecture an AI can be asked to
build in. It is a bundle, with no code of its own:

```
waveflow/mcp/frames/<frame>/
  frame.md        # protocol, errors, stages, comparisons, report (as plans/example_stream_prompts/frame.md)
  process.md      # what waveflow_get_process returns, and what the scaffold writes as AGENTS.md
  template/       # the project waveflow new-accel copies: runs as generated, function stubbed out
  prompts/        # example function specs for this frame
  frame.toml      # name, one-line synopsis, reference example(s)
```

Every tool is frame-agnostic:

- Stages 1, 2 and 5 do not know frames exist.
- Stage 3 takes the frame name: `waveflow_get_process(frame)`.
- Stage 4 takes it too: `new-accel --frame`.
- `waveflow_list_frames()` returns each `frame.toml`.

**Gate (from Stage 4 on):** every frame's template passes
`--through validate_csim` unmodified. It is parametrized over the frames, so
a new frame gets the gate for free.

| Frame | Architecture | When |
| --- | --- | --- |
| `stream_inband` | `HostActivated`, `VitisRegMap` parameters, persistent in-band loop, pre-loaded `SeqTB` | Stages 3 and 4 (the lab) |
| `freerun_bfm` | `FreeRunMod` with a concurrent BFM testbench, for designs where a later input depends on an earlier output | Stage 7: the **second instance** that tests the abstraction |

The first frame will quietly assume things that only a second one exposes.
That is the reason Stage 7 is in this plan and not deferred indefinitely.

## Where things stand

| Piece | State | Fate |
| --- | --- | --- |
| `waveflow/mcp/server.py`, `registry.py` | FastMCP + registry with profiles (`workspace` / `headless`) | **keep**, extend |
| `waveflow_get_schema_draft_plan` | static step list; its `task` arg only rewrites the summary | replace with the Stage 3 process guide |
| `waveflow_validate_schema` | parses and checks a schema source | **keep** |
| `waveflow_get_components` | component glossary | review in Stage 1; keep if accurate |
| `waveflow_rag_search_examples` + `cli_build_example_rag.py` + `example_rag.py` | OpenAI vector store | **remove** (decision D1) |
| `build_corpus.py` + committed `waveflow/mcp/corpus/` | out-of-date copy (still has `examples/conv2d`) | **remove** |
| `headless.py` | runs a prompt against the tools with no human; OpenAI client | keep as the eval harness; the client question is Stage 6 |
| `docs/guide/ai_tooling/rag.md`, `openai.md` | document the OpenAI path | rewrite with Stage 1 |

## The corpus

| Root | Chunked by | Notes |
| --- | --- | --- |
| `docs/guide/**/*.md` | `##`/`###` heading; front-matter `title` + `summary` on every chunk | the "how" |
| `docs/examples/**/*.md` | heading | the per-example tutorials |
| `examples/<name>/` **for the 14 TOC examples only** (D5), plus files the TOC pages link to directly | Python: top-level `class`/`def` (AST); C++/`.tpp`/`.h`: whole file, split at about 200 lines; `.tcl`, `.md`: whole file | the "what" |
| extra roots from config | as above | e.g. a course's own examples |

`examples/*/gen/` (generated C++) is **indexed but tagged `generated`** and
excluded from search by default. The agent may read it to see what codegen
produces, and every hit and fetch of it carries "generated; never hand-edit".

**Locating the roots.** For Stages 1 to 5 the index requires a checkout
(`pip install -e`), which is how both the course venv and the lab machines
install Waveflow. Shipping the corpus inside a wheel is deferred. When that
comes, it is a build-time bundle, never a committed copy.

## Stages

### Stage 0: baseline (no new tools), runs in parallel with Stage 1

Run `01_gain_clip.md` with Claude Code on this repo, with the current tools,
in a throwaway worktree. Record every place it got stuck, guessed, or
hand-wrote something that should have been generated. **Outputs:**

- the retrieval eval queries for Stage 1 ("what did it need to find?");
- the confirmed list of action tools for Stage 5;
- the list of Waveflow error messages that did not tell it what to do. That
  last list is a Waveflow fix list in its own right.

### Stage 1: search and read

New module `waveflow/mcp/knowledge/`:

- `corpus.py`: discover the roots, then chunk each file.
- `bm25.py`: an in-house BM25 of about 80 lines. The tokenizer is
  code-aware: it splits `snake_case` and `CamelCase` but also keeps the
  whole identifier, so `read_axi4_stream_lane` matches both as one token and
  as its parts.
- `usage.py`: the **usage index**, built by parsing source.
  - Python (AST): names imported from `waveflow.*` and every use of them;
    base classes (`HostActivated`, `FreeRunMod`, `SeqTB`, `DataList`, ...);
    decorators (`@synthesizable`, `@sim_only`); methods declared through
    hooks (`kernel_task`, `bfm_model`, ...).
  - C++: `#pragma HLS` lines, the `ns::fn` calls into generated utilities
    (`streamutils::`, `*_array_utils::`), and `#include`s of generated
    headers.
- `examples.py`: the per-example **card**, all derived. It contains the name;
  a synopsis (the `summary:` front-matter of `docs/examples/<name>/index.md`,
  falling back to the first paragraph of the main module's docstring); the
  module classes and their kinds; ports and their interface types; hook
  files; whether a build script exists; and the file list with the
  `generated` tags.

Tools, each also exposed as `waveflow kb <cmd>` on the CLI:

| Tool | Returns |
| --- | --- |
| `waveflow_browse(section=None)` | the doc tree under `section` (the top level when omitted): each page's path, `title`, `summary` (falling back to its first paragraph), and child sections. Also lists the example cards when `section="examples"` |
| `waveflow_search(query, scope="all"\|"docs"\|"examples", k=8)` | ranked hits: path, heading or symbol, line range, a snippet of about 3 lines, score |
| `waveflow_find_usage(symbol)` | every example and location using it, grouped by example; fuzzy suggestions on a miss |
| `waveflow_list_examples()` | every card |
| `waveflow_get_example(name, file=None)` | the card plus the file list, or one **whole** file |
| `waveflow_get_doc(path, heading=None)` | a whole doc page or one section |

**Gates (all non-toolchain, fast):**

- **A retrieval eval:** about 25 `(query, must-appear-in-top-5)` pairs,
  seeded from Stage 0 and from the example prompts. Examples: "error when
  TLAST arrives early", "register map parameter array",
  "`get_schema` vs `get_array`", "hand-written compute hook", "persistent
  loop END command", "pysim vs cosim timing tolerance". This is a pytest
  test, so ranking changes that break it fail CI.
- **Paraphrase tests,** kept separately and not required to pass in Stage 1.
  These are queries phrased as a newcomer would, without Waveflow's words,
  each with the page it should reach. They start with the six from the
  2026-09-29 probe (stop/`END`, own C++/hooks, cycle count/timing validation,
  CPU config/register map, packet ends too soon/TLAST, store then
  multiply/`vecmult`). Each one is marked by how it is reached: by BM25, or
  by the model through `waveflow_browse`. That second path is only
  measurable with an agent driving (Stage 0 / 6). These tests are what
  decide Stage 1b.
- **A usage-index accuracy test:** for three examples, the index equals a
  hand-checked list.
- **A card test:** every TOC example (D5) has an `example_dir:` that exists
  and a synopsis (its `summary:`). Every TOC page already has a `summary:`,
  because the TOC renders them.
- **An index build-time test:** under 3 s.

Retire the OpenAI RAG path and the committed corpus in the same stage (D1).

**Also in Stage 1:** give the 89 doc pages without a `summary:` a real one.
The first-paragraph fallback works, but the summaries are now the semantic
index, so each missing one is a retrieval gap.

### Stage 1b (conditional): local embeddings

Only if the paraphrase tests show that browsing plus BM25 still miss in
practice. Use a small ONNX embedding model (for example `fastembed` with
`bge-small`, about 70 MB, no PyTorch), fuse its ranking with BM25 by
reciprocal rank, and cache the vectors on disk (in the user cache dir,
keyed by chunk content hash, re-embedding only changed chunks). Embedding
all 2,600 chunks on a CPU takes tens of seconds, so the cache is required,
which is why this stage is conditional.

### Stage 2: wire the server

- Set FastMCP **`instructions`**. This is a short text most clients inject
  into context: what Waveflow is, the tool families, and "for a new
  accelerator, start with `waveflow_get_process('accelerator')`".
- Register the Stage 1 tools in both profiles.
- Update `docs/guide/ai_tooling/` (setup for Claude Code, Codex and Gemini in
  VS Code, including Remote-SSH).
- **Gate:** a smoke test that starts the server over stdio, lists the tools,
  and calls each one.

### Stage 3: the process

- `waveflow_get_process(kind)`, where `kind` is `"accelerator"` (and later
  others). It returns the frame steps, which tool to use at each one, and
  the freeze rules. The **source is a markdown file in the package**, the
  same text the scaffold's `AGENTS.md` carries, so the two cannot drift.
- Replaces `waveflow_get_schema_draft_plan` (D2).

### Stage 4: the scaffold

- `waveflow new-accel <name> --template stream_inband` (CLI) and
  `waveflow_new_accel_project` (MCP). It writes a project that **runs as
  generated**: `stream_inband`, renamed, with the function stubbed out
  (an identity compute) and the schemas marked TODO. The contents:
  - `spec/` Stage 1 stubs: `oracle.py`, `scenarios.py`, `check.py` (the
    comparison machinery is written; the function is a TODO);
  - `AGENTS.md` (the Stage 3 process text) plus a one-line `CLAUDE.md` and
    `GEMINI.md` that point to it;
  - a copy of `frame.md`.
- **Gate:** a freshly scaffolded project passes `--through validate_csim`
  unmodified (`-m vitis`), and pysim passes without Vitis.

### Stage 5: action tools (the list is confirmed by Stage 0)

Candidates:

- `waveflow_freeze_spec` / `check_frozen`: hash `spec/`, and have later
  tools refuse to run on a mismatch.
- `waveflow_run_build(through=step)`: `run_dag_cli` as a job you start and
  then poll, which returns step status and log paths.
- `waveflow_synth_summary`: II, latency, resources and estimated clock as
  data.
- `waveflow_vcd_measure`: handshake-to-handshake cycle counts.
- `waveflow_acceptance_report`: fills in the criteria table from
  **machine-readable criteria** (`spec/criteria.yaml`, drafted in Stage 1 of
  the lab, approved by the student) and the measurements. The AI never
  writes its own PASS column.

### Stage 6: agent eval

Run prompts 00 to 03 through the harness on at least two model families.
Score: did it call the process tool; which examples did it fetch; did
Stage 1 of the lab come out right; did it hand-edit generated files; did the
three comparisons pass. `headless.py` uses an OpenAI client today. Decide
then whether to extend it or to drive real VS Code agents by hand (D4).

### Stage 7: the second frame (`freerun_bfm`)

Build the `freerun_bfm` frame: its template, `frame.md`, process text, and one
example prompt with a closed-loop dependency. Also fix whatever it shows
Stages 3 to 5 assumed about `stream_inband`. Choosing a frame automatically
from a spec is a separate question, closer to design-space exploration, and
is out of scope. For now the course or the student names the frame.

## Decisions for review

- **D1 (APPROVED).** Remove the OpenAI RAG path, the `build_corpus` CLI and the
  committed `waveflow/mcp/corpus/`. (Recommended: yes. Nothing else depends
  on them; the saved notes already mark the corpus for a rework.)
- **D2 (APPROVED).** Remove `waveflow_get_schema_draft_plan` once Stage 3 lands, rather
  than keep it alongside. (Recommended: yes.)
- **D3 (APPROVED).** Tool naming: keep the `waveflow_` prefix and `snake_case`.
  (Recommended: yes; it matches the existing tools.)
- **D4.** The eval harness in Stage 6: extend `headless.py` to more
  providers, or evaluate by hand in VS Code. (Defer until Stage 6.)
- **D5 (DECIDED).** The corpus examples are **exactly those in the docs TOC**
  (`docs/examples/*/index.md`), which is 14 as of 2026-09-29. The TOC is the
  user's curation of which examples are worth following, and some older
  `examples/` directories are not. Each TOC page's front matter gets an
  `example_dir:` key, with a test requiring it. Files a TOC page links to
  outside its directory are included too. Everything else under `examples/`
  is outside the corpus.

## Not in this plan

- Shipping the corpus in a wheel (a build-time bundle, later).
- Embeddings, except as the conditional Stage 1b.
- The `FreeRunMod` + BFM testbench frame, for designs where a later input
  depends on an earlier output.
- Grading and rubrics.
