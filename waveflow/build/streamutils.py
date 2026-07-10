from __future__ import annotations

from pathlib import Path

from waveflow.build.build import Buildable, BuildConfig, BuildResult, FileArtifact


_SRC_DIR = Path(__file__).resolve().parent


class StreamUtilsStep(Buildable):
    """Build step that copies the streamutils support files to an output directory.

    ``streamutils_hls.h`` and ``streamutils_tb.h`` are always written.
    ``streamutils.cpp`` is written only for Vitis versions older than 2025.1
    (the conservative default when no version is specified).  If the version is
    2025.1 or newer and a stale ``streamutils.cpp`` exists in the output
    directory it is removed.

    Parameters
    ----------
    output_dir : str | Path
        Directory path **relative to** ``BuildConfig.root_dir`` where the
        streamutils files will be written.  Defaults to ``"."`` (the root
        directory itself).
    """

    def __init__(self, output_dir: str | Path = ".") -> None:
        super().__init__()
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        """Output directory path relative to ``BuildConfig.root_dir``."""
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {
            "hls": self._output_dir / "streamutils_hls.h",
            "tb": self._output_dir / "streamutils_tb.h",
        }

    def generate(self, key: str, config: BuildConfig) -> str:
        src_names: dict[str, str] = {
            "hls": "streamutils_hls.h",
            "tb": "streamutils_tb.h",
            "cpp": "streamutils.cpp",
        }
        if key not in src_names:
            raise KeyError(f"Unknown StreamUtilsStep output key: {key!r}")
        src_path = _SRC_DIR / src_names[key]
        if not src_path.exists():
            raise FileNotFoundError(f"StreamUtils source file not found: {src_path}")
        return src_path.read_text(encoding="utf-8")

    def run(self, config: BuildConfig, results: dict = {}) -> BuildResult:
        artifacts: dict = {}
        try:
            out_dir = config.root_dir / self._output_dir
            out_dir.mkdir(parents=True, exist_ok=True)

            for key in ("hls", "tb"):
                content = self.generate(key, config)
                out_path = config.root_dir / self.build_outputs[key]
                out_path.write_text(content, encoding="utf-8")
                artifacts[key] = FileArtifact(path=out_path)

            cpp_path = out_dir / "streamutils.cpp"
            if config.needs_legacy_streamutils_cpp():
                content = self.generate("cpp", config)
                cpp_path.write_text(content, encoding="utf-8")
                artifacts["cpp"] = FileArtifact(path=cpp_path)
            else:
                if cpp_path.exists():
                    cpp_path.unlink()

            return BuildResult(success=True, artifacts=artifacts)
        except Exception as exc:
            return BuildResult(success=False, message=str(exc))


class MemMgrStep(Buildable):
    """Build step that copies the memory-manager headers to an output directory.

    Writes ``memmgr.hpp`` and ``memmgr_tb.hpp``.

    Parameters
    ----------
    output_dir : str | Path
        Directory path **relative to** ``BuildConfig.root_dir`` where the
        memmgr files will be written.  Defaults to ``"."`` (the root directory).
    """

    def __init__(self, output_dir: str | Path = ".") -> None:
        super().__init__()
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        """Output directory path relative to ``BuildConfig.root_dir``."""
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {
            "memmgr": self._output_dir / "memmgr.hpp",
            "memmgr_tb": self._output_dir / "memmgr_tb.hpp",
        }

    def generate(self, key: str, config: BuildConfig) -> str:
        src_names: dict[str, str] = {
            "memmgr": "memmgr.hpp",
            "memmgr_tb": "memmgr_tb.hpp",
        }
        if key not in src_names:
            raise KeyError(f"Unknown MemMgrStep output key: {key!r}")
        src_path = _SRC_DIR / src_names[key]
        if not src_path.exists():
            raise FileNotFoundError(f"MemMgr source file not found: {src_path}")
        return src_path.read_text(encoding="utf-8")


class MemStreamStep(Buildable):
    """Build step that copies the fixed ``MemRStream`` / ``MemWStream`` task-body headers to an
    output directory.

    ``mem_r_stream_task.h`` / ``mem_w_stream_task.h`` hold the width-templated
    (``template<int MEM_DW>``) single-firing ``hls::task`` bodies (the validated sandbox
    ``a2s`` / ``s2a``).  Copied verbatim — the generated ``mem_stream`` top instantiates them at a
    concrete width — mirroring :class:`StreamUtilsStep` / :class:`MemMgrStep` (read_text ->
    write_text from ``waveflow/build/``).

    Parameters
    ----------
    output_dir : str | Path
        Directory path **relative to** ``BuildConfig.root_dir`` where the task headers are written.
        Defaults to ``"."`` (the root directory).
    """

    def __init__(self, output_dir: str | Path = ".") -> None:
        super().__init__()
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        """Output directory path relative to ``BuildConfig.root_dir``."""
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {
            "mem_r_stream_task": self._output_dir / "mem_r_stream_task.h",
            "mem_w_stream_task": self._output_dir / "mem_w_stream_task.h",
        }

    def generate(self, key: str, config: BuildConfig) -> str:
        src_names: dict[str, str] = {
            "mem_r_stream_task": "mem_r_stream_task.h",
            "mem_w_stream_task": "mem_w_stream_task.h",
        }
        if key not in src_names:
            raise KeyError(f"Unknown MemStreamStep output key: {key!r}")
        src_path = _SRC_DIR / src_names[key]
        if not src_path.exists():
            raise FileNotFoundError(f"MemStream source file not found: {src_path}")
        return src_path.read_text(encoding="utf-8")
