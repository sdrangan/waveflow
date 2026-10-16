"""resource_steps.py — the build step that turns a synthesis report into per-module resource records.

The rung after ``csynth`` in a build DAG.  It reads the report the synthesis just produced, attributes
its per-RTL-module figures to the Waveflow modules that caused them
(:mod:`waveflow.calib.synth_report`), and files them into the platform's module store — so a *later*
design that composes the same module at the same configuration finds the number instead of
re-synthesizing for it.

Everything expensive already happened.  The synthesis is the cost; this is reading its output, which
is why it is worth doing on **every** csynth rather than only when someone remembers to calibrate.
The corollary is that the step must never fail a build that synthesized correctly — a report it cannot
attribute is a real error and raises, but a build with no platform selected simply writes its JSON and
moves on.

The work is :func:`inspect_synth`, a library function: :class:`InspectSynthStep` wraps it for one
example's top, and a system's ``kernel_resources`` (:mod:`waveflow.build.system_dag`) calls it once
per kernel instance.  Its optional gate (``require_ii1``) fails the build on a pipelined loop with
II > 1 -- the check ``examples/regmap`` and ``examples/stream_inband`` once carried in local copies.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from waveflow.build.build import BuildConfig, BuildStep


class PipelineIIError(RuntimeError):
    """A pipelined loop in the csynth report has II > 1 while the build requires II = 1."""


def synth_seconds_of(value: Any) -> float:
    """A csynth's wall-clock seconds from an artifact: a number, or a JSON file holding
    ``{"synth_seconds": ...}`` (what :class:`~waveflow.build.system_dag.CsynthStep` writes).  A file
    that does not exist -- a top synthesized before the file was written -- is 0."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    p = Path(value)
    if not p.is_file():
        return 0.0
    return float(json.loads(p.read_text(encoding="utf-8")).get("synth_seconds", 0.0))


def loop_table(report_dir: str | Path, *, require_ii1: bool = False):
    """The csynth report's loop table (``CsynthParser.loop_df``).  With *require_ii1*, a pipelined
    loop at II > 1 raises :class:`PipelineIIError` naming it."""
    import numpy as np

    from waveflow.utils.csynthparse import CsynthParser

    parser = CsynthParser(sol_path=str(report_dir))
    parser.get_loop_pipeline_info()
    df = parser.loop_df
    if require_ii1 and not df.empty:
        bad = df[df["PipelineII"].apply(lambda v: isinstance(v, (int, np.integer)) and v > 1)]
        if len(bad):
            raise PipelineIIError(f"loops with PipelineII > 1:\n{bad.to_string()}")
    return df


def inspect_synth(top: Any, report_dir: str | Path, *, top_name: str, config: BuildConfig,
                  out: str | Path, synth_seconds: float = 0.0, params: dict | None = None,
                  owner: str = "", require_ii1: bool = False,
                  loop_csv: str | Path | None = None) -> dict:
    """Attribute the csynth report under *report_dir* to the elaborated module *top*, write it as
    JSON to *out*, and file the records into ``config.platform_info``'s store (if one is selected).

    *owner* labels the pysim object the top is accounted to in a system (a kernel, or the routed
    channel that brought a bus writer); it is written into each record's payload.  *require_ii1* and
    *loop_csv* run :func:`loop_table` first -- the II gate, and the table written as CSV.  Returns the
    JSON blob written."""
    from waveflow.calib.synth_report import report_from_solution, store_report

    if not Path(report_dir).exists():
        raise RuntimeError(f"Solution directory not found: {report_dir}")
    if require_ii1 or loop_csv is not None:
        df = loop_table(report_dir, require_ii1=require_ii1)
        if loop_csv is not None:
            Path(loop_csv).parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(loop_csv, index=False)
    report = report_from_solution(top, Path(report_dir), top_name=top_name)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    blob = dict(report.to_json())
    blob["params"] = dict(params or {})
    blob["synth_seconds"] = float(synth_seconds or 0.0)
    if owner:
        blob["owner"] = owner
    platform = getattr(config, "platform_info", None)
    blob["filed_into"] = (str(getattr(platform, "write_dir", platform.dir))
                          if platform is not None else None)
    out.write_text(json.dumps(blob, indent=2), encoding="utf-8")

    if platform is not None:
        from waveflow.calib.module_key import walk_modules
        from waveflow.calib.record_store import platform_store
        from waveflow.calib.resource_model import InterfaceResourceModel

        walked = walk_modules(top)
        identities = {i.key: i for _, _, i in walked}
        # The composite's own cost is keyed on its *boundary*, not on its parameters -- measured
        # invariant across every compute knob and moving only with the memory word width.  So the
        # boundary is recorded alongside the counters; deriving it later would need the component,
        # which a corpus row does not have.
        top_ident = next((i for _p, c, i in walked if c is top), None)
        boundary = InterfaceResourceModel().get_params(top)
        store_report(
            report, platform_store(platform), identities,
            source="hls_estimate", part=platform.part or "",
            period_ns=platform.synth_period_ns or 0.0,
            tool=f"vitis_hls {config.vitis_version}" if config.vitis_version else "vitis_hls",
            cost_seconds=float(synth_seconds or 0.0),
            top_identity=top_ident, boundary=boundary,
            extra={"owner": owner} if owner else None)
        print(f"  {top_name}: filed {len(report.modules)} module record(s) + 1 integration record "
              f"into {blob['filed_into']}")
    else:
        # No platform selected: the attribution is still written, it just has nowhere shared to
        # live.  Not an error — plenty of builds synthesize without wanting to publish anything.
        print(f"  {top_name}: no platform selected — wrote {out.name} only")

    i = report.integration
    print(f"  modules {report.module_sum} + integration {i} = top {report.top}")
    return blob


