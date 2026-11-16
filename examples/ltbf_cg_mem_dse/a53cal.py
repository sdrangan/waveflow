"""a53cal.py -- the calibration flow of this example's A53 kernels (``ltbf_eig``'s, made reusable).

``plans/ltbf_dse/ltbf_dse.md`` step 4.6.  A :class:`Calibration` is one kernel's campaign under
``waveflow/cpu/calib``'s rules: the points and their roles registered in
``waveflow/cpu/calib/sweep.py`` and written to a committed plan CSV before any is measured; gem5
from a work directory inside this example's ``data/``; the corpus and the fitted model local
(``data/a53_ltbf/corpus_<kernel>.csv``, ``model_<kernel>.json``).  Fit on the fit points, look at
validation, score the test points once.  :func:`load_model` reads a fitted model for pricing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from waveflow.cpu.calib.calibrate import Family
from waveflow.cpu.calib.kernels import KERNEL_DIR, Kernel


@dataclass
class Calibration:
    """One kernel's calibration: its program, twin, family, plan and local data."""

    kernel: Kernel
    family: Family
    source: Path
    plan: Path
    data: Path
    #: The ``waveflow.cpu.calib.sweep`` function that registers its points.
    rows_fn: str

    @property
    def corpus(self) -> Path:
        return self.data / f"corpus_{self.kernel.name}.csv"

    @property
    def model_path(self) -> Path:
        return self.data / f"model_{self.kernel.name}.json"

    def host_run(self, point: dict[str, Any], workdir: Path | None = None) -> dict:
        """The C program built and run on the host (no gem5), for the twin check."""
        workdir = workdir or self.data / "host"
        workdir.mkdir(parents=True, exist_ok=True)
        exe = workdir / f"{self.kernel.name}_host"
        if not exe.is_file() or exe.stat().st_mtime < self.source.stat().st_mtime:
            subprocess.run(["gcc", "-O2", f"-I{KERNEL_DIR}", str(self.source), "-lm", "-o", str(exe)],
                           check=True, capture_output=True, text=True)  # fmt: skip
        run = subprocess.run([str(exe), *self.kernel.argv(point)], check=True, capture_output=True,
                             text=True)  # fmt: skip
        return json.loads(run.stdout)

    def runner(self):
        from waveflow.cpu.calib.gem5 import Gem5Runner

        return Gem5Runner(workdir=self.data / "gem5")

    def write_plan(self) -> Path:
        from waveflow.cpu.calib import sweep
        from waveflow.cpu.calib.prereg import SweepPlan

        return SweepPlan.write(self.plan, getattr(sweep, self.rows_fn)())

    def campaign(self, workers: int = 2) -> int:
        """gem5 on every registered point not yet in the local corpus (resumable)."""
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        import pandas as pd  # type: ignore[import-untyped]

        from waveflow.cpu.calib.prereg import SweepPlan, point_key

        plan = SweepPlan.load(self.plan)
        run = self.runner()
        why = run.unavailable()
        if why:
            raise RuntimeError(f"cannot measure: {why}")
        name = self.kernel.name
        done: set[str] = set()
        if self.corpus.is_file():
            done = {
                point_key(name, json.loads(p))
                for p in pd.read_csv(self.corpus)["point"]
            }
        todo = [json.loads(r.point) for r in pd.read_csv(plan.path).itertuples()
                if r.kernel == name and point_key(r.kernel, json.loads(r.point)) not in done]  # fmt: skip
        run.build(self.kernel)
        empty = run.empty_region_cycles()
        print(
            f"{name}: {len(todo)} points, {len(done)} done; empty region {empty:g} cycles",
            flush=True,
        )
        t0, bad = time.monotonic(), 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(run.measure, self.kernel, pt, empty_cycles=empty, plan=plan): pt
                    for pt in todo}  # fmt: skip
            for i, fut in enumerate(as_completed(futs), 1):
                pt = futs[fut]
                try:
                    row = fut.result()
                except Exception as exc:  # noqa: BLE001 - record and keep going
                    bad += 1
                    print(f"[{i}/{len(todo)}] FAILED {pt}: {exc}", flush=True)
                    continue
                self.corpus.parent.mkdir(parents=True, exist_ok=True)
                new = pd.DataFrame([row])
                if self.corpus.is_file():
                    new = pd.concat([pd.read_csv(self.corpus), new], ignore_index=True)
                new.to_csv(self.corpus, index=False)
                bad += not row["output_matches_twin"]
                print(f"[{i}/{len(todo)}] {pt} {row['role']}: {row['cycles']:g} cycles, "
                      f"twin {'ok' if row['output_matches_twin'] else 'MISMATCH'}", flush=True)  # fmt: skip
        print(f"{name}: {(time.monotonic() - t0) / 60:.1f} min", flush=True)
        return 1 if bad else 0

    def fit(self) -> dict:
        """Fit on the fit points; report validation; score the test points once."""
        import pandas as pd  # type: ignore[import-untyped]

        df = self.family.select(pd.read_csv(self.corpus))
        model = self.family.model("cycles")
        model.fit(df[df["role"] == "fit"])
        out = {}
        for role in ("fit", "validation", "test"):
            part = df[df["role"] == role]
            if part.empty:
                continue
            pred = np.array([model.predict_feat(r) for r in part.to_dict("records")])
            err = np.abs(pred - part["cycles"].to_numpy()) / part["cycles"].to_numpy()
            out[role] = {"points": len(part), "median": float(np.median(err)),
                         "max": float(err.max())}  # fmt: skip
            print(f"{self.kernel.name} {role:10s} {len(part):3d} points: median "
                  f"{np.median(err):.1%}, max {err.max():.1%}")  # fmt: skip
        self.model_path.write_text(
            json.dumps({"params": model.to_params(), "errors": out}, indent=1)
        )
        return out

    def main(self, argv: list[str] | None = None) -> int:
        p = argparse.ArgumentParser(
            description=f"the {self.kernel.name} A53 kernel's calibration"
        )
        p.add_argument("cmd", choices=("plan", "smoke", "campaign", "fit"))
        p.add_argument("--workers", type=int, default=2)
        a = p.parse_args(argv)
        if a.cmd == "plan":
            print(f"wrote {self.write_plan()}; commit it before measuring")
            return 0
        if a.cmd == "smoke":
            run = self.runner()
            for pt in self.kernel.smoke:
                twin, host = self.kernel.run_twin(pt), self.host_run(pt)
                row = run.measure(self.kernel, pt, empty_cycles=0.0, smoke=True)
                print(pt, "twin == host:", twin == host, "| gem5:",
                      {k: row.get(k) for k in ("cycles", "output_matches_twin")})  # fmt: skip
            return 0
        if a.cmd == "campaign":
            return self.campaign(a.workers)
        self.fit()
        return 0


def load_model(cal: Calibration):
    """The fitted cycle model of *cal* (``None`` when the local calibration has not been run)."""
    if not cal.model_path.is_file():
        return None
    model = cal.family.model("cycles")
    model.load_params(json.loads(cal.model_path.read_text())["params"])
    return model
