"""Unified BuildDag for the shared_mem (histogram) example.

One declarative :class:`BuildDag` of dataclass :class:`BuildStep`s, driven by the
shared regmap-style introspection CLI (:func:`waveflow.build.cli.run_dag_cli`).
It consolidates the histogram build that previously lived across hist_demo.py
(the HistTest harness) and shared_mem_build.py (the codegen helper + the
CSIM_CASES coverage set): codegen -> input vectors -> Python golden -> Vitis
csim/csynth/cosim -> VCD/burst extraction -> committed figures. The data model
and components live in hist.py; the proven HLS generation + burst/timing
machinery (HistTest) is migrated here verbatim and wrapped by the steps.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.build.hwgen import header_to_cpp, kernel_to_cpp, tb_files_to_str
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep
from waveflow.hw.arrayutils import (
    ArrayUtilsStep, get_nwords, read_array, read_uint32_file, write_array,
    write_uint32_file,
)
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.hw.memory import AddrUnit, Memory
from waveflow.toolchain import toolchain
from waveflow.utils.jsonutil import hex_word, json_scalar
from waveflow.utils.vcd import AximmBeatType, vcd_trace

try:
    from examples.shared_mem.hist import (
        Float32, HistAccel, HistCmd, HistError, HistResp, HistSimResult, HistTBHls,
        HistogramAccel, INCLUDE_DIR, MAX_NBINS, MAX_NDATA, MEM_AWIDTH, MEM_DWIDTH,
        SCHEMA_CLASSES, Uint32Field, golden_counts, run_sim,
    )
    from examples.shared_mem.shared_mem_figures import (
        GenerateBurstDiagramStep, GenerateTimingDiagramStep, SyncDocsFiguresStep,
    )
except ModuleNotFoundError:  # direct execution from the example dir
    from hist import (  # type: ignore[no-redef]
        Float32, HistAccel, HistCmd, HistError, HistResp, HistSimResult, HistTBHls,
        HistogramAccel, INCLUDE_DIR, MAX_NBINS, MAX_NDATA, MEM_AWIDTH, MEM_DWIDTH,
        SCHEMA_CLASSES, Uint32Field, golden_counts, run_sim,
    )
    from shared_mem_figures import (  # type: ignore[no-redef]
        GenerateBurstDiagramStep, GenerateTimingDiagramStep, SyncDocsFiguresStep,
    )

_SOURCE_DIR = Path(__file__).resolve().parent
EXAMPLE_DIR = _SOURCE_DIR

DEFAULT_NDATA = 37
DEFAULT_NBINS = 6
DEFAULT_SEED = 3

WORD_BW_SUPPORTED = [32, 64]
TRACE_LEVELS = ("none", "port", "all")
DEFAULT_MAX_READ_BURST_LENGTH = 16
DEFAULT_MAX_WRITE_BURST_LENGTH = 16

def _run_tcl(config: BuildConfig, *, start_at: str, through: str,
             trace_level: str, live_output: bool) -> None:
    """Drive run.tcl over a stage range (the env HistTest.test_vitis used)."""
    toolchain.run_vitis_hls(
        config.root_dir / "run.tcl", work_dir=config.root_dir,
        capture_output=not live_output,
        env={
            "WAVEFLOW_HIST_START_AT": start_at,
            "WAVEFLOW_HIST_THROUGH": through,
            "WAVEFLOW_HIST_TRACE_LEVEL": trace_level,
        },
    )


# --- Migrated HLS / burst / timing harness (from hist_demo.HistTest) ---

class HistTest(object):
    """Stateful test/demo harness for the histogram accelerator flow."""

    def __init__(
            self,
            seed: int = 7,
            ndata: int = 37,
            nbins: int = 6,
            example_dir: Path = EXAMPLE_DIR,
            include_dir: str = INCLUDE_DIR,
            mem_dwidth: int = MEM_DWIDTH,
            mem_awidth: int = MEM_AWIDTH
    ):
        self.seed = int(seed)
        self.ndata = min(max(1, int(ndata)), MAX_NDATA)
        self.nbins = min(max(1, int(nbins)), MAX_NBINS)
        self.example_dir = Path(example_dir)
        self.include_dir = include_dir
        self.mem_dwidth = mem_dwidth
        self.mem_awidth = mem_awidth

        self.mem: Memory | None = None
        self.hist_accel: HistogramAccel | None = None
        self.data: np.ndarray | None = None
        self.bin_edges: np.ndarray | None = None
        self.expected: np.ndarray | None = None
        self.counts: np.ndarray | None = None
        self.cmd: HistCmd | None = None
        self.resp: HistResp | None = None
        self.data_addr: int | None = None
        self.edge_addr: int | None = None
        self.count_addr: int | None = None

    def gen_test_data(self) -> None:
        """
        Generate randomized input data and bin edges for a simulation run.
        """
        rng = np.random.default_rng(self.seed)
        self.data = rng.normal(loc=0.0, scale=1.25, size=self.ndata).astype(np.float32)
        self.bin_edges = np.sort(
            rng.uniform(-2.5, 2.5, size=max(self.nbins - 1, 0)).astype(np.float32)
        )

    def simulate(self) -> HistSimResult:
        """
        Run the Python model and store both observed and expected counts.
        """
        if self.data is None or self.bin_edges is None or self.cmd is None:
            self.gen_test_data()

        assert self.data is not None
        assert self.bin_edges is not None

        # Initialize memory.  We use byte addressing since we are emulating AXI4-style
        # interfaces which use byte addresses.  
        self.mem = Memory(
            word_size=self.mem_dwidth,
            addr_size=self.mem_awidth,
            addr_unit=AddrUnit.byte,
        )

        # Instantiate an accelerator connected to the memory
        self.hist_accel = HistogramAccel(self.mem)

        # Allocate memory for the input data, bin edges, and output counts
        # Note that the addresses are in bytes.
        nwords_data = get_nwords(elem_type=Float32, word_bw=self.mem.word_size, 
                                 shape=self.data.shape)
        nwords_edges = get_nwords(elem_type=Float32, word_bw=self.mem.word_size, 
                                  shape=self.bin_edges.shape)
        nwords_counts = get_nwords(elem_type=Uint32Field, word_bw=self.mem.word_size, 
                                   shape=self.nbins)
        self.data_addr = self.mem.alloc(nwords_data)
        self.edge_addr = self.mem.alloc(nwords_edges)
        self.count_addr = self.mem.alloc(nwords_counts)

        # Write the input data and bin edges to the allocated memory
        self.mem.write(
            self.data_addr,
            write_array(self.data, elem_type=Float32, word_bw=self.mem.word_size),
        )
        self.mem.write(
            self.edge_addr,
            write_array(self.bin_edges, elem_type=Float32, word_bw=self.mem.word_size),
        )


        # Create the command descriptor with the appropriate fields
        self.cmd = HistCmd(
            tx_id=self.seed,
            data_addr=self.data_addr,
            bin_edges_addr=self.edge_addr,
            ndata=self.ndata,
            nbins=self.nbins,
            cnt_addr=self.count_addr,
        )
 
        # Simulate the accelator and read back the results
        self.resp = self.hist_accel.compute_hist(self.cmd)

        # Read the histogram counts back from memory and compute the expected counts using numpy for comparison
        count_words = self.mem.read(
            self.count_addr,
            nwords=get_nwords(elem_type=Uint32Field, word_bw=self.mem.word_size, shape=self.nbins),
        )
        self.counts = read_array(
            count_words,
            elem_type=Uint32Field,
            word_bw=self.mem.word_size,
            shape=self.nbins,
        )

        # Compute expected counts using numpy for comparison
        self.expected = np.bincount(
            np.searchsorted(self.bin_edges, self.data, side="right"),
            minlength=self.nbins,
        ).astype(np.uint32)

        return HistSimResult(
            cmd=self.cmd,
            resp=self.resp,
            counts=self.counts,
            expected=self.expected,
        )

    def gen_vitis_code(self) -> list[Path]:
        """Generate schema and utility headers needed for the Vitis flow."""
        cfg = BuildConfig(root_dir=self.example_dir)
        dag = BuildDag()
        dag.add(StreamUtilsStep(output_dir=self.include_dir))
        schema_steps = [
            dag.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED, include_dir=self.include_dir))
            for cls in SCHEMA_CLASSES
        ]
        dag.add(ArrayUtilsStep(Float32, WORD_BW_SUPPORTED))
        dag.add(ArrayUtilsStep(Uint32Field, WORD_BW_SUPPORTED))
        results = dag.run(cfg)
        return [results[step.name].artifacts["include"] for step in schema_steps]

    def _expected_burst_summary(self) -> dict[str, object]:
        if self.cmd is None:
            self.simulate()

        assert self.cmd is not None

        max_read_burst_length, max_write_burst_length = self._detect_axi_max_burst_lengths()
        bytes_per_word = self.mem_dwidth // 8

        read_regions = [
            {
                "name": "data",
                "addr": int(self.cmd.data_addr),
                "nwords": int(get_nwords(Float32, word_bw=self.mem_dwidth, shape=self.ndata)),
            },
        ]
        if self.nbins > 1:
            read_regions.append(
                {
                    "name": "bin_edges",
                    "addr": int(self.cmd.bin_edges_addr),
                    "nwords": int(get_nwords(Float32, word_bw=self.mem_dwidth, shape=self.nbins - 1)),
                }
            )

        write_regions = [
            {
                "name": "counts",
                "addr": int(self.cmd.cnt_addr),
                "nwords": int(get_nwords(Uint32Field, word_bw=self.mem_dwidth, shape=self.nbins)),
            }
        ]

        read_bursts = [
            burst
            for region in read_regions
            for burst in self._split_region_into_bursts(region, max_read_burst_length, bytes_per_word)
        ]
        write_bursts = [
            burst
            for region in write_regions
            for burst in self._split_region_into_bursts(region, max_write_burst_length, bytes_per_word)
        ]

        return {
            "max_read_burst_length": max_read_burst_length,
            "max_write_burst_length": max_write_burst_length,
            "read_burst_count": len(read_bursts),
            "write_burst_count": len(write_bursts),
            "read_regions": read_regions,
            "write_regions": write_regions,
            "read_bursts": read_bursts,
            "write_bursts": write_bursts,
        }

    def _detect_axi_max_burst_lengths(self) -> tuple[int, int]:
        candidates = [
            self.example_dir / "waveflow_hist_proj" / "solution1" / "impl" / "verilog" / "hist.v",
            self.example_dir / "waveflow_hist_proj" / "solution1" / "impl" / "vhdl" / "hist.vhd",
        ]
        patterns = {
            "read": [
                re.compile(r"MAX_READ_BURST_LENGTH\s*=>\s*(\d+)"),
                re.compile(r"\.MAX_READ_BURST_LENGTH\s*\(\s*(\d+)\s*\)"),
            ],
            "write": [
                re.compile(r"MAX_WRITE_BURST_LENGTH\s*=>\s*(\d+)"),
                re.compile(r"\.MAX_WRITE_BURST_LENGTH\s*\(\s*(\d+)\s*\)"),
            ],
        }

        read_length = DEFAULT_MAX_READ_BURST_LENGTH
        write_length = DEFAULT_MAX_WRITE_BURST_LENGTH

        for candidate in candidates:
            if not candidate.exists():
                continue
            text = candidate.read_text(encoding="utf-8", errors="ignore")
            for pattern in patterns["read"]:
                match = pattern.search(text)
                if match is not None:
                    read_length = int(match.group(1))
                    break
            for pattern in patterns["write"]:
                match = pattern.search(text)
                if match is not None:
                    write_length = int(match.group(1))
                    break
            if read_length and write_length:
                break

        return read_length, write_length

    @staticmethod
    def _split_region_into_bursts(region: dict[str, object], max_burst_length: int, bytes_per_word: int) -> list[dict[str, int | str]]:
        addr = int(region["addr"])
        nwords = int(region["nwords"])
        name = str(region["name"])
        bursts: list[dict[str, int | str]] = []

        remaining = nwords
        offset_words = 0
        while remaining > 0:
            burst_words = min(remaining, max_burst_length)
            bursts.append(
                {
                    "name": name,
                    "addr": addr + offset_words * bytes_per_word,
                    "nwords": burst_words,
                }
            )
            offset_words += burst_words
            remaining -= burst_words

        return bursts

    @staticmethod
    def _burst_layout(bursts: list[dict[str, object]], len_key: str) -> list[dict[str, int]]:
        layout = []
        for burst in bursts:
            addr = burst.get("addr")
            if addr is None:
                continue
            raw_len = burst.get(len_key)
            nwords = len(burst.get("data", []))
            if raw_len is not None:
                nwords = int(json_scalar(raw_len)) + 1
            layout.append({
                "addr": int(json_scalar(addr)),
                "nwords": int(nwords),
            })
        return layout

    @staticmethod
    def _burst_to_jsonable(burst: dict[str, object], data_bitwidth: int) -> dict[str, object]:
        beat_types = [int(json_scalar(value)) for value in burst.get("beat_type", [])]
        data_values = [json_scalar(value) for value in np.asarray(burst.get("data", [])).tolist()]
        return {
            "addr": None if burst.get("addr") is None else int(json_scalar(burst["addr"])),
            "start_idx": int(json_scalar(burst["start_idx"])),
            "tstart": float(json_scalar(burst["tstart"])),
            "data_start_idx": None if burst.get("data_start_idx") is None else int(json_scalar(burst["data_start_idx"])),
            "data_end_idx": None if burst.get("data_end_idx") is None else int(json_scalar(burst["data_end_idx"])),
            "data_tstart": None if burst.get("data_tstart") is None else float(json_scalar(burst["data_tstart"])),
            "data_tend": None if burst.get("data_tend") is None else float(json_scalar(burst["data_tend"])),
            "queue_wait_cycles": None if burst.get("queue_wait_cycles") is None else int(json_scalar(burst["queue_wait_cycles"])),
            "beat_type": beat_types,
            "beat_type_names": [AximmBeatType(value).name.lower() for value in beat_types],
            "data": data_values,
            "data_hex": [hex_word(value, data_bitwidth) for value in data_values],
            "awlen": None if burst.get("awlen") is None else int(json_scalar(burst["awlen"])),
            "arlen": None if burst.get("arlen") is None else int(json_scalar(burst["arlen"])),
        }

    def extract_bursts(
        self,
        vcd_path: str | Path | None = None,
        output_json: str | Path | None = None,
    ) -> dict[str, object]:
        """Extract AXI-MM bursts from the histogram VCD and write a JSON report."""
        if self.cmd is None:
            self.simulate()

        from vcdvcd import VCDVCD
        from waveflow.utils.vcd import VcdParser

        if vcd_path is None:
            vcd_path = self.example_dir / "vcd" / "dump.vcd"
        vcd_path = Path(vcd_path)
        if not vcd_path.exists():
            raise FileNotFoundError(f"VCD file not found: {vcd_path}")

        if output_json is None:
            output_json = vcd_path.with_name("burst_info.json")
        output_json = Path(output_json)

        vcd = VCDVCD(str(vcd_path), signals=None, store_tvs=True)
        vp = VcdParser(vcd)
        clk_sig = vp.add_clock_signal()
        aximm_sigs, aximm_bw = vp.add_aximm_signals(
            prefix="m_axi_gmem_",
            dir="both",
            lite_only=False,
            short_name_prefix="gmem_",
        )
        write_bursts, read_bursts, clk_period = vp.extract_aximm_bursts(
            clk_name=clk_sig,
            aximm_sigs=aximm_sigs,
        )

        expected = self._expected_burst_summary()
        report = {
            "vcd_path": str(vcd_path),
            "output_json": str(output_json),
            "clock_signal": clk_sig,
            "clk_period_ns": float(clk_period),
            "beat_type_enum": {member.name.lower(): int(member) for member in AximmBeatType},
            "aximm_bitwidths": {key: int(value) for key, value in aximm_bw.items()},
            "expected": expected,
            "actual": {
                "write_burst_count": len(write_bursts),
                "read_burst_count": len(read_bursts),
                "write_bursts": [self._burst_to_jsonable(burst, data_bitwidth=int(aximm_bw["WDATA"])) for burst in write_bursts],
                "read_bursts": [self._burst_to_jsonable(burst, data_bitwidth=int(aximm_bw["RDATA"])) for burst in read_bursts],
                "write_burst_layout": self._burst_layout(write_bursts, "awlen"),
                "read_burst_layout": self._burst_layout(read_bursts, "arlen"),
            },
        }
        report["checks"] = {
            "write_burst_count_matches": report["actual"]["write_burst_count"] == expected["write_burst_count"],
            "read_burst_count_matches": report["actual"]["read_burst_count"] == expected["read_burst_count"],
            "write_burst_layout_matches": report["actual"]["write_burst_layout"] == [
                {"addr": burst["addr"], "nwords": burst["nwords"]} for burst in expected["write_bursts"]
            ],
            "read_burst_layout_matches": report["actual"]["read_burst_layout"] == [
                {"addr": burst["addr"], "nwords": burst["nwords"]} for burst in expected["read_bursts"]
            ],
        }
        report["validated"] = all(report["checks"].values())

        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

        if not report["validated"]:
            raise RuntimeError(
                "Unexpected AXI-MM burst counts in extracted VCD data. "
                f"Expected read/write burst counts {expected['read_burst_count']}/{expected['write_burst_count']}, "
                f"got {report['actual']['read_burst_count']}/{report['actual']['write_burst_count']}. "
                f"See {output_json}."
            )

        return report

    def generate_vcd(
        self,
        output_vcd: str = "dump.vcd",
        soln: str | None = "solution1",
        trace_level: str = "*",
    ) -> Path:
        """
        Generate a VCD file by re-running the Vivado RTL simulation.

        Delegates to :func:`waveflow.scripts.xsim_vcd.run_xsim_vcd`.
        Requires Vivado/xsim installed on Windows.

        Parameters
        ----------
        output_vcd : str
            Output VCD filename written inside a ``vcd/`` subdirectory.
        soln : str | None
            Solution name inside the component directory.
        trace_level : str
            VCD trace level (``'*'`` for all signals, ``'port'`` for ports only).

        Returns
        -------
        Path
            Absolute path to the written VCD file.
        """
        from waveflow.scripts.xsim_vcd import run_xsim_vcd
        return run_xsim_vcd(
            top="hist",
            comp="waveflow_hist_proj",
            out=output_vcd,
            soln=soln,
            trace_level=trace_level,
            workdir=self.example_dir,
        )


# --- Build helpers (migrated from shared_mem_build.py) ---

def _write_array_file(path: Path, arr, elem_type, count: int) -> None:
    """Write exactly ``count`` elements as the raw word stream the C++
    ``read_uint32_file_array`` expects.  ``write_uint32_file`` emits a spurious
    1-word file for an empty array, which the TB's ``count==0`` read flags as
    trailing bytes — so write a true 0-byte file when there is nothing to emit
    (the nbins==1 zero-edge case, or a validation-failure 0-data case)."""
    if count > 0:
        write_uint32_file(arr, elem_type=elem_type, file_path=path, nwrite=count)
    else:
        Path(path).write_bytes(b"")


@dataclass
class HistCase:
    """One C-sim coverage case + its golden expectation."""

    ndata: int
    nbins: int
    seed: int = 3

    def gen_data(self) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]:
        rng = np.random.default_rng(self.seed)
        data = (rng.normal(0.0, 1.25, size=max(self.ndata, 0)).astype(np.float32)
                if self.ndata > 0 else np.zeros(0, np.float32))
        edges = np.sort(
            rng.uniform(-2.5, 2.5, size=max(self.nbins - 1, 0)).astype(np.float32)
        )
        return data, edges

    @property
    def expected_status(self) -> HistError:
        # Mirrors HistAccel.validate (the kernel's validation logic).
        if self.ndata <= 0 or self.ndata > MAX_NDATA:
            return HistError.INVALID_NDATA
        if self.nbins <= 0 or self.nbins > MAX_NBINS:
            return HistError.INVALID_NBINS
        return HistError.NO_ERROR

    def write_inputs(self, data_dir: str | Path) -> tuple[np.ndarray, np.ndarray]:
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        data, edges = self.gen_data()
        # The generated TB reads the full HistCmd from cmd.bin (addresses are
        # placeholders it overwrites via alloc) and reads the input files
        # unconditionally, so write them always — even 0-element (the TB's read
        # of a 0/negative count is a no-op).
        HistCmd(tx_id=self.seed, data_addr=0, bin_edges_addr=0,
                ndata=self.ndata, nbins=self.nbins, cnt_addr=0
                ).write_uint32_file(str(data_dir / "cmd.bin"))
        _write_array_file(data_dir / "data_array.bin", data, Float32, max(self.ndata, 0))
        _write_array_file(data_dir / "edges_array.bin", edges, Float32, max(self.nbins - 1, 0))
        return data, edges

    def check_outputs(self, data_dir: str | Path, data, edges) -> tuple[bool, str]:
        """Compare the C-sim outputs against the golden; returns (passed, detail)."""
        data_dir = Path(data_dir)
        resp = HistResp().read_uint32_file(str(data_dir / "resp_data.bin"))
        if int(resp.status) != int(self.expected_status):
            return False, (f"status {int(resp.status)} != expected "
                           f"{int(self.expected_status)}")
        if self.expected_status != HistError.NO_ERROR:
            return True, f"status={int(resp.status)} (expected error)"
        counts = np.asarray(
            read_uint32_file(str(data_dir / "counts_array.bin"),
                             elem_type=Uint32Field, shape=self.nbins),
            dtype=np.uint32,
        )
        gold = golden_counts(data, edges, self.nbins)
        if not np.array_equal(counts, gold):
            return False, f"counts {counts.tolist()} != golden {gold.tolist()}"
        return True, f"counts={counts.tolist()} match golden"


CSIM_CASES = [
    HistCase(ndata=37, nbins=1),
    HistCase(ndata=37, nbins=6),
    HistCase(ndata=200, nbins=12),
    HistCase(ndata=0, nbins=6),    # INVALID_NDATA
]


def generate_vitis_sources(work_dir: str | Path) -> Path:
    """Generate ``include/`` headers and the generated ``gen/hist.{cpp,hpp}``.

    Returns the ``gen`` directory.  The include headers come from
    ``HistTest.gen_vitis_code`` (the proven path); the kernel + header are
    generated from :class:`HistAccel`.
    """
    from waveflow.build.build import BuildConfig, BuildDag

    work_dir = Path(work_dir)
    HistTest(example_dir=work_dir).gen_vitis_code()   # streams + schemas + array-utils
    # gen_vitis_code omits the memory manager; the generated header + TB include
    # include/memmgr.hpp / memmgr_tb.hpp, so generate those too.
    mm_dag = BuildDag()
    mm_dag.add(MemMgrStep(output_dir="include"))
    mm_dag.run(BuildConfig(root_dir=work_dir))
    gen = work_dir / "gen"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "hist.cpp").write_text(kernel_to_cpp(HistAccel), encoding="utf-8")
    (gen / "hist.hpp").write_text(header_to_cpp(HistAccel), encoding="utf-8")
    # The generated testbench (Phase 5) — drives the generated kernel.
    for fname, content in tb_files_to_str(HistTBHls).items():
        (gen / fname).write_text(content, encoding="utf-8")
    return gen


@dataclass(kw_only=True)
class GenSourcesStep(BuildStep):
    """Generate the Vitis HLS support headers + the m_axi kernel and testbench.

    Wraps the proven ``generate_vitis_sources`` (``HistTest.gen_vitis_code`` for
    the schema/array-utils/stream/memmgr headers, then ``kernel_to_cpp`` /
    ``header_to_cpp`` / ``tb_files_to_str`` for ``gen/``) so the generated kernel
    is byte-for-byte what the cosim safety net validates."""

    description = "Generate include/ headers + gen/hist.{cpp,hpp} + gen/hist_tb.cpp."
    consumes = ["hist_source"]
    produces = {
        "include_dir": Path("include"),
        "kernel_cpp": Path("gen/hist.cpp"),
        "kernel_hpp": Path("gen/hist.hpp"),
        "tb_cpp": Path("gen/hist_tb.cpp"),
    }

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        generate_vitis_sources(config.root_dir)
        gen = config.root_dir / "gen"
        return {
            "include_dir": config.root_dir / "include",
            "kernel_cpp": gen / "hist.cpp",
            "kernel_hpp": gen / "hist.hpp",
            "tb_cpp": gen / "hist_tb.cpp",
        }


@dataclass(kw_only=True)
class BuildInputsStep(BuildStep):
    """Write the C-sim input vectors for the reference case (cmd.bin + the data
    and edges buffers). The 4-case coverage sweep (CSIM_CASES) is driven by the
    csim step; this writes the reference vector the cosim/burst stages use."""

    description = "Write the reference-case C-sim inputs (cmd.bin + data/edges)."
    consumes = ["hist_source"]
    produces = {"data_dir": Path("data")}
    params = {"ndata": DEFAULT_NDATA, "nbins": DEFAULT_NBINS, "seed": DEFAULT_SEED}

    def run(self, config: BuildConfig, ndata, nbins, seed, **_) -> dict[str, Any]:
        data_dir = config.root_dir / "data"
        HistCase(ndata=ndata, nbins=nbins, seed=seed).write_inputs(data_dir)
        return {"data_dir": data_dir}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the SimPy model for the reference case and record golden parity.

    Drives ``run_sim`` (HistAccel + HistController + MemModel) against the
    numpy golden and writes a summary — the functional reference the C-sim and
    cosim stages are checked against."""

    description = "Run the SimPy histogram model and record golden parity."
    consumes = ["hist_source"]
    produces = {"sim_summary": Path("results/sim_summary.json")}
    params = {"ndata": DEFAULT_NDATA, "nbins": DEFAULT_NBINS, "seed": DEFAULT_SEED}

    def run(self, config: BuildConfig, ndata, nbins, seed, **_) -> dict[str, Any]:
        data, edges = HistCase(ndata=ndata, nbins=nbins, seed=seed).gen_data()
        res = run_sim(data, edges, nbins=nbins, tx_id=seed)
        out = config.root_dir / "results" / "sim_summary.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "ndata": int(ndata), "nbins": int(nbins), "seed": int(seed),
            "status": res.status.name,
            "counts": np.asarray(res.counts).tolist(),
            "expected": np.asarray(res.expected).tolist(),
            "passed": bool(res.passed),
        }, indent=2), encoding="utf-8")
        if not res.passed:
            raise RuntimeError(
                f"SimPy golden parity failed: status={res.status.name}, "
                f"counts={res.counts.tolist()} != expected={res.expected.tolist()}"
            )
        return {"sim_summary": out}


