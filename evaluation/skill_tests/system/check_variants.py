"""Run a trial's tests as given, then under each broken variant of its devices.

    python evaluation/skill_tests/system/check_variants.py <trial folder>

The trial imports its devices from the given ``devices.py``; with ``WF_VARIANT`` set, that module
exports a broken device (``_variants.py``, copied in here -- the agent never sees it):

* ``deadlock`` -- the scaler stops answering after its second job (a run that ends quietly);
* ``drop``     -- the scaler writes one word fewer than each job's output (the host reads a stale word).

Prints one line per run: PASS / FAIL and the pytest summary.  The sheet's item wants PASS, FAIL, FAIL.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(folder: Path, variant: str) -> tuple[bool, str]:
    env = dict(os.environ, WF_VARIANT=variant)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                        str(folder)], cwd=folder, env=env, capture_output=True, text=True,
                       timeout=1800)
    last = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    return r.returncode == 0, last


def main(argv: list[str]) -> int:
    folder = Path(argv[1]).resolve()
    shutil.copy2(HERE / "_variants.py", folder / "_variants.py")
    try:
        results = [(v or "as given", *run(folder, v)) for v in ("", "deadlock", "drop")]
    finally:
        (folder / "_variants.py").unlink(missing_ok=True)
    for name, ok, last in results:
        print(f"{name:10s} {'PASS' if ok else 'FAIL'}   {last}")
    want = [True, False, False]
    caught = [ok == w for (_n, ok, _l), w in zip(results, want)]
    print("variants caught:", sum(caught[1:]), "of 2;", "given run passes" if caught[0] else
          "given run FAILS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
