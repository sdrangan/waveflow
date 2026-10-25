"""report.py — what a vector unit's harness run measured: per-job cycles from the RTL run, and the
resources csynth estimated for the unit and each task.

    python -m waveflow.vecunit.report <build root>
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from waveflow.vecunit.build import TOP, WORK_DIR


def job_cycles(root: Path) -> list[int]:
    """The cycle each job's status left the unit at RTL (``m_status``, one word a job)."""
    d = Path(root) / WORK_DIR / TOP / "xsi_run" / "vectors" / "m_status"
    return [int(c) for c in np.fromfile(d / "cycles.bin", dtype="<u8")]


def output_cycles(root: Path, port: str = "s_out0") -> list[int]:
    d = Path(root) / WORK_DIR / TOP / "xsi_run" / "vectors" / port
    return [int(c) for c in np.fromfile(d / "cycles.bin", dtype="<u8")]


def _resources(xml: Path) -> dict:
    r = ET.parse(xml).getroot()
    res = r.find("AreaEstimates/Resources")
    if res is None:
        raise ValueError(f"{xml}: no AreaEstimates/Resources")
    out: dict[str, float] = {k: int(res.findtext(t) or 0) for k, t in (("lut", "LUT"), ("ff", "FF"), ("dsp", "DSP"),
                                                       ("bram", "BRAM_18K"))}
    out["clock_ns"] = float(r.findtext("PerformanceEstimates/SummaryOfTimingAnalysis/"
                                       "EstimatedClockPeriod") or 0)
    return out


def resources(root: Path) -> dict[str, dict]:
    """csynth's estimate for the top (``vecunit``) and each task, by role."""
    rep = Path(root) / "vecunit_proj" / "solution1" / "syn" / "report"
    out = {"unit": _resources(rep / "vecunit_csynth.xml")}
    for role in ("load", "exec", "store"):
        cands = sorted(p for p in rep.glob(f"vecunit_{role}_*_csynth.xml") if "Pipeline" not in p.name)
        if cands:
            out[role] = _resources(cands[0])
    return out


def pass_depth(root: Path) -> int | None:
    """The pass loop's pipeline depth, from csynth's log."""
    import re

    log = Path(root) / "logs" / "hls_run_tcl.log"
    m = re.findall(r"Final II = \d+, Depth = (\d+), loop 'PASS'", log.read_text(errors="replace"))
    return int(m[-1]) if m else None


#: A reader that takes a block within this many cycles of its commit was waiting for it.
WAITED = 16


def load_unit_trace(unit, root: Path):
    """The traced RTL run of *unit*'s harness at *root* (``build_dag(..., trace=True)``) bound by
    name (:func:`waveflow.utils.trace.load_trace`): the unit's manifest, resolved at the scope its
    tasks are found under in the waveform."""
    from vcdvcd import VCDVCD

    from waveflow.build.composite_gen import composite_top_spec
    from waveflow.utils.trace import load_trace

    vcd = Path(root) / WORK_DIR / TOP / f"{TOP}_trace.vcd"
    manifest = composite_top_spec(unit, width=int(unit.cfg.word_bits)).trace_manifest()
    done = manifest["tasks"][0]["signals"]["ap_done"]
    names = [n for n in VCDVCD(str(vcd), only_sigs=True).signals if n.endswith("." + done)]
    if len(names) != 1:
        raise LookupError(f"{vcd}: {len(names)} nets named ...{done}; cannot place the unit's scope")
    return load_trace(manifest, vcd, scope=names[0].rsplit(".", 1)[0])


def _role(inst: str) -> str:
    return next(r for r in ("load", "exec", "store") if inst.startswith(f"vecunit_{r}_"))