@dataclass(kw_only=True)
class CsimStep(BuildStep):
    """Vitis C-simulation across the 4-case coverage set, each checked against the
    numpy golden (nbins==1, two normal cases, and a validation-failure case — see
    CSIM_CASES). Restores the reference vector afterwards for the cosim stage."""

    description = "Vitis C-sim across the CSIM_CASES coverage set (vs the numpy golden)."
    consumes = ["kernel_cpp", "tb_cpp", "include_dir", "run_tcl"]
    produces = {"csim_verdict": Path("results/csim_verdict.json")}
    params = {"ndata": DEFAULT_NDATA, "nbins": DEFAULT_NBINS, "seed": DEFAULT_SEED,
              "live_output": False}

    def run(self, config: BuildConfig, ndata, nbins, seed, live_output, **_) -> dict[str, Any]:
        data_dir = config.root_dir / "data"
        cases = []
        for case in CSIM_CASES:
            data, edges = case.write_inputs(data_dir)
            _run_tcl(config, start_at="csim", through="csim",
                     trace_level="none", live_output=live_output)
            ok, detail = case.check_outputs(data_dir, data, edges)
            cases.append({"ndata": case.ndata, "nbins": case.nbins,
                          "passed": ok, "detail": detail})
            if not ok:
                raise RuntimeError(
                    f"C-sim mismatch ndata={case.ndata} nbins={case.nbins}: {detail}")
        # Leave the reference vector in data/ for the cosim stage.
        HistCase(ndata=ndata, nbins=nbins, seed=seed).write_inputs(data_dir)
        out = config.root_dir / "results" / "csim_verdict.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"cases": cases, "passed": True}, indent=2),
                       encoding="utf-8")
        return {"csim_verdict": out}


