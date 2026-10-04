"""Step 5.2 of plans/mimo_cg/mimo_cg_paper_sims.md: the measurement harness.

Without markers: the extraction is checked on synthetic events, on a committed excerpt of a real
trace (``tests/fixtures/mimo_cg/cg_mm_unit_k4_locks.vcd``: the clock and the stream-of-blocks lock
nets of the K = 4 matmul unit, 1,700 cycles) and on an excerpt of a real report.

Under ``-m xsi`` (needs Vitis HLS and Vivado xsim): the harness takes the three Phase 4 default builds
at K = 4 and a memory-bound matmul unit through csynth and a traced RTL run, and must reproduce the
recorded rows, the detector's job intervals, and block spans that agree between unit and detector.
"""

from __future__ import annotations

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
    / "cg_mm_unit_k4_locks.vcd"
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
            ("cg_mm_load_task", "p_blk", "write"): list(p_avail),
            ("cg_mm_task", "p_blk", "read"): list(mm_reads),
            ("cg_mm_task", "s_blk", "write"): list(mm_writes),
        },
        "free": {"s_blk": list(s_free)},
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
    # one job of two iterations: B → P0, S1 → P1, S2 → X
    ev = {
        "pulses": {
            ("cg_load_task", "b_blk", "write"): [10],
            ("cg_mm_task", "s_blk", "write"): [200, 700],
            ("cg_vec_task", "b_blk", "read"): [40],
            ("cg_vec_task", "p_blk", "write"): [80, 500],
            ("cg_vec_task", "s_blk", "read"): [460, 960],
            ("cg_vec_task", "x_blk", "write"): [1000],
        },
        "free": {},
    }
    spans = M.block_spans(ev)
    assert [s["span"] for s in spans["vec.init"]] == [70]
    assert [(s["span"], s["regime"]) for s in spans["vec.iter"]] == [(300, "wait")]
    assert [(s["span"], s["regime"]) for s in spans["vec.last"]] == [(300, "wait")]
    assert (
        "mm.iter" in spans and spans["mm.iter"] == []
    )  # the matmul is traced, with no P pulses here


def test_lock_events_and_spans_of_the_committed_trace():
    ev = M.lock_events(FIXTURE)
    assert set(ev["pulses"]) == {
        ("cg_mm_load_task", "a_blk", "write"),
        ("cg_mm_load_task", "p_blk", "write"),
        ("cg_mm_task", "a_blk", "read"),
        ("cg_mm_task", "p_blk", "read"),
        ("cg_mm_task", "s_blk", "write"),
        ("cg_mm_store_task", "s_blk", "read"),
    }
    assert ev["pulses"][("cg_mm_task", "s_blk", "write")] == [
        337,
        553,
        761,
        977,
        1185,
        1393,
        1609,
    ]
    assert ev["free"]["s_blk"][:2] == [
        409,
        625,
    ]  # the store released S: free one cycle later
    spans = M.block_spans(ev)
    assert list(spans) == ["mm.iter"]
    assert [(s["span"], s["regime"]) for s in spans["mm.iter"]] == [
        (208, "wait"),
        (209, "b2b"),
        (208, "b2b"),
        (209, "b2b"),
        (208, "b2b"),
        (208, "b2b"),
        (209, "b2b"),
    ]
    assert M.summarize_spans(spans)["mm.iter"]["span"] == 208


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
    |cg_mm_task_U0   |cg_mm_task_s   |        0|  64| 4000| 4779|    0|
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
    (tmp_path / "cg_mm_unit_csynth.rpt").write_text(_RPT, encoding="utf-8")
    rows = M.channel_rows(tmp_path, "cg_mm_unit", {"cg_mm_task_s"})
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
    assert "R" not in M.gen_kwargs("vec", c)
    assert M.elab_params("det", c)[
        "mem_dwidth"
    ] == 32 and "mem_dw" not in M.elab_params("det", c)
    assert M.job_nits(16) == [1, 2, 3, 16, 1, 2]
    for top in ("vec", "mm", "det"):
        problems, jobs = M.workload(top, HwConfig(), "some_build")
        again, _ = M.workload(top, HwConfig(), "some_build")
        assert len(problems) == len(jobs) == 6
        assert all(
            (a[0] == b[0]).all() and (a[1] == b[1]).all()
            for a, b in zip(problems, again, strict=True)
        )
        assert (
            problems[-1][1][:, 2] == 0
        ).all()  # the zero-residual problem: column 2 of B is zero


