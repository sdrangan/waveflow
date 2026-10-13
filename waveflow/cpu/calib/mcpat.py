"""mcpat.py — energy, area and leakage of an A53-class core from gem5 activity, through McPAT.

No published gem5 -> McPAT converter reads MinorCPU/HPI statistics: those found (GEM5ToMcPAT,
Gem5ToMcPAT-Parser, gem5McPATparse) read ``config.json`` + ``stats.txt`` with out-of-order (O3)
statistic names against out-of-order templates.  This one is small and HPI-specific, and reads a
**corpus row** (its ``s_*`` activity columns, see :data:`waveflow.cpu.calib.gem5.MCPAT_STATS`), so the
energy model can be re-run from committed data alone.

The processor description starts from McPAT's own ``ARM_A9_2GHz.xml`` and is changed to the A53 the
platform describes (``plans/cpu_model.md`` §14): **in-order** (``machine_type=1``), 2-wide, 8-stage,
64-bit; 32 KB 2-way L1I and 32 KB 4-way L1D with 64-byte lines; a shared 16-way L2; McPAT's smallest
node (**22 nm**, the A53's own 16 nm is not modelled); no NoC and no memory controller, so energy is
the **core and L2 only** -- DRAM is not included; and a 32-bit virtual address (McPAT cannot size the
TLB for the A53's 48), which slightly under-sizes the TLB tags.

Energy of a measured region = McPAT's runtime dynamic power x the region's time, in pJ.  Leakage is
reported separately, per configuration.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: McPAT's smallest supported node (``cacti/technology.cc`` at ``74d4759f``).
TECH_NODE_NM = 22
MCPAT_COMMIT = "74d4759f3ba2dff8f5a69e07a68efdb46b42fb8c"


def default_mcpat_root() -> Path:
    return Path(os.environ.get("WAVEFLOW_MCPAT_ROOT", Path.home() / "ali/tools/mcpat"))


@dataclass(frozen=True)
class McpatConfig:
    """The core configuration McPAT describes (sizes in bytes, clock in Hz)."""

    n_cores: int = 1
    f_clk_hz: float = 1.2e9
    l1i_bytes: int = 32 * 1024
    l1d_bytes: int = 32 * 1024
    l2_bytes: int = 1024 * 1024


@dataclass(frozen=True)
class McpatResult:
    """McPAT's processor-level figures (W, mm²)."""

    area_mm2: float
    leakage_w: float
    runtime_dynamic_w: float
    peak_power_w: float


def _set(comp: ET.Element, kind: str, name: str, value: Any) -> None:
    for el in comp.findall(kind):
        if el.get("name") == name:
            el.set("value", str(value))
            return
    raise KeyError(f"{comp.get('id')}: no {kind} {name!r}")


def _comp(root: ET.Element, cid: str) -> ET.Element:
    for el in root.iter("component"):
        if el.get("id") == cid:
            return el
    raise KeyError(cid)


