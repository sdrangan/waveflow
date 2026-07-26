"""Tests for HlsCodegenStep — BuildStep wrapper around HLS codegen."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest

from waveflow.build.build import BuildConfig
from waveflow.build.hwcodegen_steps import HlsCodegenStep
from waveflow.build.hwgen import kernel_files_to_str
from tests.hw.test_resolve import Demo


@dataclass
class _MultiVariantDemo(Demo):
    """A Demo subclass with a second param_supports variant.

    Defined at module scope so dataclass can resolve the ``ClassVar``
    annotation under ``from __future__ import annotations`` — function-local
    classes fail that resolution and treat ``param_supports`` as a field.
    """
    cpp_kernel_name: ClassVar[str | None] = "mvd"
    param_supports: ClassVar[dict[str, dict[str, Any]] | None] = {
        "bw64": {"in_bw": 64, "out_bw": 64},
    }


# ---------------------------------------------------------------------------
# Phase 1: skeleton + always-overwrite hpp/cpp
# ---------------------------------------------------------------------------

def test_produces_default_output_dir():
    step = HlsCodegenStep(comp_class=Demo, source_artifact="demo_src")
    produces = step.produces
    assert set(produces.keys()) == {"demo_hpp", "demo_cpp", "demo_process_impl"}
    assert produces["demo_hpp"] == Path("demo.hpp")
    assert produces["demo_cpp"] == Path("demo.cpp")
    assert produces["demo_process_impl"] == Path("demo_process_impl.cpp")


def test_produces_with_output_dir():
    step = HlsCodegenStep(
        comp_class=Demo,
        source_artifact="demo_src",
        output_dir="gen",
    )
    produces = step.produces
    assert produces["demo_hpp"] == Path("gen/demo.hpp")
    assert produces["demo_cpp"] == Path("gen/demo.cpp")
    assert produces["demo_process_impl"] == Path("gen/demo_process_impl.cpp")


def test_consumes_is_source_artifact():
    step = HlsCodegenStep(comp_class=Demo, source_artifact="demo_src")
    assert step.consumes == ["demo_src"]


def test_run_writes_hpp_and_cpp(tmp_path: Path):
    step = HlsCodegenStep(comp_class=Demo, source_artifact="demo_src")
    config = BuildConfig(root_dir=tmp_path)
    artifacts = step.run(config)

    hpp = tmp_path / "demo.hpp"
    cpp = tmp_path / "demo.cpp"
    assert hpp.exists()
    assert cpp.exists()

    expected = kernel_files_to_str(Demo)
    assert hpp.read_text(encoding="utf-8") == expected["demo.hpp"]
    assert cpp.read_text(encoding="utf-8") == expected["demo.cpp"]

    # Artifacts dict contains expected keys and points at the written files.
    assert artifacts["demo_hpp"] == hpp
    assert artifacts["demo_cpp"] == cpp


def test_second_run_rewrites_hpp_and_cpp(tmp_path: Path):
    """Running twice must update the hpp/cpp mtimes (always-overwrite rule)."""
    import os
    step = HlsCodegenStep(comp_class=Demo, source_artifact="demo_src")
    config = BuildConfig(root_dir=tmp_path)
    step.run(config)
    hpp = tmp_path / "demo.hpp"
    cpp = tmp_path / "demo.cpp"

    # Backdate the mtimes so a rewrite shows up clearly.
    old_time = 0.0
    os.utime(hpp, (old_time, old_time))
    os.utime(cpp, (old_time, old_time))

    step.run(config)
    assert hpp.stat().st_mtime > old_time
    assert cpp.stat().st_mtime > old_time


def test_run_creates_output_dir(tmp_path: Path):
    step = HlsCodegenStep(
        comp_class=Demo,
        source_artifact="demo_src",
        output_dir="nested/gen",
    )
    config = BuildConfig(root_dir=tmp_path)
    step.run(config)
    assert (tmp_path / "nested" / "gen" / "demo.hpp").exists()
    assert (tmp_path / "nested" / "gen" / "demo.cpp").exists()


# ---------------------------------------------------------------------------
# Phase 2: sticky impl-file behavior
# ---------------------------------------------------------------------------

def test_first_run_creates_impl_stub(tmp_path: Path):
    step = HlsCodegenStep(comp_class=Demo, source_artifact="demo_src")
    step.run(BuildConfig(root_dir=tmp_path))
    impl = tmp_path / "demo_process_impl.cpp"
    assert impl.exists()
    content = impl.read_text(encoding="utf-8")
    assert "// TODO: implement process" in content


def test_rerun_preserves_user_edited_impl(tmp_path: Path):
    step = HlsCodegenStep(comp_class=Demo, source_artifact="demo_src")
    config = BuildConfig(root_dir=tmp_path)
    step.run(config)

    impl = tmp_path / "demo_process_impl.cpp"
    custom = "// hand-written implementation, do not overwrite\n"
    impl.write_text(custom, encoding="utf-8")

    step.run(config)
    assert impl.read_text(encoding="utf-8") == custom


def test_rerun_does_not_touch_existing_impl_mtime(tmp_path: Path):
    """A second run must not even rewrite identical contents (mtime is preserved)."""
    import os
    step = HlsCodegenStep(comp_class=Demo, source_artifact="demo_src")
    config = BuildConfig(root_dir=tmp_path)
    step.run(config)

    impl = tmp_path / "demo_process_impl.cpp"
    backdated = 1_000_000_000.0
    os.utime(impl, (backdated, backdated))

    step.run(config)
    assert impl.stat().st_mtime == backdated


# ---------------------------------------------------------------------------
# Phase 3: DAG integration + freshness skipping
# ---------------------------------------------------------------------------

def _make_dag_with_source(tmp_path: Path):
    """Build a DAG with a real SourceStep + HlsCodegenStep against ``tmp_path``."""
    from waveflow.build.build import BuildDag, SourceStep

    src = tmp_path / "demo_source.py"
    src.write_text("# placeholder source\n", encoding="utf-8")

    dag = BuildDag()
    dag.add(SourceStep(artifact="demo_src", path="demo_source.py"))
    dag.add(HlsCodegenStep(
        comp_class=Demo,
        source_artifact="demo_src",
        output_dir="gen",
    ))
    return dag, src


def test_dag_run_writes_all_three_files(tmp_path: Path):
    dag, _src = _make_dag_with_source(tmp_path)
    results = dag.run(BuildConfig(root_dir=tmp_path))

    for name, result in results.items():
        assert result.success, f"{name} failed: {result.message}"

    gen = tmp_path / "gen"
    assert (gen / "demo.hpp").exists()
    assert (gen / "demo.cpp").exists()
    assert (gen / "demo_process_impl.cpp").exists()


def test_dag_second_run_skips_step(tmp_path: Path):
    dag, _src = _make_dag_with_source(tmp_path)
    config = BuildConfig(root_dir=tmp_path)
    dag.run(config)
    results = dag.run(config)
    codegen = results["HlsCodegenStep"]
    assert codegen.success
    assert codegen.skipped is True


def test_dag_source_touch_invalidates_step(tmp_path: Path):
    import os
    dag, src = _make_dag_with_source(tmp_path)
    config = BuildConfig(root_dir=tmp_path)
    dag.run(config)

    # Touch the source forward so the produced files look stale relative to it.
    future = src.stat().st_mtime + 10.0
    os.utime(src, (future, future))

    results = dag.run(config)
    assert results["HlsCodegenStep"].skipped is False


def test_dag_rebuild_preserves_impl_under_cascade(tmp_path: Path):
    """Even when the .hpp/.cpp are re-generated on cascade, impl file is sticky."""
    import os
    dag, src = _make_dag_with_source(tmp_path)
    config = BuildConfig(root_dir=tmp_path)
    dag.run(config)

    impl = tmp_path / "gen" / "demo_process_impl.cpp"
    custom = "// user-edited content\n"
    impl.write_text(custom, encoding="utf-8")

    future = src.stat().st_mtime + 10.0
    os.utime(src, (future, future))

    results = dag.run(config)
    assert results["HlsCodegenStep"].skipped is False
    assert impl.read_text(encoding="utf-8") == custom


def test_dag_force_reruns_step_but_impl_stays_sticky(tmp_path: Path):
    dag, _src = _make_dag_with_source(tmp_path)
    config = BuildConfig(root_dir=tmp_path)
    dag.run(config)

    impl = tmp_path / "gen" / "demo_process_impl.cpp"
    custom = "// user-edited\n"
    impl.write_text(custom, encoding="utf-8")

    results = dag.run(config, force=True)
    assert results["HlsCodegenStep"].skipped is False
    assert impl.read_text(encoding="utf-8") == custom


# ---------------------------------------------------------------------------
# Hook-template Phase 5: per-hook extension awareness + stale-file detection
# ---------------------------------------------------------------------------

def _make_tmpl_step(tmp_path: Path):
    """A HlsCodegenStep that targets the templated-hook fixture from test_hwgen."""
    from tests.hw.test_hwgen import _TmplComp
    return HlsCodegenStep(
        comp_class=_TmplComp, source_artifact="demo_src", output_dir="gen",
    )


def test_produces_uses_tpp_extension_for_templated_hook():
    from tests.hw.test_hwgen import _TmplComp
    step = HlsCodegenStep(comp_class=_TmplComp, source_artifact="src")
    produces = step.produces
    assert produces["tcomp_process_impl"] == Path("tcomp_process_impl.tpp")
    assert "tcomp_hpp" in produces
    assert "tcomp_cpp" in produces


def test_run_writes_tpp_for_templated_hook(tmp_path: Path):
    step = _make_tmpl_step(tmp_path)
    step.run(BuildConfig(root_dir=tmp_path))
    tpp = tmp_path / "gen" / "tcomp_process_impl.tpp"
    assert tpp.exists()
    content = tpp.read_text(encoding="utf-8")
    assert "template <int in_bw>" in content
    assert "// TODO: implement process" in content


def test_run_sticky_preserves_tpp_on_rerun(tmp_path: Path):
    step = _make_tmpl_step(tmp_path)
    config = BuildConfig(root_dir=tmp_path)
    step.run(config)

    tpp = tmp_path / "gen" / "tcomp_process_impl.tpp"
    custom = "// user-edited tpp content\n"
    tpp.write_text(custom, encoding="utf-8")

    step.run(config)
    assert tpp.read_text(encoding="utf-8") == custom


def test_run_stale_cpp_when_hook_now_templated_raises(tmp_path: Path):
    """A leftover .cpp from a prior non-templated state must trigger an error."""
    step = _make_tmpl_step(tmp_path)
    out_dir = tmp_path / "gen"
    out_dir.mkdir(parents=True, exist_ok=True)
    stale = out_dir / "tcomp_process_impl.cpp"
    stale.write_text("// stale non-templated impl\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Stale impl file"):
        step.run(BuildConfig(root_dir=tmp_path))


def test_run_stale_tpp_when_hook_now_non_templated_raises(tmp_path: Path):
    """And the reverse — a leftover .tpp must trigger an error for a .cpp hook."""
    step = HlsCodegenStep(
        comp_class=Demo,
        source_artifact="demo_src",
        output_dir="gen",
    )
    out_dir = tmp_path / "gen"
    out_dir.mkdir(parents=True, exist_ok=True)
    stale = out_dir / "demo_process_impl.tpp"
    stale.write_text("// stale templated impl\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Stale impl file"):
        step.run(BuildConfig(root_dir=tmp_path))


# ---------------------------------------------------------------------------
# Phase 12b Phase 1: impl_dir parameter + relative include path
# ---------------------------------------------------------------------------

def test_produces_splits_paths_when_impl_dir_differs():
    from tests.hw.test_hwgen import _TmplComp
    step = HlsCodegenStep(
        comp_class=_TmplComp,
        source_artifact="src",
        output_dir="gen",
        impl_dir=".",
    )
    produces = step.produces
    assert produces["tcomp_hpp"] == Path("gen/tcomp.hpp")
    assert produces["tcomp_cpp"] == Path("gen/tcomp.cpp")
    assert produces["tcomp_process_impl"] == Path("tcomp_process_impl.tpp")


def test_impl_dir_none_defaults_to_output_dir():
    """Backward compat: omitting impl_dir keeps impl files under output_dir."""
    from tests.hw.test_hwgen import _TmplComp
    step = HlsCodegenStep(
        comp_class=_TmplComp,
        source_artifact="src",
        output_dir="gen",
    )
    produces = step.produces
    assert produces["tcomp_process_impl"] == Path("gen/tcomp_process_impl.tpp")


def test_run_writes_hpp_to_output_and_tpp_to_impl_dir(tmp_path: Path):
    from tests.hw.test_hwgen import _TmplComp
    step = HlsCodegenStep(
        comp_class=_TmplComp,
        source_artifact="src",
        output_dir="gen",
        impl_dir=".",
    )
    step.run(BuildConfig(root_dir=tmp_path))
    hpp = tmp_path / "gen" / "tcomp.hpp"
    tpp = tmp_path / "tcomp_process_impl.tpp"
    assert hpp.exists()
    assert tpp.exists()
    assert not (tmp_path / "gen" / "tcomp_process_impl.tpp").exists()


def test_header_emits_relative_include_path_when_impl_dir_differs(tmp_path: Path):
    from tests.hw.test_hwgen import _TmplComp
    step = HlsCodegenStep(
        comp_class=_TmplComp,
        source_artifact="src",
        output_dir="gen",
        impl_dir=".",
    )
    step.run(BuildConfig(root_dir=tmp_path))
    hpp_content = (tmp_path / "gen" / "tcomp.hpp").read_text()
    assert '#include "../tcomp_process_impl.tpp"' in hpp_content
    # The bare-filename form must NOT appear when impl_dir differs.
    assert '#include "tcomp_process_impl.tpp"' not in hpp_content


def test_stale_check_looks_at_impl_dir_not_output_dir(tmp_path: Path):
    """A stale .cpp lingering in impl_dir is detected; one in output_dir is not."""
    from tests.hw.test_hwgen import _TmplComp
    step = HlsCodegenStep(
        comp_class=_TmplComp,
        source_artifact="src",
        output_dir="gen",
        impl_dir=".",
    )
    # Place a stale .cpp where impl_dir is — should trigger the stale check.
    stale = tmp_path / "tcomp_process_impl.cpp"
    stale.write_text("// stale", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Stale impl file"):
        step.run(BuildConfig(root_dir=tmp_path))


def test_stale_in_output_dir_ignored_when_impl_dir_differs(tmp_path: Path):
    """A leftover file in output_dir is not the impl file's location and is ignored."""
    from tests.hw.test_hwgen import _TmplComp
    step = HlsCodegenStep(
        comp_class=_TmplComp,
        source_artifact="src",
        output_dir="gen",
        impl_dir=".",
    )
    out_dir = tmp_path / "gen"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Place a stale .cpp in output_dir — should be ignored.
    decoy = out_dir / "tcomp_process_impl.cpp"
    decoy.write_text("// decoy", encoding="utf-8")
    step.run(BuildConfig(root_dir=tmp_path))  # no raise expected
    # The impl file landed in impl_dir.
    assert (tmp_path / "tcomp_process_impl.tpp").exists()


