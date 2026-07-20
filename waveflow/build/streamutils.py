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
            # Standalone gate-1 mem-stream bodies (3-arg) + the done-emitting write variant.  Legacy
            # two-stream composition bodies: MemCopy retired them for the framed trio below (they own
            # m_axi, so all stay hand-written and copied rather than generated).
            "mem_r_stream_task": self._output_dir / "mem_r_stream_task.h",
            "mem_w_stream_task": self._output_dir / "mem_w_stream_task.h",
            "mem_w_stream_done_task": self._output_dir / "mem_w_stream_done_task.h",
            # In-band/framed chain (plans/memcopy_inband_integration.md): Sequencer -> reader -> writer,
            # what MemCopy is built from.  All three are hand-written (they construct descriptors and
            # drive framed_word channels, neither in the extractor vocabulary) -- including the
            # Sequencer body, unlike the retired two-stream sequencer that TaskBodyStep generated.
            "mem_seq_framed_task": self._output_dir / "mem_seq_framed_task.h",
            "mem_r_stream_framed_task": self._output_dir / "mem_r_stream_framed_task.h",
            "mem_w_stream_framed_done_task": self._output_dir / "mem_w_stream_framed_done_task.h",
            # Canonical six-stage interleaver tiles: a forwarded per-job token through every tile.
            "cmd_rx_task": self._output_dir / "cmd_rx_task.h",
            "il_mem_r_task": self._output_dir / "il_mem_r_task.h",
            "il_load_task": self._output_dir / "il_load_task.h",
            "il_compute_task": self._output_dir / "il_compute_task.h",
            "il_store_task": self._output_dir / "il_store_task.h",
            "il_mem_w_task": self._output_dir / "il_mem_w_task.h",
        }

    def generate(self, key: str, config: BuildConfig) -> str:
        src_names: dict[str, str] = {
            "mem_r_stream_task": "mem_r_stream_task.h",
            "mem_w_stream_task": "mem_w_stream_task.h",
            "mem_w_stream_done_task": "mem_w_stream_done_task.h",
            "mem_seq_framed_task": "mem_seq_framed_task.h",
            "mem_r_stream_framed_task": "mem_r_stream_framed_task.h",
            "mem_w_stream_framed_done_task": "mem_w_stream_framed_done_task.h",
            "cmd_rx_task": "cmd_rx_task.h",
            "il_mem_r_task": "il_mem_r_task.h",
            "il_load_task": "il_load_task.h",
            "il_compute_task": "il_compute_task.h",
            "il_store_task": "il_store_task.h",
            "il_mem_w_task": "il_mem_w_task.h",
        }
        if key not in src_names:
            raise KeyError(f"Unknown MemStreamStep output key: {key!r}")
        src_path = _SRC_DIR / src_names[key]
        if not src_path.exists():
            raise FileNotFoundError(f"MemStream source file not found: {src_path}")
        return src_path.read_text(encoding="utf-8")


class XsiHarnessStep(Buildable):
    """Build step that copies the XSI testbench harness into an example's ``xsi/`` directory.

    The harness is **framework**, not example code: ``xsi_bfm.h`` models AXI4 / AXI4-Stream and knows
    nothing about any kernel (see :mod:`waveflow.build.xsi`), and ``xsi_loader`` + ``run.bat`` are the
    generic XSI flow.  They lived in ``examples/interleaver/xsi/`` only because that is where the
    first four testbenches were written, which made any second example wanting XSI reach across into
    a sibling example.

    Copied rather than included-in-place for the same reason the task-body headers are (cf.
    :class:`MemStreamStep`): each example builds in its own directory, and ``run.bat`` compiles the
    testbench against files beside it.

    Parameters
    ----------
    output_dir : str | Path
        Directory path **relative to** ``BuildConfig.root_dir`` — the example's ``xsi/``.
    """

    def __init__(self, output_dir: str | Path = "xsi") -> None:
        super().__init__()
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {
            "xsi_bfm": self._output_dir / "xsi_bfm.h",
            "xsi_bundle": self._output_dir / "xsi_bundle.h",
            "xsi_loader_h": self._output_dir / "xsi_loader.h",
            "xsi_loader_cpp": self._output_dir / "xsi_loader.cpp",
            "xsi_shared_lib": self._output_dir / "xsi_shared_lib.h",
            "xsi_run_bat": self._output_dir / "run.bat",
        }

    def generate(self, key: str, config: BuildConfig) -> str:
        src_names = {
            "xsi_bfm": "xsi_bfm.h",
            "xsi_bundle": "xsi_bundle.h",
            "xsi_loader_h": "xsi_loader.h",
            "xsi_loader_cpp": "xsi_loader.cpp",
            "xsi_shared_lib": "xsi_shared_lib.h",
            "xsi_run_bat": "run.bat",
        }
        if key not in src_names:
            raise KeyError(f"Unknown XsiHarnessStep output key: {key!r}")
        src_path = _SRC_DIR / "xsi" / src_names[key]
        if not src_path.exists():
            raise FileNotFoundError(f"XSI harness source file not found: {src_path}")
        return src_path.read_text(encoding="utf-8")