def build_xml(
    template: Path, cfg: McpatConfig, row: Mapping[str, float] | None
) -> ET.ElementTree[ET.Element]:
    """The A53 description, with *row*'s activity (``None``: an idle configuration, for area)."""
    tree = ET.parse(template)
    root = tree.getroot()
    sysc = _comp(root, "system")
    mhz = round(cfg.f_clk_hz / 1e6)
    table: tuple[tuple[str, Any], ...] = (
        ("number_of_cores", cfg.n_cores),
        ("number_of_L1Directories", 0),
        ("number_of_L2Directories", 0),
        ("number_of_L2s", 1),
        ("Private_L2", 0),
        ("number_of_L3s", 0),
        ("number_of_NoCs", 0),
        ("core_tech_node", TECH_NODE_NM),
        ("target_core_clockrate", mhz),
        ("machine_bits", 64),
        # A53 has 48-bit VAs, but McPAT@74d4759f finds no valid TLB array organization for any
        # virtual address wider than 32 bits ("no valid data array organizations found").  32 bits
        # under-sizes the TLB tags slightly; the effect on energy is small and documented.
        ("virtual_address_width", 32),
        ("physical_address_width", 40),
    )
    for name, value in table:
        _set(sysc, "param", name, value)

    r = dict(row or {})
    cycles = max(1.0, float(r.get("cycles_raw", 1.0)))
    insts = float(r.get("insts", 0.0))
    for name in ("total_cycles", "busy_cycles"):
        _set(sysc, "stat", name, cycles)
    _set(sysc, "stat", "idle_cycles", 0)

    core = _comp(root, "system.core0")
    table = (
        ("clock_rate", mhz),
        ("machine_type", 1),
        ("fetch_width", 2),
        ("decode_width", 2),
        ("issue_width", 2),
        ("peak_issue_width", 2),
        ("commit_width", 2),
        ("fp_issue_width", 1),
        ("pipeline_depth", "8,8"),
        ("ALU_per_core", 2),
        ("MUL_per_core", 1),
        ("FPU_per_core", 1),
        ("instruction_buffer_size", 16),
        ("phy_Regs_IRF_size", 32),
        ("phy_Regs_FRF_size", 32),
    )
    for name, value in table:
        _set(core, "param", name, value)
    fp = r.get("s_fp_insts", 0.0) + r.get("s_vec_insts", 0.0)
    loads, stores = r.get("s_load_insts", 0.0), r.get("s_store_insts", 0.0)
    table = (
        ("total_instructions", insts),
        ("int_instructions", r.get("s_int_insts", 0.0)),
        ("fp_instructions", fp),
        ("branch_instructions", r.get("s_branches", 0.0)),
        ("branch_mispredictions", r.get("s_mispredicts", 0.0)),
        ("load_instructions", loads),
        ("store_instructions", stores),
        ("committed_instructions", insts),
        ("committed_int_instructions", r.get("s_int_insts", 0.0)),
        ("committed_fp_instructions", fp),
        ("pipeline_duty_cycle", min(1.0, insts / cycles / 2.0)),
        ("total_cycles", cycles),
        ("busy_cycles", cycles),
        ("idle_cycles", 0),
        # In-order: no ROB, no renaming; the instruction buffer is read and written once per instruction.
        ("ROB_reads", 0),
        ("ROB_writes", 0),
        ("rename_reads", 0),
        ("rename_writes", 0),
        ("fp_rename_reads", 0),
        ("fp_rename_writes", 0),
        ("inst_window_reads", insts),
        ("inst_window_writes", insts),
        ("inst_window_wakeup_accesses", 0),
        ("fp_inst_window_reads", fp),
        ("fp_inst_window_writes", fp),
        ("fp_inst_window_wakeup_accesses", 0),
        ("int_regfile_reads", r.get("s_int_rf_reads", 0.0)),
        ("int_regfile_writes", r.get("s_int_rf_writes", 0.0)),
        ("float_regfile_reads", r.get("s_fp_rf_reads", 0.0)),
        ("float_regfile_writes", r.get("s_fp_rf_writes", 0.0)),
        ("function_calls", r.get("s_calls", 0.0)),
        ("context_switches", 0),
        ("ialu_accesses", r.get("s_ialu", 0.0)),
        ("fpu_accesses", r.get("s_fpalu", 0.0) + r.get("s_vecalu", 0.0)),
        ("mul_accesses", r.get("s_int_mult", 0.0) + r.get("s_int_div", 0.0)),
        ("cdb_alu_accesses", r.get("s_ialu", 0.0)),
        ("cdb_mul_accesses", r.get("s_int_mult", 0.0) + r.get("s_int_div", 0.0)),
        ("cdb_fpu_accesses", r.get("s_fpalu", 0.0) + r.get("s_vecalu", 0.0)),
    )
    for name, value in table:
        _set(core, "stat", name, value)

    icache = _comp(root, "system.core0.icache")
    _set(icache, "param", "icache_config", f"{cfg.l1i_bytes},64,2,1,1,3,16,0")
    _set(icache, "stat", "read_accesses", r.get("s_icache_reads", 0.0))
    _set(icache, "stat", "read_misses", r.get("s_icache_read_misses", 0.0))
    dcache = _comp(root, "system.core0.dcache")
    _set(dcache, "param", "dcache_config", f"{cfg.l1d_bytes},64,4,1,1,3,16,1")
    _set(dcache, "stat", "read_accesses", r.get("s_dcache_reads", 0.0))
    _set(dcache, "stat", "write_accesses", r.get("s_dcache_writes", 0.0))
    _set(dcache, "stat", "read_misses", r.get("s_dcache_read_misses", 0.0))
    _set(dcache, "stat", "write_misses", r.get("s_dcache_write_misses", 0.0))
    _set(
        _comp(root, "system.core0.itlb"),
        "stat",
        "total_accesses",
        r.get("s_icache_reads", 0.0),
    )
    _set(_comp(root, "system.core0.itlb"), "stat", "total_misses", 0)
    _set(_comp(root, "system.core0.dtlb"), "stat", "total_accesses", loads + stores)
    _set(_comp(root, "system.core0.dtlb"), "stat", "total_misses", 0)
    btb = _comp(root, "system.core0.BTB")
    _set(btb, "stat", "read_accesses", r.get("s_branches", 0.0))
    _set(btb, "stat", "write_accesses", r.get("s_mispredicts", 0.0))

    l2 = _comp(root, "system.L20")
    _set(l2, "param", "L2_config", f"{cfg.l2_bytes},64,16,1,1,13,32,1")
    _set(l2, "param", "clockrate", mhz)
    wb = r.get("s_l2_writebacks", 0.0)
    _set(l2, "stat", "read_accesses", max(0.0, r.get("s_l2_accesses", 0.0) - wb))
    _set(l2, "stat", "write_accesses", wb)
    _set(l2, "stat", "read_misses", r.get("s_l2_misses", 0.0))
    _set(l2, "stat", "write_misses", 0)
    return tree