@dataclass(kw_only=True)
class CosimStep(BuildStep):
    """C-synth + RTL co-simulation of the reference vector (one run.tcl invocation,
    START_AT=csim THROUGH=cosim — the proven test_hist_cosim flow), checked against
    the golden. Gated on csim passing."""

    description = "Vitis C-synth + RTL co-simulation of the reference vector."
    consumes = ["kernel_cpp", "tb_cpp", "include_dir", "run_tcl", "csim_verdict"]
    produces = {"cosim_dir": Path("waveflow_hist_proj")}
    params = {"ndata": DEFAULT_NDATA, "nbins": DEFAULT_NBINS, "seed": DEFAULT_SEED,
              "trace_level": "port", "live_output": False}

    def run(self, config: BuildConfig, ndata, nbins, seed, trace_level,
            live_output, **_) -> dict[str, Any]:
        data_dir = config.root_dir / "data"
        case = HistCase(ndata=ndata, nbins=nbins, seed=seed)
        data, edges = case.write_inputs(data_dir)
        # trace_level goes to run.tcl RAW. Two different vocabularies share this
        # param name: run.tcl validates against {none port all} and hands it to
        # `cosim_design -trace_level`, which wants the level; vcd_trace() maps to the
        # xsim glob ("none" -> "*") and belongs on the GenerateVcdStep path below.
        # Passing vcd_trace() here sent "*" to run.tcl, which rejected it — so the
        # default `--trace-level none` made `--through cosim` fail outright.
        _run_tcl(config, start_at="csim", through="cosim",
                 trace_level=trace_level, live_output=live_output)
        ok, detail = case.check_outputs(data_dir, data, edges)
        if not ok:
            raise RuntimeError(f"Cosim output mismatch (reference vector): {detail}")
        return {"cosim_dir": config.root_dir / "waveflow_hist_proj"}


