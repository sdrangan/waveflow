"""Functional verification BuildSteps.

A :class:`FunctionalVerifyStep` compares two artifact directories (a
*golden* reference and an *actual* run output) on a per-file basis and
raises ``RuntimeError`` on mismatch.  It is intentionally generic: it
does not know about any specific design's I/O surface — its behaviour
is fully driven by a manifest of file comparisons declared on the step
instance.

Three comparator shapes are supported, matching what
``examples/stream_inband/poly_build.py``'s legacy ``ValidateCSimStep`` needed:

- **Schemas**: a structured :class:`~waveflow.hw.dataschema.DataSchema`
  instance per file, compared via :meth:`DataSchema.is_close`.
- **Arrays**: a flat raw-storage array (e.g. ``samp_out.bin``), compared
  via :func:`numpy.allclose`.  The element count can either be a
  literal or pulled from a sibling schema-typed file's field (e.g.
  ``data_cmd_hdr.nsamp`` → number of samples to read from
  ``samp_out.bin``).
- **JSONs**: a flat JSON object (e.g. ``regmap_status.json``); the
  manifest entry can require specific fields to be zero (the "no-error"
  invariant), or simply assert exact equality of named fields.

The plan: see Phase 5 of ``plans/hwcomponent_testbench_codegen_plan.md``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from waveflow.build.build import BuildConfig, BuildStep
from waveflow.hw.arrayutils import read_uint32_file


@dataclass(kw_only=True)
class FunctionalVerifyStep(BuildStep):
    """Compare a ``golden_dir`` artifact against an ``actual_dir`` artifact.

    Each manifest list (``schemas``, ``arrays``, ``jsons``) describes
    files inside the two directories to compare.  See module docstring
    for the comparator semantics.

    Optionally mirrors the actual outputs into ``output_dir`` for
    archival; when set, ``output_dir`` is exposed as an artifact
    keyed by ``output_artifact``.

    Produces a small report JSON (``{ "pass": bool, "checks": [...] }``)
    at ``report_path`` so downstream UIs can inspect what passed.
    """

    description: str = (
        "Compare two artifact directories (golden vs actual) on a per-file basis."
    )
    params: ClassVar[dict] = {}

    golden_dir_artifact: str
    actual_dir_artifact: str

    # Manifest entries (each: dict).  See module docstring for the shape.
    schemas: list[dict] = field(default_factory=list)
    arrays: list[dict] = field(default_factory=list)
    jsons: list[dict] = field(default_factory=list)

    # Extra artifacts that comparators reference by name (e.g. a
    # ``data_cmd_hdr`` path that ``arrays`` entries pull ``nsamp`` from).
    extra_artifacts: list[str] = field(default_factory=list)

    # Optional: copy each actual file into ``output_dir`` after a
    # successful verification (mirrors the legacy ValidateCSimStep
    # behaviour of populating ``results/vitis/``).
    output_dir: str | None = None
    output_artifact: str = "verify_output_dir"

    report_path: str = "verify_report.json"
    # The artifact the report is published under.  A DAG that verifies
    # more than once -- after C simulation and again after co-simulation,
    # say -- needs a distinct name per step, since BuildDag refuses two
    # producers of one artifact.
    report_artifact: str = "verify_report"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [
            self.golden_dir_artifact,
            self.actual_dir_artifact,
            *self.extra_artifacts,
        ]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        d: dict[str, Path] = {self.report_artifact: Path(self.report_path)}
        if self.output_dir is not None:
            d[self.output_artifact] = Path(self.output_dir)
        return d

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        golden_dir = Path(artifacts[self.golden_dir_artifact])
        actual_dir = Path(artifacts[self.actual_dir_artifact])
        extra = {k: artifacts[k] for k in self.extra_artifacts if k in artifacts}

        # Resolve counts that arrays reference up-front.  ``count_from_extra``
        # names an extra-artifact path; ``count_schema`` + ``count_field``
        # name the schema class and field to read out of that file.
        loaded_counts: dict[str, int] = {}
        for spec in self.arrays:
            count_key = spec.get("count_from_extra")
            if count_key is None:
                continue
            if count_key in loaded_counts:
                continue
            ext_path = Path(extra[count_key])
            schema_cls = spec["count_schema"]
            count_field = spec["count_field"]
            loaded = schema_cls().read_uint32_file(ext_path)
            loaded_counts[count_key] = int(getattr(loaded, count_field))

        checks: list[dict] = []
        failures: list[str] = []

        # ----- Schema comparisons -----
        for spec in self.schemas:
            filename = spec["filename"]
            golden_filename = spec.get("golden_filename", filename)
            schema_cls = spec["schema"]
            gpath = golden_dir / golden_filename
            apath = actual_dir / filename
            try:
                g = schema_cls().read_uint32_file(gpath)
                a = schema_cls().read_uint32_file(apath)
                ok = bool(g.is_close(a))
            except Exception as exc:
                ok = False
                checks.append({
                    "kind": "schema", "filename": filename, "pass": False,
                    "message": f"load error: {exc}",
                })
                failures.append(f"{filename}: {exc}")
                continue
            entry = {"kind": "schema", "filename": filename, "pass": ok}
            if not ok:
                entry["message"] = "schema mismatch (is_close returned False)"
                failures.append(f"{filename}: schema mismatch")
            checks.append(entry)

        # ----- Array comparisons -----
        for spec in self.arrays:
            filename = spec["filename"]
            golden_filename = spec.get("golden_filename", filename)
            elem_type = spec["elem_type"]
            rtol = spec.get("rtol", 1e-6)
            atol = spec.get("atol", 1e-6)
            if "count" in spec:
                count = int(spec["count"])
            elif "count_from_extra" in spec:
                count = loaded_counts[spec["count_from_extra"]]
            else:
                raise RuntimeError(
                    f"Array spec for {filename} requires 'count' or "
                    f"'count_from_extra'/'count_schema'/'count_field'"
                )
            gpath = golden_dir / golden_filename
            apath = actual_dir / filename
            try:
                g = np.array(read_uint32_file(
                    gpath, elem_type=elem_type, shape=count,
                ), dtype=_np_dtype_for(elem_type))
                a = np.array(read_uint32_file(
                    apath, elem_type=elem_type, shape=count,
                ), dtype=_np_dtype_for(elem_type))
                ok = bool(np.allclose(a, g[:a.size], rtol=rtol, atol=atol))
            except Exception as exc:
                ok = False
                checks.append({
                    "kind": "array", "filename": filename, "pass": False,
                    "message": f"load error: {exc}",
                })
                failures.append(f"{filename}: {exc}")
                continue
            entry = {"kind": "array", "filename": filename, "pass": ok}
            if not ok:
                entry["message"] = (
                    f"array mismatch (count={count}, rtol={rtol}, atol={atol})"
                )
                failures.append(f"{filename}: array mismatch")
            checks.append(entry)

        # ----- JSON comparisons -----
        for spec in self.jsons:
            filename = spec["filename"]
            golden_filename = spec.get("golden_filename", filename)
            expect_zero = spec.get("expect_zero", [])
            compare_fields = spec.get("compare_fields")
            apath = actual_dir / filename
            gpath = golden_dir / golden_filename
            try:
                a_data = json.loads(apath.read_text(encoding="utf-8"))
                g_data = (
                    json.loads(gpath.read_text(encoding="utf-8"))
                    if gpath.exists() else None
                )
            except Exception as exc:
                checks.append({
                    "kind": "json", "filename": filename, "pass": False,
                    "message": f"load error: {exc}",
                })
                failures.append(f"{filename}: {exc}")
                continue
            entry = {"kind": "json", "filename": filename, "pass": True}
            for fname in expect_zero:
                if int(a_data.get(fname, 0)) != 0:
                    entry["pass"] = False
                    entry.setdefault("message", "")
                    msg = (f"actual {filename}.{fname}="
                           f"{a_data.get(fname)} (expected 0)")
                    entry["message"] = (
                        msg if not entry["message"]
                        else f"{entry['message']}; {msg}"
                    )
                    failures.append(msg)
                if g_data is not None and int(g_data.get(fname, 0)) != 0:
                    entry["pass"] = False
                    msg = (f"golden {filename}.{fname}="
                           f"{g_data.get(fname)} (expected 0)")
                    entry["message"] = (
                        msg if "message" not in entry
                        else f"{entry['message']}; {msg}"
                    )
                    failures.append(msg)
            if compare_fields is not None and g_data is not None:
                for fname in compare_fields:
                    if a_data.get(fname) != g_data.get(fname):
                        entry["pass"] = False
                        msg = (f"{filename}.{fname}: golden="
                               f"{g_data.get(fname)} vs actual="
                               f"{a_data.get(fname)}")
                        entry["message"] = (
                            msg if "message" not in entry
                            else f"{entry['message']}; {msg}"
                        )
                        failures.append(msg)
            checks.append(entry)

        passed = not failures
        report = {"pass": passed, "checks": checks}
        root_dir = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        report_path = root_dir / self.report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        result_artifacts: dict[str, Any] = {self.report_artifact: report_path}

        if self.output_dir is not None:
            out_dir = root_dir / self.output_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            # Copy each actual file the manifest references, so downstream
            # tooling can find the verified outputs in one place.  The
            # mirror uses ``golden_filename`` when present so the output
            # directory uses the canonical names (matching the layout the
            # golden producer writes); downstream consumers can then read
            # both directories with a single set of filenames.
            for spec in (*self.schemas, *self.arrays, *self.jsons):
                src = actual_dir / spec["filename"]
                if src.exists():
                    dst = out_dir / spec.get("golden_filename", spec["filename"])
                    dst.write_bytes(src.read_bytes())
            result_artifacts[self.output_artifact] = out_dir

        if not passed:
            raise RuntimeError(
                f"FunctionalVerifyStep '{self.name}' failed: "
                + "; ".join(failures)
            )
        return result_artifacts


def _np_dtype_for(elem_type) -> Any:
    """Pick a NumPy dtype matching a :class:`DataSchema` element type."""
    from waveflow.hw.dataschema import FloatField, IntField
    if isinstance(elem_type, type) and issubclass(elem_type, FloatField):
        bw = elem_type.get_bitwidth()
        if bw == 32:
            return np.float32
        if bw == 64:
            return np.float64
    if isinstance(elem_type, type) and issubclass(elem_type, IntField):
        bw = elem_type.get_bitwidth()
        signed = getattr(elem_type, 'signed', False)
        if signed:
            return {8: np.int8, 16: np.int16, 32: np.int32, 64: np.int64}.get(
                bw, np.int64
            )
        return {8: np.uint8, 16: np.uint16, 32: np.uint32, 64: np.uint64}.get(
            bw, np.uint64
        )
    return None
