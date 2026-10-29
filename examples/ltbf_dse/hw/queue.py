"""queue.py -- the DSE's build queue (plan step 5.4): every pre-registered part, detached.

``plans/ltbf_dse/ltbf_dse.md`` step 5.4, AC11.  The parts of :mod:`~examples.ltbf_dse.hw.space`'s
three sets, in order -- fit, held-out, brute force -- each in its own root ``hw/build/<name>/`` (the
example's ``src/`` copied in): codegen of its top at its parameters, csynth, and, for a 64-bit fit or
held-out part, its device gate through the system flow (``devsys``: RTL against pysim on its job set,
the XSI run timing each job).  Harvested into ``hw/reports/<name>.json`` (local): csynth's resources
and estimated clock; the gate's comparison, both runs' ends, and the C++ host's log of when each job
went in and each ``Resp`` came out.  Then the root is removed.  A part with a report is skipped, so
the queue resumes.  At most ``--workers`` parts at once (4), 2 while one at N_rx >= 256 runs, each in
its own process group under a 10 GB watchdog and a 2 h cap.

    setsid nohup python -m examples.ltbf_dse.hw.queue run > examples/ltbf_dse/logs/queue.log 2>&1 &
    python -m examples.ltbf_dse.hw.queue status
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from examples.ltbf_dse.hw import space as sp

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = HERE / "build"
REPORTS = HERE / "reports"
RSS_CAP_KB = 10 * 1024 * 1024
TIME_CAP_S = 2 * 3600
TOP = {"sys": "ltbf_sys", "vec": "ltbf_vec", "ing": "ltbf_ingest"}


def params(p: sp.Part):
    """The part's ``SysParams`` (the rest at AC9's)."""
    from examples.ltbf_dse.hw import system as sy

    kw: dict[str, Any] = {"n": math.isqrt(p.n_rx), "c": p.c, "word_bits": p.word_bits}
    if p.kind == "sys":
        kw.update(Mmax=p.Mmax, R=p.R, C=p.C, W=p.W)
    elif p.kind == "vec":
        kw.update(L=p.L, W=p.W)
    else:
        kw.update(J=p.J)
    return sy.SysParams(**kw)


def gated(p: sp.Part, role: str) -> bool:
    """Whether the part's device gate runs: a 64-bit fit or held-out part (the gates are 64-bit)."""
    return role in ("fit", "heldout") and p.word_bits == 64


def _codegen_step(p: sp.Part):
    from examples.ltbf_dse.hw import build as hb
    from waveflow.build.build import BuildConfig, BuildStep
    from waveflow.build.composite_gen import GEN_DIR, INCLUDE_DIR

    @dataclass(kw_only=True)
    class PartCodegen(BuildStep):
        """Headers, the part's top and the one-source Resp hub (its gate's), at its parameters."""

        description = "Headers and the part's tops at its parameters."
        params: ClassVar[dict] = {}
        produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}

        def is_fresh(self, config, paths) -> bool:
            return False

        def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
            from examples.ltbf_dse.hw import system as sy
            from examples.ltbf_dse.hw.devsys import RespHub1
            from examples.ltbf_dse.hw.ingest import IngestDev
            from examples.ltbf_dse.hw.sys_wrap import SysWrap
            from examples.ltbf_dse.hw.vec_wrap import VecWrap
            from waveflow.simulation.simulation import Simulation

            r = Path(config.root_dir)
            shutil.copytree(ROOT / "src", r / "src", dirs_exist_ok=True)
            S = sy.LtbfSystem(sy.inputs(params(p)))
            sim, W = Simulation(), S.p.word_bits
            devs = {"ltbf_sys": SysWrap(name="ltbf_sys", sim=sim, engine_cfg=sy.engine_cfg(S)),
                    "ltbf_vec": VecWrap(name="ltbf_vec", sim=sim, vu_cfg=S.vu, word_bits=W),
                    "ltbf_ingest": IngestDev(name="ltbf_ingest", sim=sim, dma_cfg=sy.ingest_cfg(S)),
                    "ltbf_hub1": RespHub1(name="ltbf_hub1", sim=sim, word_bits=W),
                    "_vu": S.vu, "_word_bits": W}  # fmt: skip
            hb.gen_headers(r, devs)
            tcl = {"sys": {"solution_config": hb.FLP}, "vec": {"defines": hb.DEFINES}, "ing": {}}[p.kind]
            hb.gen_top(r, devs[TOP[p.kind]], W, **tcl)
            hb.gen_top(r, devs["ltbf_hub1"], W)
            return {"include": r / INCLUDE_DIR, "gen": r / GEN_DIR}

    return PartCodegen(name="codegen")


