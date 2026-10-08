"""gem5.py — measure a calibration kernel on gem5 and turn the reading into a corpus row.

The ground truth for the processor model's cycle costs (``plans/cpu_model.md`` §9): each kernel's C
source is cross-compiled static for aarch64 with the m5 markers on, run on gem5's HPI core in
syscall-emulation mode inside the gem5 dependency image, and its measured region read from the first
statistics block (the one ``m5_dump_stats`` writes at the end of the region).

What a row records, and why:

* the **counters** the program printed, which must equal the Python twin's (``output_matches_twin``)
  — a cost model fitted on counters the simulation would not reproduce is fitted on fiction;
* ``cycles_raw`` and ``cycles`` = ``cycles_raw - empty_region_cycles``: the markers cost 94 cycles on
  HPI at 1.2 GHz, which is most of a small scheduler operation;
* ``code_bytes``: the measured functions' symbol sizes in the very binary that ran;
* provenance — gem5 tag and commit, compiler and its version, flags, core, cache and DRAM
  configuration, and the pre-registration commit — because a measured number belongs to its tools.

Tools are located by environment variable with defaults matching the plan's layout:
``WAVEFLOW_GEM5_ROOT`` (``~/ali/tools/gem5``), ``WAVEFLOW_GEM5_IMAGE``, ``WAVEFLOW_AARCH64_GCC``
(else the Vitis-bundled cross compiler) and ``WAVEFLOW_CPU_CALIB_WORK`` for build and run output.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from waveflow.cpu.calib.kernels import EMPTY, KERNEL_DIR, Kernel
from waveflow.cpu.calib.prereg import PreregistrationError, SweepPlan
from waveflow.cpu.task import regime_features

GEM5_TAG = "v25.1.0.1"
DEFAULT_IMAGE = "ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1"
#: Compile flags of every measured binary (``-DWF_GEM5`` turns the markers on).
CFLAGS = ("-O2", "-static", "-DWF_GEM5")

#: Statistics kept on every row (name in the row -> name in ``stats.txt``).  Absent means zero:
#: gem5 omits some counters that never incremented.
STATS = {
    "cycles_raw": "system.cpu_cluster.cpus.numCycles",
    "insts": "system.cpu_cluster.cpus.commitStats0.numInsts",
    "ops": "system.cpu_cluster.cpus.commitStats0.numOps",
    "l1i_misses": "system.cpu_cluster.cpus.icache.overallMisses::total",
    "l1d_accesses": "system.cpu_cluster.cpus.dcache.overallAccesses::total",
    "l1d_misses": "system.cpu_cluster.cpus.dcache.overallMisses::total",
    "l2_accesses": "system.cpu_cluster.l2.overallAccesses::total",
    "l2_misses": "system.cpu_cluster.l2.overallMisses::total",
    "dram_reads": "system.mem_ctrls.readReqs",
    "dram_writes": "system.mem_ctrls.writeReqs",
}

_CPU = "system.cpu_cluster.cpus"
#: The activity McPAT is driven by (``waveflow.cpu.calib.mcpat``), kept on every row with an ``s_``
#: prefix so the corpus alone can re-run the energy model -- no gem5 output directory needed.
MCPAT_STATS = {
    "s_int_insts": f"{_CPU}.commitStats0.numIntInsts",
    "s_fp_insts": f"{_CPU}.commitStats0.numFpInsts",
    "s_vec_insts": f"{_CPU}.commitStats0.numVecInsts",
    "s_load_insts": f"{_CPU}.commitStats0.numLoadInsts",
    "s_store_insts": f"{_CPU}.commitStats0.numStoreInsts",
    "s_calls": f"{_CPU}.commitStats0.functionCalls",
    "s_branches": f"{_CPU}.branchPred.committed_0::total",
    "s_mispredicts": f"{_CPU}.branchPred.mispredicted_0::total",
    "s_int_mult": f"{_CPU}.commitStats0.committedInstType::IntMult",
    "s_int_div": f"{_CPU}.commitStats0.committedInstType::IntDiv",
    "s_ialu": f"{_CPU}.executeStats0.numIntAluAccesses",
    "s_fpalu": f"{_CPU}.executeStats0.numFpAluAccesses",
    "s_vecalu": f"{_CPU}.executeStats0.numVecAluAccesses",
    "s_int_rf_reads": f"{_CPU}.executeStats0.numIntRegReads",
    "s_int_rf_writes": f"{_CPU}.executeStats0.numIntRegWrites",
    "s_fp_rf_reads": f"{_CPU}.executeStats0.numFpRegReads",
    "s_fp_rf_writes": f"{_CPU}.executeStats0.numFpRegWrites",
    "s_icache_reads": f"{_CPU}.icache.ReadReq.accesses::total",
    "s_icache_read_misses": f"{_CPU}.icache.ReadReq.misses::total",
    "s_dcache_reads": f"{_CPU}.dcache.ReadReq.accesses::total",
    "s_dcache_read_misses": f"{_CPU}.dcache.ReadReq.misses::total",
    "s_dcache_writes": f"{_CPU}.dcache.WriteReq.accesses::total",
    "s_dcache_write_misses": f"{_CPU}.dcache.WriteReq.misses::total",
    "s_l2_accesses": "system.cpu_cluster.l2.overallAccesses::total",
    "s_l2_misses": "system.cpu_cluster.l2.overallMisses::total",
    "s_l2_writebacks": "system.cpu_cluster.l2.WritebackDirty.accesses::total",
}

_BLOCK = "---------- Begin Simulation Statistics ----------"


def parse_stats_blocks(text: str) -> list[dict[str, float]]:
    """Every statistics block in a gem5 ``stats.txt``, as ``{name: value}`` (scalars only)."""
    blocks = []
    for chunk in text.split(_BLOCK)[1:]:
        stats: dict[str, float] = {}
        for line in chunk.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0].startswith("-"):
                continue
            try:
                stats[parts[0]] = float(parts[1])
            except ValueError:
                continue
        blocks.append(stats)
    return blocks


def roi_stats(text: str) -> dict[str, float]:
    """The measured region's statistics: the first block (the final one is the whole run's tail)."""
    blocks = parse_stats_blocks(text)
    if len(blocks) < 2:
        raise RuntimeError(
            f"expected the region's block and the exit block, found {len(blocks)}"
        )
    return blocks[0]


def pick_stats(stats: Mapping[str, float]) -> dict[str, float]:
    """The row's statistics -- :data:`STATS` and :data:`MCPAT_STATS` -- with absent as zero."""
    return {
        k: float(stats.get(name, 0.0)) for k, name in {**STATS, **MCPAT_STATS}.items()
    }


