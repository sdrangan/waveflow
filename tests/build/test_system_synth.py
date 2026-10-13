"""The system top through Vivado (``plans/system_resources.md`` Stage 4) -- the toolchain-free half:
the hierarchical report parsed, joined to the inventory by instance name, the records adding up, and
the cache that keeps a re-run from starting Vivado.  The report text is a trimmed copy of markov's real
``report_utilization -hierarchical`` (Vivado 2025.1, xc7z020); the run itself is ``-m vivado``-free
here and exercised by ``markov_build.py --through system_synth``.
"""
from __future__ import annotations

import json

import pytest

from waveflow.build.system_synth import (INSTANCE_TARGET, SystemSynth, UnmappedInstanceError,
                                         attribute, synth_system, system_identity,
                                         system_synth_records)
from waveflow.utils.vivado_report import parse_utilization_hierarchical

_HEAD = """\
1. Utilization by Hierarchy
---------------------------

+--------------------+---------------------------------+------------+------------+---------+------+-------+--------+--------+------------+
|      Instance      |              Module             | Total LUTs | Logic LUTs | LUTRAMs | SRLs |  FFs  | RAMB36 | RAMB18 | DSP Blocks |
+--------------------+---------------------------------+------------+------------+---------+------+-------+--------+--------+------------+
| markov_top         |                           (top) |      18593 |      17847 |     304 |  442 | 40130 |      1 |      0 |          0 |
|   (markov_top)     |                           (top) |          0 |          0 |       0 |    0 |     2 |      0 |      0 |          0 |
"""
_ROWS = {
    "u_u_fwd_ff": (58, 16, 0), "u_xbar": (848, 680, 0), "u_gen_mm_front": (79, 30, 0),
    "u_gen_qcmd": (136, 57, 0), "u_gen_u_crd": (31, 130, 0), "u_chain_mm_front": (69, 30, 0),
    "u_chain_qu": (277, 64, 0), "u_chain_qresp": (130, 84, 0), "u_mem_front": (3625, 76, 0),
    "u_mem_mem": (8769, 32832, 0), "u_mem": (41, 74, 0), "u_gen": (1192, 1298, 0),
    "u_chain": (2034, 3115, 0), "u_u_fwd_wr": (812, 1020, 1), "u_u_crd_wr": (492, 622, 0),
}


def _row(indent: int, n: str, lut: int, ff: int, b36: int) -> str:
    name = " " * indent + n
    return (f"|{name:<20}|                          module | {lut:>10} | {lut:>10} |       0 |    0 "
            f"| {ff:>5} | {b36:>6} |      0 |          0 |")


def _report(drop: str | None = None) -> str:
    """The rows nested as the top now holds them: the crossbar a child of the top, every other
    instance a child of its device's or the memory's sub-top (``-hierarchical_depth 2``)."""
    spec = _spec()
    rows = []
    for sub in [""] + [s.inst for s in spec.subs]:
        mine = [i.inst for i in spec.instances() if i.sub == sub and i.inst != drop]
        if sub:
            tot = [sum(_ROWS[n][k] for n in mine) for k in range(3)]
            rows.append(_row(3, sub, *tot))
            rows.append(_row(5, f"({sub})", 0, 0, 0))
        rows += [_row(5 if sub else 3, n, *_ROWS[n]) for n in mine]
    return _HEAD + "\n".join(rows) + "\n+----+\n"


def _spec():
    from examples.markov.markov_build import build_dag
    return next(s for s in build_dag().steps() if s.name == "system_rtl").spec


def test_the_hierarchical_report_parses():
    rows = parse_utilization_hierarchical(_report())
    assert rows[0] == {"instance": "markov_top", "module": "(top)", "depth": 0,
                       "counters": {"lut": 18593, "lutram": 304, "srl": 442, "ff": 40130,
                                    "bram": 2, "dsp": 0}}
    assert rows[1]["instance"] == "(markov_top)" and rows[1]["depth"] == 1
    fwd = next(r for r in rows if r["instance"] == "u_u_fwd_wr")
    assert fwd["depth"] == 2 and fwd["counters"]["bram"] == 2        # one RAMB36 = two 18K halves
    from waveflow.build.system_synth import row_paths
    assert row_paths(rows)[rows.index(fwd)] == "u_gen_sub/u_u_fwd_wr"   # under its device
    with pytest.raises(ValueError):
        parse_utilization_hierarchical("no table here")


def test_every_instance_is_attributed_and_the_terms_add_up():
    spec = _spec()
    total, inst, unclaimed, recovered = attribute(spec, parse_utilization_hierarchical(_report()))
    assert recovered == []
    assert set(inst) == {i.inst for i in spec.instances()} and len(inst) == 15
    assert set(unclaimed) == {"(markov_top)", "u_gen_sub/(u_gen_sub)",   # the top's own cells,
                              "u_chain_sub/(u_chain_sub)", "u_mem_sub/(u_mem_sub)"}  # each sub-top's
    res = SystemSynth(top=spec.top, digest="d", part="p", period_ns=10.0, impl=False, total=total,
                      instances=inst)
    for c in ("lut", "ff", "bram"):
        assert sum(v[c] for v in inst.values()) + res.integration[c] == total[c]


