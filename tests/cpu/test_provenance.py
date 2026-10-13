"""AC12 of ``plans/cpu_model.md`` (provenance): every measured row names its tools.

A measured number belongs to its tools (``CLAUDE.md``): the gem5 tag and commit, the compiler, its
version and flags, the core / cache / DRAM configuration, the McPAT commit and node, and the
pre-registration it was measured under.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from waveflow.cpu.calib.gem5 import CFLAGS, GEM5_TAG
from waveflow.cpu.calib.mcpat import MCPAT_COMMIT, TECH_NODE_NM

CPU = (
    Path(__file__).resolve().parents[2]
    / "waveflow/calib/platforms/a53_hpi_1200mhz_gem5v25_1/cpu"
)

COLUMNS = (
    "source",
    "gem5_tag",
    "gem5_commit",
    "compiler",
    "compiler_version",
    "cflags",
    "cpu_model",
    "f_clk_hz",
    "l1i",
    "l1d",
    "l2",
    "dram",
    "cache_state",
    "kernel_sig",
    "prereg_commit",
    "mcpat_commit",
    "mcpat_node_nm",
    "empty_region_cycles",
    "empty_region_energy_pj",
)


def _corpus() -> pd.DataFrame:
    from waveflow.cpu.calib.calibrate import load_corpus

    return load_corpus(
        CPU
    )  # the kernel corpora by name: cpu/area/ holds McPAT-only rows


def test_every_row_carries_every_provenance_column():
    df = _corpus()
    assert len(df) == 305
    for col in COLUMNS:
        assert col in df, col
        assert df[col].notna().all(), col
        assert (df[col].astype(str) != "").all(), col


def test_the_tools_are_the_ones_the_plan_names():
    df = _corpus()
    assert set(df["source"]) == {"gem5"}
    assert set(df["gem5_tag"]) == {GEM5_TAG}
    assert df["gem5_commit"].str.startswith("c8222cc").all()  # v25.1.0.1
    assert df["compiler_version"].str.contains("12.2.0").all()
    assert set(df["cflags"]) == {" ".join(CFLAGS)}
    assert set(df["cpu_model"]) == {"hpi"} and set(df["f_clk_hz"]) == {1.2e9}
    assert set(df["dram"]) == {"DDR4_2400_8x8x1"}
    assert set(df["mcpat_commit"]) == {MCPAT_COMMIT}
    assert set(df["mcpat_node_nm"]) == {TECH_NODE_NM}
    assert set(df["empty_region_cycles"]) == {94.0}


def test_no_row_records_a_home_path():
    df = _corpus()
    assert "rundir" not in df
    for col in df.columns:
        assert not df[col].astype(str).str.contains("/home/").any(), col
