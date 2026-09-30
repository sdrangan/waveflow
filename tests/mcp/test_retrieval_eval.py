"""The retrieval eval: queries that must find their page.

This is the gate that makes the ranking safe to change.  BM25 weights, the
tokenizer's identifier splitting, the per-file cap in ``waveflow_search`` --
every one of them is a judgement call that looked right on the six queries it
was tuned on, and the only way to change one later without quietly breaking
the other twenty is to write the twenty down.

Two sets, kept apart because they mean different things:

* :func:`test_query_finds_page` -- queries in **Waveflow's own words**.  These
  must pass.  A failure is a regression in the index.
* ``test_paraphrase`` in ``test_paraphrase.py`` -- queries phrased the way a
  newcomer would, avoiding Waveflow's vocabulary.  Those are ``xfail`` and
  decide whether Stage 1b (local embeddings) is needed at all.

Each case names the *paths* that may appear in the top 5, not a rank, because
which section of a page wins is exactly the kind of detail that should be free
to change.
"""
from __future__ import annotations

import pytest

from waveflow.mcp.knowledge import waveflow_search

#: ``(query, acceptable path prefixes)`` -- the top 5 must contain at least one.
#:
#: Seeded from the example prompts in ``plans/example_stream_prompts/`` and
#: from the plan's own list.  A query is worth adding here when an agent
#: building an accelerator would actually type it.
#:
#: Several entries list more than one prefix, and that is a statement about the
#: docs rather than a loosened assertion: "write a kernel task body in C++" is
#: answered both by ``custom_hooks/`` (the mechanism) and by
#: ``comp_codegen/freerunning.md`` (the free-running task body), and an index
#: that returns either has done its job.  A single prefix means there is one
#: right page and the eval holds the ranking to it.
QUERIES: list[tuple[str, tuple[str, ...]]] = [
    # --- the protocol and the stream -----------------------------------
    ("error when TLAST arrives early", ("docs/guide/custom_hooks/stream.md",)),
    ("persistent loop END command", ("docs/examples/stream_inband/index.md",)),
    ("in-band command header ahead of the samples", ("docs/examples/stream_inband/",)),
    (
        "AXI4-Stream master and slave ports on a module",
        ("docs/guide/interface/", "docs/guide/comp_codegen/interface.md"),
    ),
    (
        "halted error tx_id status after a failure",
        # Two right answers: the register-map fields are declared in `poly.py`,
        # and what they mean after a halt is documented across the example's
        # own pages.
        ("examples/stream_inband/poly.py", "docs/examples/stream_inband/"),
    ),
    # --- the register map ----------------------------------------------
    ("register map parameter array", ("docs/guide/interface/primitive/regmap.md",)),
    ("VitisRegMap RegField offsets", ("docs/guide/interface/primitive/regmap.md",)),
    ("ap_start ap_done launch protocol", ("docs/guide/comp_codegen/",)),
    # --- schemas ---------------------------------------------------------
    ("DataList schema definition", ("docs/guide/schema/python/datalists.md",)),
    ("DataArray fixed size array schema", ("docs/guide/schema/python/dataarrays.md",)),
    ("fixed point rounding mode quantization", ("docs/guide/schema/python/fixpoint.md",)),
    ("EnumField generated C++ enum header", ("docs/guide/schema/",)),
    ("generate include files for a schema", ("docs/guide/schema/hls/codegen.md",)),
    # --- hooks: the AI's own C++ -----------------------------------------
    ("hand-written compute hook", ("docs/guide/custom_hooks/",)),
    ("synthesizable decorator stub form", ("docs/guide/custom_hooks/",)),
    (
        "write a kernel task body in C++",
        ("docs/guide/custom_hooks/", "docs/guide/comp_codegen/freerunning"),
    ),
    # --- simulation and testbenches --------------------------------------
    ("SeqTB sequential testbench", ("docs/guide/comp_codegen/testbench.md",)),
    ("HostActivated host launched module", ("docs/guide/comp_codegen/",)),
    ("FreeRunMod free running task", ("docs/guide/",)),
    # --- build and timing --------------------------------------------------
    ("BuildDag build steps run_dag_cli", ("docs/guide/build/", "docs/guide/flows/")),
    ("pysim vs cosim timing tolerance", ("docs/guide/timing/cosim_timing.md",)),
    ("cycle count from a VCD waveform", ("docs/guide/timing/",)),
    ("csynth II latency resource report", ("docs/guide/",)),
    # --- the examples themselves ------------------------------------------
    ("store then multiply a vector", ("docs/examples/vecmult/",)),
    ("polynomial accelerator example", ("docs/examples/stream_inband/",)),
]


@pytest.fixture(scope="module")
def search():
    # One index build for the whole module; `get_index` caches per process.
    waveflow_search("warm the index", k=1)
    return waveflow_search


@pytest.mark.parametrize("query,expected", QUERIES, ids=[q for q, _ in QUERIES])
def test_query_finds_page(search, query: str, expected: tuple[str, ...]) -> None:
    hits = search(query, k=5)["hits"]
    paths = [h["path"] for h in hits]
    assert any(p.startswith(expected) for p in paths), (
        f"{query!r} did not reach {expected!r} in the top 5.\n"
        + "\n".join(f"  {h['path']}  [{h['score']}]  {h['heading']}" for h in hits)
    )


def test_scope_restricts_results() -> None:
    docs = waveflow_search("TLAST", scope="docs", k=10)["hits"]
    assert docs and all(h["path"].startswith("docs/") for h in docs)

    examples = waveflow_search("TLAST", scope="examples", k=10)["hits"]
    assert examples and all(h["path"].startswith("examples/") for h in examples)


def test_generated_code_is_excluded_by_default() -> None:
    """Generated C++ is readable on request, never a default search result."""
    default = waveflow_search("poly evaluate kernel", k=20)["hits"]
    assert not any(h.get("note") for h in default), (
        "a default search returned a file tagged as generated or build output"
    )

    with_gen = waveflow_search(
        "poly evaluate kernel", k=20, include_generated=True
    )["hits"]
    tagged = [h for h in with_gen if h.get("note")]
    assert tagged, "include_generated=True returned no generated files at all"
    assert all(h["note"] for h in tagged)


def test_bad_scope_is_an_error_not_an_exception() -> None:
    result = waveflow_search("anything", scope="guide")
    assert "error" in result and "scope" in result["error"]