def build_part(p: sp.Part, role: str, root: Path) -> dict:
    """Build *p* in *root* and return its report (see the module docstring)."""
    from examples.ltbf_dse.hw import devsys
    from waveflow.build.build import BuildDag
    from waveflow.build.system_dag import CsynthStep, add_system_steps
    from waveflow.utils.csynthparse import CsynthParser, synth_target
    from waveflow.vecunit.build import build_config

    t0 = time.time()
    cfg = build_config(root)
    cfg.params["synth"] = "build"
    dag = BuildDag()
    dag.add(_codegen_step(p))
    top = TOP[p.kind]
    gate = gated(p, role)
    if gate:
        sysm = {"sys": lambda: devsys.sys_system(params(p)), "vec": lambda: devsys.vec_system(params(p)),
                "ing": lambda: devsys.ingest_system(p=params(p))}[p.kind]()  # fmt: skip
        add_system_steps(dag, sysm, work_dir="xsi_work", top=f"{top}_top", prefix="g_", workspace="gate")
        res = dag.run(cfg, through="g_compare")
    else:
        dag.add(CsynthStep(top=top))
        res = dag.run(cfg, through=f"csynth_{top}")
    rep: dict[str, Any] = {"part": p.as_dict(), "name": p.name, "role": role, "tool": "Vitis HLS / Vivado 2024.1",
                           "steps": {k: bool(r.success) for k, r in res.items()},
                           "errors": {k: (r.message or "")[-1500:] for k, r in res.items() if not r.success}}  # fmt: skip
    rdir = root / f"{top}_proj" / "solution1" / "syn" / "report"
    if (rdir / "csynth.xml").is_file():
        cp = CsynthParser(report_path=str(rdir))
        cp.get_total_resources()
        rep["resources"], rep["available"] = cp.total_resources, cp.available_resources
        rep["target"] = synth_target(rdir)
    if gate:
        work = root / "xsi_work" / "gate"
        for f in ("compare", "pysim"):
            if (work / f"{f}.json").is_file():
                rep[f] = json.loads((work / f"{f}.json").read_text())
        if (work / "report.json").is_file():
            out = json.loads((work / "report.json").read_text()).get("output", "")
            m = re.search(r"DONE done=(\d) cycles=(\d+)", out)
            rep["rtl_done"], rep["rtl_cycles"] = (int(m.group(1)), int(m.group(2))) if m else (0, None)
            rep["ops"] = [(k, int(a, 16), int(n), int(s), int(e)) for k, a, n, s, e in
                          re.findall(r"OP (\w) 0x([0-9a-f]+) n=(\d+) s=(\d+) e=(\d+)", out)]  # fmt: skip
    rep["minutes"] = round((time.time() - t0) / 60, 2)
    return rep


# -- the queue ----------------------------------------------------------------------------------------


def plan() -> list[tuple[str, sp.Part]]:
    """``(role, part)`` in queue order, each part once (its first set's role)."""
    seen, out = set(), []
    for role in ("fit", "heldout", "brute"):
        for p in sp.load(role):
            if p not in seen:
                seen.add(p)
                out.append((role, p))
    return out


def _pgroup_rss_kb(pgid: int) -> int:
    total = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            if int(fields[2]) != pgid:  # the process group, after the state and the parent
                continue
            with open(f"/proc/{pid}/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return total


def run(workers: int = 4) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    todo = [(r, p) for r, p in plan() if not (REPORTS / f"{p.name}.json").is_file()]
    print(f"{time.strftime('%T')} {len(todo)} parts to build", flush=True)
    running: dict[int, tuple] = {}  # pgid -> (popen, part, t0)
    while todo or running:
        big = any(p.n_rx >= 256 for _po, p, _t in running.values())
        cap = 2 if big else workers
        while todo and len(running) < cap and not (todo[0][1].n_rx >= 256 and len(running) >= 2):
            role, p = todo.pop(0)
            po = subprocess.Popen([sys.executable, "-m", "examples.ltbf_dse.hw.queue", "one", role, p.name],
                                  cwd=ROOT.parents[1], start_new_session=True)  # fmt: skip
            running[po.pid] = (po, p, time.time())
            print(f"{time.strftime('%T')} start {p.name} ({role})", flush=True)
            big = big or p.n_rx >= 256
            cap = 2 if big else workers
        time.sleep(10)
        for pgid, (po, p, t0) in list(running.items()):
            if po.poll() is not None:
                ok = (REPORTS / f"{p.name}.json").is_file()
                print(f"{time.strftime('%T')} {'done' if ok else 'FAILED'} {p.name} in "
                      f"{(time.time() - t0) / 60:.1f} min", flush=True)
                del running[pgid]
                continue
            rss = _pgroup_rss_kb(pgid)
            if rss > RSS_CAP_KB or time.time() - t0 > TIME_CAP_S:
                os.killpg(pgid, 15)
                why = f"rss {rss // 1024} MB" if rss > RSS_CAP_KB else "over 2 h"
                (REPORTS / f"{p.name}.json").write_text(json.dumps(
                    {"part": p.as_dict(), "name": p.name, "stopped": why}, indent=1))
                print(f"{time.strftime('%T')} STOPPED {p.name}: {why}", flush=True)
                del running[pgid]
    print(f"{time.strftime('%T')} queue empty", flush=True)


def one(role: str, name: str) -> None:
    p = next(q for r, q in plan() if q.name == name)
    root = BUILD / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    try:
        rep = build_part(p, role, root)
    except Exception as e:  # noqa: BLE001 -- the report says what went wrong
        rep = {"part": p.as_dict(), "name": name, "role": role, "error": f"{type(e).__name__}: {e}"[-1500:]}
    (REPORTS / f"{name}.json").write_text(json.dumps(rep, indent=1))
    shutil.rmtree(root, ignore_errors=True)


def status() -> None:
    done = {f.stem: json.loads(f.read_text()) for f in REPORTS.glob("*.json")} if REPORTS.is_dir() else {}
    by: dict[str, list] = {}
    for role, p in plan():
        r = done.get(p.name)
        state = ("todo" if r is None else "stopped" if "stopped" in r else "error" if "error" in r
                 else "ok" if all(r.get("steps", {}).values()) else "failed")  # fmt: skip
        by.setdefault(role, []).append(state)
    for role, states in by.items():
        print(role, {s: states.count(s) for s in sorted(set(states))})


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        run(int(sys.argv[2]) if len(sys.argv) > 2 else 4)
    elif cmd == "one":
        one(sys.argv[2], sys.argv[3])
    else:
        status()
