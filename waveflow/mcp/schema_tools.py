"""Deterministic schema-validation helpers for waveflow MCP tools.

The schema *planning* helper that used to live here was a static step list
whose only argument rewrote its summary.  It was replaced by the frame process
(``waveflow_get_process``), which is real text about a real architecture --
decision D2.
"""
from __future__ import annotations

import ast
import json
from enum import IntEnum
from pathlib import Path
from typing import Any

from waveflow.hw import DataArray, DataField, DataList, DataSchema, EnumField, FloatField, IntField, MemAddr


def validate_schema(schema: str, workspace_root: str | None = None) -> dict:
	"""Validate a drafted waveflow schema provided as Python source text.

	This helper is deterministic. It parses the input as Python, executes it in
	a constrained namespace populated with the public waveflow schema classes,
	and then validates any discovered schema classes using existing class-level
	metadata helpers.
	"""
	path = workspace_root
	errors: list[dict[str, Any]] = []
	warnings: list[dict[str, Any]] = []

	if not isinstance(schema, str) or not schema.strip():
		errors.append(
			_diagnostic(
				message="Schema text must be a non-empty string.",
				code="empty_schema",
				path=path,
			)
		)
		return _validation_result(
			valid=False,
			summary="Schema validation failed with 1 error.",
			errors=errors,
			warnings=warnings,
			schema_info=None,
		)

	try:
		tree = ast.parse(schema, filename="<schema>")
	except SyntaxError as exc:
		errors.append(
			_diagnostic(
				message=exc.msg,
				code="syntax_error",
				line=exc.lineno,
				column=exc.offset,
				path=path,
			)
		)
		return _validation_result(
			valid=False,
			summary="Schema validation failed with 1 error.",
			errors=errors,
			warnings=warnings,
			schema_info=None,
		)

	class_lines = {
		node.name: node.lineno
		for node in ast.walk(tree)
		if isinstance(node, ast.ClassDef)
	}

	namespace = _build_validation_namespace()
	initial_names = set(namespace)

	try:
		exec(compile(tree, "<schema>", "exec"), namespace)
	except Exception as exc:  # noqa: BLE001
		errors.append(_diagnostic_from_exception(exc, code="execution_error", path=path))
		return _validation_result(
			valid=False,
			summary=f"Schema validation failed with {len(errors)} error.",
			errors=errors,
			warnings=warnings,
			schema_info=None,
		)

	schema_classes = _discover_schema_classes(namespace, initial_names, class_lines)
	if not schema_classes:
		errors.append(
			_diagnostic(
				message="No waveflow schema classes were defined in the provided text.",
				code="no_schema_found",
				path=path,
			)
		)
		return _validation_result(
			valid=False,
			summary="Schema validation failed with 1 error.",
			errors=errors,
			warnings=warnings,
			schema_info=None,
		)

	for entry in schema_classes:
		line = entry["line"]
		schema_cls = entry["class"]
		try:
			_validate_schema_class(schema_cls)
		except Exception as exc:  # noqa: BLE001
			errors.append(
				_diagnostic(
					message=f"{schema_cls.__name__}: {exc}",
					code="schema_validation_error",
					line=line,
					path=path,
				)
			)

	primary_info = _schema_info(schema_classes[0]["class"])

	if len(schema_classes) > 1:
		warnings.append(
			_diagnostic(
				message=(
					"Multiple schema classes were found. schema_info describes the first "
					"discovered schema class."
				),
				code="multiple_schema_classes",
				line=schema_classes[0]["line"],
				path=path,
			)
		)

	if errors:
		summary = f"Schema validation failed with {len(errors)} error{'s' if len(errors) != 1 else ''}."
		if primary_info is None:
			primary_info = None
		return _validation_result(
			valid=False,
			summary=summary,
			errors=errors,
			warnings=warnings,
			schema_info=primary_info,
		)

	summary = "Schema is valid."
	if warnings:
		summary = f"Schema is valid with {len(warnings)} warning{'s' if len(warnings) != 1 else ''}."

	return _validation_result(
		valid=True,
		summary=summary,
		errors=errors,
		warnings=warnings,
		schema_info=primary_info,
	)


def _build_validation_namespace() -> dict[str, Any]:
	return {
		"__builtins__": __builtins__,
		"__name__": "waveflow.mcp._schema_validation",
		"DataSchema": DataSchema,
		"DataField": DataField,
		"IntField": IntField,
		"MemAddr": MemAddr,
		"FloatField": FloatField,
		"EnumField": EnumField,
		"DataList": DataList,
		"DataArray": DataArray,
		"IntEnum": IntEnum,
	}


def _discover_schema_classes(
	namespace: dict[str, Any],
	initial_names: set[str],
	class_lines: dict[str, int],
) -> list[dict[str, Any]]:
	classes: list[dict[str, Any]] = []
	for name, value in namespace.items():
		if name in initial_names or name.startswith("_"):
			continue
		if not isinstance(value, type) or not issubclass(value, DataSchema):
			continue
		if value in {DataSchema, DataField, IntField, MemAddr, FloatField, EnumField, DataList, DataArray}:
			continue
		classes.append(
			{
				"name": name,
				"class": value,
				"line": class_lines.get(name),
			}
		)

	classes.sort(key=lambda item: (item["line"] is None, item["line"] or 0, item["name"]))
	return classes


def _validate_schema_class(schema_cls: type[DataSchema]) -> None:
	schema_cls.get_bitwidth()
	schema_cls.init_value()

	if issubclass(schema_cls, DataList):
		schema_cls._iter_elements()
		schema_cls()
		return

	if issubclass(schema_cls, DataArray):
		schema_cls._element_type()
		schema_cls._normalized_shape()
		schema_cls()