_NUM = r"([0-9.eE+-]+)"


def parse_output(text: str) -> McpatResult:
    """The processor-level figures: the first ``Area`` / ``Total Leakage`` / ``Runtime Dynamic``."""

    def first(label: str) -> float:
        m = re.search(rf"{label} = {_NUM}", text)
        if m is None:
            raise RuntimeError(f"McPAT output has no {label!r}")
        return float(m.group(1))

    return McpatResult(
        area_mm2=first("Area"),
        leakage_w=first("Total Leakage"),
        runtime_dynamic_w=first("Runtime Dynamic"),
        peak_power_w=first("Peak Power"),
    )


@dataclass
class Mcpat:
    """Run McPAT natively on an A53 description."""

    root: Path = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.root is None:
            self.root = default_mcpat_root()

    @property
    def exe(self) -> Path:
        return self.root / "mcpat"

    @property
    def template(self) -> Path:
        return self.root / "ProcessorDescriptionFiles" / "ARM_A9_2GHz.xml"

    def unavailable(self) -> str | None:
        if not self.exe.is_file():
            return f"no McPAT build at {self.exe} (set WAVEFLOW_MCPAT_ROOT)"
        if not self.template.is_file():
            return f"no template at {self.template}"
        return None

    def run(
        self, cfg: McpatConfig, row: Mapping[str, float] | None = None
    ) -> McpatResult:
        tree = build_xml(self.template, cfg, row)
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "a53.xml"
            tree.write(xml)
            proc = subprocess.run(
                [str(self.exe), "-infile", str(xml), "-print_level", "1"],
                capture_output=True,
                text=True,
                check=False,
                cwd=tmp,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"McPAT failed: {proc.stderr.strip()[:400]}")
        return parse_output(proc.stdout)

    def region_energy_pj(self, cfg: McpatConfig, row: Mapping[str, float]) -> float:
        """Dynamic energy of a measured region: runtime dynamic power x its time, in pJ."""
        res = self.run(cfg, row)
        seconds = float(row["cycles_raw"]) / cfg.f_clk_hz
        return res.runtime_dynamic_w * seconds * 1e12


def have_mcpat() -> bool:
    return Mcpat().unavailable() is None and shutil.which("true") is not None
