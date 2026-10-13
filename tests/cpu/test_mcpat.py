"""Step 11 of ``plans/cpu_model.md``: the McPAT converter.

Unmarked: the output parser on a committed fixture.  ``gem5``-marked (the ground-truth tools, of
which McPAT is one): the A53 description the converter builds from McPAT's template, and a run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.cpu.calib.mcpat import (
    TECH_NODE_NM,
    Mcpat,
    McpatConfig,
    _comp,
    build_xml,
    parse_output,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/cpu/mcpat_idle_a53.txt"


def test_the_parser_reads_the_processor_summary():
    res = parse_output(FIXTURE.read_text())
    assert (res.area_mm2, res.leakage_w, res.runtime_dynamic_w) == (
        3.24105,
        0.0969237,
        0.0,
    )
    assert res.peak_power_w == 1.16674


def test_the_parser_refuses_output_without_a_summary():
    with pytest.raises(RuntimeError, match="Area"):
        parse_output(
            "McPAT (version 1.3) ...\nERROR: no valid data array organizations found\n"
        )


@pytest.fixture(scope="module")
def mcpat():
    m = Mcpat()
    why = m.unavailable()
    if why:
        pytest.skip(why)
    return m


def _param(root, cid, name):
    comp = _comp(root, cid)
    return next(e.get("value") for e in comp.findall("param") if e.get("name") == name)


@pytest.mark.gem5
def test_the_description_is_an_in_order_a53(mcpat):
    root = build_xml(
        mcpat.template, McpatConfig(n_cores=2, l2_bytes=512 * 1024), None
    ).getroot()
    assert _param(root, "system.core0", "machine_type") == "1"
    assert _param(root, "system", "core_tech_node") == str(TECH_NODE_NM)
    assert _param(root, "system", "number_of_cores") == "2"
    assert _param(root, "system", "number_of_L2s") == "1"
    assert _param(root, "system", "virtual_address_width") == "32"
    assert _param(root, "system.L20", "L2_config").startswith("524288,64,16,")
    assert _param(root, "system.core0.dcache", "dcache_config").startswith(
        "32768,64,4,"
    )


@pytest.mark.gem5
def test_mcpat_runs_and_energy_scales_with_time(mcpat):
    cfg = McpatConfig()
    idle = mcpat.run(cfg, None)
    assert idle.area_mm2 == pytest.approx(3.24105, rel=1e-6)  # McPAT is deterministic
    row = {
        "cycles_raw": 1000.0,
        "insts": 800.0,
        "s_int_insts": 800.0,
        "s_ialu": 800.0,
        "s_icache_reads": 200.0,
        "s_dcache_reads": 100.0,
    }
    e1 = mcpat.region_energy_pj(cfg, row)
    e2 = mcpat.region_energy_pj(
        cfg,
        {
            **row,
            "cycles_raw": 2000.0,
            "insts": 1600.0,
            "s_int_insts": 1600.0,
            "s_ialu": 1600.0,
            "s_icache_reads": 400.0,
            "s_dcache_reads": 200.0,
        },
    )
    assert e1 > 0
    assert e2 == pytest.approx(
        2 * e1, rel=0.05
    )  # twice the work at the same rate: twice the energy