def test_cycle_budget_covers_the_measured_default_runs():
    """The budget is only an upper bound; these are the cycles the K = 4 default runs needed."""
    jobs = M.job_nits(4)
    assert (
        M.cycles_bound("det", HwConfig(), jobs)
        > 1513 + 2461 + 3654 + 4847 + 1268 + 2461
    )
    assert M.cycles_bound("vec", _vec(4, 4, 12, 8), jobs) > 14_000
    assert M.cycles_bound("mm", _mm(4, 4, 4, 4, 12, 4), jobs) > 3_000
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


@pytest.fixture(scope="module")
def default_builds():
    """The three Phase 4 default builds at K = 4, measured by the harness."""
    _require_tools()
    builds = {
        "vec": _vec(4, 4, 12, 8),
        "mm": _mm(4, 4, 4, 4, 12, 4),
        "det": HwConfig(),
    }
    out = {}
    for top, c in builds.items():
        name = f"gate5_{top}_k4"
        out[top] = M.measure(name, top, c, B.BUILD_ROOT / name)
        assert "error" not in out[top], out[top].get("error")
    return out


@pytest.mark.xsi
def test_harness_reproduces_the_phase_4_rows(default_builds):
    vec, mm, det = (default_builds[t] for t in ("vec", "mm", "det"))
    assert _module(vec, "CgVec") == (48, 14018, 9000, 0)
    assert _module(mm, "CgMm") == (64, 4779, 4000, 0)
    total = det["resources"]["total"]
    assert (total["dsp"], total["lut"], total["ff"], total["bram"]) == (
        112,
        32991,
        19791,
        20,
    )
    # a block's row does not depend on the top it is synthesized in
    assert _module(det, "CgVec") == _module(vec, "CgVec")
    assert _module(det, "CgMm") == _module(mm, "CgMm")
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
        # the channel rows account for the whole remainder; the only part the report does not
        # itemize is the FIFOs' LUTs (its FIFO table lists none, its summary does)
        for k in ("lut", "ff", "bram"):
            assert sum(ch[k] for ch in res["channels"]) == res["integration"][k]
        (rest,) = [ch for ch in res["channels"] if ch["kind"] == "unitemized"]
        assert rest["lut"] > 0 and rest["ff"] == rest["bram"] == rest["dsp"] == 0
        assert all(ch["lut"] == 0 for ch in res["channels"] if ch["kind"] == "fifo")


@pytest.mark.xsi
def test_harness_reproduces_the_detector_job_intervals(default_builds):
    det = default_builds["det"]
    assert det["rtl"]["bit_exact"] and det["rtl"]["jobs"] == [1, 2, 3, 4, 1, 2]
    fit = det["intervals"]
    assert dict(zip(fit["nit"], fit["interval"], strict=True)) == {
        1: 1268,
        2: 2461,
        3: 3654,
        4: 4847,
    }
    assert fit["t_iter"] == pytest.approx(1193) and fit["t0"] == pytest.approx(75)
    assert fit["max_resid"] < 1e-6


@pytest.mark.xsi
def test_a_blocks_span_is_the_same_in_its_unit_and_in_the_detector(default_builds):
    vec, mm, det = (default_builds[t]["spans"] for t in ("vec", "mm", "det"))
    for spans in (vec, mm, det):
        assert all(s["stalled"] == 0 and s["span"] is not None for s in spans.values())
    assert abs(vec["vec.iter"]["span"] - det["vec.iter"]["span"]) <= 1
    assert abs(vec["vec.last"]["span"] - det["vec.last"]["span"]) <= 1
    assert abs(vec["vec.init"]["span"] - det["vec.init"]["span"]) <= 1
    assert abs(mm["mm.iter"]["span"] - det["mm.iter"]["span"]) <= 1
    # in the detector the two blocks wait for each other, so their spans tile the loop exactly
    assert det["mm.iter"]["wait"]["n"] == det["mm.iter"]["n"]
    assert det["mm.iter"]["wait"]["min"] == det["mm.iter"]["wait"]["max"]
    assert det["vec.iter"]["wait"]["min"] == det["vec.iter"]["wait"]["max"]
    t_iter = default_builds["det"]["intervals"]["t_iter"]
    assert det["mm.iter"]["span"] + det["vec.iter"]["span"] == pytest.approx(t_iter)


