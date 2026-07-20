"""BuildStep that emits a component's trace manifest.

The manifest maps the Python graph onto the RTL net names a waveform will carry (see
:meth:`~waveflow.build.composite_gen.TopSpec.trace_manifest`).  It is a pure function of
``elaborate()`` -- no Vitis, no RTL, no simulation -- so it belongs on the same rung as the C++
codegen steps rather than downstream of csynth, and it is cheap enough to regenerate whenever the
design source changes.

Kept in its own module for the same reason :mod:`waveflow.build.cosim_steps` is: it is one small
concern with one consumer (:mod:`waveflow.utils.trace`), and burying it in the C++ codegen module
would suggest it emits C++.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildStep
from waveflow.build.composite_gen import DEFAULT_MEM_DW, composite_top_spec
from waveflow.build.elaborate import elaborate


@dataclass(kw_only=True)
class TraceManifestStep(BuildStep):
    """Write ``<top>_trace.json`` -- the RTL net names for this component's channels and ports.

    The artifact is what lets a timing/waveform consumer bind to a VCD by exact name instead of
    guessing.  Binding by substring is actively wrong on these traces: an interleaver waveform holds
    both ``ywords_fifo_cap`` and ``il_store_..._U0_ywords_fifo_cap``, which differ in width and
    meaning.

    Consuming ``source_artifact`` (the design's Python source) is what makes the DAG regenerate the
    manifest when the graph changes -- the same dependency :class:`~waveflow.build.hwcodegen_steps.
    HlsCodegenStep` declares, and for the same reason.  There is no RTL input by design: the whole
    point is that these names are known before anything is synthesized.

    Construction parameters
    -----------------------
    comp_class : type
        The component to elaborate.  A composite gives channels; a leaf gives ports and one task.
    source_artifact : str
        Upstream artifact naming the design's Python source, for staleness.
    output_path : str
        Repo-relative location of the produced JSON.
    width : int
        Bus width passed to ``composite_top_spec`` -- the payload ``W`` of the channels.
    elab_params : dict | None
        Compile-time ``HwParam`` overrides for :func:`elaborate`.  Named ``elab_params`` because
        ``params`` is already a :class:`BuildStep` class attribute.
    """

    description: str = "Emit the trace manifest (RTL net names) for a component."
    params: ClassVar[dict] = {}

    comp_class: type
    source_artifact: str
    output_path: str = "results/trace_manifest.json"
    width: int = DEFAULT_MEM_DW
    elab_params: dict[str, Any] | None = field(default=None)

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.source_artifact]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"trace_manifest": Path(self.output_path)}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        comp = elaborate(self.comp_class, self.elab_params)
        manifest = composite_top_spec(comp, width=self.width).trace_manifest()

        root_dir = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out_path = root_dir / self.output_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # sort_keys so the artifact is byte-stable across runs: an unstable artifact would make the
        # DAG think the design changed on every build.
        out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
        return {"trace_manifest": out_path}
