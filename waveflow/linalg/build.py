"""build.py — the headers a design built from linear-algebra components needs.

:class:`LinalgStep` copies the shared helper headers (``wf_lanes.h``, ``wf_matrix_io.h``,
``wf_linalg_msg.h``) and any component task bodies into a design's include directory, and renders
:data:`~waveflow.linalg.formats.TRAITS_HEADER` with one specialization per format id
(:func:`~waveflow.linalg.formats.render_traits_header`), plus the array utilities of every memory
element type those specializations use.

The header schema of the messages (``wf_linalg_header.h``), the components' command schemas and
``streamutils_hls.h`` come from the framework's own steps; :func:`linalg_headers_dag` puts them
together, which is what a design or a test that builds alone needs.  A component says what it needs
in a :class:`LinalgParts` (its ``linalg_parts()``), and :func:`collect_parts` gathers them from a
design's tree.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from waveflow.build.build import Buildable, BuildConfig, BuildDag, BuildResult
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import gen_array_utils
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.linalg.formats import (
    TRAITS_HEADER,
    Traits,
    render_traits_header,
    unique_traits,
)
from waveflow.linalg.lanes import WORD_BITS_SUPPORTED
from waveflow.linalg.message import LinalgHeader

#: Where the hand-written headers live in the package.
BUILD_DIR = Path(__file__).resolve().parents[1] / "build"
#: The helper headers every linear-algebra design includes.
HELPERS = ("wf_lanes.h", "wf_matrix_io.h", "wf_linalg_msg.h")


@dataclass(frozen=True)
class LinalgParts:
    """What a linear-algebra component needs in its design's include directory: traits to
    render, task bodies to copy (names in ``waveflow/build/``) and command schemas to generate.
    """

    traits: tuple[Traits, ...] = ()
    bodies: tuple[str, ...] = ()
    schemas: tuple[type, ...] = ()

    def __add__(self, other: LinalgParts) -> LinalgParts:
        def merged(x: tuple, y: tuple) -> tuple:
            return tuple(dict.fromkeys((*x, *y)))

        return LinalgParts(
            merged(self.traits, other.traits),
            merged(self.bodies, other.bodies),
            merged(self.schemas, other.schemas),
        )


def collect_parts(comp) -> LinalgParts:
    """The :class:`LinalgParts` of ``comp`` and every module below it, merged in order."""
    parts = LinalgParts()
    own = getattr(comp, "linalg_parts", None)
    if callable(own):
        parts = parts + own()
    for sub in getattr(comp, "sub_comps", {}).values():
        parts = parts + collect_parts(sub)
    return parts


class LinalgStep(Buildable):
    """Copy the helper headers and ``bodies``, render the traits of ``traits``, and generate the
    array utilities of their memory elements, all into ``output_dir``.

    Parameters
    ----------
    traits
        Every component instance's :class:`~waveflow.linalg.formats.Traits`; repeats are merged,
        and two different format sets with one id raise.
    bodies
        Task-body headers (file names in ``waveflow/build/``) to copy as well.
    output_dir
        The include directory, relative to ``BuildConfig.root_dir``.
    word_bits
        The message word widths the array utilities are generated for.
    """

    def __init__(
        self,
        traits: Iterable[Traits] = (),
        bodies: Iterable[str] = (),
        output_dir: str | Path = "include",
        word_bits: Iterable[int] = WORD_BITS_SUPPORTED,
    ) -> None:
        super().__init__()
        self._traits = unique_traits(traits)
        self._bodies = tuple(dict.fromkeys((*HELPERS, *bodies)))
        self._output_dir = Path(output_dir)
        self._word_bits = sorted(int(w) for w in word_bits)

    @property
    def traits(self) -> list[Traits]:
        return list(self._traits)

    @property
    def build_outputs(self) -> dict[str, Path]:
        out = {name: self._output_dir / name for name in self._bodies}
        out[TRAITS_HEADER] = self._output_dir / TRAITS_HEADER
        return out

    def generate(self, key: str, config: BuildConfig) -> str:
        if key == TRAITS_HEADER:
            return render_traits_header(self._traits, self._output_dir.as_posix())
        return (BUILD_DIR / key).read_text(encoding="utf-8")

    def run(
        self, config: BuildConfig, results: dict[str, BuildResult] | None = None
    ) -> BuildResult:
        result = super().run(config, results or {})
        if not result.success:
            return result
        include_dir = self._output_dir.as_posix()
        seen: list = []
        for t in self._traits:
            for elem in t.mem_elems(include_dir):
                if elem not in seen:
                    seen.append(elem)
                    gen_array_utils(
                        elem, self._word_bits, cfg=config, streamutils_dir=include_dir
                    )
        return result


def linalg_headers_dag(
    traits: Iterable[Traits] = (),
    bodies: Iterable[str] = (),
    include_dir: str = "include",
    word_bits: Iterable[int] = WORD_BITS_SUPPORTED,
    schemas: Iterable[type] = (),
) -> BuildDag:
    """A DAG with ``streamutils_hls.h``, the message header schema, the command ``schemas`` and
    :class:`LinalgStep`.  The schemas are generated for every word width in ``word_bits`` and 64.
    """
    word_bits = sorted(int(w) for w in word_bits)
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=include_dir))
    for cls in dict.fromkeys((LinalgHeader, *schemas)):
        dag.add(
            DataSchemaStep(
                cls,
                word_bw_supported=sorted({*word_bits, 64}),
                include_dir=include_dir,
                framed=True,
            )
        )
    dag.add(LinalgStep(traits, bodies, include_dir, word_bits))
    return dag


def gen_linalg_headers(
    root_dir: str | Path,
    traits: Iterable[Traits] = (),
    bodies: Iterable[str] = (),
    include_dir: str = "include",
    word_bits: Iterable[int] = WORD_BITS_SUPPORTED,
    schemas: Iterable[type] = (),
) -> Path:
    """Run :func:`linalg_headers_dag` under ``root_dir``; returns the include directory."""
    config = BuildConfig(root_dir=Path(root_dir), params={})
    dag = linalg_headers_dag(traits, bodies, include_dir, word_bits, schemas)
    results = dag.run(config, force=True)
    failed = {name: r.message for name, r in results.items() if not r.success}
    if failed:
        raise RuntimeError(f"linalg header generation failed: {failed}")
    return Path(root_dir) / include_dir
