"""Step 5.2 of plans/mimo_cg/mimo_cg_paper_sims.md: the measurement harness, adapted at step 9.4a to
the detector on Waveflow's cores and to unit builds that are the components' standalone units.

Without markers: the extraction is checked on synthetic events, on a committed excerpt of a real
trace (``tests/fixtures/mimo_cg/systolic_unit_k4_locks.vcd``: the clock, the stream-of-blocks lock
nets and the channels' ``i_full_n`` nets of the K = 4 systolic unit, six requests, 2,520 cycles; cut
from a step 9.4a scratch run, Vivado xsim 2024.1) and on an excerpt of a real report.

Under ``-m xsi`` (needs Vitis HLS and Vivado xsim): the harness takes the K = 4 default detector and
its two cores' units, and a memory-bound systolic unit, through csynth and a traced RTL run, and must
reproduce the rows and job intervals the re-measurement committed (step 9.4b), with core spans that
agree between unit and detector.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from examples.mimo_cg.hw import build as B
from examples.mimo_cg.hw import measure as M
from examples.mimo_cg.hw.space import HwConfig, _mm, _vec
from waveflow.toolchain import toolchain

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "mimo_cg"
    / "systolic_unit_k4_locks.vcd"
)


# --- job intervals -----------------------------------------------------------------------------


def test_job_intervals_fit_one_word_per_done():
    jobs = [1, 2, 3, 4, 1, 2]
    done, t = [], 1513
    for i, nit in enumerate(jobs):
        t = t if i == 0 else t + 75 + 1193 * nit
        done.append(t)
    fit = M.job_intervals(done, jobs)
    assert fit["first_done"] == 1513
    assert fit["nit"] == [2, 3, 4, 1, 2]
    assert fit["interval"] == [2461, 3654, 4847, 1268, 2461]
    assert fit["t0"] == pytest.approx(75) and fit["t_iter"] == pytest.approx(1193)
    assert fit["max_resid"] < 1e-6


def test_job_intervals_take_the_last_word_of_a_two_word_done():
    jobs = [1, 2, 3]
    last = [100, 300, 600]
    done = [c for t in last for c in (t - 1, t)]  # 32-bit words: CgDesc is two words
    fit = M.job_intervals(done, jobs)
    assert fit["first_done"] == 100 and fit["interval"] == [200, 300]
    with pytest.raises(ValueError, match="done words"):
        M.job_intervals(done[:-1], jobs)


# --- block spans -------------------------------------------------------------------------------


def _mm_events(p_avail, mm_reads, mm_writes, s_free=()):
    return {
        "pulses": {
            ("systolic_load_task", "b_blk", "write"): list(p_avail),
            ("systolic_core_task", "b_blk", "read"): list(mm_reads),
            ("systolic_core_task", "c_blk", "write"): list(mm_writes),
        },
        "free": {"c_blk": list(s_free)},
    }


def test_block_spans_separate_waiting_from_back_to_back():
    # iteration 1: the input arrives at 100 to an idle task; iteration 2: its input (at 150) was
    # already waiting when the task finished iteration 1 at 300.
    ev = _mm_events(p_avail=[100, 150], mm_reads=[130, 330], mm_writes=[300, 501])
    spans = M.block_spans(ev)["mm.iter"]
    assert [(s["span"], s["regime"], s["stalled"]) for s in spans] == [
        (200, "wait", False),
        (201, "b2b", False),
    ]
    summary = M.summarize_spans({"mm.iter": spans})["mm.iter"]
    assert summary["span"] == 200
    assert summary["wait"] == {"n": 1, "min": 200, "max": 200}
    assert summary["b2b"] == {"n": 1, "min": 201, "max": 201}


def test_block_spans_mark_a_span_that_waited_for_its_output_channel():
    # the output channel became free at 499 and the block was handed over at 500: it was waiting.
    ev = _mm_events(p_avail=[100], mm_reads=[130], mm_writes=[500], s_free=[499])
    (span,) = M.block_spans(ev)["mm.iter"]
    assert span["stalled"]
    summary = M.summarize_spans({"mm.iter": [span]})["mm.iter"]
    assert summary["span"] is None and summary["stalled"] == 1
    # free long before the hand-over: not stalled
    ev = _mm_events(p_avail=[100], mm_reads=[130], mm_writes=[500], s_free=[300])
    assert not M.block_spans(ev)["mm.iter"][0]["stalled"]


def test_block_spans_of_the_vector_unit_name_init_iter_and_last():
    # one job of two iterations: B → P0, S1 → P1, S2 → X.  S is the systolic core's c_blk port
    # on the detector's s_blk channel, which the channel map says.
    ev = {
        "pulses": {
            ("cg_load_task", "b_blk", "write"): [10],
            ("systolic_core_task", "c_blk", "write"): [200, 700],
            ("cg_vector_task", "b_blk", "read"): [40],
            ("cg_vector_task", "p_blk", "write"): [80, 500],
            ("cg_vector_task", "s_blk", "read"): [460, 960],
            ("cg_vector_task", "x_blk", "write"): [1000],
        },
        "free": {},
    }
    channels = {
        ("systolic_core_task", "c_blk"): "s_blk",
        ("systolic_core_task", "b_blk"): "p_blk",
    }
    spans = M.block_spans(ev, channels)
    assert [s["span"] for s in spans["vec.init"]] == [70]
    assert [(s["span"], s["regime"]) for s in spans["vec.iter"]] == [(300, "wait")]
    assert [(s["span"], s["regime"]) for s in spans["vec.last"]] == [(300, "wait")]
    assert (
        "mm.iter" in spans and spans["mm.iter"] == []
    )  # the systolic core is traced, with no B pulses here
    # without the map, S's producer is not found: no iteration span
    assert M.block_spans(ev)["vec.iter"] == []


def test_the_channel_map_follows_the_generated_top():
    c = HwConfig()
    det = M.block_channels("det", c)
    assert det[("systolic_core_task", "b_blk")] == "p_blk"
    assert det[("systolic_core_task", "c_blk")] == "s_blk"
    assert det[("cg_vector_task", "p_blk")] == "p_blk"
    assert det[("cg_load_task", "a_blk")] == det[("systolic_core_task", "a_blk")]
    unit = M.block_channels("mm", c)
    assert (
        unit[("systolic_load_task", "b_blk")] == unit[("systolic_core_task", "b_blk")]
    )
    assert (
        unit[("systolic_core_task", "c_blk")] == unit[("systolic_store_task", "c_blk")]
    )
    vec = M.block_channels("vec", _vec(4, 4, 12, 8))
    assert vec[("cg_vector_load_task", "s_blk")] == vec[("cg_vector_task", "s_blk")]


def test_lock_events_and_spans_of_the_committed_trace():
    ev = M.lock_events(FIXTURE)
    assert set(ev["pulses"]) == {
        ("systolic_load_task", "a_blk", "write"),
        ("systolic_load_task", "a_blk", "read"),
        ("systolic_load_task", "b_blk", "write"),
        ("systolic_core_task", "a_blk", "read"),
        ("systolic_core_task", "b_blk", "read"),
        ("systolic_core_task", "c_blk", "write"),
        ("systolic_store_task", "c_blk", "read"),
    }
    assert ev["pulses"][("systolic_core_task", "c_blk", "write")] == [
        352,
        763,
        1174,
        1585,
        1996,
        2407,
    ]
    # the store released C at 444: the channel is free one cycle later
    assert ev["free"]["unit_c"][:2] == [445, 856]
    spans = M.block_spans(ev, M.block_channels("mm", HwConfig()))
    assert list(spans) == ["mm.iter"]
    # each request reloads A, so the core is idle when its B arrives: every span waits
    assert [(s["span"], s["regime"], s["stalled"]) for s in spans["mm.iter"]] == [
        (220, "wait", False)
    ] * 6
    assert M.summarize_spans(spans)["mm.iter"]["span"] == 220


# --- the report tables and the build helpers ---------------------------------------------------

_RPT = """\
== Performance Estimates
    + Detail:
        * Instance:
        +-----+-----+
        |  a  |  b  |
        |  c  |  d  |
        +-----+-----+
