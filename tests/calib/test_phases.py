"""tests/calib/test_phases.py — calibrating declared phases from RTL (``plans/pipelined_ops.md`` Stage 5).

No toolchain: the RTL side is the committed cosim trace of poly's ``early_tlast_vcd`` scenario
(``examples/stream_inband/vcd/error_path.vcd``).  What is checked is that everything is generated from
the module's declarations -- the ports to read, the transfers on them, the pairing with pysim's records
-- and that the fit and its hold-out work.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("vcdvcd")

from examples.stream_inband.poly import PolyAccel, PolyTB, connect  # noqa: E402
from waveflow.calib.phases import fit_phase, phase_corpus, rtl_xfers, timed_ports  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.utils.burst_io import read_bursts  # noqa: E402

EX = Path(__file__).resolve().parents[2] / "examples" / "stream_inband"
VCD = EX / "vcd" / "error_path.vcd"
SCOPE = "apatb_poly_top.AESL_inst_poly"


def _poly_after(scenario: str) -> PolyAccel:
    """Poly run in pysim on a w32 scenario, its phases' records filled."""
    from examples.stream_inband import scenarios as S

    d = EX / "data" / "w32"
    S.write_scenarios(d, 32)
    sim = Simulation()
    clk = Clock(freq=100e6)
    a = PolyAccel(name="poly_accel", sim=sim, clk=clk)
    tb = PolyTB(name="poly_tb", sim=sim, stimulus=d / scenario / "in", word_bw=32,
                n_out=len(read_bursts(d / scenario / "expected")))
    connect(sim, tb, a, clk)
    sim.run_sim()
    return a


def test_the_ports_come_from_the_declarations():
    a = PolyAccel(name="poly_accel", sim=Simulation())
    assert {k: v[1] for k, v in timed_ports(a).items()} == {
        "poly_accel_s_in": "s_in", "poly_accel_m_out": "m_out"}


def test_rtl_transfers_are_decoded_from_the_trace():
    """Header (6 words), samples (8), header, the early-ended samples (6): framed by TLAST."""
    a = PolyAccel(name="poly_accel", sim=Simulation())
    x = rtl_xfers(VCD, a, SCOPE)
    assert [r[3] for r in x["poly_accel_s_in"]] == [6, 8, 6, 6]
    assert [r[3] for r in x["poly_accel_m_out"]] == [1, 8, 1, 6]
    assert x["poly_accel_s_in"][0][1:3] == (14.0, 22.0)


def test_the_join_pairs_pysim_records_with_rtl_times():
    a = _poly_after("early_tlast_vcd")
    rtl = rtl_xfers(VCD, a, SCOPE)
    hdr = phase_corpus(a.timing_phases["hdr"], rtl)
    samples = phase_corpus(a.timing_phases["samples"], rtl)
    assert [r["rtl"] for r in hdr] == [4.0, 4.0]                    # response 4 cycles after header
    lat = [r for r in samples if r["span"] == "latency"]
    fill = [r for r in samples if r["span"] == "fill_time"]
    assert [r["rtl"] for r in lat] == [34.0, 34.0]                  # first result 34 after first sample
    assert fill[0]["nwords"] == 8 and fill[0]["rtl"] == 7.0         # 8 words: beats 0..7
    # The second command's burst ends early (6 of 10 words): its rows are marked short.
    assert [r["short"] for r in fill] == [False, True]


def test_the_fit_and_its_holdout():
    """Rows built by hand: fill = n - 1 exactly; the held-out row is predicted, not used."""
    a = PolyAccel(name="poly_accel", sim=Simulation())
    ph = a.timing_phases["samples"]
    ph.calib_dir = None
    rows = ([{"nwords": n, "span": "fill_time", "rtl": n - 1.0, "stalled": False} for n in (8, 16, 32, 64)]
            + [{"nwords": n, "span": "latency", "rtl": 34.0, "stalled": False} for n in (8, 16, 32, 64)]
            + [{"nwords": 24, "span": "latency", "rtl": 99.0, "stalled": True}]    # stalled: not used
            + [{"nwords": 40, "span": "fill_time", "rtl": 5.0, "stalled": False, "short": True}])
    rep = fit_phase(ph, rows, holdout=lambda f: f["nwords"] == 32, save=False)
    assert rep["fill_time"]["params"]["nwords"] == pytest.approx(1.0)
    assert rep["fill_time"]["params"]["intercept"] == pytest.approx(-1.0)
    (feats, rtl, pred), = rep["fill_time"]["holdout"]
    assert feats == {"nwords": 32} and pred == pytest.approx(31.0)
    assert rep["latency"]["params"]["intercept"] == pytest.approx(34.0)


def test_a_featureless_phase_fits_its_mean():
    a = PolyAccel(name="poly_accel", sim=Simulation())
    ph = a.timing_phases["hdr"]
    ph.calib_dir = None
    rows = [{"span": "resp_time", "rtl": 4.0, "stalled": False} for _ in range(3)]
    assert fit_phase(ph, rows, save=False)["resp_time"]["params"]["intercept"] == pytest.approx(4.0)


@pytest.mark.parametrize("bw", [32, 64])
def test_the_filed_parameters_are_what_the_committed_trace_fits(bw, tmp_path):
    """The fitted parameters in ``calib/w<W>/`` are reproduced from the committed traced run
    (``vcd/calib_w<W>.vcd``) with no toolchain -- every number has a script."""
    import json

    from examples.stream_inband import poly_calibrate as C

    filed = {f"{p.parent.name}.{p.stem}": json.loads(p.read_text(encoding="utf-8"))
             for p in sorted(C.calib_dir(bw).rglob("*.json"))}
    rep = C.calibrate(bw, vcd=EX / "vcd" / f"calib_w{bw}.vcd", root=EX, file=False)
    got = {f"{ph}.{span}": f["params"] for ph, v in rep["phases"].items() for span, f in v["fit"].items()}
    assert set(got) == set(filed)
    for key, params in got.items():
        for name, value in params.items():
            assert filed[key][name] == pytest.approx(value), key
