"""
Tests for waveflow MCP tools: schema_examples, registry, server, and mode-aware tool exposure.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from waveflow.mcp.components import get_components
from waveflow.mcp.schema_examples import get_schema_example, list_schema_examples
from waveflow.mcp.registry import REGISTRY, ToolRegistry
from waveflow.mcp.schema_tools import validate_schema, validate_schema_from_file


# ---------------------------------------------------------------------------
# list_schema_examples
# ---------------------------------------------------------------------------


def test_list_schema_examples_returns_dict_with_examples_key():
    result = list_schema_examples()
    assert "examples" in result
    assert "summary" in result


def test_list_schema_examples_returns_all_expected_ids():
    result = list_schema_examples()
    ids = {ex["id"] for ex in result["examples"]}
    expected = {
        "poly_cmd_hdr",
        "poly_resp_hdr",
        "poly_resp_ftr",
        "hist_cmd",
        "hist_resp",
        "conv2d_cmd",
        "conv2d_resp",
        "conv2d_debug",
    }
    assert ids == expected


def test_list_schema_examples_each_entry_has_required_fields():
    result = list_schema_examples()
    for entry in result["examples"]:
        assert "id" in entry
        assert "title" in entry
        assert "description" in entry
        assert "features" in entry
        assert isinstance(entry["features"], list), (
            f"features for {entry['id']!r} should be a list"
        )


# ---------------------------------------------------------------------------
# get_schema_example
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "example_id",
    [
        "poly_cmd_hdr",
        "poly_resp_hdr",
        "poly_resp_ftr",
        "hist_cmd",
        "hist_resp",
        "conv2d_cmd",
        "conv2d_resp",
        "conv2d_debug",
    ],
)
def test_get_schema_example_returns_expected_fields(example_id: str):
    result = get_schema_example(example_id)
    assert result["id"] == example_id
    assert "title" in result
    assert "description" in result
    assert "features" in result
    assert isinstance(result["features"], list)
    assert "primary_symbol" in result
    assert "supporting_symbols" in result
    assert isinstance(result["supporting_symbols"], list)
    assert "source_code" in result
    assert len(result["source_code"]) > 0


def test_get_schema_example_source_code_contains_primary_symbol():
    for example_id in ("poly_cmd_hdr", "hist_resp", "conv2d_debug"):
        result = get_schema_example(example_id)
        assert result["primary_symbol"] in result["source_code"], (
            f"primary_symbol {result['primary_symbol']!r} not found in source_code "
            f"for example {example_id!r}"
        )


def test_get_schema_example_unknown_id_raises_value_error():
    with pytest.raises(ValueError, match="Unknown schema example id"):
        get_schema_example("definitely_not_a_real_id")


def test_get_schema_example_poly_resp_ftr_includes_enum_support():
    result = get_schema_example("poly_resp_ftr")
    assert "PolyError" in result["supporting_symbols"]
    assert "PolyErrorField" in result["supporting_symbols"]
    # Source code should define the enum and the DataList
    assert "PolyError" in result["source_code"]
    assert "PolyRespFtr" in result["source_code"]


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


def test_registry_has_expected_tools():
    all_names = {s["function"]["name"] for s in REGISTRY.tool_schemas()}
    # Curated example tools were removed
    assert "waveflow_list_schema_examples" not in all_names
    assert "waveflow_get_schema_example" not in all_names
    # Old search aliases were removed
    assert "waveflow_search_examples" not in all_names
    assert "waveflow_search_schema_examples" not in all_names
    # Domain tools present in all profiles
    # The static schema-draft plan was replaced by the frame process (D2)
    assert "waveflow_get_schema_draft_plan" not in all_names
    assert "waveflow_get_process" in all_names
    assert "waveflow_list_frames" in all_names
    assert "waveflow_validate_schema" in all_names
    assert "waveflow_get_components" in all_names
    # The OpenAI RAG tool was removed with D1; local search replaced it
    assert "waveflow_rag_search_examples" not in all_names
    assert "waveflow_search" in all_names
    assert "waveflow_browse" in all_names
    assert "waveflow_find_usage" in all_names
    assert "waveflow_list_examples" in all_names
    assert "waveflow_get_example" in all_names
    assert "waveflow_get_doc" in all_names
    # File tools not in the registry (they are added dynamically by build_mcp)
    assert "list_files" not in all_names
    assert "read_file" not in all_names
    assert "write_file" not in all_names
    assert "edit_file" not in all_names


def test_registry_workspace_profile_has_only_domain_tools():
    workspace_names = {s["function"]["name"] for s in REGISTRY.tool_schemas(profile="workspace")}
    assert "waveflow_get_process" in workspace_names
    assert "waveflow_validate_schema" in workspace_names
    assert "waveflow_get_components" in workspace_names
    assert "waveflow_search" in workspace_names
    assert "list_files" not in workspace_names


def test_registry_headless_profile_has_knowledge_and_domain_tools():
    headless_names = {s["function"]["name"] for s in REGISTRY.tool_schemas(profile="headless")}
    assert "waveflow_get_process" in headless_names
    assert "waveflow_validate_schema" in headless_names
    assert "waveflow_get_components" in headless_names
    assert "waveflow_search" in headless_names


def test_registry_tool_schemas_are_openai_style():
    for schema in REGISTRY.tool_schemas():
        assert schema["type"] == "function"
        assert "function" in schema
        assert "name" in schema["function"]
        assert "description" in schema["function"]
        assert "parameters" in schema["function"]
        assert schema["function"]["parameters"]["type"] == "object"


def test_registry_dispatch_disabled_list_schema_examples_raises_value_error():
    with pytest.raises(ValueError, match="Unknown tool name"):
        REGISTRY.dispatch("waveflow_list_schema_examples", {})


def test_registry_dispatch_disabled_get_schema_example_raises_value_error():
    with pytest.raises(ValueError, match="Unknown tool name"):
        REGISTRY.dispatch(
            "waveflow_get_schema_example", {"example_id": "hist_cmd"}
        )


def test_validate_schema_accepts_valid_datalist_source():
    result = validate_schema(
        "\n".join(
            [
                "from waveflow.hw import DataList, IntField",
                "U16 = IntField.specialize(bitwidth=16, signed=False)",
                "class DemoPacket(DataList):",
                "    elements = {",
                "        'count': U16,",
                "    }",
            ]
        )
    )

    assert result["valid"] is True
    assert result["errors"] == []
    assert result["schema_info"] == {
        "name": "DemoPacket",
        "kind": "DataList",
        "field_count": 1,
        "field_names": ["count"],
    }


def test_validate_schema_reports_syntax_error_location():
    result = validate_schema(
        "class Broken(DataList)\n    pass",
        workspace_root="demo_workspace",
    )

    assert result["valid"] is False
    assert result["errors"][0]["code"] == "syntax_error"
    assert result["errors"][0]["location"]["line"] == 1
    assert result["errors"][0]["location"]["path"] == "demo_workspace"
    assert result["schema_info"] is None


def test_validate_schema_reports_invalid_elements_definition():
    result = validate_schema(
        "\n".join(
            [
                "from waveflow.hw import DataList, IntField",
                "U16 = IntField.specialize(bitwidth=16, signed=False)",
                "class Broken(DataList):",
                "    elements = {",
                "        'count': {'description': 'missing schema'},",
                "    }",
            ]
        )
    )

    assert result["valid"] is False
    assert result["errors"][0]["code"] == "schema_validation_error"
    assert "must define a 'schema' entry" in result["errors"][0]["message"]
    assert result["schema_info"]["name"] == "Broken"


def test_registry_dispatch_validate_schema(tmp_path):
    schema_file = tmp_path / "payload.py"
    schema_file.write_text(
        "\n".join(
            [
                "from waveflow.hw import DataArray, IntField",
                "U8 = IntField.specialize(bitwidth=8, signed=False)",
                "class Payload(DataArray):",
                "    element_type = U8",
                "    max_shape = (4,)",
                "    static = True",
            ]
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    result = REGISTRY.dispatch(
        "waveflow_validate_schema",
        {
            "schema_name": "Payload",
            "input_path": str(schema_file),
            "output_path": str(report_path),
        },
    )

    assert result["ok"] is True
    assert result["error_count"] == 0
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_info"]["kind"] == "DataArray"


def test_registry_dispatch_unknown_tool_raises_value_error():
    with pytest.raises(ValueError, match="Unknown tool name"):
        REGISTRY.dispatch("not_a_real_tool", {})


def test_registry_register_all_adds_tools_to_mcp():
    from mcp.server.fastmcp import FastMCP

    fresh_mcp = FastMCP("test_server")
    fresh_registry = ToolRegistry()
    fresh_registry.add(
        name="test_tool",
        description="A test tool",
        parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        fn=lambda: {"ok": True},
    )
    fresh_registry.register_all(fresh_mcp)
    # FastMCP doesn't expose a public tool list, but registration should not raise
    # and the tool should be dispatchable via our registry
    result = fresh_registry.dispatch("test_tool", {})
    assert result == {"ok": True}


def test_registry_register_all_respects_profile():
    from mcp.server.fastmcp import FastMCP

    fresh_registry = ToolRegistry()
    fresh_registry.add(
        name="workspace_only_tool",
        description="workspace only",
        parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        fn=lambda: {"ok": True},
        profiles={"workspace"},
    )
    fresh_registry.add(
        name="headless_only_tool",
        description="headless only",
        parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        fn=lambda: {"ok": True},
        profiles={"headless"},
    )

    workspace_mcp = FastMCP("ws")
    fresh_registry.register_all(workspace_mcp, profile="workspace")
    ws_schemas = {s["function"]["name"] for s in fresh_registry.tool_schemas(profile="workspace")}
    assert "workspace_only_tool" in ws_schemas
    assert "headless_only_tool" not in ws_schemas

    headless_mcp = FastMCP("hl")
    fresh_registry.register_all(headless_mcp, profile="headless")
    hl_schemas = {s["function"]["name"] for s in fresh_registry.tool_schemas(profile="headless")}
    assert "headless_only_tool" in hl_schemas
    assert "workspace_only_tool" not in hl_schemas


# ---------------------------------------------------------------------------
# server smoke test
# ---------------------------------------------------------------------------


def test_server_imports_and_has_mcp_instance():
    from waveflow.mcp.server import mcp
    assert mcp.name == "waveflow"


# ---------------------------------------------------------------------------
# build_mcp factory / mode-aware tool exposure
# ---------------------------------------------------------------------------


def test_build_mcp_invalid_mode_raises():
    from waveflow.mcp.server import build_mcp

    with pytest.raises(ValueError, match="mode must be"):
        build_mcp(mode="unknown")


def test_build_mcp_headless_without_work_dir_raises():
    from waveflow.mcp.server import build_mcp

    with pytest.raises(ValueError, match="work_dir is required"):
        build_mcp(mode="headless")


def test_build_mcp_workspace_returns_fastmcp(tmp_path):
    from waveflow.mcp.server import build_mcp

    mcp_inst = build_mcp(mode="workspace")
    assert mcp_inst.name == "waveflow"


def test_build_mcp_headless_returns_fastmcp(tmp_path):
    from waveflow.mcp.server import build_mcp

    mcp_inst = build_mcp(mode="headless", work_dir=tmp_path)
    assert mcp_inst.name == "waveflow"


def test_build_mcp_workspace_exposes_knowledge_but_not_file_tools(tmp_path):
    from waveflow.mcp.server import build_mcp

    mcp_inst = build_mcp(mode="workspace")
    # We check via the registry profile filter (the MCP instance doesn't have a
    # public tool-list API, but registry.tool_schemas gives us the profile view).
    ws_names = {s["function"]["name"] for s in REGISTRY.tool_schemas(profile="workspace")}
    assert "waveflow_search" in ws_names
    assert "list_files" not in ws_names
    assert "read_file" not in ws_names


def test_build_mcp_headless_registry_exposes_knowledge_tools(tmp_path):
    hl_names = {s["function"]["name"] for s in REGISTRY.tool_schemas(profile="headless")}
    assert "waveflow_search" in hl_names


# ---------------------------------------------------------------------------
# File tools (path safety and basic operation)
# ---------------------------------------------------------------------------


def test_file_tools_write_and_read(tmp_path):
    from waveflow.mcp.file_tools import make_file_tools

    list_files_fn, read_file_fn, write_file_fn, edit_file_fn = make_file_tools(tmp_path)

    result = write_file_fn("hello.txt", "hello world")
    assert result["ok"] is True

    result = read_file_fn("hello.txt")
    assert result["content"] == "hello world"
    assert result["path"] == "hello.txt"


def test_file_tools_list_files(tmp_path):
    from waveflow.mcp.file_tools import make_file_tools

    list_files_fn, read_file_fn, write_file_fn, edit_file_fn = make_file_tools(tmp_path)
    write_file_fn("a.txt", "a")
    write_file_fn("b.txt", "b")

    result = list_files_fn()
    names = [e["name"] for e in result["entries"]]
    assert "a.txt" in names
    assert "b.txt" in names


def test_file_tools_edit_file(tmp_path):
    from waveflow.mcp.file_tools import make_file_tools

    list_files_fn, read_file_fn, write_file_fn, edit_file_fn = make_file_tools(tmp_path)
    write_file_fn("edit_me.txt", "foo bar baz")

    result = edit_file_fn("edit_me.txt", "bar", "qux")
    assert result["ok"] is True

    result = read_file_fn("edit_me.txt")
    assert result["content"] == "foo qux baz"


def test_file_tools_edit_file_not_unique_fails(tmp_path):
    from waveflow.mcp.file_tools import make_file_tools

    _, read_file_fn, write_file_fn, edit_file_fn = make_file_tools(tmp_path)
    write_file_fn("dup.txt", "x x x")

    result = edit_file_fn("dup.txt", "x", "y")
    assert result["ok"] is False
    assert "3" in result["error"]


def test_file_tools_path_escape_rejected(tmp_path):
    from waveflow.mcp.file_tools import make_file_tools

    _, read_file_fn, _, _ = make_file_tools(tmp_path)

    result = read_file_fn("../../../etc/passwd")
    assert "error" in result
    assert result["content"] is None


def test_file_tools_absolute_path_outside_root_rejected(tmp_path):
    from waveflow.mcp.file_tools import make_file_tools

    _, read_file_fn, _, _ = make_file_tools(tmp_path)

    result = read_file_fn("/etc/passwd")
    assert "error" in result
    assert result["content"] is None


def test_file_tools_empty_path_rejected(tmp_path):
    from waveflow.mcp.file_tools import make_file_tools

    _, read_file_fn, _, _ = make_file_tools(tmp_path)

    result = read_file_fn("")
    assert "error" in result


def test_file_tools_write_creates_subdirectories(tmp_path):
    from waveflow.mcp.file_tools import make_file_tools

    _, _, write_file_fn, _ = make_file_tools(tmp_path)

    result = write_file_fn("sub/dir/file.txt", "content")
    assert result["ok"] is True
    assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "content"


# ---------------------------------------------------------------------------
# validate_schema_from_file (file-based MCP tool)
# ---------------------------------------------------------------------------


def test_validate_schema_from_file_valid(tmp_path):
    schema_file = tmp_path / "demo.py"
    schema_file.write_text(
        "\n".join(
            [
                "from waveflow.hw import DataList, IntField",
                "U16 = IntField.specialize(bitwidth=16, signed=False)",
                "class DemoPacket(DataList):",
                "    elements = {",
                "        'count': U16,",
                "    }",
            ]
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    result = validate_schema_from_file(
        schema_name="DemoPacket",
        input_path=str(schema_file),
        output_path=str(report_path),
    )

    assert result["ok"] is True
    assert result["error_count"] == 0
    assert result["report_path"] == str(report_path.resolve())
    assert "valid" in result["summary"].lower()

    # Report file should be written
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_name"] == "DemoPacket"
    assert report["ok"] is True
    assert report["schema_info"]["kind"] == "DataList"


def test_validate_schema_from_file_invalid(tmp_path):
    schema_file = tmp_path / "broken.py"
    schema_file.write_text(
        "\n".join(
            [
                "from waveflow.hw import DataList, IntField",
                "class Broken(DataList):",
                "    elements = {",
                "        'count': {'description': 'missing schema key'},",
                "    }",
            ]
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    result = validate_schema_from_file(
        schema_name="Broken",
        input_path=str(schema_file),
        output_path=str(report_path),
    )

    assert result["ok"] is False
    assert result["error_count"] > 0
    assert report_path.exists()


def test_validate_schema_from_file_missing_input(tmp_path):
    report_path = tmp_path / "report.json"

    result = validate_schema_from_file(
        schema_name="Ghost",
        input_path=str(tmp_path / "nonexistent.py"),
        output_path=str(report_path),
    )

    assert result["ok"] is False
    assert "not found" in result["summary"].lower() or "not found" in result["summary"]
    # Report still written
    assert report_path.exists()


def test_validate_schema_from_file_creates_report_parent_dirs(tmp_path):
    schema_file = tmp_path / "schema.py"
    schema_file.write_text(
        "\n".join(
            [
                "from waveflow.hw import DataList, IntField",
                "U8 = IntField.specialize(bitwidth=8, signed=False)",
                "class Simple(DataList):",
                "    elements = {'val': U8}",
            ]
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "reports" / "sub" / "report.json"

    result = validate_schema_from_file(
        schema_name="Simple",
        input_path=str(schema_file),
        output_path=str(report_path),
    )

    assert report_path.exists()
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# waveflow_get_components
# ---------------------------------------------------------------------------


def test_get_components_returns_expected_shape():
    result = get_components()
    assert "summary" in result
    assert "components" in result
    assert isinstance(result["summary"], str)
    assert isinstance(result["components"], list)
    assert len(result["components"]) > 0


def test_get_components_each_entry_has_required_fields():
    result = get_components()
    for entry in result["components"]:
        assert "name" in entry, f"entry missing 'name': {entry}"
        assert "kind" in entry, f"entry missing 'kind': {entry}"
        assert "description" in entry, f"entry missing 'description': {entry}"
        assert "keywords" in entry, f"entry missing 'keywords': {entry}"
        assert isinstance(entry["keywords"], list), (
            f"keywords for {entry['name']!r} should be a list"
        )


def test_get_components_contains_core_classes():
    result = get_components()
    names = {c["name"] for c in result["components"]}
    for expected in (
        "DataSchema",
        "DataList",
        "DataArray",
        "DataField",
        "IntField",
        "FloatField",
        "EnumField",
        "MemAddr",
        "IntEnum",
    ):
        assert expected in names, f"Expected component {expected!r} not found"


def test_get_components_is_deterministic():
    result1 = get_components()
    result2 = get_components()
    assert result1 == result2


def test_registry_dispatch_get_components():
    result = REGISTRY.dispatch("waveflow_get_components", {})
    assert "components" in result
    assert "summary" in result