def test_kernel_files_to_str_emits_relative_include():
    """The header emission helper directly returns a header with the relpath include."""
    from waveflow.build.hwgen import kernel_files_to_str
    from tests.hw.test_hwgen import _TmplComp

    files = kernel_files_to_str(_TmplComp, output_dir="gen", impl_dir=".")
    assert '#include "../tcomp_process_impl.tpp"' in files["tcomp.hpp"]


# ---------------------------------------------------------------------------
# Phase 13 Phase 3: HlsCodegenStep with multi-variant param_supports
# ---------------------------------------------------------------------------

def test_single_variant_demo_writes_one_concrete_kernel(tmp_path: Path):
    """Single-variant case (no param_supports): one concrete void demo(...)."""
    step = HlsCodegenStep(
        comp_class=Demo,
        source_artifact="demo_src",
        output_dir="gen",
    )
    step.run(BuildConfig(root_dir=tmp_path))
    cpp = (tmp_path / "gen" / "demo.cpp").read_text(encoding="utf-8")
    assert cpp.count("void demo(") == 1
    assert "template" not in cpp


def test_multi_variant_emits_one_concrete_kernel_per_variant(tmp_path: Path):
    """A component with param_supports yields default + variant in one .cpp."""
    step = HlsCodegenStep(
        comp_class=_MultiVariantDemo,
        source_artifact="demo_src",
        output_dir="gen",
    )
    step.run(BuildConfig(root_dir=tmp_path))
    cpp = (tmp_path / "gen" / "mvd.cpp").read_text(encoding="utf-8")
    assert "void mvd(" in cpp
    assert "void mvd_bw64(" in cpp
    assert "axi4s_word<32>" in cpp
    assert "axi4s_word<64>" in cpp
    assert "template" not in cpp

    hpp = (tmp_path / "gen" / "mvd.hpp").read_text(encoding="utf-8")
    assert "void mvd(" in hpp
    assert "void mvd_bw64(" in hpp