== Utilization Estimates
+ Detail:
    * Instance:
    +----------------+---------------+---------+----+-----+-----+-----+
    |    Instance    |     Module    | BRAM_18K| DSP|  FF | LUT | URAM|
    +----------------+---------------+---------+----+-----+-----+-----+
    |core_U0         |core_s         |        0|  64| 4000| 4779|    0|
    |gmem0_m_axi_U   |gmem0_m_axi    |        4|   0|  725|  823|    0|
    +----------------+---------------+---------+----+-----+-----+-----+
    |Total           |               |        4|  64| 4725| 5602|    0|
    +----------------+---------------+---------+----+-----+-----+-----+

    * DSP:
    N/A

    * Memory:
    +---------+---------------------+---------+----+----+-----+------+-----+------+-------------+
    |  Memory |        Module       | BRAM_18K| FF | LUT| URAM| Words| Bits| Banks| W*Bits*Banks|
    +---------+---------------------+---------+----+----+-----+------+-----+------+-------------+
    |a_blk_U  |a_blk_RAM_AUTO_1R1W  |        0|  96|  12|    0|     4|   96|     2|          768|
    |p_blk_U  |p_blk_RAM_AUTO_1R1W  |        3|   0|   0|    0|    32|   96|     2|         6144|
    +---------+---------------------+---------+----+----+-----+------+-----+------+-------------+
    |Total    |                     |        3|  96|  12|    0|    36|  192|     4|         6912|
    +---------+---------------------+---------+----+----+-----+------+-----+------+-------------+

    * FIFO:
    +-----------+---------+----+----+-----+------+-----+---------+
    |    Name   | BRAM_18K| FF | LUT| URAM| Depth| Bits| Size:D*B|
    +-----------+---------+----+----+-----+------+-----+---------+
    |cmd_rd_U   |        0|  99|   0|    -|     2|   65|      130|
    +-----------+---------+----+----+-----+------+-----+---------+
    |Total      |        0|  99|   0|    0|     2|   65|      130|
    +-----------+---------+----+----+-----+------+-----+---------+

    * Expression:
    N/A
