"""BuildStep wrappers for HLS codegen."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildStep
from waveflow.build.elaborate import elaborate
from waveflow.build.hwcodegen import extract_kernel
from waveflow.build.hwgen import (
    _collect_hooks_with_params,
    cpp_kernel_name,
    kernel_files_to_str,
)
from waveflow.hw.hw_component import HwComponent


@dataclass(kw_only=True)
class HlsCodegenStep(BuildStep):
    """Generate ``<component>.hpp``, ``<component>.cpp``, and one impl stub per hook.

    File-lifecycle rules:
    - ``<kernel>.hpp`` / ``<kernel>.cpp`` are always rewritten on every ``run()``
      into ``output_dir``.
    - ``<kernel>_<hook>_impl.{cpp,tpp}`` lives in ``impl_dir`` (defaulting to
      ``output_dir``) and is written **only if absent**, so user (or future
      AI-completion) edits survive rebuilds.  A hook with no
      ``HwParam``-driven stream argument lands in ``.cpp``; a templated hook
      lands in ``.tpp`` (per the hook-templating contract).

    When ``impl_dir`` differs from ``output_dir``, the generated ``.hpp``
    emits an ``#include "<relpath>"`` line pointing at the ``.tpp`` so the
    template definition is visible to compilers that include the header.

    If a hook switches between templated and non-templated across runs, the
    step refuses to proceed: a stale ``.cpp``/``.tpp`` lingers on disk and
    must be deleted explicitly.  Auto-deletion is intentionally not done.
    """

    description: str = (
        "Generate HLS kernel files (.hpp, .cpp, impl stubs) from an HwComponent."
    )
    params: ClassVar[dict] = {}

    comp_class: type[HwComponent]
    source_artifact: str
    output_dir: str = "."
    impl_dir: str | None = None  # None = use output_dir
    is_testbench: bool | None = None  # None = auto-detect via HwTestbench

    def __post_init__(self) -> None:
        super().__post_init__()
        self._kernel_name = cpp_kernel_name(self.comp_class)
        # Resolve testbench mode: explicit flag wins; otherwise auto-detect.
        if self.is_testbench is None:
            self._is_testbench = bool(
                getattr(self.comp_class, '_is_testbench', False)
            )
        else:
            self._is_testbench = self.is_testbench
        # Kernel-mode side state is meaningless in testbench mode but keep
        # the attrs defined for any code paths that grep for them.
        if self._is_testbench:
            self._hook_info: list[tuple[str, str]] = []
        else:
            self._hook_info = self._discover_hooks()
        self._impl_dir = self.impl_dir if self.impl_dir is not None else self.output_dir

    def _discover_hooks(self) -> list[tuple[str, str]]:
        """Return ``[(hook_name, extension)]`` per hook on the component.

        Extension is ``"tpp"`` if the hook is templated, ``"cpp"`` otherwise.
        """
        comp = elaborate(self.comp_class)
        tree = extract_kernel(comp)
        result: list[tuple[str, str]] = []
        for hook, tparams in _collect_hooks_with_params(tree):
            ext = "tpp" if tparams else "cpp"
            result.append((hook.__name__, ext))  # type: ignore[attr-defined]
        return result

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.source_artifact]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        out_dir = Path(self.output_dir)
        kn = self._kernel_name
        if self._is_testbench:
            # Testbench mode: single `<kernel>_tb.cpp`.  No header, no sticky
            # impl files — the testbench main() body is fully framework-
            # controlled.
            return {f"{kn}_tb": out_dir / f"{kn}_tb.cpp"}
        impl_dir = Path(self._impl_dir)
        d: dict[str, Path] = {
            f"{kn}_hpp": out_dir / f"{kn}.hpp",
            f"{kn}_cpp": out_dir / f"{kn}.cpp",
        }
        for hook_name, ext in self._hook_info:
            d[f"{kn}_{hook_name}_impl"] = impl_dir / f"{kn}_{hook_name}_impl.{ext}"
        return d

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        if self._is_testbench:
            return self._run_testbench(config)
        files = kernel_files_to_str(
            self.comp_class,
            output_dir=self.output_dir,
            impl_dir=self._impl_dir,
        )
        # BuildConfig.__post_init__ normalises root_dir to a Path, but the type
        # annotation is broader; narrow it here.
        root_dir = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out_root = root_dir / self.output_dir
        impl_root = root_dir / self._impl_dir
        out_root.mkdir(parents=True, exist_ok=True)
        impl_root.mkdir(parents=True, exist_ok=True)

        artifacts: dict[str, Any] = {}
        kn = self._kernel_name

        # Always overwrite .hpp and .cpp into output_dir.
        for ext in ("hpp", "cpp"):
            filename = f"{kn}.{ext}"
            path = out_root / filename
            path.write_text(files[filename], encoding="utf-8")
            artifacts[f"{kn}_{ext}"] = path

        # Stale-file detection: if a hook's expected extension differs from
        # what already exists in impl_dir, refuse to proceed.
        for hook_name, ext in self._hook_info:
            stem = f"{kn}_{hook_name}_impl"
            other_ext = "cpp" if ext == "tpp" else "tpp"
            stale_path = impl_root / f"{stem}.{other_ext}"
            if stale_path.exists():
                raise RuntimeError(
                    f"Stale impl file detected: {stale_path}. Hook "
                    f"'{hook_name}' is now "
                    f"{'templated' if ext == 'tpp' else 'non-templated'}; "
                    f"expected file is {stem}.{ext}. Delete the stale file "
                    f"and re-run."
                )

        # Sticky impl stubs: write into impl_dir only if absent.
        for hook_name, ext in self._hook_info:
            filename = f"{kn}_{hook_name}_impl.{ext}"
            path = impl_root / filename
            if not path.exists():
                path.write_text(files[filename], encoding="utf-8")
            artifacts[f"{kn}_{hook_name}_impl"] = path

        return artifacts

    def _run_testbench(self, config: BuildConfig) -> dict[str, Any]:
        """Testbench-mode run: emit a single ``<kernel>_tb.cpp``.

        The testbench file is always overwritten — its body is fully
        framework-controlled, so there is no sticky-impl semantics like
        the kernel-mode ``.tpp`` files.
        """
        from waveflow.build.hwgen import tb_files_to_str
        files = tb_files_to_str(
            self.comp_class, output_dir=self.output_dir,
        )
        root_dir = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out_root = root_dir / self.output_dir
        out_root.mkdir(parents=True, exist_ok=True)
        kn = self._kernel_name
        filename = f"{kn}_tb.cpp"
        path = out_root / filename
        path.write_text(files[filename], encoding="utf-8")
        return {f"{kn}_tb": path}