@dataclass(kw_only=True)
class GenerateVcdStep(BuildStep):
    """Re-run the synthesized RTL to write the port-level VCD (Vivado/xsim)."""

    description = "Re-run the RTL sim to write the port-level VCD."
    consumes = ["cosim_dir"]
    produces = {"vcd": Path("vcd/dump.vcd")}
    params = {"ndata": DEFAULT_NDATA, "nbins": DEFAULT_NBINS, "trace_level": "port"}

    def run(self, config: BuildConfig, ndata, nbins, trace_level, **_) -> dict[str, Any]:
        ht = HistTest(example_dir=config.root_dir, ndata=ndata, nbins=nbins)
        vcd = ht.generate_vcd(trace_level=vcd_trace(trace_level))
        return {"vcd": Path(vcd)}


@dataclass(kw_only=True)
class ExtractBurstsStep(BuildStep):
    """Extract the multi-buffer AXI-MM burst report from the VCD and validate the
    layout against the expected allocation (data + bin_edges reads, counts write)."""

    description = "Extract + validate the multi-buffer AXI-MM burst report."
    consumes = ["vcd"]
    produces = {"burst_info": Path("vcd/burst_info.json")}
    params = {"ndata": DEFAULT_NDATA, "nbins": DEFAULT_NBINS}

    def run(self, config: BuildConfig, ndata, nbins, vcd, **_) -> dict[str, Any]:
        ht = HistTest(example_dir=config.root_dir, ndata=ndata, nbins=nbins)
        ht.simulate()
        report = ht.extract_bursts(vcd_path=vcd)
        if not report.get("validated"):
            raise RuntimeError("AXI-MM burst layout did not validate against the golden.")
        return {"burst_info": config.root_dir / "vcd" / "burst_info.json"}