def symbol_sizes(nm: str, exe: Path, kernel: Kernel) -> dict[str, int]:
    """``{symbol: bytes}`` for every symbol in *exe* that is one of *kernel*'s or a GCC clone of it."""
    run = subprocess.run(
        [nm, "-S", str(exe)], capture_output=True, text=True, check=True
    )
    wanted = set(kernel.symbols)
    sizes: dict[str, int] = {}
    for line in run.stdout.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[3].split(".")[0] in wanted:
            sizes[parts[3]] = int(parts[1], 16)
    missing = wanted - {s.split(".")[0] for s in sizes}
    if missing:
        raise RuntimeError(
            f"{kernel.name}: symbols {sorted(missing)} not in {exe.name}"
        )
    return sizes


def default_cross_gcc() -> str | None:
    """``WAVEFLOW_AARCH64_GCC``, else the aarch64-linux gcc a Vitis install bundles, else PATH."""
    env = os.environ.get("WAVEFLOW_AARCH64_GCC")
    if env:
        return env
    try:
        from waveflow.toolchain.toolchain import find_vitis_path

        vitis = find_vitis_path()
    except Exception:  # noqa: BLE001 - toolchain detection is best effort here
        vitis = None
    if vitis:
        # .../Vitis/<ver>/bin/vitis-ish -> .../Vitis/<ver>/gnu/aarch64/lin/aarch64-linux/bin/
        root = Path(vitis).resolve()
        for parent in root.parents:
            cand = parent / "gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-gcc"
            if cand.is_file():
                return str(cand)
    return shutil.which("aarch64-linux-gnu-gcc")


