from __future__ import annotations

import json

import pytest

from waveflow.mcp.headless import _build_tool_runtime


def test_build_tool_runtime_workspace_includes_knowledge_but_not_file_tools():
    tool_schemas, dispatch_tool = _build_tool_runtime(mode="workspace", work_dir=None)

    names = {schema["function"]["name"] for schema in tool_schemas}

    assert "waveflow_get_components" in names
    assert "waveflow_search" in names
    assert "list_files" not in names
    with pytest.raises(ValueError, match="Unknown tool name"):
        dispatch_tool("list_files", {})


def test_build_tool_runtime_headless_includes_file_tools(tmp_path):
    sample = tmp_path / "sample.txt"
    sample.write_text("hello world", encoding="utf-8")

    tool_schemas, dispatch_tool = _build_tool_runtime(mode="headless", work_dir=tmp_path)

    names = {schema["function"]["name"] for schema in tool_schemas}

    assert "waveflow_get_components" in names
    assert "waveflow_search" in names
    assert {"list_files", "read_file", "write_file", "edit_file"}.issubset(names)

    result = dispatch_tool("read_file", {"path": "sample.txt"})

    assert result == {"path": "sample.txt", "content": "hello world"}


def test_build_tool_runtime_headless_resolves_validation_paths_against_work_dir(tmp_path):
    schema_path = tmp_path / "demo_schema.py"
    schema_path.write_text(
        "from waveflow.hw import DataList, IntField\n"
        "U16 = IntField.specialize(bitwidth=16, signed=False)\n"
        "class DemoPacket(DataList):\n"
        "    elements = {\n"
        "        'value': {'schema': U16, 'description': 'Demo field'},\n"
        "    }\n",
        encoding="utf-8",
    )

    tool_schemas, dispatch_tool = _build_tool_runtime(mode="headless", work_dir=tmp_path)

    names = {schema["function"]["name"] for schema in tool_schemas}
    assert "waveflow_validate_schema" in names

    result = dispatch_tool(
        "waveflow_validate_schema",
        {
            "schema_name": "demo_schema",
            "input_path": "demo_schema.py",
            "output_path": "demo_schema_validation.json",
        },
    )

    report_path = tmp_path / "demo_schema_validation.json"

    assert result["ok"] is True
    assert result["report_path"] == str(report_path.resolve())
    assert report_path.exists()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["input_path"] == str(schema_path.resolve())
    assert report["schema_info"]["name"] == "DemoPacket"


def test_build_tool_runtime_rejects_invalid_mode():
    with pytest.raises(ValueError, match="mode must be None, 'workspace', or 'headless'"):
        _build_tool_runtime(mode="invalid", work_dir=None)