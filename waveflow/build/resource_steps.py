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
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from waveflow.build.build import BuildConfig, BuildStep


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
    """

    description = "Attribute the csynth report's resources to modules and file the records."
    consumes = ["report_dir", "synth_seconds"]
    produces = {"resource_report": Path("results/resources.json")}

    comp_class: type
    top_name: str
    elaborate_params: tuple = ()
    params: dict = field(default_factory=dict)

    def run(self, config: BuildConfig, report_dir, synth_seconds=0.0, **kw) -> dict:
        from waveflow.build.elaborate import elaborate
        from waveflow.calib.module_key import walk_modules
        from waveflow.calib.record_store import ModuleStore
        from waveflow.calib.synth_report import report_from_solution, store_report

        elab = {k: kw[k] for k in self.elaborate_params if k in kw}
        top = elaborate(self.comp_class, elab, name=self.top_name)
        report = report_from_solution(top, Path(report_dir), top_name=self.top_name)

        out = Path(config.root_dir) / "results" / "resources.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        blob = dict(report.to_json())
        blob["params"] = dict(elab)
        blob["synth_seconds"] = float(synth_seconds or 0.0)
        out.write_text(json.dumps(blob, indent=2), encoding="utf-8")

        platform = getattr(config, "platform_info", None)
        if platform is not None:
            from waveflow.calib.resource_model import InterfaceResourceModel

            identities = {i.key: i for _, _, i in walk_modules(top)}
            # The composite's own cost is keyed on its *boundary*, not on its parameters -- measured
            # invariant across every compute knob and moving only with the memory word width.  So the
            # boundary is recorded alongside the counters; deriving it later would need the component,
            # which a corpus row does not have.
            top_ident = next((i for _p, c, i in walk_modules(top) if c is top), None)
            boundary = InterfaceResourceModel().get_params(top)
            store_report(
                report, ModuleStore(platform.dir), identities,
                source="hls_estimate", part=platform.part or "",
                period_ns=platform.synth_period_ns or 0.0,
                tool=f"vitis_hls {config.vitis_version}" if config.vitis_version else "vitis_hls",
                cost_seconds=float(synth_seconds or 0.0),
                top_identity=top_ident, boundary=boundary)
            print(f"  filed {len(report.modules)} module record(s) + 1 integration record "
                  f"into {platform.dir}")
        else:
            # No platform selected: the attribution is still written, it just has nowhere shared to
            # live.  Not an error — plenty of builds synthesize without wanting to publish anything.
            print("  (no platform selected — wrote results/resources.json only)")

        i = report.integration
        print(f"  modules {report.module_sum} + integration {i} = top {report.top}")
        return {"resource_report": out}