@dataclass(kw_only=True)
class InspectSynthStep(BuildStep):
    """Attribute a csynth report to modules, write it as JSON, and file the records.

    Parameters
    ----------
    comp_class :
        The top's class, elaborated with the step's :attr:`elaborate_params` to get the module graph
        the report is attributed against.  Elaboration is how the *same* parameters that generated the
        C++ are used to read its report back — nothing is re-derived or hand-listed.
    top_name :
        The top-level name, which is also the RTL row holding the design total.
    elaborate_params :
        Which of this step's params are elaboration parameters.  The rest (a ``live_output`` flag,
        say) are the step's own business and must not reach ``elaborate``.
    report_artifact, seconds_artifact :
        The consumed artifacts: the Vitis solution directory and the csynth's seconds (``None``: not
        consumed).  ``report_dir`` / ``synth_seconds`` by default.
    output, output_artifact :
        Where the attribution JSON goes (relative to the root) and its artifact name.
    require_ii1, loop_csv, loop_artifact :
        The II gate (fail on a pipelined loop at II > 1), and the loop table written as CSV under
        artifact *loop_artifact*.
    """

    description = "Attribute the csynth report's resources to modules and file the records."

    comp_class: type
    top_name: str
    elaborate_params: tuple = ()
    params: dict = field(default_factory=dict)
    report_artifact: str = "report_dir"
    seconds_artifact: str | None = "synth_seconds"
    output: Path = Path("results/resources.json")
    output_artifact: str = "resource_report"
    require_ii1: bool = False
    loop_csv: Path | None = None
    loop_artifact: str = "loop_df"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.report_artifact] + ([self.seconds_artifact] if self.seconds_artifact else [])

    @property
    def produces(self) -> dict:  # type: ignore[override]
        out = {self.output_artifact: Path(self.output)}
        if self.loop_csv is not None:
            out[self.loop_artifact] = Path(self.loop_csv)
        return out

    def run(self, config: BuildConfig, **kw) -> dict:
        from waveflow.build.elaborate import elaborate

        elab = {k: kw[k] for k in self.elaborate_params if k in kw}
        top = elaborate(self.comp_class, elab, name=self.top_name)
        root = Path(config.root_dir)
        out = root / self.output
        loop = root / self.loop_csv if self.loop_csv is not None else None
        secs = synth_seconds_of(kw.get(self.seconds_artifact)) if self.seconds_artifact else 0.0
        inspect_synth(top, kw[self.report_artifact], top_name=self.top_name, config=config, out=out,
                      synth_seconds=secs, params=elab, require_ii1=self.require_ii1,
                      loop_csv=loop)
        res = {self.output_artifact: out}
        if loop is not None:
            res[self.loop_artifact] = loop
        return res


__all__ = ["InspectSynthStep", "PipelineIIError", "inspect_synth", "loop_table", "synth_seconds_of"]
