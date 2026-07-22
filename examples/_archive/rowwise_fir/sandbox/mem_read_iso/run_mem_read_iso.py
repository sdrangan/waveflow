"""run_mem_read_iso.py — isolate the m_axi read FORM that broke cosim in the FIR hook.

Cosims a load->store copy for each READ_MODE and reports PASS (cosim bit-exact) / FAIL (first-read
or other mismatch). Pins whether the trigger is the running pointer, the helper, or the combination:

  0 indexed direct      | 1 helper+recomputed addr | 2 helper+running ptr
  3 direct base+index   | 4 direct running ptr

Run (project venv, Vitis 2025.1)::

    PYTHONPATH=../../../.. ../../../../pysilicon-venv/Scripts/python.exe run_mem_read_iso.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO))

from waveflow.toolchain import toolchain   # noqa: E402

TCL = HERE / "mem_read_iso.tcl"
MODES = {
    0: "indexed direct        mem[xoff+i]",
    1: "helper + recomputed   my_read_lane(mem+xoff+i)   (read_array_slice's form)",
    2: "helper + running ptr  my_read_lane(p); ++p       (the hook's failing form)",
    3: "direct base+index     p[i]  (pointer, indexed, no ++)",
    4: "direct running ptr    *p; ++p  (running ptr, no helper)",
}


def run(mode: int, n: int, nj: int) -> tuple[bool, str]:
    env = {"WAVEFLOW_MR_MODE": str(mode), "WAVEFLOW_MR_N": str(n), "WAVEFLOW_MR_NJ": str(nj)}
    res = toolchain.run_vitis_hls_result(TCL, work_dir=HERE, capture_output=True, env=env)
    out = (res.get("stdout") or "") + (res.get("stderr") or "")
    ok = "WAVEFLOW_SUCCESS" in out and "RTL co-simulation failed" not in out
    m = re.search(r"job \d+ elem \d+: got 0x[0-9a-f]+ exp 0x[0-9a-f]+", out)
    detail = m.group(0) if m else ("bit-exact" if ok else "cosim failed")
    return ok, detail


def main() -> None:
    n, nj = 64, 3
    print(f"mem_read_iso  N={n}/job  NJOBS={nj}   (does the read FORM change RTL correctness?)\n")
    print(f"{'mode':>4}  {'verdict':>10}  form / first-mismatch")
    for mode, desc in MODES.items():
        ok, detail = run(mode, n, nj)
        print(f"{mode:>4}  {('PASS' if ok else 'FAIL'):>10}  {desc}", flush=True)
        if not ok:
            print(f"        -> {detail}", flush=True)


if __name__ == "__main__":
    main()
