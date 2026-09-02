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


class RfSampBufStep(Buildable):
    """Build step that copies the fixed :class:`~waveflow.hw.rf_samp_buf.RfSampBufRx` task bodies.

    The RF sample buffer is **framework** (``plans/adc_model.md`` § *Two design patterns*: it is the
    default way a design reaches a converter), so its two hand-written ``hls::task`` bodies ship in
    ``waveflow/build/`` beside the ``mem_*`` and ``il_*`` ones rather than in whichever example
    happened to need them first.  Same mechanism as :class:`MemStreamStep`: copied verbatim into an
    example's include directory, because each example builds in its own tree and Vitis compiles
    against the files beside it.

    All four bodies are hand-written for the same reason the ``mem_*`` ones are — the RX ingress must
    be a single word-granular firing that never stalls its input, the TX player a single firing that
    never misses a deadline, and the capture and loader block inside a loop.  None of those shapes is
    in the extractor's vocabulary, and none should be: this is the one module that is *supposed* to
    own that difficulty so no user's DUT has to.

    **Both directions ship together**, deliberately.  ``RfSampBufRx`` and ``RfSampBufTx`` are two
    buffers rather than one (``plans/adc_model.md``: "never refuse a write" and "never miss a
    deadline" are different contracts), but they share a geometry, a status vocabulary and a wrapping
    sample counter — so a build that copies one and not the other could drift silently.  A TX-only
    design pays two unused headers, which costs nothing: Vitis compiles what the top includes.

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
            "rf_samp_buf_ingress_task": self._output_dir / "rf_samp_buf_ingress_task.h",
            "rf_samp_buf_capture_task": self._output_dir / "rf_samp_buf_capture_task.h",
            "rf_samp_buf_loader_task": self._output_dir / "rf_samp_buf_loader_task.h",
            "rf_samp_buf_player_task": self._output_dir / "rf_samp_buf_player_task.h",
        }

    def generate(self, key: str, config: BuildConfig) -> str:
        src_names = {
            "rf_samp_buf_ingress_task": "rf_samp_buf_ingress_task.h",
            "rf_samp_buf_capture_task": "rf_samp_buf_capture_task.h",
            "rf_samp_buf_loader_task": "rf_samp_buf_loader_task.h",
            "rf_samp_buf_player_task": "rf_samp_buf_player_task.h",
        }
        if key not in src_names:
            raise KeyError(f"Unknown RfSampBufStep output key: {key!r}")
        src_path = _SRC_DIR / src_names[key]
        if not src_path.exists():
            raise FileNotFoundError(f"RfSampBuf source file not found: {src_path}")
        return src_path.read_text(encoding="utf-8")


class RfTxStreamStep(Buildable):
    """Copy the streaming transmitter's two hand-written ``hls::task`` bodies.

    The same mechanism as :class:`RfSampBufStep` and for the same reason: these are **framework**
    (``waveflow/hw/rf_tx_stream.py``), not example code, so they ship from ``waveflow/build/`` and
    each example gets a copy beside the top Vitis compiles.

    **A separate step from** :class:`RfSampBufStep`, not an addition to it.  The two designs are
    alternatives — a circular buffer with a progress channel, or a stream with an ack — and a build
    that wanted one would otherwise be handed both vocabularies, whose status codes deliberately
    differ (``RF_TX_TOO_LATE`` here is a *deadline*; ``RF_SAMP_BUF_TOO_LATE`` there is a *slot that
    already played out of a buffer*).  Shipping them together would put two encodings of "too late"
    in one include directory, which is exactly the confusion the split naming avoids.
    """

    def __init__(self, output_dir: str | Path = ".") -> None:
        super().__init__()
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {
            "rf_tx_loader_task": self._output_dir / "rf_tx_loader_task.h",
            "rf_tx_player_task": self._output_dir / "rf_tx_player_task.h",
            "rf_circ_play_task": self._output_dir / "rf_circ_play_task.h",
        }

    def generate(self, key: str, config: BuildConfig) -> str:
        names = {"rf_tx_loader_task": "rf_tx_loader_task.h",
                 "rf_tx_player_task": "rf_tx_player_task.h",
                 "rf_circ_play_task": "rf_circ_play_task.h"}
        if key not in names:
            raise KeyError(f"Unknown RfTxStreamStep output key: {key!r}")
        src_path = _SRC_DIR / names[key]
        if not src_path.exists():
            raise FileNotFoundError(f"RfTxStream source file not found: {src_path}")
        return src_path.read_text(encoding="utf-8")


class RfShotBufStep(Buildable):
    """Copy the shot family's **logic-side re-layout** task bodies.

    The same mechanism as :class:`RfSampBufStep` and for the same reason:
    :mod:`waveflow.hw.rf_relayout` is **framework**, so its ``hls::task`` bodies ship from
    ``waveflow/build/`` and each example gets a copy beside the top Vitis compiles.

    **It shipped six more bodies until ``plans/rf_shot_unify.md`` Stage B.**  Those were the
    ``ShotPhase``-and-``rdy`` buffer primitive, the finite command layer on top of it, and the
    infinite-play sibling beside it — three designs that
    :class:`~waveflow.hw.rf_shot_tx.RfShotTx` now covers on one lock, and whose bodies went with
    them.  The step keeps its name because ``RfShotBuf`` survives as the **family** name (see
    ``docs/guide/rf/rfshotbuf/``); what it ships is now the pair every member of that family needs
    and nobody else does.

    **A separate step from** :class:`RfSampBufStep`, deliberately.  ``plans/rf_shot_buf.md`` opens by
    saying why the two designs are separate plans rather than two halves of one: every line of the
    streaming buffer's machinery — credit, ack, progress, ``MARGIN``, the horizon — exists to
    arbitrate between a live reader and a live writer, and the shot family has no such pair.  Handing
    a shot build the streaming vocabulary would put two answers to "how does the reader know where
    the writer is?" in one include directory, when the shot answer is *there is nothing to know*.

    **Both bodies together**, because a design takes one or the other by direction and a build that
    guessed would be guessing: TX converts dense words to converter slots on the way out, RX converts
    slots to dense words on the way in.  One unused header costs nothing — Vitis compiles what the
    top includes.

    The two bodies ``#include`` the generated ``rf_slot_elem_array_utils.h`` /
    ``rf_dense_elem_array_utils.h`` by plain name, so an
    :class:`~waveflow.hw.arrayutils.ArrayUtilsStep` for
    :func:`~waveflow.hw.rf_relayout.slot_elem_type` and
    :func:`~waveflow.hw.rf_relayout.dense_elem_type` must write into the **same** *output_dir*.

    Parameters
    ----------
    output_dir : str | Path
        Directory path **relative to** ``BuildConfig.root_dir`` where the task headers are written.
    """

    def __init__(self, output_dir: str | Path = ".") -> None:
        super().__init__()
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {name: self._output_dir / f"{name}.h" for name in self._SRC}

    _SRC = ("rf_relayout_to_dense_task", "rf_relayout_to_slots_task")

    def generate(self, key: str, config: BuildConfig) -> str:
        if key not in self._SRC:
            raise KeyError(f"Unknown RfShotBufStep output key: {key!r}")
        src_path = _SRC_DIR / f"{key}.h"
        if not src_path.exists():
            raise FileNotFoundError(f"RfShotBufStep source file not found: {src_path}")
        return src_path.read_text(encoding="utf-8")


class MemLockStep(Buildable):
    """Copy ``mem_lock.h`` — the C++ half of :mod:`waveflow.hw.locked_mem`.

    ``plans/t2p_lock_chan.md`` S1.  A step of its own rather than a line in another one, because the
    lock is a **primitive**: it is not the shot buffer's, not the streaming buffer's, and the RX
    consumer S2 builds will reach for the same header.  Folding it into
    :class:`RfShotBufStep` would file a general mechanism under its first user, which is the shape
    :class:`~waveflow.hw.reverse_stream.CreditStreamIF` is still paying for.

    One header, and it carries no task body: what a lock-aware ``hls::task`` *does* is the design's,
    and the three moves it needs (request, await, poll+grant) are inline functions here so no body
    hand-rolls a beat.

    ``mem_lock.h`` ``#include``\\ s the generated ``mem_lock_cmd.h`` / ``mem_lock_resp.h`` by plain
    name, so a :class:`~waveflow.hw.dataschema.DataSchemaStep` for
    :data:`~waveflow.hw.locked_mem.LOCK_SCHEMA_CLASSES` must write into the **same** *output_dir*.
    Plain ``read_stream`` / ``write_stream`` are enough and ``framed=True`` is not wanted: the lock
    channels are *internal* edges, where ``ap_axis`` is refused outright (HLS 214-208).
    """

    def __init__(self, output_dir: str | Path = ".") -> None:
        super().__init__()
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {"mem_lock": self._output_dir / "mem_lock.h"}

    def generate(self, key: str, config: BuildConfig) -> str:
        if key != "mem_lock":
            raise KeyError(f"Unknown MemLockStep output key: {key!r}")
        return (_SRC_DIR / "mem_lock.h").read_text(encoding="utf-8")


class RfPingPongStep(Buildable):
    """Copy the continuous-capture receiver's two hand-written ``hls::task`` bodies.

    ``plans/t2p_lock_chan.md`` S2.  :mod:`waveflow.hw.rf_shot_rx` is **framework**, so its bodies
    ship from ``waveflow/build/`` and each example gets a copy beside the top Vitis compiles — the
    same mechanism :class:`RfShotBufStep` uses.

    **A step of its own rather than a line in** :class:`RfShotBufStep`, and the reason is the one that
    step's own docstring gives for splitting from :class:`RfSampBufStep`: these are a different
    *design*, not a different half of one.  The shot buffer's vocabulary is a shot, a verdict and a
    repeat count; this one's is a region, a window and a drop.  They share the **lock**, and the lock
    is :class:`MemLockStep`'s — a primitive filed under neither of its users.

    Both bodies ``#include`` the generated ``capture_window_hdr.h`` and ``mem_lock.h``, so a
    :class:`~waveflow.hw.dataschema.DataSchemaStep` for
    :data:`~waveflow.hw.rf_shot_rx.CAPTURE_SCHEMA_CLASSES` and a :class:`MemLockStep` must write
    into the same *output_dir*.  The window body writes an ``axi4s`` frame, so the schema needs its
    ``write_axi4_stream`` methods — which ``DataSchemaStep`` emits by default.
    """

    def __init__(self, output_dir: str | Path = ".") -> None:
        super().__init__()
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {name: self._output_dir / f"{name}.h" for name in self._SRC}

    _SRC = ("pingpong_capture_task", "pingpong_window_task")

    def generate(self, key: str, config: BuildConfig) -> str:
        if key not in self._SRC:
            raise KeyError(f"Unknown RfPingPongStep output key: {key!r}")
        src_path = _SRC_DIR / f"{key}.h"
        if not src_path.exists():
            raise FileNotFoundError(f"RfPingPong source file not found: {src_path}")
        return src_path.read_text(encoding="utf-8")


class RfShotTxStep(Buildable):
    r"""Copy the shot transmitter's two hand-written ``hls::task`` bodies.

    ``plans/rf_shot_unify.md`` Stage A.  :mod:`waveflow.hw.rf_shot_tx` is framework, so its
    bodies ship from ``waveflow/build/`` and each example gets a copy beside the top Vitis compiles —
    the same mechanism :class:`RfShotBufStep` and :class:`RfPingPongStep` use.

    **A step of its own while Stage A runs**, and that is the merge-only rule made structural: the
    predecessors' bodies were still shipped by :class:`RfShotBufStep` and a build had to be able to
    ask for either family without getting both.  **Stage B deleted those bodies**, so the separation
    no longer buys anything; the step stays separate because merging it back would be a refactor of a
    survivor inside a stage whose whole property is that every regression is a removal.

    The loader body ``#include``\ s the generated ``rf_shot_tx_hdr.h`` / ``rf_shot_tx_resp.h`` and
    both bodies ``#include`` ``shot_play_cmd.h``, so ``DataSchemaStep``\ s for
    :data:`~waveflow.hw.rf_shot_tx.SHOT_TX_SCHEMA_CLASSES` **and**
    :data:`~waveflow.hw.rf_shot_tx.SHOT_PLAY_SCHEMA_CLASSES` must write into the same
    *output_dir*, along with :class:`MemLockStep`'s ``mem_lock.h``.  The re-layout body is Stage A's
    and comes from :class:`RfShotBufStep`.
    """

    def __init__(self, output_dir: str | Path = ".") -> None:
        super().__init__()
        self._output_dir = Path(output_dir)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {name: self._output_dir / f"{name}.h" for name in self._SRC}

    _SRC = ("shot_tx_loader_task", "shot_tx_player_task")

    def generate(self, key: str, config: BuildConfig) -> str:
        if key not in self._SRC:
            raise KeyError(f"Unknown RfShotTxStep output key: {key!r}")
        src_path = _SRC_DIR / f"{key}.h"
        if not src_path.exists():
            raise FileNotFoundError(f"RfShotTx source file not found: {src_path}")
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
            # In-band interleaver tiles: the framers + gather that compose the framework mem-streams
            # (the descriptor forwards in-band instead of a custom token).  Hand-written framed bodies.
            "il_cmd_rx_framed_task": self._output_dir / "il_cmd_rx_framed_task.h",
            "il_load_inband_task": self._output_dir / "il_load_inband_task.h",
            "il_compute_inband_task": self._output_dir / "il_compute_inband_task.h",
            "il_store_inband_task": self._output_dir / "il_store_inband_task.h",
        }

    def generate(self, key: str, config: BuildConfig) -> str:
        src_names: dict[str, str] = {
            "mem_r_stream_task": "mem_r_stream_task.h",
            "mem_w_stream_task": "mem_w_stream_task.h",
            "mem_w_stream_done_task": "mem_w_stream_done_task.h",
            "mem_seq_framed_task": "mem_seq_framed_task.h",
            "mem_r_stream_framed_task": "mem_r_stream_framed_task.h",
            "mem_w_stream_framed_done_task": "mem_w_stream_framed_done_task.h",
            "il_cmd_rx_framed_task": "il_cmd_rx_framed_task.h",
            "il_load_inband_task": "il_load_inband_task.h",
            "il_compute_inband_task": "il_compute_inband_task.h",
            "il_store_inband_task": "il_store_inband_task.h",
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
    nothing about any kernel (see :mod:`waveflow.build.xsi`), and ``xsi_loader`` + ``run.bat`` /
    ``run.sh`` are the generic XSI flow.  They lived in ``examples/interleaver/xsi/`` only because
    that is where the first four testbenches were written, which made any second example wanting XSI
    reach across into a sibling example.

    Both runner scripts are copied regardless of host OS, so a workspace built on one platform can
    be simulated on the other; :func:`waveflow.build.trace_steps.xsi_runner_cmd` picks the one that
    matches the machine actually running it.

    Copied rather than included-in-place for the same reason the task-body headers are (cf.
    :class:`MemStreamStep`): each example builds in its own directory, and the runner compiles the
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
            # The participant lifecycle, and the edge model that implements it without binding any
            # RTL pin.  Copied unconditionally rather than only for a graph with a behavioral edge:
            # xsi_bfm.h includes xsi_simobj.h, so a workspace without it does not compile at all,
            # and a per-graph copy list would make that a build-order question.
            "xsi_simobj": self._output_dir / "xsi_simobj.h",
            "xsi_channel": self._output_dir / "xsi_channel.h",
            # The RF-domain edge + its file-backed peers, and the converter models.  Copied
            # unconditionally, like every other framework header: a per-graph copy list would make
            # "which headers does this workspace need?" a build-order question, and the cost of an
            # unused header in a workspace is nothing.
            "xsi_rf_block": self._output_dir / "xsi_rf_block.h",
            "xsi_rfdc_samp": self._output_dir / "xsi_rfdc_samp.h",
            "xsi_rfdc": self._output_dir / "xsi_rfdc.h",
            "xsi_bundle": self._output_dir / "xsi_bundle.h",
            "xsi_loader_h": self._output_dir / "xsi_loader.h",
            "xsi_loader_cpp": self._output_dir / "xsi_loader.cpp",
            "xsi_shared_lib": self._output_dir / "xsi_shared_lib.h",
            "xsi_run_bat": self._output_dir / "run.bat",
            "xsi_run_sh": self._output_dir / "run.sh",
        }

    def generate(self, key: str, config: BuildConfig) -> str:
        src_names = {
            "xsi_bfm": "xsi_bfm.h",
            "xsi_simobj": "xsi_simobj.h",
            "xsi_channel": "xsi_channel.h",
            "xsi_rf_block": "xsi_rf_block.h",
            "xsi_rfdc_samp": "xsi_rfdc_samp.h",
            "xsi_rfdc": "xsi_rfdc.h",
            "xsi_bundle": "xsi_bundle.h",
            "xsi_loader_h": "xsi_loader.h",
            "xsi_loader_cpp": "xsi_loader.cpp",
            "xsi_shared_lib": "xsi_shared_lib.h",
            "xsi_run_bat": "run.bat",
            "xsi_run_sh": "run.sh",
        }
        if key not in src_names:
            raise KeyError(f"Unknown XsiHarnessStep output key: {key!r}")
        src_path = _SRC_DIR / "xsi" / src_names[key]
        if not src_path.exists():
            raise FileNotFoundError(f"XSI harness source file not found: {src_path}")
        return src_path.read_text(encoding="utf-8")