def test_a_dropped_instance_raises():
    with pytest.raises(UnmappedInstanceError, match="u_gen_qcmd"):
        attribute(_spec(), parse_utilization_hierarchical(_report(drop="u_gen_qcmd")))


def test_records_file_under_each_identity_and_never_under_resource(tmp_path):
    from waveflow.build.system_dag import rtl_rel
    from waveflow.build.system_top import render_system_top
    from examples.markov.markov_build import HERE

    spec = _spec()
    kdirs = {m: HERE / rtl_rel(m) for m in spec.modules}
    if not all(d.is_dir() for d in kdirs.values()):
        pytest.skip("no csynth RTL for markov")
    total, inst, _u, _r = attribute(spec, parse_utilization_hierarchical(_report()))
    res = SystemSynth(top=spec.top, digest="d", part="xc7z020clg484-1", period_ns=10.0, impl=False,
                      total=total, instances=inst, seconds=600.0)
    recs = system_synth_records(spec, res, render_system_top(spec), kdirs)
    assert len(recs) == 15 + 2
    assert {r.target for r, _i in recs} == {INSTANCE_TARGET, "integration"}
    assert all(r.source == "vivado_synth" for r, _i in recs)
    by_inst = {r.payload.get("inst"): r for r, _i in recs if r.target == INSTANCE_TARGET}
    assert by_inst["u_chain"].payload["context"] == "markov_top"
    assert by_inst["u_chain"].payload["lut"] == 2034
    assert sum(r.cost_seconds for r, _i in recs) == pytest.approx(600.0)    # counted once


def test_a_cached_run_starts_no_vivado(tmp_path, monkeypatch):
    from waveflow.build.system_dag import rtl_rel
    from waveflow.build.system_top import render_system_top
    from examples.markov.markov_build import HERE

    spec = _spec()
    kdirs = {m: HERE / rtl_rel(m) for m in spec.modules}
    if not all(d.is_dir() for d in kdirs.values()):
        pytest.skip("no csynth RTL for markov")
    top_v = tmp_path / f"{spec.top}.v"
    top_v.write_text(render_system_top(spec), encoding="utf-8")
    _ident, run = system_identity(spec, top_v.read_text(), kdirs, part="p", period_ns=10.0,
                                  impl=False)
    cached = SystemSynth(top=spec.top, digest=run, part="p", period_ns=10.0, impl=False,
                         total={"lut": 1}, instances={})
    (tmp_path / "out" / run).mkdir(parents=True)
    (tmp_path / "out" / run / "result.json").write_text(json.dumps(cached.to_json()))

    def no_vivado():
        raise AssertionError("a cached run must not look for Vivado")

    monkeypatch.setattr("waveflow.toolchain.toolchain.find_vivado_path", no_vivado)
    got = synth_system(spec, top_v=top_v, kernel_dirs=kdirs, ip=None, out_dir=tmp_path / "out",
                       part="p", period_ns=10.0)
    assert got.total == {"lut": 1}
    # Another clock is another run.
    _i2, run2 = system_identity(spec, top_v.read_text(), kdirs, part="p", period_ns=4.0,
                                impl=False)
    assert run2 != run


def test_keep_hierarchy_marks_every_inventory_instance_in_the_synthesis_copy():
    from waveflow.build.system_synth import keep_hierarchy_top
    from waveflow.build.system_top import render_system_top

    spec = _spec()
    top = render_system_top(spec)
    kept = keep_hierarchy_top(top, [i.inst for i in spec.instances()])
    assert kept.count('(* keep_hierarchy = "yes" *)') == 15
    assert "keep_hierarchy" not in top                              # the XSI top is untouched


def test_a_dissolved_instance_is_priced_by_its_cell_names():
    """markov with block RAM: synthesis dissolved u_mem_mem's hierarchy; its RAMB36 keeps its name."""
    from waveflow.build.system_synth import parse_prims, prim_counters

    out = "WF_PRIM_synth u_gen_sub/u_gen_qcmd LUT6 3\nWF_PRIM_synth u_gen_sub/u_gen_qcmd FDRE 9\n" \
          "WF_PRIM_synth u_gen_sub/u_gen_qcmd RAM32M 2\nWF_PRIM_synth u_gen_sub/u_gen_qcmd SRLC32E 1\n" \
          "WF_PRIM_synth u_gen_sub/u_gen_qcmd RAMB36E1 1\nWF_PRIM_synth u_gen_sub/u_gen_qcmd CARRY4 5\n"
    prims = parse_prims(out, "synth")
    assert prim_counters(prims["u_gen_sub/u_gen_qcmd"]) == {"lut": 3 + 8 + 1, "ff": 9, "lutram": 8, "srl": 1,
                                                  "bram": 2}
    total, inst, _u, recovered = attribute(
        _spec(), parse_utilization_hierarchical(_report(drop="u_gen_qcmd")), prims)
    assert recovered == ["u_gen_qcmd"] and inst["u_gen_qcmd"]["bram"] == 2