def trace_report(bt, root: Path | None = None) -> dict:
    """Which task binds, from a traced RTL run (``plans/vecunit_new.md`` Stage 9b; read the way
    ``docs/examples/systolic/timing.md`` reads the systolic engine's):

    * per task (``load``, ``exec``, ``store``): its firings -- one a job -- and their spans;
    * per queue: its blocks, how many of the producer's commits were **held** (it had filled the
      next block and waited for the consumer to free one), and the consumer's **slack** -- the
      cycles from a block's commit to the consumer's first access (under :data:`WAITED`: the
      consumer was waiting for it).  A run's first block is its start-up, where every consumer
      waits, and is not counted; nor is a negative slack -- the execute task's fused loop enables
      every queue's memory in every pass, so its first "access" can precede the commit, and its
      wait is then unknown;
    * with *root*, each input port's accepted words (the BFMs' own record, ``vectors/<port>_acc``):
      how many, over how many cycles;
    * ``binds``: the task that waits on neither side -- none of its commits held, and its input
      blocks taken with slack to spare (the loader: its input port busy instead)."""
    tasks = {_role(t["inst"]): t["inst"] for t in bt.manifest["tasks"]}
    out: dict = {"tasks": {}, "queues": {}, "ports": {}}
    for role, inst in tasks.items():
        f = bt.component_firings(inst)
        out["tasks"][role] = {"firings": len(f), "spans": [x.span for x in f],
                              "first": f[0].start if f else None, "last": f[-1].end if f else None}
    for ch in bt.manifest["channels"]:
        if ch["kind"] != "sob":
            continue
        w, r = bt.block_events(ch["id"], "write"), bt.block_events(ch["id"], "read")
        slack = [ra - wr for (_wa, wr, _h), (ra, _rr, _rh) in zip(w, r)]
        out["queues"][ch["id"]] = {
            "producer": _role(ch["producer"]), "consumer": _role(ch["consumer"]), "blocks": len(w),
            "held": sum(h for _a, _r, h in w), "slack": slack,
            "waited": sum(0 <= x < WAITED for x in slack[1:])}
    if root is not None:
        vec = Path(root) / WORK_DIR / TOP / "xsi_run" / "vectors"
        for d in sorted(vec.glob("s_in*_acc")):
            c = np.fromfile(d / "cycles.bin", dtype="<u8").astype(np.int64)
            if c.size:
                out["ports"][d.name[:-4]] = {"words": int(c.size), "first": int(c[0]),
                                             "last": int(c[-1]),
                                             "busy": float(c.size / (c[-1] - c[0] + 1))}
    q = out["queues"].values()

    def free(role: str) -> bool:
        held = sum(x["held"] for x in q if x["producer"] == role)
        waited = sum(x["waited"] for x in q if x["consumer"] == role)
        return held == 0 and waited == 0

    candidates = [r for r in ("exec", "store") if free(r)]
    load_busy = max((p["busy"] for p in out["ports"].values()), default=0.0)
    if not any(x["held"] for x in q if x["producer"] == "load") and load_busy > 0.9:
        candidates.append("load")
    out["binds"] = candidates[0] if len(candidates) == 1 else candidates or None
    return out


def job_bounds(cfg, progs) -> dict:
    """What bounds one job (``plans/vecunit_new.md`` Stage 9c), in cycles, each side at its own pace:

    * ``input``: the words its load program reads, at a word a cycle; ``load``: the loader's own time
      -- a word a cycle, or a value a cycle where it takes the slow path (``L`` does not divide the
      block's columns, or a lane group is not whole words), plus its constant a block;
    * ``output``: the words its store program writes, ``VecResp`` headers included; ``store``: the
      store's own time, by the same rule;
    * ``elements``: the execute datapath's floor -- a run element a cycle (passes times ``VL``) and
      two cycles a scalar instruction;
    * ``execute``: the execute task's calibrated time (:class:`~waveflow.vecunit.unit.VecTiming`
      over :func:`~waveflow.vecunit.unit.job_features`);
    * ``binds``: whichever of ``load``, ``execute`` and ``store`` is longest -- with a queue of two
      blocks between them, consecutive jobs overlap and the slowest task sets the interval."""
    from waveflow.linalg.lanes import nwords
    from waveflow.vecunit import isa as I
    from waveflow.vecunit.unit import _slow_path, _VecPart, job_features

    t = _VecPart.timing
    lb, wb = cfg.lane_bits, cfg.word_bits

    def side(instrs, header: int) -> tuple[int, float]:
        words, time = 0, 0.0
        for x in instrs:
            k, n = int(x.imm1), int(x.imm2)
            w = nwords(k * n, lb, wb)
            words += header + w
            time += header + (k * n if _slow_path(cfg, n) else w)
        return words, time

    inp, load = side(progs.load, 0)
    load += t.load * len(progs.load)
    out, store = side(progs.store, I.VecResp.nwords_per_inst(wb))
    f = job_features(progs.execute, cfg)
    b = {"input": inp, "load": load, "output": out, "store": store,
         "elements": f["elem"] + 2 * f["sop"], "execute": t.predict(f)}
    b["binds"] = max(("load", "execute", "store"), key=lambda k: b[k])
    return b


#: The execute task's loops a :class:`~waveflow.vecunit.unit.Schedule` holds, by their labels.
SCHEDULE_LOOPS = {"rung": "RUNG", "run": "RUN", "deliver": "DELIVER", "sgather": "SGATHER",
                  "srun": "SRUN"}


def loop_schedule(root: Path) -> dict:
    """The execute task's loop schedule in the build at *root*, from csynth's log: each loop's
    ``(II, depth)`` (Stage 9d).  The labels are the execute task's own; no other task of the unit
    uses them."""
    import re

    log = (Path(root) / "logs" / "hls_run_tcl.log").read_text(errors="replace")
    found = {name: (int(ii), int(d)) for ii, d, name in
             re.findall(r"Final II = (\d+), Depth = (\d+), loop '(\w+)'", log)}
    missing = [lab for lab in SCHEDULE_LOOPS.values() if lab not in found]
    if missing:
        raise ValueError(f"{root}: csynth's log schedules no loop {missing}")
    return {k: list(found[lab]) for k, lab in SCHEDULE_LOOPS.items()}


def main(argv=None) -> int:
    root = Path((argv or sys.argv[1:])[0])
    jobs = job_cycles(root)
    print(json.dumps({"job_status_cycles": jobs, "job_intervals": np.diff([0, *jobs]).tolist(),
                      "pass_depth": pass_depth(root), "resources": resources(root)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