@pytest.mark.xsi
def test_a_memory_bound_unit_still_gives_the_blocks_own_span():
    """The matmul unit at K = 16, R = C = L = 16, W16g8: its job interval is set by memory traffic
    (it is not even linear in nit), yet the block's span is the same at every iteration and shorter.
    """
    _require_tools()
    name = "gate5_mm_membound"
    rec = M.measure(name, "mm", _mm(16, 16, 16, 4, 16, 16), B.BUILD_ROOT / name)
    assert "error" not in rec, rec.get("error")
    assert rec["rtl"]["bit_exact"]
    assert _module(rec, "CgMm")[0] == 4 * 16 * 16
    span = rec["spans"]["mm.iter"]
    # the store is slower than the block, so most hand-overs wait for the channel: those are
    # marked stalled, and every clean sample gives the same span
    assert span["stalled"] > 0 and span["wait"]["n"] >= 2 and span["b2b"]["n"] == 0
    assert span["wait"]["min"] == span["wait"]["max"] == span["span"]
    assert span["span"] < rec["intervals"]["t_iter"]
    assert (
        rec["intervals"]["max_resid"] > 10
    )  # the job intervals do not follow T0 + nit·T_iter


# --- the campaign driver -----------------------------------------------------------------------


def test_campaign_grid_roles_and_shards():
    from examples.mimo_cg.hw import campaign as C

    labels = list(C.split())
    assert len(C.grid()) == len(labels) == 101
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
    ):
        out = C.DryPointStep(name="hw_dry").run(None, build=build)
        text = out["hw_dry"].read_text()
        assert text.startswith(build) and "n_cycles=" in text


def _fake_record(build: str, top: str, role: str, c) -> dict:
    mods = [
        {
            "cls": "CgVec",
            "key": "k",
            "rtl_module": "cg_vec_task_s",
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


def test_which_records_a_build_files():
    """Gate 5.0 decision 3: a block is calibrated from its own unit builds, the glue from detectors."""
    assert M.files_record("vec", "CgVec") and not M.files_record("vec", "CgVecLoad")
    assert not M.files_record("vec", "MemRStream") and not M.files_record("vec", "CgMm")
    assert M.files_record("mm", "CgMm") and not M.files_record("mm", "CgMmStore")
    assert not M.files_record("det", "CgVec") and not M.files_record("det", "CgMm")
    for glue in ("CgCmdRx", "CgLoad", "CgCtrl", "CgStore", "MemRStream", "MemWStream"):
        assert M.files_record("det", glue)


@pytest.mark.xsi
def test_filing_follows_the_per_block_protocol(default_builds, tmp_path):
    """The store the models are fitted from: one block record per unit build; the glue and the
    integration record, but neither block, from a detector build."""
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
            B.BUILD_ROOT / f"gate5_{top}_k4",
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
    assert ("resource", "cg_vec", "cg_vec_unit") in by_origin
    assert ("resource", "cg_mm", "cg_mm_unit") in by_origin
    assert not any(
        k in ("cg_vec", "cg_mm") and src == "cg_detector" for _t, k, src in by_origin
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
    assert roles in (["fit"], ["fit", "holdout"])
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
            assert float(rows["max_resid"]["cycles"]) < 1e-3
            loop = int(rows["mm.iter"]["cycles"]) + int(rows["vec.iter"]["cycles"])
            assert loop == round(t_iter), b["build"]
            assert 0 <= t0 - int(rows["vec.init"]["cycles"]) <= 8, b["build"]