@dataclass(kw_only=True)
class ExtractCosimTimingStep(BuildStep):
    """Extract the measured per-transaction cycle latency from the cosim report."""

    description = "Extract the measured cosim transaction latency."
    consumes = ["cosim_dir"]
    produces = {"cosim_timing": Path("results/cosim_timing.json")}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        from waveflow.utils.cosimparse import CosimReportParser
        sol = config.root_dir / "waveflow_hist_proj" / "solution1"
        cycles = CosimReportParser(sol_path=sol, top="hist").get_transaction_cycles()
        out = config.root_dir / "results" / "cosim_timing.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"transaction_cycles": cycles}, indent=2),
                       encoding="utf-8")
        return {"cosim_timing": out}


def build_hist_dag() -> BuildDag:
    """Assemble the unified histogram BuildDag."""
    dag = BuildDag()
    dag.add(SourceStep(artifact="hist_source", path=_SOURCE_DIR / "hist.py"))
    dag.add(SourceStep(artifact="run_tcl", path=_SOURCE_DIR / "run.tcl"))
    dag.add(GenSourcesStep(name="gen_sources"))
    dag.add(BuildInputsStep(name="build_inputs"))
    dag.add(PySimStep(name="py_sim"))
    dag.add(CsimStep(name="csim"))
    dag.add(CosimStep(name="cosim"))
    dag.add(GenerateVcdStep(name="generate_vcd"))
    dag.add(ExtractBurstsStep(name="extract_bursts"))
    dag.add(ExtractCosimTimingStep(name="extract_cosim_timing"))
    # Figure steps — an independent branch: they render from vcd/burst_info.json,
    # regenerated from the committed vcd/dump.vcd (ensure_burst_info), so a docs
    # refresh (`--through sync_docs_figures`) needs no Vitis even though the full
    # Vitis pipeline lives in the same DAG.
    dag.add(GenerateBurstDiagramStep(name="generate_burst_diagram"))
    dag.add(GenerateTimingDiagramStep(name="generate_timing_diagram"))
    dag.add(SyncDocsFiguresStep(name="sync_docs_figures"))
    return dag


def main() -> None:
    run_dag_cli(
        build_hist_dag,
        description="Run the histogram (shared_mem) example.",
        default_through="py_sim",
        root_dir=_SOURCE_DIR,
        extra_args=[
            (("--ndata",), {"type": int, "default": DEFAULT_NDATA,
                            "help": "Number of data samples for the reference case."}),
            (("--nbins",), {"type": int, "default": DEFAULT_NBINS,
                            "help": "Number of histogram bins for the reference case."}),
            (("--seed",), {"type": int, "default": DEFAULT_SEED,
                           "help": "RNG seed / transaction id for the reference case."}),
            (("--trace-level",), {"default": "none", "choices": ["none", "port", "all"],
                                  "help": "RTL cosim VCD trace level (Vitis stages)."}),
            (("--live-output",), {"action": "store_true"}),
        ],
        params_from_args=lambda a: {
            "ndata": a.ndata, "nbins": a.nbins, "seed": a.seed,
            "trace_level": a.trace_level, "live_output": a.live_output,
        },
    )


if __name__ == "__main__":
    main()
