"""Compile a trial's ``gain_stats_body.cpp`` as plain C++ and compare it with the golden model.

    python evaluation/skill_tests/kernel_hls/check_body.py <trial folder>

For the reviewer of ``01_gain_stats``: not a pattern score (that is the review sheet), only whether
the body compiles (Vivado's MinGW g++ and the Vitis headers, no Vitis run) and writes exactly the
golden's words, TLAST included, at both widths, over several commands (so ``total`` is carried
across firings).  It works in a scratch copy and leaves the trial folder as it was.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DRIVER = HERE / "answers" / "ref_01" / "run_ref.cpp"


def _mingw() -> Path | None:
    sys.path.insert(0, str(HERE.parents[2]))
    try:
        from tests.hw.test_arrayutils_datalist_elem import _mingw as find
    finally:
        sys.path.pop(0)
    return find()


def check(trial: Path, work: Path | None = None) -> dict:
    """``{"compiled": bool, "error": str, "widths": {32: {...}, 64: {...}}}``."""
    from waveflow.toolchain.toolchain import find_vitis_include_dir

    trial = Path(trial).resolve()
    work = Path(work or tempfile.mkdtemp(prefix="hlsbody_"))
    work.mkdir(parents=True, exist_ok=True)
    body = trial / "gain_stats_body.cpp"
    if not body.is_file():
        return {"compiled": False, "error": "no gain_stats_body.cpp"}
    # The given files, never the trial's copies (it was told not to change them).
    for f in (HERE / "given_01").glob("*.py"):
        shutil.copy2(f, work / f.name)
    shutil.copy2(body, work / body.name)
    shutil.copy2(DRIVER, work / DRIVER.name)
    r = subprocess.run([sys.executable, "gen_headers.py"], cwd=work, capture_output=True, text=True)
    if r.returncode:
        return {"compiled": False, "error": "gen_headers.py: " + r.stderr[-2000:]}

    gxx, vitis_inc = _mingw(), find_vitis_include_dir()
    if gxx is None or vitis_inc is None:
        return {"compiled": False, "error": "needs Vivado's MinGW and Vitis (ap_int.h, hls_stream.h)",
                "skipped": True}
    env = dict(os.environ, PATH=f"{gxx.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    exe = work / "run_ref.exe"
    r = subprocess.run([str(gxx), "-std=c++17", "-O1", f"-I{vitis_inc}", f"-I{work}",
                        f"-I{work / 'include'}", str(work / DRIVER.name), "-o", str(exe)],
                       capture_output=True, text=True, env=env, cwd=work)
    if r.returncode:
        return {"compiled": False, "error": r.stderr[-4000:]}

    sys.path.insert(0, str(work))
    try:
        for m in ("gain_model", "gain_msgs"):
            sys.modules.pop(m, None)
        gm = importlib.import_module("gain_model")
        from gain_msgs import GainResp, Out
    finally:
        sys.path.remove(str(work))
    from waveflow.hw.arrayutils import get_nwords

    # The worked example, a burst ending mid-word, and a long one with a negative gain.
    rng = np.random.default_rng(3)
    cmds = gm.EXAMPLE + [(9, 1.25, list(rng.uniform(-1, 1, 7))),
                         (10, -3.5, list(rng.uniform(-1, 1, 64)))]
    out: dict = {"compiled": True, "error": "", "widths": {}}
    for w in (32, 64):
        words = gm.make_input(cmds, w)
        (work / f"in{w}.txt").write_text("\n".join(str(int(v)) for v in words), encoding="utf-8")
        try:
            run = subprocess.run([str(exe), str(w), str(work / f"in{w}.txt")], capture_output=True,
                                 text=True, env=env, timeout=60)
        except subprocess.TimeoutExpired:
            out["widths"][w] = {"ran": False, "why": "timed out (a firing blocked on an empty stream?)"}
            continue
        got: dict = {"out": [], "status": []}
        last: dict = {"out": [], "status": []}
        for line in run.stdout.splitlines():
            if line.startswith(("out ", "status ")):          # not hls_stream's INFO lines
                kind, v, tl = line.split()
                got[kind].append(int(v))
                last[kind].append(int(tl))
        want_out, want_status = gm.gain_stats_model(words, w)
        ends, pos = [], 0
        for _, _, samples in cmds:
            pos += GainResp.nwords_per_inst(w) + get_nwords(Out, w, len(samples))
            ends.append(pos - 1)
        per = len(want_status) // len(cmds)
        out["widths"][w] = {
            "ran": run.returncode == 0,
            "m_out": got["out"] == [int(v) for v in want_out],
            "m_status": got["status"] == [int(v) for v in want_status],
            "tlast_out": [i for i, t in enumerate(last["out"]) if t] == ends,
            "tlast_status": last["status"] == ([0] * (per - 1) + [1]) * len(cmds),
        }
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    res = check(Path(sys.argv[1]))
    print("compiled:", res["compiled"])
    if res.get("error"):
        print(res["error"])
    for w, r in res.get("widths", {}).items():
        print(f"W={w}:", ", ".join(f"{k} {'ok' if v is True else v}" for k, v in r.items()))
