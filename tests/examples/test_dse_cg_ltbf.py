"""examples/dse_cg's LTBF design at its grid's points, no toolchain (plans/dse_cg.md steps 1a, 1b, 1d).

* 1a: the options of ``examples/ltbf_cg_mem_dse`` the DSE uses -- the per-design formats make the
  workload (``t``, ``target``) and the simulated memory (``mem_banks``, ``mem_rate``) generate no new
  HLS, while ``W`` and ``form`` do; the long-term update runs alone (``slot=False``);
* 1b: the adaptor's default point is AC9's design with those options;
* 1d: the end-to-end run in pysim -- a cached Sionna RT drop, rendered and OFDM-demodulated, through the
  system (the long-term update, then one slot) to the equalized symbols, bit for bit against the
  fixed-point golden, and their SINR the accuracy pipeline's.

The RTL halves are ``-m xsi`` (``test_ltbf_cg_mem_dse_xsi.py::test_modelled_memory_at_rtl``), and the
Sionna re-trace is ``-m sionna`` (``test_ltbf_cg_mem_dse_link.py::test_drops_retrace_bit_for_bit``).
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace

import numpy as np
import pytest

from examples.dse_cg.ltbf_cg_mem import LtbfCgMemAdaptor
from examples.ltbf_cg_mem_dse.hw import build as hb
from examples.ltbf_cg_mem_dse.hw import system as sy


@pytest.fixture(scope="module")
def adaptor():
    return LtbfCgMemAdaptor()


def _hls(root, p) -> dict[str, str]:
    root.mkdir(parents=True)
    shutil.copytree(hb.ROOT / "src", root / "src")
    hb.generate(root, p)
    files = sorted(f for d in ("gen", "include") for f in (root / d).rglob("*") if f.is_file())
    return {str(f.relative_to(root)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def test_the_default_point_is_ac9_with_the_dse_options(adaptor):
    p = adaptor.sys_params({})
    assert p == replace(sy.AC9, formats="design", slot=False, drop=1, sector=1)
    assert adaptor.sys_params({}, "end_to_end").slot


def test_what_generates_hls_and_what_does_not(tmp_path):
    """Per-design formats: the workload and the simulated memory change no source; W and form do."""
    base = replace(sy.AC9, formats="design")
    ref = _hls(tmp_path / "ref", base)
    for tag, kw in {"t2": {"t": 2}, "target": {"target": "-6_3"}, "banks": {"mem_banks": 2},
                    "rate": {"mem_rate": 1}, "slot": {"slot": False}}.items():  # fmt: skip
        assert _hls(tmp_path / tag, replace(base, **kw)) == ref, tag
    for tag, kw, parts in (("form", {"form": "explicit"}, {"gen/ltbf_sys.cpp"}),
                           ("W", {"W": 12}, {"gen/ltbf_sys.cpp", "gen/ltbf_vec.cpp"})):  # fmt: skip
        got = _hls(tmp_path / tag, replace(base, **kw))
        changed = {f for f in set(got) | set(ref) if got.get(f) != ref.get(f) and f.startswith("gen/")}
        assert changed == parts, (tag, changed)


def test_the_long_term_update_runs_alone(adaptor):
    hw = adaptor.build_system({})
    S, host = hw.ltbf
    hw.run()
    assert sy.check(S, host) == {"G'": True}
    head = sy.scenario(S, host)[0]
    assert int(head[12]) == len(S.lt.jobs) and int(head[13]) == 0  # n_lt, n_rt (after K_HEAD)


@pytest.mark.parametrize("params", [{}, {"banks": 2, "mem_rate": 1, "cg_window": 4}],
                         ids=["defaults", "b2r1g4"])  # fmt: skip
def test_end_to_end_from_a_sionna_drop_in_pysim(adaptor, params):
    from examples.ltbf_cg_mem_dse import ltbf as lt
    from examples.ltbf_cg_mem_dse import ltbf_chain as lc
    from examples.ltbf_cg_mem_dse import ltbf_fixed as lf
    from examples.ltbf_cg_mem_dse import ltbf_fixed_chain as fc

    hw = adaptor.build_system(params, "end_to_end")
    S, host = hw.ltbf
    hw.run()
    assert all(sy.check(S, host).values()), sy.check(S, host)  # G', Z, w, x̂ bit for bit
    inp = S.inp
    cfg, prep, p = inp.cfg, inp.prep, inp.p
    hw_sinr = {u: lc.measure(lf.to_complex(*inp.gold["xh"][u], cfg.eq.xh), prep.data[u])["sinr_mmse_db"]
               for u in inp.own}  # fmt: skip
    chain = fc.run_fixed(prep, [cfg], t_max=p.t, t_eval=(p.t,), ranks=(1,))[(cfg.name, p.t, 1)]
    for u in inp.own:
        assert hw_sinr[u] == pytest.approx(chain[u]["sinr_mmse_db"], abs=1e-9), u
    exact = lt.sector_a(prep.ltin, p.sector, lt.Solve(), None)
    _g, gp = lc.project(prep, exact, 1)
    ref = lc.real_time(prep, gp)
    loss = np.array([ref[u]["sinr_mmse_db"] - hw_sinr[u] for u in inp.own])
    print(f"drop {p.drop} sector {p.sector}: {len(loss)} UEs, SINR loss median {np.median(loss):.3f} dB")
    assert np.all(np.isfinite(loss))


# -- plan step 2b: the adaptor's bookkeeping -----------------------------------------------------------


def test_defaults_fill_and_legal(adaptor):
    from examples.dse_cg.ltbf_cg_mem import GRID

    assert adaptor.defaults() == {k: v[2] for k, v in GRID.items()}
    with pytest.raises(KeyError):
        adaptor.fill({"Wx": 12})                                   # a typo is refused
    with pytest.raises(KeyError):
        adaptor.fill({"t": 2})                                     # a workload parameter is not a design one
    assert adaptor.legal({}) is None
    assert adaptor.legal({"R": 2, "C": 8}) is not None             # not a calibrated shape


def test_the_architecture_is_the_same_at_every_extreme(adaptor):
    arch = adaptor.architecture()                                  # raises if a parameter changes it
    assert {n["name"] for n in arch["nodes"]} == {"sys", "vec", "hub", "xbar", "ing", "mem"}
    assert adaptor.architecture_at({"banks": 2})["sizes"]["memories"] == 2


def test_rebuilds_are_what_the_grid_says(adaptor):
    """Computed from content (each part's Verilog and HLS sources), the parts each design parameter
    reaches are exactly the grid table's (plans/dse_cg.md, *The space*)."""
    from examples.dse_cg.ltbf_cg_mem import GRID

    want = {k: sorted(v[3]) for k, v in GRID.items() if v[0] == "design"}
    assert adaptor.rebuilds() == want
    base, sim = adaptor.identities({}), adaptor.sim_identity({})
    for params, workload in (({}, {"name": "long_term", "t": 2}),
                             ({}, {"name": "long_term", "target": "-6_3"}),
                             ({"cg_window": 4}, None)):  # fmt: skip
        assert adaptor.identities(params) == base
        got = adaptor.sim_identity(params, workload)
        assert got["build"] == sim["build"] and got["run"] != sim["run"]
    assert adaptor.sim_identity({"mem_rate": 1})["build"] != sim["build"]   # its memory leaf


def test_pysim_and_golden_report_the_same_accuracy(adaptor):
    from examples.dse_cg.ltbf_cg_mem import ACC_DIR

    if not (ACC_DIR / "W16f_-6_14.json").is_file():
        pytest.skip("the accuracy table is local: run examples.dse_cg.ltbf_cg_mem.accuracy_losses first")
    for params in ({}, {"L": 2, "banks": 2}):
        g = adaptor.golden(params, {"name": "accuracy", "t": 3, "target": "-6_14"})
        p = adaptor.pysim(params, "long_term")
        assert p["bit_exact"] and (p["median_loss_db"], p["p90_loss_db"]) == (g["median_loss_db"], g["p90_loss_db"])
