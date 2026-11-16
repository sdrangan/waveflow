"""stepsim.py — the SIMD core's HLS source run as C++ in step mode (``plans/simdcore.md`` Stage 5).

The generated header (:mod:`waveflow.simdcore.hls`) compiled with ``-DSC_STEP``: each task runs one
loop iteration a call, and a generated testbench interleaves them (the vector task every step, the
scalar task every fourth: its II).  It is the HLS source's C simulation -- the same text csynth reads --
without Vitis's thread-based ``hls::task`` simulation, so it is deterministic and fast.  It needs the
Vitis HLS headers (``ap_int.h``, ``hls_stream.h``) and a C++ compiler: the MinGW g++ Vitis ships, or
one on ``PATH`` (``WAVEFLOW_CXX`` overrides).

:func:`run` takes a configuration and its jobs (:class:`waveflow.simdcore.unit.Job`) and returns every
output port's bursts and the status words in :func:`waveflow.simdcore.unit.golden_bursts`' form, plus
the step at which each status left -- a cycle count of the HLS source's own loops, not of the RTL.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from waveflow.simdcore.config import SimdCoreConfig
from waveflow.simdcore.hls import D_DEFAULT, header_name, out_ports, render_header, render_tb
from waveflow.simdcore.unit import Job, golden_bursts, job_bursts

BUILD = Path(__file__).resolve().parents[1] / "build"


def find_cxx() -> Path | None:
    env = os.environ.get("WAVEFLOW_CXX")
    if env:
        return Path(env)
    from waveflow.toolchain import toolchain

    exe = toolchain.find_vitis_path()
    if exe:
        root = Path(exe).resolve().parent.parent  # <install>/Vitis
        cands = sorted(root.glob("tps/mingw/*/win64.o/nt/bin/g++.exe"), key=lambda p: [int(x) for x in p.parts[-5].split(".")])
        if cands:
            return cands[-1]
    for n in ("g++", "clang++"):
        p = shutil.which(n)
        if p:
            return Path(p)
    return None


def find_include() -> Path | None:
    from waveflow.toolchain import toolchain

    return toolchain.find_vitis_include_dir()


def available() -> bool:
    return find_cxx() is not None and find_include() is not None


@dataclass
class StepResult:
    outputs: dict[str, list[np.ndarray]]
    steps: int
    status_steps: list[int] = field(default_factory=list)
    log: str = ""


def _write_bursts(path: Path, bursts) -> None:
    lines = []
    for b in bursts:
        w = np.asarray(b.words, np.uint64).reshape(-1)
        for k, x in enumerate(w):
            last = int(b.tlast and k == w.size - 1)
            lines.append(f"{int(x):016x} {last}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def _read_bursts(path: Path) -> list[np.ndarray]:
    out, cur = [], []
    if not path.is_file():
        return out
    toks = path.read_text().split()
    for k in range(0, len(toks), 2):
        cur.append(int(toks[k], 16))
        if toks[k + 1] == "1":
            out.append(np.asarray(cur, np.uint64))
            cur = []
    if cur:
        out.append(np.asarray(cur, np.uint64))
    return out


def build(cfg: SimdCoreConfig, work: Path, d: int = D_DEFAULT) -> Path:
    """Render the header and testbench into ``work`` and compile them (cached on the sources)."""
    cxx, inc = find_cxx(), find_include()
    if cxx is None or inc is None:
        raise RuntimeError("step-mode C simulation needs the Vitis HLS headers and a C++ compiler")
    work.mkdir(parents=True, exist_ok=True)
    hdr = render_header(cfg, d, top=False)
    tb = render_tb(cfg, d)
    (work / header_name(cfg, d)).write_text(hdr)
    (work / "tb.cpp").write_text(tb)
    core = (BUILD / "simdcore_core.h").read_text() + (BUILD / "streamutils_hls.h").read_text()
    key = hashlib.sha1((hdr + tb + core).encode()).hexdigest()[:16]
    exe = work / f"tb_{key}.exe"
    if not exe.is_file():
        cmd = [str(cxx), "-std=c++14", "-O2", "-w", f"-I{BUILD}", f"-I{inc}", "-o", str(exe), str(work / "tb.cpp")]
        env = dict(os.environ)
        env["PATH"] = str(cxx.parent) + os.pathsep + env.get("PATH", "")
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"compile failed:\n{r.stdout}\n{r.stderr[-6000:]}")
    return exe


def run(cfg: SimdCoreConfig, jobs: list[Job], work: str | Path, *, d: int = D_DEFAULT,
        max_steps: int = 20_000_000, golden: dict | None = None) -> StepResult:
    """Run ``jobs`` through the step-mode C simulation; stop once the golden's bursts are out."""
    work = Path(work)
    exe = build(cfg, work, d)
    stim = job_bursts(cfg, jobs)
    _write_bursts(work / "prog.txt", stim["s_prog"])
    for name, bursts in stim.items():
        if name.startswith("s_in"):
            _write_bursts(work / f"in{name[4:]}.txt", bursts)
    gold = golden if golden is not None else golden_bursts(cfg, jobs)
    want = [len(gold.get(f"s_out{p}", [])) for p in out_ports(cfg)]
    cmd = [str(exe), str(work), str(max_steps), str(len(gold["m_status"])), *map(str, want)]
    env = dict(os.environ)
    cxx = find_cxx()
    if cxx is not None:
        env["PATH"] = str(cxx.parent) + os.pathsep + env.get("PATH", "")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"step simulation failed ({r.returncode}):\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
    outs = {f"s_out{p}": _read_bursts(work / f"out{p}.txt") for p in out_ports(cfg)}
    outs["m_status"] = _read_bursts(work / "status.txt")
    steps = int(r.stdout.split("steps")[-1].split()[0]) if "steps" in r.stdout else -1
    times = [int(x) for x in (work / "times.txt").read_text().split()] if (work / "times.txt").is_file() else []
    return StepResult(outs, steps, times, r.stdout)


def compare(got: dict, gold: dict) -> list[str]:
    """Every difference between two ``{port: [burst, ...]}`` dicts, as messages (empty: equal)."""
    bad = []
    for name in sorted(set(gold) | set(got)):
        g, h = gold.get(name, []), got.get(name, [])
        if len(g) != len(h):
            bad.append(f"{name}: {len(h)} bursts, golden {len(g)}")
        for k, (a, b) in enumerate(zip(h, g)):
            a, b = np.asarray(a, np.uint64).reshape(-1), np.asarray(b, np.uint64).reshape(-1)
            if a.shape != b.shape:
                bad.append(f"{name}[{k}]: {a.size} words, golden {b.size}")
            elif not np.array_equal(a, b):
                i = int(np.argmax(a != b))
                bad.append(f"{name}[{k}] word {i}: 0x{int(a[i]):016x}, golden 0x{int(b[i]):016x}")
    return bad


__all__ = ["StepResult", "available", "build", "compare", "find_cxx", "find_include", "run"]
