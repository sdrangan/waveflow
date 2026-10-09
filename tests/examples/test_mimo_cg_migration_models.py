"""Step 9.4e of plans/mimo_cg/mimo_cg_paper_sims.md: the study's models refitted on the detector
built from Waveflow's components, and the brute force on the components (no toolchain).

* The frozen model file is what a refit of the re-measured calibration builds gives.
* Its structure is the new hardware's: the cores' DSP and block RAM are the components' counted
  rules, the loader deserializes ``A`` in L-lane groups, and the ``A`` channel holds them; on the
  re-measured detectors its DSP and block RAM match except where the counted rule misses.
* The campaign's role runs the 1,440 detectors of the committed sub-grid under new labels, as the
  brute force did, and the scoring judges the committed decisions with this model.
"""

from __future__ import annotations

import pytest

from examples.mimo_cg.hw import campaign as C
from examples.mimo_cg.hw import migration as MG
from examples.mimo_cg.hw import migration_models as MM
from examples.mimo_cg.hw.space import HwConfig, read_bruteforce
from examples.mimo_cg.mimo_cg import read_table, write_table

#: The model file as frozen at step 9.4e, before any brute-force build on the components ran.
FROZEN_SHA256 = "8f4a342f28dfcf2c5e0ae480d4b4f0c65769c57eda74782a1c500a10c84f2ac3"


def test_the_frozen_model_is_what_the_refit_gives():
    frozen = MM.calibrated()
    again = MM.fit(loo=False)
    assert again.table == frozen.table
    for name, co in frozen.coef.items():
        assert again.coef[name] == pytest.approx(co, rel=1e-6), name
    assert frozen.meta["version"] == MM.VERSION and frozen.meta["fit_builds"] == 86
    if FROZEN_SHA256 is not None:
        assert MM.model_sha256() == FROZEN_SHA256


def test_the_model_has_the_new_structure():
    m = MM.calibrated()
    assert MM.DETECTOR_MODULES == (
        "CgCmdRx", "MemRStream", "CgLoad", "CgCtrl",
        "CgVectorCore", "SystolicCore", "CgStore", "MemWStream",
    )  # fmt: skip
    c = HwConfig()
    res = m.resources(c)
    core = MM.core_counted(c)
    assert res["modules"]["CgVectorCore"]["dsp"] == core["vec"]["dsp"]
    assert res["modules"]["SystolicCore"]["dsp"] == core["mm"]["dsp"]
    # the A channel holds L-lane groups: at K = 16 it no longer takes 11 block RAMs
    (a,) = [ch for ch in MM.channels({**vars(c), "K": 16, "R": 16}) if ch[0] == "a_blk"]
    assert a[1:3] == (64, 96)
    assert m.table["handoff|loop"] == {"cycles": 0.0}


def test_counted_quantities_match_the_remeasured_detectors():
    m = MM.calibrated()
    builds = {r["build"]: r for r in read_table(MG.PAPER_DATA / "migration_builds.csv")}
    misses = []
    for r in MG.read_list():
        if r["top"] != "det" or r["role"] == "finalist":
            continue
        got = builds[r["build"]]
        res = m.resources(r["config"])["total"]
        if res["bram"] != int(got["bram"]):
            misses.append((r["old"], "bram"))
        if res["dsp"] != int(got["dsp"]):
            misses.append((r["old"], "dsp"))
    # the one detector where the systolic core's counted rule misses (R = 1, C = L; step 9.4d)
    assert misses == [("det_k8_l4_r1_c4_m4_w12g4_d64_s2_q4", "dsp")]


def test_the_campaign_runs_the_subgrid_on_the_components():
    builds = MM.bruteforce_builds()
    assert len(builds) == 1440
    old = [b for b, *_ in read_bruteforce()]
    assert [b for b, *_ in builds] == [f"mig_{b}" for b in old]
    split = C.split()
    assert all(split[b][1] == C.MIGRATION_BRUTEFORCE for b, *_ in builds)
    assert C.records_dir((C.MIGRATION_BRUTEFORCE,)) == MG.POINTS


def test_the_role_is_measured_as_the_brute_force(tmp_path, monkeypatch):
    from examples.mimo_cg.hw import measure as M

    calls = []
    monkeypatch.setattr(
        M, "measure", lambda *a, **kw: calls.append(kw) or {"build": a[0]}
    )
    monkeypatch.setattr(MG, "POINTS", tmp_path)
    monkeypatch.setattr(M, "POINTS_DIR", tmp_path)  # where the dry run writes its note
    build = MM.bruteforce_builds()[0][0]
    C.HwPointStep(name="hw_point").run(None, build=build)
    assert calls == [
        {
            "role": C.MIGRATION_BRUTEFORCE,
            "steady": True,
            "trace": False,
            "prune": True,
            "points_dir": tmp_path,
            "workload_label": MG.old_label(build),
        }
    ]
    text = C.DryPointStep(name="hw_dry").run(None, build=build)["hw_dry"].read_text()
    _top, _role, c = C.split()[build]
    assert f"jobs={M.job_nits(c.K, steady=True)}" in text


def test_scoring_judges_every_committed_decision(tmp_path):
    """On stand-in tables (the old brute force under the new labels) the scoring runs and judges
    each of the 2,592 committed decisions; what it finds there means nothing."""
    for name in ("builds", "cycles"):
        rows = read_table(MG.PAPER_DATA / f"bruteforce_{name}.csv")
        for r in rows:
            r["build"] = MG.new_label(r["build"])
        write_table(tmp_path / f"{MM.STEM}_{name}.csv", rows, "stand-in")
    out = MM.score(tmp_path)
    assert len(out[f"{MM.STEM}_decisions"]) == 2592
    assert {r["resource"] for r in out[f"{MM.STEM}_fidelity_metrics"]} == {
        "dsp", "lut", "ff", "bram", "any",
    }  # fmt: skip