@dataclass(frozen=True)
class Gem5Config:
    """The simulated core: gem5's HPI by default, as the RFSoC 4x2's A53 (``plans/cpu_model.md``
    §14, step 1).  Cache sizes are HPI's own, which equal UG1085's A53; other sizes need ``-P``
    overrides, which arrive with the cross-configuration step."""

    cpu: str = "hpi"
    f_clk_hz: float = 1.2e9
    num_cores: int = 1
    mem_type: str = "DDR4_2400_8x8"
    mem_channels: int = 1
    l1i_bytes: int = 32 * 1024
    l1d_bytes: int = 32 * 1024
    l2_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        hpi = (32 * 1024, 32 * 1024, 1024 * 1024)
        if (self.l1i_bytes, self.l1d_bytes, self.l2_bytes) != hpi:
            raise NotImplementedError(
                "non-default cache sizes need -P overrides (plan step 15)"
            )

    def starter_args(self) -> list[str]:
        return [
            "--cpu",
            self.cpu,
            "--cpu-freq",
            f"{self.f_clk_hz / 1e9:g}GHz",
            "--num-cores",
            str(self.num_cores),
            "--mem-type",
            self.mem_type,
            "--mem-channels",
            str(self.mem_channels),
        ]


@dataclass
class Gem5Runner:
    """Build kernels and measure them on gem5.  One runner is one tool + core configuration."""

    config: Gem5Config = field(default_factory=Gem5Config)
    gem5_root: Path = field(
        default_factory=lambda: Path(
            os.environ.get("WAVEFLOW_GEM5_ROOT", Path.home() / "ali/tools/gem5")
        )
    )
    image: str = field(
        default_factory=lambda: os.environ.get("WAVEFLOW_GEM5_IMAGE", DEFAULT_IMAGE)
    )
    cc: str | None = field(default_factory=default_cross_gcc)
    workdir: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "WAVEFLOW_CPU_CALIB_WORK", Path.home() / ".cache/waveflow/cpu_calib"
            )
        )
    )

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @property
    def gem5_opt(self) -> Path:
        return self.gem5_root / "build/ARM/gem5.opt"

    @property
    def libm5(self) -> Path:
        return self.gem5_root / "util/m5/build/arm64/out/libm5.a"

    def unavailable(self) -> str | None:
        """Why this runner cannot measure, or ``None`` when it can."""
        if not self.gem5_opt.is_file():
            return f"no gem5 build at {self.gem5_opt} (set WAVEFLOW_GEM5_ROOT)"
        if not self.libm5.is_file():
            return f"no libm5.a at {self.libm5} (build util/m5 for arm64)"
        if not self.cc or not Path(self.cc).exists():
            return "no aarch64-linux-gnu-gcc (set WAVEFLOW_AARCH64_GCC)"
        if shutil.which("docker") is None:
            return "docker is not on PATH"
        return None

    def gem5_commit(self) -> str:
        run = subprocess.run(
            ["git", "-C", str(self.gem5_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        return run.stdout.strip()

    def compiler_version(self) -> str:
        run = subprocess.run(
            [str(self.cc), "--version"], capture_output=True, text=True, check=True
        )
        return run.stdout.splitlines()[0].strip()

    def _sibling(self, tool: str) -> str:
        assert self.cc is not None
        return re.sub(r"gcc$", tool, str(self.cc))

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def kernel_sig(self, kernel: Kernel) -> str:
        """sha256 over what determines the measured binary and how it is run."""
        h = hashlib.sha256()
        for path in (kernel.source, KERNEL_DIR / "wf_kernel.h"):
            h.update(path.read_bytes())
        h.update(" ".join(CFLAGS).encode())
        h.update(self.compiler_version().encode())
        h.update(GEM5_TAG.encode())
        h.update(json.dumps(asdict(self.config), sort_keys=True).encode())
        return h.hexdigest()

    def build(self, kernel: Kernel) -> Path:
        """Cross-compile *kernel* (cached by its signature); return the binary."""
        sig = self.kernel_sig(kernel)[:16]
        exe = self.workdir / "bin" / f"{kernel.name}-{sig}"
        if not exe.is_file():
            exe.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                str(self.cc),
                *CFLAGS,
                f"-I{KERNEL_DIR}",
                f"-I{self.gem5_root / 'include'}",
                str(kernel.source),
                str(self.libm5),
                "-o",
                str(exe),
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        return exe

    def code_bytes(self, exe: Path, kernel: Kernel) -> int:
        """The summed sizes of *kernel*'s measured symbols in *exe* (``nm -S``).

        GCC renames functions it specializes -- ``tg_remove`` becomes ``tg_remove.isra.0`` after
        IPA-SRA, and ``.constprop`` / ``.part`` / ``.cold`` clones are as common -- so a symbol
        counts toward a name when its part before the first dot is that name.
        """
        return sum(symbol_sizes(self._sibling("nm"), exe, kernel).values())

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(
        self, kernel: Kernel, point: Mapping[str, Any]
    ) -> tuple[dict, dict[str, float], Path]:
        """Run *kernel* at *point* on gem5; return its printed JSON, region stats, run directory."""
        exe = self.build(kernel)
        tag = hashlib.sha256(
            json.dumps(dict(point), sort_keys=True).encode()
        ).hexdigest()[:12]
        rundir = self.workdir / "runs" / f"{exe.name}-{tag}"
        if rundir.exists():
            shutil.rmtree(rundir)
        rundir.mkdir(parents=True)
        shutil.copy2(exe, rundir / "prog")
        command = " ".join(["/run/prog", *kernel.argv(point)])
        cmd = [
            "docker",
            "run",
            "--rm",
            "-u",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{self.gem5_root}:/gem5:ro",
            "-v",
            f"{rundir}:/run",
            "-w",
            "/run",
            self.image,
            "/gem5/build/ARM/gem5.opt",
            "-q",
            "-d",
            "/run/m5out",
            "/gem5/configs/example/arm/starter_se.py",
            *self.config.starter_args(),
            command,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        (rundir / "gem5.log").write_text(proc.stdout + proc.stderr)
        if proc.returncode != 0:
            raise RuntimeError(
                f"gem5 failed for {kernel.name} {dict(point)}; see {rundir}/gem5.log"
            )
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        if len(lines) != 1:
            raise RuntimeError(
                f"expected one JSON line from {kernel.name}, got {len(lines)}"
            )
        stats = roi_stats((rundir / "m5out" / "stats.txt").read_text())
        (rundir / "roi_stats.json").write_text(json.dumps(stats))
        return json.loads(lines[0]), stats, rundir

    def empty_region_cycles(self) -> float:
        """The markers' own cost under this configuration."""
        _, stats, _ = self.run(EMPTY, {})
        return pick_stats(stats)["cycles_raw"]

    def provenance(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "source": "gem5",
            "gem5_tag": GEM5_TAG,
            "gem5_commit": self.gem5_commit(),
            "compiler": Path(str(self.cc)).name,
            "compiler_version": self.compiler_version(),
            "cflags": " ".join(CFLAGS),
            "cpu_model": cfg.cpu,
            "f_clk_hz": cfg.f_clk_hz,
            "l1i": cfg.l1i_bytes,
            "l1d": cfg.l1d_bytes,
            "l2": cfg.l2_bytes,
            "dram": f"{cfg.mem_type}x{cfg.mem_channels}",
            "cache_state": "warm",
        }

    def measure(
        self,
        kernel: Kernel,
        point: Mapping[str, Any],
        *,
        empty_cycles: float,
        plan: SweepPlan | None = None,
        smoke: bool = False,
    ) -> dict[str, Any]:
        """One corpus row for *kernel* at *point*.

        A point is measured only if it is registered in *plan* — or, with ``smoke=True``, if it is one
        of the kernel's own smoke points (used to check the toolchain, never fitted).
        """
        if smoke:
            if dict(point) not in [dict(p) for p in kernel.smoke]:
                raise PreregistrationError(
                    f"{kernel.name} {dict(point)} is not a smoke point"
                )
            role, prereg = "smoke", ""
        else:
            if plan is None:
                raise PreregistrationError(
                    "a non-smoke point needs a committed SweepPlan"
                )
            role, prereg = plan.role(kernel.name, point), plan.commit
        out, stats, rundir = self.run(kernel, point)
        twin = kernel.run_twin(point)
        row: dict[str, Any] = {
            "kernel": kernel.name,
            "point": json.dumps(dict(point), sort_keys=True),
            "role": role,
            **kernel.counters_of(out),
            **pick_stats(stats),
        }
        if kernel.working_set is not None:
            cfg = self.config
            from waveflow.cpu.config import CpuConfig

            row.update(
                regime_features(
                    kernel.working_set(out),
                    CpuConfig(l1d_bytes=cfg.l1d_bytes, l2_bytes=cfg.l2_bytes),
                )
            )
        row["empty_region_cycles"] = empty_cycles
        row["cycles"] = row["cycles_raw"] - empty_cycles
        row["output_matches_twin"] = bool(out == twin)
        row["code_bytes"] = self.code_bytes(self.build(kernel), kernel)
        row["kernel_sig"] = self.kernel_sig(kernel)
        row["prereg_commit"] = prereg
        row["run_id"] = (
            rundir.name
        )  # under the runner's workdir; no home path in committed data
        row.update(self.provenance())
        return row
