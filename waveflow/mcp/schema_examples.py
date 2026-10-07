"""
Schema example registry for waveflow MCP tools.

Provides ``list_schema_examples`` and ``get_schema_example`` backed by curated
source files stored alongside this package, so the examples stay executable
and maintainable rather than being embedded as large inline strings.
"""
from __future__ import annotations

from importlib import resources
from typing import Any


# ---------------------------------------------------------------------------
# Example metadata catalog
# ---------------------------------------------------------------------------

# Each entry describes one curated schema example.  The ``file`` field names
# a .py file inside the ``waveflow.mcp.resources.schema_examples`` package.
# ``primary_symbol`` is the main class being demonstrated; the rest of the
# required definitions are listed under ``supporting_symbols``.

_EXAMPLES: dict[str, dict[str, Any]] = {
    "poly_cmd_hdr": {
        "id": "poly_cmd_hdr",
        "title": "Polynomial accelerator command header",
        "description": (
            "Command header sent to the polynomial accelerator containing the "
            "command type, a transaction ID, the number of input samples to process, "
            "and the polynomial coefficients the command is evaluated with."
        ),
        "features": [
            "DataList",
            "nested DataArray",
            "FloatField specialization",
            "static array",
            "transaction ID pattern",
        ],
        "file": "poly.py",
        "primary_symbol": "PolyCmdHdr",
        "supporting_symbols": ["PolyCmdType", "PolyCmdTypeField", "TxIdField", "NsampField",
                               "Float32", "CoeffArray"],
    },
    "poly_resp_hdr": {
        "id": "poly_resp_hdr",
        "title": "Polynomial accelerator response header",
        "description": (
            "Response header returned by the polynomial accelerator, echoing the "
            "transaction ID so the host can correlate responses with commands."
        ),
        "features": [
            "DataList",
            "transaction ID echo pattern",
            "minimal single-field schema",
        ],
        "file": "poly.py",
        "primary_symbol": "PolyRespHdr",
        "supporting_symbols": ["TxIdField"],
    },
    "poly_error": {
        "id": "poly_error",
        "title": "Polynomial accelerator error codes",
        "description": (
            "The error codes the polynomial accelerator reports in its halted/error/tx_id "
            "status registers when a command fails: an IntEnum wrapped as an EnumField."
        ),
        "features": [
            "enum field",
            "IntEnum error codes",
            "EnumField specialization",
        ],
        "file": "poly.py",
        "primary_symbol": "PolyErrorField",
        "supporting_symbols": ["PolyError"],
    },
    "hist_cmd": {
        "id": "hist_cmd",
        "title": "Histogram accelerator command",
        "description": (
            "Command descriptor for the histogram accelerator, referencing input "
            "data, bin-edge, and output count buffers via memory addresses."
        ),
        "features": [
            "DataList",
            "MemAddr fields",
            "multiple address fields",
            "integer count fields",
            "transaction ID pattern",
        ],
        "file": "hist.py",
        "primary_symbol": "HistCmd",
        "supporting_symbols": ["TxIdField", "AddrField", "NdataField", "NbinField"],
    },
    "hist_resp": {
        "id": "hist_resp",
        "title": "Histogram accelerator response",
        "description": (
            "Response descriptor returned by the histogram accelerator, containing "
            "the echoed transaction ID and a status enum."
        ),
        "features": [
            "DataList",
            "enum field",
            "IntEnum status codes",
            "transaction ID echo pattern",
        ],
        "file": "hist.py",
        "primary_symbol": "HistResp",
        "supporting_symbols": ["TxIdField", "HistError", "HistErrorField"],
    },
    "conv2d_cmd": {
        "id": "conv2d_cmd",
        "title": "2-D convolution accelerator command",
        "description": (
            "Command descriptor for the conv2d accelerator, specifying image "
            "dimensions, kernel size, and memory addresses for input image, "
            "output image, and kernel data."
        ),
        "features": [
            "DataList",
            "MemAddr fields",
            "multiple address fields",
            "dimension fields",
            "custom include_filename",
        ],
        "file": "conv2d.py",
        "primary_symbol": "Conv2DCmd",
        "supporting_symbols": ["AddrField", "NrowField", "NcolField", "KernelSizeField"],
    },
    "conv2d_resp": {
        "id": "conv2d_resp",
        "title": "2-D convolution accelerator response",
        "description": (
            "Response descriptor for the conv2d accelerator, returning a single "
            "enum error code indicating the outcome of the operation."
        ),
        "features": [
            "DataList",
            "enum field",
            "minimal single-field schema",
            "custom include_filename",
        ],
        "file": "conv2d.py",
        "primary_symbol": "Conv2DResp",
        "supporting_symbols": ["Conv2DError", "Conv2DErrorField"],
    },
    "conv2d_debug": {
        "id": "conv2d_debug",
        "title": "2-D convolution accelerator debug event",
        "description": (
            "Debug schema for the conv2d accelerator, combining a row index with "
            "an event enum to allow the testbench to trace accelerator internals "
            "during co-simulation."
        ),
        "features": [
            "DataList",
            "enum field",
            "debug / co-simulation pattern",
            "narrow enum bitwidth",
            "custom include_filename",
        ],
        "file": "conv2d.py",
        "primary_symbol": "Conv2DDebug",
        "supporting_symbols": ["NrowField", "Conv2DEvent", "Conv2DEventField"],
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_schema_examples() -> dict:
    """Return a compact catalog of available schema examples.

    Returns a dict with a ``summary`` string and an ``examples`` list. Each
    entry in the list contains the example ``id``, ``title``, ``description``,
    and ``features``.
    """
    examples = [
        {
            "id": meta["id"],
            "title": meta["title"],
            "description": meta["description"],
            "features": meta["features"],
        }
        for meta in _EXAMPLES.values()
    ]
    return {
        "summary": (
            "Curated waveflow schema examples. Inspect these before authoring a new "
            "DataList or DataArray schema so you can adapt a proven pattern."
        ),
        "examples": examples,
    }


def get_schema_example(example_id: str) -> dict:
    """Return the full content for a schema example by ID.

    Parameters
    ----------
    example_id:
        One of the IDs returned by ``list_schema_examples``.

    Returns
    -------
    dict
        Metadata fields plus ``source_code`` (full curated source file) and
        ``primary_symbol`` / ``supporting_symbols`` to orient the reader.

    Raises
    ------
    ValueError
        If *example_id* is not found in the registry.
    """
    meta = _EXAMPLES.get(example_id)
    if meta is None:
        known = sorted(_EXAMPLES.keys())
        raise ValueError(
            f"Unknown schema example id: {example_id!r}. Known ids: {known}"
        )

    source_code = _read_example_file(meta["file"])

    return {
        "id": meta["id"],
        "title": meta["title"],
        "description": meta["description"],
        "features": meta["features"],
        "primary_symbol": meta["primary_symbol"],
        "supporting_symbols": meta["supporting_symbols"],
        "source_file": meta["file"],
        "source_code": source_code,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_EXAMPLES_PACKAGE = "waveflow.mcp.resources.schema_examples"


def _read_example_file(filename: str) -> str:
    """Read a curated example source file from the package resources."""
    try:
        return (
            resources.files(_EXAMPLES_PACKAGE)
            .joinpath(filename)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(
            f"Failed to read schema example file {filename!r}: {exc}"
        ) from exc
