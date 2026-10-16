"""Local search and read over the Waveflow guide and its reference examples.

No API key, no service, no committed corpus: the index is built in memory from
the live checkout at server start, and a query takes about a millisecond.

Two ways to find things, because a probe on 2026-09-29 showed that neither is
enough alone.  :func:`waveflow_browse` hands the model every page's title and
summary so it does the meaning-matching; :func:`waveflow_search` is BM25 over
heading-sized chunks, which is excellent as soon as a query uses Waveflow's own
words and poor when it avoids them.
"""
from waveflow.mcp.knowledge.index import KnowledgeIndex, get_index
from waveflow.mcp.knowledge.tools import (
    waveflow_browse,
    waveflow_find_usage,
    waveflow_get_doc,
    waveflow_get_example,
    waveflow_list_examples,
    waveflow_search,
)

__all__ = [
    "KnowledgeIndex",
    "get_index",
    "waveflow_browse",
    "waveflow_search",
    "waveflow_find_usage",
    "waveflow_list_examples",
    "waveflow_get_example",
    "waveflow_get_doc",
]