def _schema_info(schema_cls: type[DataSchema]) -> dict[str, Any]:
	if issubclass(schema_cls, DataList):
		try:
			field_names = [name for name, _ in schema_cls._iter_elements()]
			field_count: int | None = len(field_names)
		except Exception:  # noqa: BLE001
			field_names = []
			field_count = None
		return {
			"name": schema_cls.__name__,
			"kind": "DataList",
			"field_count": field_count,
			"field_names": field_names,
		}

	if issubclass(schema_cls, DataArray):
		try:
			member_name = schema_cls._member_name()
		except Exception:  # noqa: BLE001
			member_name = None
		return {
			"name": schema_cls.__name__,
			"kind": "DataArray",
			"field_count": 1 if member_name is not None else None,
			"field_names": [member_name] if member_name is not None else [],
		}

	if issubclass(schema_cls, DataField):
		return {
			"name": schema_cls.__name__,
			"kind": "DataField",
			"field_count": 0,
			"field_names": [],
		}

	return {
		"name": schema_cls.__name__,
		"kind": "DataSchema",
		"field_count": None,
		"field_names": [],
	}


def _diagnostic(
	*,
	message: str,
	code: str | None,
	line: int | None = None,
	column: int | None = None,
	path: str | None = None,
) -> dict[str, Any]:
	location = None
	if line is not None or column is not None or path is not None:
		location = {
			"line": line,
			"column": column,
			"path": path,
		}
	return {
		"message": message,
		"code": code,
		"location": location,
	}


def _diagnostic_from_exception(exc: Exception, *, code: str | None, path: str | None) -> dict[str, Any]:
	line = None
	column = None
	if isinstance(exc, SyntaxError):
		line = exc.lineno
		column = exc.offset
	traceback_obj = exc.__traceback__
	while traceback_obj is not None:
		if traceback_obj.tb_frame.f_code.co_filename == "<schema>":
			line = traceback_obj.tb_lineno
		traceback_obj = traceback_obj.tb_next
	return _diagnostic(
		message=str(exc),
		code=code,
		line=line,
		column=column,
		path=path,
	)


def _validation_result(
	*,
	valid: bool,
	summary: str,
	errors: list[dict[str, Any]],
	warnings: list[dict[str, Any]],
	schema_info: dict[str, Any] | None,
) -> dict:
	return {
		"valid": valid,
		"summary": summary,
		"errors": errors,
		"warnings": warnings,
		"schema_info": schema_info,
	}


# ---------------------------------------------------------------------------
# File-based validation (MCP tool interface)
# ---------------------------------------------------------------------------


def validate_schema_from_file(
	schema_name: str,
	input_path: str,
	output_path: str,
) -> dict[str, Any]:
	"""Validate a waveflow schema file and write a report artifact.

	Reads Python source from *input_path*, runs the same deterministic
	validation as :func:`validate_schema`, writes the full structured report as
	JSON to *output_path*, and returns a compact summary dict.

	This is the MCP-facing interface for ``waveflow_validate_schema``.
	:func:`validate_schema` remains available for direct Python use.

	Parameters
	----------
	schema_name:
		A human-readable label for the schema being validated (used in the
		report; does not affect validation logic).
	input_path:
		Absolute or relative path to the Python source file containing the
		waveflow schema definition to validate.
	output_path:
		Path where the JSON validation report will be written.  Parent
		directories are created automatically.

	Returns
	-------
	dict
		Compact result with keys:

		``ok``            – ``True`` if validation passed.
		``error_count``   – number of errors found.
		``warning_count`` – number of warnings found.
		``report_path``   – absolute path of the written report (echoed back).
		``summary``       – human-readable one-line result.
	"""
	in_path = Path(input_path)
	out_path = Path(output_path)

	# Read input
	if not in_path.exists():
		compact = {
			"ok": False,
			"error_count": 1,
			"warning_count": 0,
			"report_path": str(out_path.resolve()),
			"summary": f"Input file not found: {input_path!r}",
		}
		_write_report(out_path, schema_name, input_path, compact, full_result=None)
		return compact

	try:
		schema_text = in_path.read_text(encoding="utf-8")
	except OSError as exc:
		compact = {
			"ok": False,
			"error_count": 1,
			"warning_count": 0,
			"report_path": str(out_path.resolve()),
			"summary": f"Cannot read input file: {exc}",
		}
		_write_report(out_path, schema_name, input_path, compact, full_result=None)
		return compact

	# Run validation
	full_result = validate_schema(schema_text, workspace_root=str(in_path))

	compact = {
		"ok": full_result["valid"],
		"error_count": len(full_result["errors"]),
		"warning_count": len(full_result["warnings"]),
		"report_path": str(out_path.resolve()),
		"summary": full_result["summary"],
	}

	_write_report(out_path, schema_name, input_path, compact, full_result=full_result)
	return compact


def _write_report(
	out_path: Path,
	schema_name: str,
	input_path: str,
	compact: dict[str, Any],
	full_result: dict[str, Any] | None,
) -> None:
	"""Write the full JSON validation report to *out_path*."""
	report: dict[str, Any] = {
		"schema_name": schema_name,
		"input_path": str(Path(input_path).resolve()),
		"ok": compact["ok"],
		"summary": compact["summary"],
	}
	if full_result is not None:
		report["errors"] = full_result["errors"]
		report["warnings"] = full_result["warnings"]
		report["schema_info"] = full_result.get("schema_info")
	else:
		report["errors"] = [{"message": compact["summary"], "code": "io_error", "location": None}]
		report["warnings"] = []
		report["schema_info"] = None

	try:
		out_path.parent.mkdir(parents=True, exist_ok=True)
		out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
	except OSError:
		# Best-effort write; do not mask the validation result
		pass