"""


def test_channel_rows_read_the_utilization_tables_only(tmp_path):
    (tmp_path / "unit_bench_csynth.rpt").write_text(_RPT, encoding="utf-8")
    rows = M.channel_rows(tmp_path, "unit_bench", {"core_s"})
    assert [(r["name"], r["kind"]) for r in rows] == [
        ("a_blk_U", "memory"),
        ("p_blk_U", "memory"),
        ("cmd_rd_U", "fifo"),
        ("gmem0_m_axi_U", "instance"),  # an instance that is not a module
    ]
    p_blk = rows[1]
    assert (p_blk["bram"], p_blk["words"], p_blk["bits"], p_blk["banks"]) == (
        3,
        32,
        96,
        2,
    )
    assert (rows[0]["ff"], rows[0]["lut"]) == (96, 12)
    assert (rows[3]["bram"], rows[3]["lut"]) == (4, 823)


def test_build_parameters_and_workload():
    c = HwConfig(
        K=8, L=8, R=4, C=8, cmul=3, W=10, g_s=4, mem_dw=32, sob_depth=3, cmd_depth=4
    )
    kw = M.gen_kwargs("det", c)
    assert kw == {
        "K": 8, "L": 8, "fmt": c.fmt, "mem_dw": 32, "cmd_depth": 4, "sob_depth": 3,
        "R": 4, "C": 8, "cmul": 3,
    }  # fmt: skip
    # a unit build sees no command queue, and the vector unit no array
    assert M.gen_kwargs("vec", c) == {
        "K": 8, "L": 8, "fmt": c.fmt, "mem_dw": 32, "sob_depth": 3
    }  # fmt: skip
    assert M.elab_params("det", c)[
        "mem_dwidth"
    ] == 32 and "mem_dw" not in M.elab_params("det", c)
    # a unit build elaborates the component's unit in its bench, with the study's registers
    mm = dict(M.elab_params("mm", c)["unit"])
    assert (mm["Mmax"], mm["Kmax"], mm["R"], mm["C"], mm["form"]) == (8, 8, 4, 8, 3)
    assert (mm["word_bits"], mm["L"], mm["sob_depth"], mm["lane_bits"]) == (
        32,
        8,
        3,
        16,
    )
    f = B.hw_format(c.fmt)
    assert (mm["a"], mm["b"], mm["c"]) == (f.A, f.P, f.S)
    vec = dict(M.elab_params("vec", c)["unit"])
    assert (vec["Kmax"], vec["nitmax"], vec["formats"]) == (8, 8, f)
    assert M.job_nits(16) == [1, 2, 3, 16, 1, 2]
    for top in ("vec", "mm", "det"):
        problems, jobs = M.workload(top, HwConfig(), "some_build")
        again, _ = M.workload(top, HwConfig(), "some_build")
        assert jobs == [1, 2, 3, 4, 1, 2]
        if top == "det":
            assert len(problems) == len(jobs)
            assert all(
                (a[0] == b[0]).all() and (a[1] == b[1]).all()
                for a, b in zip(problems, again, strict=True)
            )
            assert (problems[-1][1][:, 2] == 0).all()  # the zero-residual problem
        elif top == "vec":
            # one CG job per entry of the list, the last with a zero column
            assert [j.nit for j in problems] == jobs
            assert all(
                (a.b[0] == b.b[0]).all() for a, b in zip(problems, again, strict=True)
            )
            assert (problems[-1].b[0][:, 0] == 0).all() and (
                problems[-1].b[1][:, 0] == 0
            ).all()
        else:  # one matrix request per iteration of the list
            assert len(problems) == sum(jobs)
            assert all(
                (a.a[0] == b.a[0]).all() for a, b in zip(problems, again, strict=True)
            )


def test_cycle_budget_covers_the_measured_default_runs():
    """The budget is only an upper bound; these are the cycles the K = 4 default runs needed on the
    components (step 9.4a, scratch runs, Vivado xsim 2024.1): the last done of the detector, and the
    last reply of the two units."""
    jobs = M.job_nits(4)
    assert M.cycles_bound("det", HwConfig(), jobs) > 16_893
    assert M.cycles_bound("vec", _vec(4, 4, 12, 8), jobs) > 16_657
    assert M.cycles_bound("mm", _mm(8, 4, 8, 4, 12, 4), M.job_nits(8)) > 12_309
    slow = HwConfig(K=16, L=1, R=1, C=4, W=16, g_s=8, mem_dw=32)
    assert M.cycles_bound("det", slow, M.job_nits(16)) > M.cycles_bound(
        "det", HwConfig(), jobs
    )


# --- the harness end to end (Vitis HLS + Vivado xsim) ------------------------------------------


def _require_tools() -> None:
    if not toolchain.find_vitis_path() or not toolchain.find_vivado_path():
        pytest.skip("Vitis HLS and Vivado are both needed for the measurement harness")


def _module(rec: dict, cls: str) -> tuple:
    (m,) = [m for m in rec["resources"]["modules"] if m["cls"] == cls]
    return m["dsp"], m["lut"], m["ff"], m["bram"]


def _migrated(table: str, old: str) -> list[dict]:
    """The committed rows of the re-measured build ``mig_<old>`` (plan step 9.4b)."""
    from examples.mimo_cg.hw import campaign as C
    from examples.mimo_cg.mimo_cg import read_table

    return [
        r
        for r in read_table(C.PAPER_DATA / f"{table}.csv")
        if r["build"] == f"mig_{old}"
    ]


DET_K4 = "det_k4_l4_r4_c4_m4_w12g8_d64_s2_q2"
VEC_K4 = "vec_k4_l4_w12g8"


@pytest.fixture(scope="module")
def default_builds():
    """The K = 4 default builds on the components, measured by the harness: the detector, its
    vector core in the CG unit and its systolic core in the systolic unit."""
    _require_tools()
    builds = {
        "vec": _vec(4, 4, 12, 8),
        "mm": _mm(4, 4, 4, 4, 12, 4),
        "det": HwConfig(),
    }
    out = {}
    for top, c in builds.items():
        name = f"gate9_{top}_k4"
        out[top] = M.measure(name, top, c, B.BUILD_ROOT / name)
        assert "error" not in out[top], out[top].get("error")
    return out


@pytest.mark.xsi
def test_harness_reproduces_the_committed_rows_of_the_default_builds(default_builds):
    vec, mm, det = (default_builds[t] for t in ("vec", "mm", "det"))
    # the rows the campaign committed (step 9.4b) for the same configurations
    (row,) = [
        r for r in _migrated("migration_modules", VEC_K4) if r["name"] == "CgVectorCore"
    ]
    assert _module(vec, "CgVectorCore") == tuple(
        int(row[k]) for k in ("dsp", "lut", "ff", "bram")
    )
    (row,) = _migrated("migration_builds", DET_K4)
    total = det["resources"]["total"]
    assert tuple(total[k] for k in ("dsp", "lut", "ff", "bram")) == tuple(
        int(row[k]) for k in ("dsp", "lut", "ff", "bram")
    )
    # a core's row does not depend on the top it is synthesized in
    assert _module(det, "CgVectorCore") == _module(vec, "CgVectorCore")
    assert _module(det, "SystolicCore") == _module(mm, "SystolicCore")
    for rec in (vec, mm, det):
        res = rec["resources"]
        assert rec["tool"] == "vitis_hls 2024.1" and res["part"].startswith("xczu48dr")
        assert res["est_ns"] <= res["target_ns"] == 4.0
        # modules + integration = total, and the channel rows are the whole integration term
        for k in ("lut", "ff", "dsp", "bram"):
            assert (
                sum(m[k] for m in res["modules"]) + res["integration"][k]
                == res["total"][k]
            )
        for k in ("lut", "ff", "bram"):
            assert sum(ch[k] for ch in res["channels"]) == res["integration"][k]


@pytest.mark.xsi
def test_harness_reproduces_the_detector_job_intervals(default_builds):
    det = default_builds["det"]
    assert det["rtl"]["bit_exact"] and det["rtl"]["jobs"] == [1, 2, 3, 4, 1, 2]
    rows = _migrated("migration_cycles", DET_K4)
    want = {
        int(r["nit"]): int(r["cycles"]) for r in rows if r["quantity"] == "job_interval"
    }
    fit = det["intervals"]
    assert dict(zip(fit["nit"], fit["interval"], strict=True)) == want
    terms = {r["quantity"]: float(r["cycles"]) for r in rows}
    assert fit["t_iter"] == pytest.approx(terms["t_iter"])
    assert fit["t0"] == pytest.approx(terms["t0"])
    assert fit["max_resid"] < 1e-6


@pytest.mark.xsi
def test_a_blocks_span_is_the_same_in_its_unit_and_in_the_detector(default_builds):
    vec, mm, det = (default_builds[t]["spans"] for t in ("vec", "mm", "det"))
    for spans in (vec, mm, det):
        assert all(s["stalled"] == 0 and s["span"] is not None for s in spans.values())
    for kind in ("vec.init", "vec.iter", "vec.last"):
        assert abs(vec[kind]["span"] - det[kind]["span"]) <= 1, kind
    assert abs(mm["mm.iter"]["span"] - det["mm.iter"]["span"]) <= 1
    # in the detector the two cores wait for each other, so their spans tile the loop exactly
    assert det["mm.iter"]["wait"]["n"] == det["mm.iter"]["n"]
    assert det["mm.iter"]["wait"]["min"] == det["mm.iter"]["wait"]["max"]
    assert det["vec.iter"]["wait"]["min"] == det["vec.iter"]["wait"]["max"]
    t_iter = default_builds["det"]["intervals"]["t_iter"]
    assert det["mm.iter"]["span"] + det["vec.iter"]["span"] == pytest.approx(t_iter)


@pytest.mark.xsi
def test_a_memory_bound_unit_still_gives_the_blocks_own_span():
    """The systolic unit at K = 16, R = C = L = 16, W16g8: every request carries ``A`` and ``B`` and
    returns ``C``, so the time between replies is set by the memory traffic, yet the core's sweep
    is the same at every request and much shorter."""
    _require_tools()
    name = "gate9_mm_membound"
    rec = M.measure(name, "mm", _mm(16, 16, 16, 4, 16, 16), B.BUILD_ROOT / name)
    assert "error" not in rec, rec.get("error")
    assert rec["rtl"]["bit_exact"]
    assert _module(rec, "SystolicCore")[0] == 4 * 16 * 16 + 2  # and two index products
    span = rec["spans"]["mm.iter"]
    # the core is idle when each B arrives: every sample waits, none stalls, all are equal
    assert span["wait"]["n"] == span["n"] >= 2 and span["stalled"] == 0
    assert span["wait"]["min"] == span["wait"]["max"] == span["span"]
    gaps = [b - a for a, b in itertools.pairwise(rec["rtl"]["reply_cycles"])]
    assert span["span"] < 0.5 * min(gaps)


# --- the campaign driver -----------------------------------------------------------------------


def test_campaign_grid_roles_and_shards():
    from examples.mimo_cg.hw import campaign as C

    labels = list(C.split())
    # the split's 101, the 6 supplementary builds, the second round's 19 + 6 (step 6.1), the
    # brute-force sub-grid's 1,440 (step 6.3), and the 132 rebuilt on the components (step 9.4)
    roles = [role for _t, role, _c in C.split().values()]
    assert len(C.grid()) == len(labels) == 132 + 1440 + 132
    assert [
        b for b, r in zip(labels, roles, strict=True) if r == "supplement"
    ] == labels[101:107]
    assert [b for b, r in zip(labels, roles, strict=True) if r == "fit2"] == labels[
        107:126
    ]
    assert [
        b for b, r in zip(labels, roles, strict=True) if r == "supplement2"
    ] == labels[126:132]
    assert set(roles[132:1572]) == {C.BRUTEFORCE} and C.BRUTEFORCE not in roles[:132]
    assert all(b.startswith("bf_det_") for b in labels[132:1572])
    # the migration: the first 132 again, in their order, each under its new label
    assert set(roles[1572:]) == {C.MIGRATION}
    assert labels[1572:] == [f"mig_{b}" for b in labels[:132]]
    assert [C.split()[f"mig_{b}"][2] for b in labels[:132]] == [
        C.split()[b][2] for b in labels[:132]
    ]
    fit = [b for b in labels if C.split()[b][1] == "fit"]
    assert len(fit) == 67
    shards = [C.shard(fit, f"{i}/4") for i in range(4)]
    assert sorted(b for s in shards for b in s) == sorted(fit)  # a partition
    assert max(map(len, shards)) - min(map(len, shards)) <= 1
    with pytest.raises(ValueError, match="shard"):
        C.shard(fit, "4/4")


def test_campaign_dry_run_needs_no_toolchain(tmp_path, monkeypatch):
    """The pre-flight elaborates a build and budgets its run; any toolchain call would fail it."""
    import subprocess

    from examples.mimo_cg.hw import campaign as C

    def boom(*a, **k):
        raise AssertionError("the dry run must not start a tool")

    monkeypatch.setattr(toolchain, "run_vitis_hls", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(M, "POINTS_DIR", tmp_path)
    for build in (
        "vec_k8_l4_w12g8",
        "mm_k16_r16_c16_m4_w16_l4",
        "det_k4_l4_r4_c4_m4_w12g8_d64_s2_q2",
        "mig_mm_k16_r16_c16_m4_w16_l4",
        "mig_det_k4_l4_r4_c4_m4_w12g8_d64_s2_q2",
    ):
        out = C.DryPointStep(name="hw_dry").run(None, build=build)
        text = out["hw_dry"].read_text()
        assert text.startswith(build) and "n_cycles=" in text


def _fake_record(build: str, top: str, role: str, c) -> dict:
    mods = [
        {
            "cls": "CgVectorCore",
            "key": "k",
            "rtl_module": "cg_vector_task_s",
            "lut": 100,
            "ff": 50,
            "dsp": 12,
            "bram": 0,
            "uram": 0,
        }
    ]
    chans = [
        {
            "name": "p_blk_U",
            "kind": "memory",
            "bram": 3,
            "ff": 0,
            "lut": 5,
            "words": 32,
            "bits": 96,
            "banks": 2,
        }
    ]
    return {
        "build": build,
        "top": top,
        "role": role,
        "config": {k: getattr(c, k) for k in HwConfig.__dataclass_fields__},
        "tool": "vitis_hls 2024.1",
        "csynth_seconds": 30.0,
        "xsi_seconds": 12.0,
        "resources": {
            "modules": mods,
            "channels": chans,
            "integration": {"lut": 5, "ff": 0, "dsp": 0, "bram": 3, "uram": 0},
            "total": {"lut": 105, "ff": 50, "dsp": 12, "bram": 3, "uram": 0},
            "est_ns": 3.352,
        },
        "rtl": {"bit_exact": True},
        "intervals": {"first_done": 100, "nit": [2, 3], "interval": [210, 310], "t0": 10.0, "t_iter": 100.0, "max_resid": 0.0},
        "spans": {"vec.iter": {"span": 90, "n": 3, "stalled": 1, "wait": {"n": 2, "min": 90, "max": 90}, "b2b": {"n": 0, "min": None, "max": None}}},
    }  # fmt: skip


def test_merge_writes_the_three_tables_for_the_asked_roles_only(tmp_path):
    import json

    from examples.mimo_cg.hw import campaign as C
    from examples.mimo_cg.mimo_cg import read_table

    points = tmp_path / "points"
    points.mkdir()
    for build, (top, role, c) in C.split().items():
        if role == "fit":  # the held-out builds are not measured yet
            (points / f"{build}.json").write_text(
                json.dumps(_fake_record(build, top, role, c))
            )
    out = C.merge(("fit",), points_dir=points, out_dir=tmp_path)
    builds = read_table(out["hw_builds"])
    assert len(builds) == 67 and {r["role"] for r in builds} == {"fit"}
    assert (
        builds[0]["dsp"] == "12"
        and builds[0]["integ_bram"] == "3"
        and builds[0]["bit_exact"] == "1"
    )
    modules = read_table(out["hw_modules"])
    assert len(modules) == 2 * 67 and {r["kind"] for r in modules} == {
        "module",
        "memory",
    }
    cycles = read_table(out["hw_cycles"])
    assert len(cycles) == 67 * (2 + 4 + 1)
    span = next(r for r in cycles if r["quantity"] == "vec.iter")
    assert (span["cycles"], span["stalled"], span["wait_n"], span["b2b_min"]) == (
        "90",
        "1",
        "2",
        "",
    )
    first = out["hw_builds"].read_text().splitlines()[0]
    assert (
        "tool=vitis_hls 2024.1" in first
        and "roles=fit" in first
        and "xczu48dr" in first
    )
    # asking for the held-out builds before they are measured is an error, not a partial table
    with pytest.raises(FileNotFoundError, match="not measured yet"):
        C.merge(("fit", "holdout"), points_dir=points, out_dir=tmp_path)


# --- the brute-force harness (plan step 6.3) ---------------------------------------------------


def test_steady_job_list_issues_short_jobs_twice():
    from examples.mimo_cg.hw.space import NITS

    assert M.job_nits(4, steady=True) == [1, 1, 2, 2, 3, 3, 4, 4]
    assert M.job_nits(8, steady=True) == [1, 1, 2, 2, 3, 3, 4, 4, 6, 8]
    assert M.job_nits(16, steady=True) == [1, 1, 2, 2, 3, 3, 4, 4, 6, 8, 12, 16]
    for (
        K,
        nits,
    ) in NITS.items():  # every iteration count of the accuracy table, in rising order
        jobs = M.job_nits(K, steady=True)
        assert tuple(dict.fromkeys(jobs)) == nits and jobs == sorted(jobs)
    assert M.job_nits(8) == [1, 2, 3, 8, 1, 2]  # the calibration list is unchanged
    _problems, jobs = M.workload("det", HwConfig(K=8, R=8), "bf_x", steady=True)
    assert jobs == M.job_nits(8, steady=True) and len(_problems) == len(jobs)


def test_steady_intervals_take_the_last_job_of_each_count():
    """A stream of equal jobs settles after the first; the job time is the second one's interval.
    Here T0 = 100 and T_iter = 50, and the first job of each pair is 30 cycles late."""
    jobs = M.job_nits(8, steady=True)
    done, t, prev = [], 0, None
    for n in jobs:
        t += 100 + 50 * n + (30 if n != prev and n <= 4 else 0)
        done.append(t)
        prev = n
    fit = M.job_intervals(done, jobs, steady=True)
    assert fit["steady"] == {"1": 150, "2": 200, "3": 250, "4": 300, "6": 400, "8": 500}
    assert fit["t0"] == pytest.approx(100) and fit["t_iter"] == pytest.approx(50)
    assert fit["max_resid"] == pytest.approx(0, abs=1e-6)
    # every interval is still recorded, the late ones included
    assert (
        fit["nit"] == jobs[1:]
        and fit["interval"][1] == 230
        and fit["interval"][2] == 200
    )
    # without the flag the same completions are one fit over all of them
    plain = M.job_intervals(done, jobs)
    assert "steady" not in plain and plain["max_resid"] > 10


def test_model_budget_covers_every_measured_detector_run():
    """The brute force budgets a run from the predicted job time.  Against the committed runs of
    the 28 measured detectors it leaves at least 25% to spare, and it is at most half of the
    model-free bound.  (A budget that is too small costs a second run, never a wrong number.)
    """
    from examples.mimo_cg.hw import campaign as C
    from examples.mimo_cg.mimo_cg import read_table

    need: dict = {}
    for r in read_table(M.POINTS_DIR.parents[1] / "paper_data" / "hw_cycles.csv"):
        if r["top"] == "det" and r["quantity"] in ("first_done", "job_interval"):
            need[r["build"]] = need.get(r["build"], 0) + float(r["cycles"])
    assert len(need) == 28
    for build, last_done in need.items():
        _top, _role, c = C.split()[build]
        budget = M.cycles_budget(c, M.job_nits(c.K))
        assert budget >= 1.25 * last_done, (build, budget, last_done)
        assert budget <= 0.5 * M.cycles_bound("det", c, M.job_nits(c.K)), build
    # the steady list is longer, and so is its budget
    slow = HwConfig(K=16, L=1, R=1, C=4, W=16, g_s=8, mem_dw=32)
    assert M.cycles_budget(slow, M.job_nits(16, steady=True)) > M.cycles_budget(
        slow, M.job_nits(16)
    )


def test_prune_build_keeps_the_reports(tmp_path):
    sol = tmp_path / "cg_detector_proj" / "solution1"
    keep = [
        sol / "syn" / "report" / "cg_detector_csynth.rpt",
        tmp_path / "csynth.log",
        tmp_path / "gen" / "cg_detector.cpp",
        tmp_path / "xsi" / "vectors" / "s_done" / "cycles.bin",
    ]
    drop = [
        sol / ".autopilot" / "db" / "a.bc",
        sol / "impl" / "verilog" / "x.v",
        sol / "syn" / "verilog" / "cg_detector.v",
        tmp_path / "xsi" / "xsim.dir" / "work" / "lib.so",
        tmp_path / "xsi" / "cg_detector_bfm.wdb",
    ]
    for path in keep + drop:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    M.prune_build(tmp_path, "cg_detector")
    assert all(p.is_file() for p in keep) and not any(p.exists() for p in drop)
    M.prune_build(tmp_path, "cg_detector")  # a second pass has nothing to do


def test_campaign_keeps_the_brute_force_apart(tmp_path, monkeypatch):
    """Its builds run the steady list without a waveform and are pruned; its tables are its own."""
    import json

    from examples.mimo_cg.hw import campaign as C
    from examples.mimo_cg.mimo_cg import read_table

    brute = [(b, c) for b, (_t, role, c) in C.split().items() if role == C.BRUTEFORCE]
    assert len(brute) == 1440 and all(
        t == "det" for t, r, _c in C.split().values() if r == C.BRUTEFORCE
    )
    build, c = brute[0]

    calls = []
    monkeypatch.setattr(
        M, "measure", lambda *a, **kw: calls.append(kw) or {"build": a[0]}
    )
    monkeypatch.setattr(M, "POINTS_DIR", tmp_path)
    C.HwPointStep(name="hw_point").run(None, build=build)
    C.HwPointStep(name="hw_point").run(None, build="det_k4_l4_r4_c4_m4_w12g8_d64_s2_q2")
    assert calls[0] == {
        "role": "bruteforce",
        "steady": True,
        "trace": False,
        "prune": True,
    }
    assert calls[1] == {"role": "fit", "steady": False, "trace": True, "prune": False}
    # a migration build: traced, pruned, its records apart, seeded by its old build
    from examples.mimo_cg.hw import migration

    C.HwPointStep(name="hw_point").run(
        None, build="mig_det_k4_l4_r4_c4_m4_w12g8_d64_s2_q2"
    )
    assert calls.pop() == {
        "role": "migration",
        "trace": True,
        "prune": True,
        "points_dir": migration.POINTS,
        "workload_label": "det_k4_l4_r4_c4_m4_w12g8_d64_s2_q2",
    }
    assert C.records_dir((C.MIGRATION,)) == migration.POINTS
    assert C.records_dir(("fit",)) == M.POINTS_DIR
    with pytest.raises(ValueError, match="on its own"):
        C.merge(("fit", C.MIGRATION), points_dir=tmp_path, out_dir=tmp_path)
    text = C.DryPointStep(name="hw_dry").run(None, build=build)["hw_dry"].read_text()
    assert f"jobs={M.job_nits(c.K, steady=True)}" in text
    # a measured brute-force build is not built again; an incomplete or failed record is
    done = _fake_record(build, "det", C.BRUTEFORCE, c)
    (tmp_path / f"{build}.json").write_text(json.dumps(done))
    assert not C.measured(build, c)  # no steady-state job time in it
    done["intervals"] |= {"steady": {"2": 210}}
    (tmp_path / f"{build}.json").write_text(json.dumps(done))
    assert C.measured(build, c) and not C.measured(build, brute[1][1])
    C.HwPointStep(name="hw_point").run(None, build=build)
    assert len(calls) == 2  # no third measurement
    (tmp_path / f"{build}.json").write_text(
        json.dumps(done | {"error": "csynth failed"})
    )
    assert not C.measured(build, c)
    C.HwPointStep(name="hw_point").run(None, build=build)
    assert len(calls) == 3

    points = tmp_path / "points"
    points.mkdir()
    for b, cfg in brute:
        rec = _fake_record(b, "det", C.BRUTEFORCE, cfg)
        rec["intervals"] |= {"steady": {"2": 210, "3": 310}}
        del rec["spans"]
        (points / f"{b}.json").write_text(json.dumps(rec))
    out = C.merge((C.BRUTEFORCE,), points_dir=points, out_dir=tmp_path)
    assert sorted(out) == [
        "bruteforce_builds",
        "bruteforce_cycles",
        "bruteforce_modules",
    ]
    assert len(read_table(out["bruteforce_builds"])) == 1440
    modules = read_table(out["bruteforce_modules"])
    assert len(modules) == 1440 and {r["kind"] for r in modules} == {"module"}
    cycles = read_table(out["bruteforce_cycles"])
    times = [r for r in cycles if r["quantity"] == "job_time"]
    assert len(times) == 2 * 1440 and {(r["nit"], r["cycles"]) for r in times} == {
        ("2", "210"),
        ("3", "310"),
    }
    assert "roles=bruteforce" in out["bruteforce_builds"].read_text().splitlines()[0]
    assert not (tmp_path / "hw_builds.csv").exists()
    with pytest.raises(ValueError, match="on its own"):
        C.merge(("fit", C.BRUTEFORCE), points_dir=points, out_dir=tmp_path)
    # a merge of the brute force alone has no calibration build, and must not touch the
    # calibration work store (it once rebuilt it empty)
    store = tmp_path / "work" / M.PLATFORM / "modules"
    store.mkdir(parents=True)
    (store / "keep.txt").write_text("x")
    monkeypatch.setattr(M, "WORK_ROOT", tmp_path / "work")
    assert C.file_fit_records((C.BRUTEFORCE,), points_dir=points) == 0
    assert (store / "keep.txt").is_file()


@pytest.mark.xsi
def test_steady_run_measures_the_job_time_of_a_stream():
    """The brute-force mode on the K = 4 default detector, in real RTL: eight jobs, each count
    twice, no waveform, the build pruned.  The loop is the bottleneck here, so both jobs of a pair
    take the same time, and that time is the calibration run's: t0 + t_iter·nit of the committed
    re-measurement (step 9.4b)."""
    _require_tools()
    c, name = HwConfig(), "gate9_det_k4_steady"
    out_dir = B.BUILD_ROOT / name
    rec = M.measure(name, "det", c, out_dir, steady=True, trace=False, prune=True)
    assert "error" not in rec, rec.get("error")
    assert rec["rtl"]["bit_exact"] and rec["rtl"]["jobs"] == [1, 1, 2, 2, 3, 3, 4, 4]
    rows = _migrated("migration_cycles", DET_K4)
    want = {
        int(r["nit"]): int(r["cycles"]) for r in rows if r["quantity"] == "job_interval"
    }
    fit = rec["intervals"]
    assert fit["steady"] == {str(n): want[n] for n in (1, 2, 3, 4)}
    assert fit["interval"] == [want[n] for n in (1, 2, 2, 3, 3, 4, 4)]
    assert fit["max_resid"] == pytest.approx(0, abs=1e-6)
    assert "spans" not in rec  # no waveform, so no block spans
    (row,) = _migrated("migration_builds", DET_K4)
    total = rec["resources"]["total"]
    assert tuple(total[k] for k in ("dsp", "lut", "ff", "bram")) == tuple(
        int(row[k]) for k in ("dsp", "lut", "ff", "bram")
    )
    # pruned: the tool's working files are gone, the report is not, and it can be read again
    sol = out_dir / "cg_detector_proj" / "solution1"
    assert (
        not (sol / ".autopilot").exists()
        and not (out_dir / "xsi" / "xsim.dir").exists()
    )
    assert (sol / "syn" / "report" / "cg_detector_csynth.rpt").is_file()
    assert M.attribute("det", c, out_dir) == rec["resources"]


def test_which_records_a_build_files():
    """Gate 5.0 decision 3: a block is calibrated from its own unit builds, the glue from detectors
    (the blocks are the components' cores since gate 9.0)."""
    assert M.files_record("vec", "CgVectorCore")
    assert not M.files_record("vec", "CgVectorLoad")
    assert not M.files_record("vec", "MemRStream")
    assert not M.files_record("vec", "SystolicCore")
    assert M.files_record("mm", "SystolicCore")
    assert not M.files_record("mm", "SystolicStore")
    assert not M.files_record("det", "CgVectorCore")
    assert not M.files_record("det", "SystolicCore")
    for glue in ("CgCmdRx", "CgLoad", "CgCtrl", "CgStore", "MemRStream", "MemWStream"):
        assert M.files_record("det", glue)


@pytest.mark.xsi
def test_filing_follows_the_per_block_protocol(default_builds, tmp_path):
    """The store a model is fitted from: one core record per unit build; the glue and the
    integration record, but neither core, from a detector build."""
    import json

    configs = {
        "vec": _vec(4, 4, 12, 8),
        "mm": _mm(4, 4, 4, 4, 12, 4),
        "det": HwConfig(),
    }
    filed = {
        top: M.file_records(
            top,
            c,
            B.BUILD_ROOT / f"gate9_{top}_k4",
            tool=default_builds[top]["tool"],
            cost_seconds=1.0,
            work_root=tmp_path,
        )
        for top, c in configs.items()
    }
    assert filed == {"vec": 1, "mm": 1, "det": 7}
    recs = [
        json.loads(line)
        for f in tmp_path.rglob("records.jsonl")
        for line in f.read_text().splitlines()
    ]
    by_origin = {
        (r["target"], r["key"].split("-")[0], r["payload"].get("attributed_from"))
        for r in recs
    }
    assert ("resource", "cg_vector_core", B.CG_UNIT_TOP) in by_origin
    assert ("resource", "systolic_core", B.SYSTOLIC_UNIT_TOP) in by_origin
    assert not any(
        k in ("cg_vector_core", "systolic_core") and src == B.DET_TOP
        for _t, k, src in by_origin
    )
    assert sum(r["target"] == "integration" for r in recs) == 1
    assert all(r["provenance"]["tool"] == "vitis_hls 2024.1" for r in recs)


# --- the committed campaign tables (steps 5.3 and 5.7) -----------------------------------------


def _committed(name: str):
    from examples.mimo_cg.hw import campaign as C
    from examples.mimo_cg.mimo_cg import read_table

    path = C.PAPER_DATA / f"{name}.csv"
    header = path.read_text(encoding="utf-8").splitlines()[0]
    return header, read_table(path)


def test_committed_tables_hold_every_build_of_their_roles():
    from examples.mimo_cg.hw import campaign as C

    header, builds = _committed("hw_builds")
    assert "tool=vitis_hls 2024.1" in header and "part=xczu48dr-ffvg1517-2-e" in header
    roles = header.split("roles=")[1].split(",")[0].split("+")
    assert "fit" in roles and set(roles) <= set(C.ROLES) - {C.BRUTEFORCE}
    expected = [b for b, (_t, role, _c) in C.split().items() if role in roles]
    assert [r["build"] for r in builds] == expected
    for r in builds:
        top, role, c = C.split()[r["build"]]
        assert (r["top"], r["role"]) == (top, role)
        assert all(int(r[k]) == getattr(c, k) for k in C.KNOBS)
        assert (
            r["error"] == "" and r["bit_exact"] == "1"
        )  # every build ran and is bit-exact at RTL
        assert float(r["est_ns"]) <= 4.0


def test_committed_module_rows_add_up_to_the_build_totals():
    _, builds = _committed("hw_builds")
    _, modules = _committed("hw_modules")
    assert {r["build"] for r in modules} == {r["build"] for r in builds}
    for b in builds:
        mine = [r for r in modules if r["build"] == b["build"]]
        rows = [
            r for r in mine if r["kind"] != "subblock"
        ]  # a sub-block is part of its module
        for k in ("lut", "ff", "dsp", "bram"):
            assert sum(int(r[k]) for r in rows) == int(b[k]), (b["build"], k)
        integ = [r for r in rows if r["kind"] != "module"]
        for k in ("lut", "ff", "bram"):
            assert sum(int(r[k]) for r in integ) == int(b[f"integ_{k}"])
        for mod in (r for r in rows if r["kind"] == "module"):
            parts = [
                r
                for r in mine
                if r["kind"] == "subblock" and r["name"].startswith(mod["name"] + ".")
            ]
            for k in ("lut", "ff", "dsp", "bram"):
                assert sum(int(r[k]) for r in parts) <= int(mod[k]), (
                    b["build"],
                    mod["name"],
                    k,
                )


def test_committed_cycles_have_a_fit_and_a_clean_span_for_every_block():
    _, builds = _committed("hw_builds")
    _, cycles = _committed("hw_cycles")
    kinds = {"vec": {"vec.init", "vec.iter", "vec.last"}, "mm": {"mm.iter"}}
    kinds["det"] = kinds["vec"] | kinds["mm"]
    memory_bound: list[str] = []
    for b in builds:
        rows = {
            r["quantity"]: r
            for r in cycles
            if r["build"] == b["build"] and r["quantity"] != "job_interval"
        }
        assert {"first_done", "t0", "t_iter", "max_resid"} <= set(rows)
        assert kinds[b["top"]] <= set(rows)
        for kind in kinds[b["top"]]:
            assert rows[kind]["cycles"] != "", (
                b["build"],
                kind,
            )  # at least one clean sample
            assert int(rows[kind]["n"]) > int(rows[kind]["stalled"])
        if b["top"] == "det":
            # the blocks wait for each other, so their spans tile the loop exactly; the job
            # overhead is the vector unit's start span plus a few cycles
            t_iter, t0 = float(rows["t_iter"]["cycles"]), float(rows["t0"]["cycles"])
            loop = int(rows["mm.iter"]["cycles"]) + int(rows["vec.iter"]["cycles"])
            if float(rows["max_resid"]["cycles"]) >= 1e-3:
                # a memory-bound detector: its job intervals depend on the jobs around them, so
                # the fit of the done log is not the loop.  One build, of the supplementary set
                # (K = 16, 16 lanes, 32-bit words); the spans are still the loop's own time.
                memory_bound.append(b["build"])
                assert abs(loop - t_iter) / loop < 0.02
                continue
            assert loop == round(t_iter), b["build"]
            assert 0 <= t0 - int(rows["vec.init"]["cycles"]) <= 8, b["build"]
    assert memory_bound in ([], ["det_k16_l16_r8_c16_m3_w10g0_d32_s3_q2"])
