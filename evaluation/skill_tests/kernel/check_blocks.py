"""The reviewer's checker for module/01_three_blocks: does the trial's test pass, and what did each module
declare?

    python evaluation/skill_tests/kernel/check_blocks.py <trial folder> [--no-tests]

1. Runs the trial's ``test_blocks.py`` (pytest, in the trial folder): item G1.
2. Imports every ``block_*.py``, finds the hardware modules defined there, builds each on a fresh
   ``Simulation`` and lists its kind (FreeRunMod / HostActivated / HwModule), its ports by type and
   direction, and its ``mm_views``: the evidence for A1, B1-B4, C1.  A port that is not a stream
   (a memory master or slave, a register map) is flagged: the module's logic must read and write only
   streams.

It points; the review sheet scores.
"""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path

STREAM_WORDS = ("Stream",)                  # a stream endpoint's class name contains it
NOT_STREAM_WORDS = ("MMIF", "AXIMM", "Lite", "Bram", "Regmap", "RegMap")


def run_tests(folder: Path) -> dict:
    t = folder / "test_blocks.py"
    if not t.is_file():
        return {"ran": False, "passed": False, "out": "no test_blocks.py"}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", t.name],
                       cwd=folder, capture_output=True, text=True, timeout=1800)
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-5:])
    return {"ran": True, "passed": r.returncode == 0, "out": tail}


def _kind(cls) -> str:
    names = [c.__name__ for c in cls.__mro__]
    for k in ("HostActivated", "FreeRunMod", "HwModule"):
        if k in names:
            return k
    return "?"


def modules_in(path: Path) -> list[dict]:
    """The hardware modules a block file defines, built and described."""
    from waveflow.hw.hw_module import HwModule
    from waveflow.simulation.simulation import Simulation

    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)
    out = []
    for name, cls in inspect.getmembers(mod, inspect.isclass):
        if cls.__module__ != mod.__name__ or not issubclass(cls, HwModule):
            continue
        row = {"file": path.name, "class": name, "kind": _kind(cls),
               "mm_views": [f"{type(v).__name__}({v.name})" for v in getattr(cls, "mm_views", ()) or ()]}
        try:
            inst = cls(name="dut", sim=Simulation())
            ports = []
            for ep in inst.endpoints.values():
                t = type(ep).__name__
                stream = any(w in t for w in STREAM_WORDS) and not any(w in t for w in NOT_STREAM_WORDS)
                ports.append({"name": ep.name.removeprefix("dut_"), "type": t, "stream": stream,
                              "tlast": getattr(ep, "has_tlast", None)})
            row["ports"] = ports
            row["children"] = [type(c).__name__ for c in getattr(inst, "comps", {}).values()] \
                if isinstance(getattr(inst, "comps", None), dict) else []
        except Exception as e:                       # noqa: BLE001 -- report, do not stop
            row["error"] = f"{type(e).__name__}: {e}"
        out.append(row)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", type=Path)
    ap.add_argument("--no-tests", action="store_true")
    a = ap.parse_args(argv)
    folder = a.folder.resolve()

    if not a.no_tests:
        t = run_tests(folder)
        print(f"G1 test_blocks.py: {'PASS' if t['passed'] else 'FAIL'}\n{t['out']}\n")

    for f in sorted(folder.glob("block_*.py")):
        try:
            rows = modules_in(f)
        except Exception as e:                       # noqa: BLE001
            print(f"{f.name}: import failed: {type(e).__name__}: {e}\n")
            continue
        for r in rows:
            print(f"{r['file']}  {r['class']}  [{r['kind']}]")
            if "error" in r:
                print(f"    could not build: {r['error']}")
                continue
            hw = r["kind"] in ("FreeRunMod", "HostActivated")   # a plain HwModule: a model (a host)
            if not hw:
                print("    (a plain HwModule: a model, e.g. test equipment; its ports are not flagged)")
            for p in r["ports"]:
                flag = "" if p["stream"] or not hw else "   <-- NOT A STREAM"
                print(f"    {p['name']:<14} {p['type']:<24} tlast={p['tlast']}{flag}")
            if r["mm_views"]:
                print(f"    mm_views: {', '.join(r['mm_views'])}")
            if r.get("children"):
                print(f"    children: {', '.join(r['children'])}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
