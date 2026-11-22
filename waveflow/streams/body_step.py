"""body_step.py -- :class:`StreamBodyStep`: copy one stream block's HLS body into a build.

Every block in ``waveflow.streams`` ships its body under ``src/`` as framework code, as the RF blocks do;
its step is this one with the body's name.  One output, the header, at ``output_dir`` (relative to the
build root)."""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from waveflow.build.build import Buildable, BuildConfig

SRC_DIR = Path(__file__).resolve().parent / "src"

__all__ = ["StreamBodyStep", "SRC_DIR"]


class StreamBodyStep(Buildable):
    """Copy ``<KEY>.h`` from ``waveflow/streams/src`` into a build.  A subclass names its body in
    ``KEY`` (the output key and the file's stem)."""

    KEY: ClassVar[str] = ""

    def __init__(self, output_dir: str | Path = ".") -> None:
        super().__init__()
        if not self.KEY:
            raise TypeError(f"{type(self).__name__} must name its body in KEY")
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {self.KEY: self._output_dir / f"{self.KEY}.h"}

    def generate(self, key: str, config: BuildConfig) -> str:
        if key != self.KEY:
            raise KeyError(f"Unknown {type(self).__name__} output key: {key!r}")
        return (SRC_DIR / f"{self.KEY}.h").read_text(encoding="utf-8")
