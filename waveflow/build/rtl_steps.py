"""rtl_steps.py — the build step that places a module's declared Verilog where a build can reach it.

The ``rtl_module`` target's emitter, and it emits nothing: it **copies**.  That is the whole
contract — the hook declares a pre-written artifact, so the build's job is to put the file beside the
generated C++, not to produce a second version of it.  The peer of
:class:`~waveflow.build.streamutils.MemStreamStep` (hand-written ``hls::task`` headers) and
:class:`~waveflow.build.streamutils.XsiHarnessStep` (the hand-written XSI harness), one language
over.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildStep
from waveflow.build.elaborate import elaborate
from waveflow.build.rtl_gen import resolve_rtl_module, rtl_source_paths
from waveflow.hw.hw_module import HwModule


@dataclass(kw_only=True)
class GenRtlStep(BuildStep):
    """Copy the Verilog declared by *comp_class*'s ``rtl_module()`` into ``output_dir``.

    Resolution runs first (:func:`~waveflow.build.rtl_gen.resolve_rtl_module`), so a module whose
    declaration is wrong — a missing file, an endpoint with no port mapping, a port map naming a port
    the module does not declare — fails the **build**, not a later elaboration in a simulator.  That
    is the same discipline the other two hooks follow: the artifact is declared, and the declaration
    is checked against the artifact.

    Copied **verbatim**, byte for byte.  A step that rewrote the file while placing it would make the
    committed, simulated artifact stop being the thing that ships, which is the one property this
    whole path exists to preserve.  Parameterization rides on the instantiation instead
    (:attr:`~waveflow.build.rtl_gen.RtlModule.params`).

    Parameters
    ----------
    comp_class : type[HwModule]
        The module declaring ``rtl_module()``.
    output_dir : str
        Directory **relative to** ``BuildConfig.root_dir`` for the copied sources.  Defaults to
        ``"rtl"`` — its own directory, because these files are inputs to Vivado/xsim rather than to
        Vitis, and mixing them into the C++ output dir would put two toolchains' sources in one
        place.
    """

    description: str = "Copy a module's declared (pre-written) Verilog into the build tree."
    params: ClassVar[dict] = {}

    comp_class: type[HwModule]
    output_dir: str = "rtl"

    def __post_init__(self) -> None:
        super().__post_init__()
        self._rtl = resolve_rtl_module(elaborate(self.comp_class))
        self._srcs = rtl_source_paths(self._rtl)

    @property
    def rtl(self):
        """The resolved :class:`~waveflow.build.rtl_gen.RtlModule` — what a wrapper emitter needs."""
        return self._rtl

    @property
    def produces(self) -> dict:  # type: ignore[override]
        out = Path(self.output_dir)
        # Keyed by file stem, so a consumer names the artifact it wants (`rtl_bram_t2p`) rather than
        # an index into a list whose order it would have to know.
        return {f"rtl_{p.stem}": out / p.name for p in self._srcs}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out_root = root / self.output_dir
        out_root.mkdir(parents=True, exist_ok=True)
        result: dict[str, Any] = {}
        for src in self._srcs:
            dst = out_root / src.name
            # write_bytes, not read_text/write_text: a copy that normalized line endings would make
            # "byte-identical to the artifact that was simulated" false on one of the two platforms.
            dst.write_bytes(src.read_bytes())
            result[f"rtl_{src.stem}"] = dst
        return result
