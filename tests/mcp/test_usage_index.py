"""Usage-index accuracy: three examples, checked against a hand-made list.

The index is derived by parsing, so the failure mode is not "a label went
stale" but "the parser quietly stopped seeing a form".  A decorator written
``@synthesizable(...)`` instead of ``@synthesizable``, a base class reached
through a module alias, a port assigned in a helper rather than ``__init__``:
each of those drops entries with no error anywhere.

So the check is an exact set for three examples, read off the source by hand
rather than generated from the index.  ``regmap`` is the smallest
host-activated design, ``stream_inband`` is the one the lab's frame is modeled
on, and ``vecmult`` is a free-running composite -- between them they cover
every form the Python parser looks for.
"""
from __future__ import annotations

import pytest

from waveflow.mcp.knowledge import get_index, waveflow_find_usage


@pytest.fixture(scope="module")
def index():
    return get_index()


# ---------------------------------------------------------------------------
# Hand-checked expectations
# ---------------------------------------------------------------------------

#: ``example -> file -> the classes it defines that the index should classify``.
#: Read off the ``class X(Base):`` lines of each source.
EXPECTED_MODULES: dict[str, dict[str, str]] = {
    "regmap": {
        "SimpFun": "host-activated module",
        "SimpFunHost": "simulation object",
        "SimpFunTBHls": "sequential testbench",
    },
    "stream_inband": {
        "PolyAccel": "host-activated module",
        "PolyTB": "simulation object",
        "CoeffArray": "schema (array)",
        "PolyCmdHdr": "schema (list)",
        "PolyRespHdr": "schema (list)",
    },
    # A free-running composite, spread over two files: the design in
    # `vecmult.py`, the testbench and its stimulus in `vecmult_sim.py`.
    "vecmult": {
        "VecCmd": "schema (list)",
        "VecResp": "schema (list)",
        "VecMult": "free-running module",
        "VecSource": "simulation object",
        "VecCapture": "simulation object",
        "VecMultTB": "free-running module",
    },
}

#: Ports, by the class that declares them.  ``poly.py`` declares them in
#: ``__init__`` as ``self.<name> = <Ctor>(...)``; the index must attribute each
#: to its own class and not to every class in the file.
EXPECTED_PORTS: dict[str, dict[str, set[str]]] = {
    "stream_inband": {
        "PolyAccel": {"s_in", "m_out", "s_lite"},
        "PolyTB": {"m_in", "s_out", "m_lite"},
    },
    # Two classes in one file each declaring a port of the same name: the
    # check that attribution is per class and not per file.
    "vecmult": {
        "VecMult": {"s_in", "z_out"},
        "VecSource": {"stream_ep"},
        "VecCapture": {"stream_ep"},
    },
}


def test_module_kinds_match_the_source(index) -> None:
    for example, expected in EXPECTED_MODULES.items():
        card = index.cards[example]
        got = {m.name: m.kind for m in card.modules}
        assert got == expected, f"{example}: module classification drifted"


def test_ports_belong_to_the_class_that_declares_them(index) -> None:
    for example, expected in EXPECTED_PORTS.items():
        card = index.cards[example]
        by_class = {m.name: {p for p, _ in m.ports} for m in card.modules}
        for cls, ports in expected.items():
            assert by_class.get(cls) == ports, f"{example}.{cls}: wrong ports"
        # The schemas in the same file must carry none of them.
        for module in card.modules:
            if module.name not in expected:
                assert not module.ports, (
                    f"{example}.{module.name} is a {module.kind} and should "
                    f"declare no ports, got {module.ports}"
                )


def test_hostactivated_subclasses_are_exactly_these_three() -> None:
    """``HostActivated`` is subclassed in three TOC examples and nowhere else."""
    result = waveflow_find_usage("HostActivated")
    assert result["found"]
    bases = {
        example
        for example, uses in result["by_example"].items()
        for u in uses
        if u["how"] == "base"
    }
    assert bases == {"regmap", "shared_mem", "stream_inband"}


def test_kernel_body_in_stream_inband() -> None:
    """``cpp_body = "body"`` names ``PolyAccel``'s whole kernel body: a body-only kernel.

    The index records it where it records ``@synthesizable`` -- both say "this method's
    C++ is hand-written" -- so ``find_usage("cpp_body")`` finds the declaration and the
    card lists the body's ``.tpp``.
    """
    result = waveflow_find_usage("cpp_body")
    assert result["found"]
    assert any(u["path"] == "examples/stream_inband/poly.py"
               for u in result["by_example"]["stream_inband"])


def test_hook_body_is_the_tpp_not_the_generated_headers() -> None:
    """The card names the hand-written body and nothing from ``include/``.

    ``examples/stream_inband/include/`` holds twenty-odd generated schema
    headers with the same extensions as a hook.  Listing them as hooks would
    point an agent straight at files it must never edit.
    """
    card = get_index().cards["stream_inband"]
    assert card.hook_files == ["examples/stream_inband/poly_body_impl.tpp"]


def test_cpp_pragmas_and_namespace_calls_are_indexed() -> None:
    pragma = waveflow_find_usage("#pragma HLS pipeline")
    assert pragma["found"] and pragma["n_uses"] > 0

    ns = waveflow_find_usage("streamutils::")
    assert ns["found"] and "stream_inband" in ns["by_example"]


def test_a_miss_suggests_close_names() -> None:
    result = waveflow_find_usage("HostActivatd")
    assert not result["found"]
    assert "HostActivated" in result["suggestions"]


def test_generated_files_are_excluded_from_usage_by_default(index) -> None:
    default = waveflow_find_usage("streamutils::")
    with_gen = waveflow_find_usage("streamutils::", include_generated=True)
    assert with_gen["n_uses"] > default["n_uses"], (
        "no generated uses at all -- either gen/ is missing from the index or "
        "the default filter is not doing anything"
    )
    tracked = index.corpus.tracked
    shown = {
        u["path"]
        for uses in default["by_example"].values()
        for u in uses
    }
    assert all(p in tracked for p in shown)
