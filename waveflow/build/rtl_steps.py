"""rtl_steps.py — the build step that places a module's declared Verilog where a build can reach it.

The ``rtl_module`` target's emitter, and it emits nothing: it **copies**.  That is the whole
contract — the hook declares a pre-written artifact, so the build's job is to put the file beside the
generated C++, not to produce a second version of it.  The peer of
:class:`~waveflow.build.streamutils.MemStreamStep` (hand-written ``hls::task`` headers) and
:class:`~waveflow.build.streamutils.XsiHarnessStep` (the hand-written XSI harness), one language
over.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildStep
from waveflow.build.elaborate import elaborate
from waveflow.build.rtl_gen import resolve_rtl_module, rtl_source_paths
from waveflow.hw.hw_module import HwModule


@dataclass(kw_only=True)
class GenWrapperStep(BuildStep):
    """Emit the **wrapper**: the generated kernel plus the hand-written RTL beside it, as one module.

    The other half of :class:`GenRtlStep`.  That step places the memory; this one writes the module
    that joins it to the kernel — and after it, the design has a single elaboratable boundary whose
    only pins are AXI-Stream.

    Unlike ``GenRtlStep``, this step *does* generate Verilog, and the distinction is the one the hook
    family rests on: it emits **wiring**, never behaviour.  Every name in the file was decided
    somewhere else (the kernel's ports by Vitis's naming rules, the memory's by its declared port
    map, the join by the ``add_rtl_if`` registry), so there is nothing here a human authored that a
    generator is now re-deriving.

    Parameters
    ----------
    comp_class : type[HwModule]
        The composite carrying ``add_rtl_mod`` / ``add_rtl_if`` registries.
    params : dict
        Elaboration parameters for *comp_class* (the same ones the kernel is generated at — a
        wrapper built at a different width would not match the kernel it instantiates).
    width : int
        Payload width for the kernel's spec, as passed to ``composite_top_spec``.
    output_dir : str
        Directory **relative to** ``BuildConfig.root_dir``.  Defaults to ``xsi`` — the wrapper is an
        input to xsim, and it lands where the ``.f`` that names it lives.
    """

    description: str = "Emit the wrapper joining a generated kernel to its hand-written RTL."
    params: ClassVar[dict] = {}

    comp_class: type[HwModule]
    elab_params: dict = field(default_factory=dict)
    width: int = 64
    output_dir: str = "xsi"

    def __post_init__(self) -> None:
        super().__post_init__()
        self._spec = self._wrapper_spec()

    def _wrapper_spec(self):
        from waveflow.build.composite_gen import composite_top_spec
        from waveflow.build.wrapper_gen import wrapper_spec

        comp = elaborate(self.comp_class, dict(self.elab_params))
        return wrapper_spec(comp, composite_top_spec(comp, width=int(self.width)))

    @property
    def spec(self):
        """The resolved :class:`~waveflow.build.wrapper_gen.WrapperSpec`."""
        return self._spec

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {f"wrapper_{self._spec.name}": Path(self.output_dir) / f"{self._spec.name}.v"}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        from waveflow.build.wrapper_gen import render_wrapper

        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out = root / self.output_dir
        out.mkdir(parents=True, exist_ok=True)
        dst = out / f"{self._spec.name}.v"
        dst.write_text(render_wrapper(self._spec), encoding="utf-8")
        return {f"wrapper_{self._spec.name}": dst}


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
