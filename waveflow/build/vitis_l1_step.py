"""Build support for the Vitis L1 wrappers — S2 of ``plans/vitis_l1_hwmodule.md``.

Two things a design wrapping vendor IP needs that a generated-body design does not:

* **the body header, copied** — nothing can extract an ``xf::dsp::fft::fft<>`` instantiation from
  a Python ``run_iter``, so :class:`VitisL1Step` copies a hand-written one, exactly as
  :class:`~waveflow.build.streamutils.MemStreamStep` does for the ``m_axi`` owners;
* **an include path to the vendor headers**, which are *not* copied anywhere — they stay in the
  Vitis install and are reached with ``-I``.  :func:`vitis_fft_include_dir` resolves it, and
  ``render_tcl(include_dirs=...)`` puts it in the generated TCL.
"""
from __future__ import annotations

import os
from pathlib import Path

from waveflow.build.build import Buildable, BuildConfig

_SRC_DIR = Path(__file__).resolve().parent

#: Where Vitis ships the DSP library.  ``tps/xf_dsp`` is code-identical to the upstream
#: ``v2025.1_re`` tag across all 45 L1 headers (only copyright glyphs differ), which is why a
#: checkout is not required — see ``tests/vitis_l1/README.md``.  Both layouts appear in the wild:
#: some installs put ``tps/`` under the version root, others under ``<root>/Vitis/``.
_REL_CANDIDATES = (
    "tps/xf_dsp/L1/include/hw/vitis_fft/fixed",
    "Vitis/tps/xf_dsp/L1/include/hw/vitis_fft/fixed",
)
_ROOT_CANDIDATES = (
    "/tools/Xilinx/2025.1", "/opt/Xilinx/2025.1", "/c/Xilinx/2025.1", "C:/Xilinx/2025.1",
)


def vitis_fft_include_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """The directory to ``-I`` so ``#include "vitis_fft/hls_ssr_fft.hpp"`` resolves.

    Order: an *explicit* argument, then ``$WF_VITIS_LIBS`` (an environment variable someone set on
    purpose outranks a guess), then the Vitis install — so a user with only Vitis installed needs
    neither a checkout nor an environment variable, which is the point.

    Raises rather than returning a path that would fail at csynth with a confusing error.
    """
    tried: list[str] = []

    def ok(d: Path) -> bool:
        tried.append(str(d))
        return (d / "vitis_fft" / "hls_ssr_fft.hpp").is_file()

    if explicit is not None:
        d = Path(explicit)
        if ok(d):
            return d
    env = os.environ.get("WF_VITIS_LIBS")
    if env:
        d = Path(env)
        if ok(d):
            return d
    for root in (os.environ.get("XILINX_VITIS"), *_ROOT_CANDIDATES):
        if not root:
            continue
        for rel in _REL_CANDIDATES:
            d = Path(root) / rel
            if ok(d):
                return d
    raise FileNotFoundError(
        "Could not locate the Vitis DSP FFT headers (vitis_fft/hls_ssr_fft.hpp). Set "
        "WF_VITIS_LIBS to the directory that contains vitis_fft/, or pass it explicitly. Tried:\n  "
        + "\n  ".join(tried))


class VitisL1Step(Buildable):
    """Copies the hand-written Vitis L1 task bodies into a design's include directory.

    Verbatim ``read_text`` -> ``write_text``, like ``MemStreamStep``: this is the *copy* path, not
    the extract path.  ``TaskBodyStep`` renders a body out of ``run_iter``; nothing can do that for
    a vendor template instantiation, so the body is checked in under ``waveflow/build/`` and the
    Python ``run_iter`` stays the pysim golden beside it.

    Note the recorded trap: the RTL staleness digest hashes ``include/*.{h,hpp,cpp}`` — the
    **copies**, not ``waveflow/build/*.h``.  Editing the source without this step re-running is the
    documented way to get a stale gate that still passes.
    """

    def __init__(self, output_dir: str | Path = ".") -> None:
        super().__init__()
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        """Output directory, relative to ``BuildConfig.root_dir``."""
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {"vitis_fft_task": self._output_dir / "vitis_fft_task.h"}

    def generate(self, key: str, config: BuildConfig) -> str:
        src_names = {"vitis_fft_task": "vitis_fft_task.h"}
        if key not in src_names:
            raise KeyError(f"Unknown VitisL1Step output key: {key!r}")
        src = _SRC_DIR / src_names[key]
        if not src.exists():
            raise FileNotFoundError(f"Vitis L1 task body not found: {src}")
        return src.read_text(encoding="utf-8")
