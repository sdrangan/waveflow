"""platform.py — a calibrated processor platform, ready to simulate with.

A platform directory (``waveflow/calib/platforms/<name>/``) holds what a calibration produced: the
manifest (``platform.json``: the core and its clock), the corpus, and under ``cpu/models/`` one fitted
cycle model and one energy model per family, plus the area and leakage models.  :class:`CpuPlatform`
turns that into the objects a simulation uses:

* :meth:`CpuPlatform.sw_function` — a :class:`~waveflow.cpu.task.SwFunction` per family, running the
  family's Python twin and priced by the fitted models (its confidence comes with them);
* :meth:`CpuPlatform.cpu_config` — a :class:`~waveflow.cpu.config.CpuConfig` at the platform's clock,
  with the context-switch cost from the ``ctx_switch`` calibration and the static power from the
  leakage model;
* :meth:`CpuPlatform.area_model` — the :class:`~waveflow.cpu.area.CpuAreaModel` for a DSE.

The reference platform is the RFSoC 4x2's A53 on gem5 v25.1 (``a53_hpi_1200mhz_gem5v25_1``).  Its
measured accuracy is in ``cpu/accuracy.csv``; three cycle models miss the 25 % max bound on small
operations (``plans/cpu_model.md`` §14), which the docs state beside every number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]  # no pandas-stubs in the dev deps

from waveflow.calib.calib import LinCalibModel
from waveflow.cpu.area import CpuAreaModel
from waveflow.cpu.calib.calibrate import (
    AREA_TARGETS,
    FAMILIES,
    Family,
    area_model,
)
from waveflow.cpu.calib.kernels import KERNELS
from waveflow.cpu.config import CpuConfig
from waveflow.cpu.task import SwFunction

PLATFORMS_ROOT = Path(__file__).resolve().parents[1] / "calib" / "platforms"
DEFAULT_PLATFORM = "a53_hpi_1200mhz_gem5v25_1"


@dataclass
class CpuPlatform:
    """A calibrated processor platform directory."""

    name: str
    dir: Path

    @classmethod
    def load(
        cls, name: str = DEFAULT_PLATFORM, root: str | Path | None = None
    ) -> CpuPlatform:
        pdir = Path(root or PLATFORMS_ROOT) / name
        if not (pdir / "cpu" / "models").is_dir():
            raise FileNotFoundError(f"{pdir} has no calibrated cpu/models/")
        return cls(name=name, dir=pdir)

    @cached_property
    def manifest(self) -> dict[str, Any]:
        return json.loads((self.dir / "platform.json").read_text(encoding="utf-8"))

    @property
    def f_clk_hz(self) -> float:
        return float(self.manifest["clk_freq_hz"])

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(f.name for f in FAMILIES)

    def _family(self, name: str) -> Family:
        for fam in FAMILIES:
            if fam.name == name:
                return fam
        raise KeyError(f"no family {name!r}; have {self.families}")

    def model(self, family: str, target: str = "cycles") -> LinCalibModel:
        """The fitted *target* model of *family* (``cycles`` or ``energy_pj``)."""
        model = self._family(family).model(target)
        path = self.dir / "cpu" / "models" / family / f"{target}.json"
        if model.load_model(path) is None:
            raise FileNotFoundError(path)
        return model

    def code_bytes(self, kernel: str) -> int | None:
        path = self.dir / "cpu" / kernel / "corpus.csv"
        if not path.is_file():
            return None
        return int(pd.read_csv(path, usecols=["code_bytes"])["code_bytes"].iloc[0])

    def sw_function(self, family: str) -> SwFunction:
        """*family*'s Python twin, priced by the platform's fitted cycle and energy models.

        Call it with the twin's arguments: ``cpu.execute(f, n=100, seed=1)``.  A ``sched_ops.<op>``
        family fixes its operation, so its function takes ``n`` and ``seed`` only.
        """
        fam = self._family(family)
        kernel = KERNELS[fam.kernel]
        fixed = {"op": fam.op} if fam.op is not None else {}
        ws = kernel.working_set

        def fn(**point: Any) -> tuple[dict, dict[str, float]]:
            out = kernel.run_twin({**point, **fixed})
            return out, kernel.counters_of(out)

        return SwFunction(
            name=family,
            fn=fn,
            cycles=self.model(family, "cycles"),
            energy_pj=self.model(family, "energy_pj"),
            working_set=(lambda c: ws(c)) if ws is not None else None,
            code_bytes=self.code_bytes(fam.kernel),
        )

    def switch_cycles(self) -> float:
        """Cycles per context switch: the ``ctx_switch`` model's slope in ``n_switches``."""
        return float(self.model("ctx_switch").coeffs["n_switches"])

    def area_model(self) -> CpuAreaModel:
        """Area (mm²) and leakage (mW, the whole configuration) from the McPAT fit."""
        models = {}
        for target in AREA_TARGETS:
            m = area_model(target)
            if (
                m.load_model(self.dir / "cpu" / "models" / "area" / f"{target}.json")
                is None
            ):
                raise FileNotFoundError(f"{self.name} has no area model {target}")
            models[target] = m
        return CpuAreaModel(
            area_mm2=models["area_mm2"],
            leak_mw=models["leak_mw"],
            source=f"{self.name} (McPAT 22 nm)",
        )

    def cpu_config(self, n_cores: int = 1, **kw: Any) -> CpuConfig:
        """A configuration at the platform's clock, with its switch cost and static power.

        Any keyword in *kw* is a :class:`CpuConfig` field and overrides the platform's default for
        it (``name``, ``f_clk_hz`` and ``switch_cycles`` included).  Static power is the leakage
        model's whole-configuration figure divided by *n_cores* (the report charges
        ``n_cores x static_power_mw``), unless *kw* gives ``static_power_mw``; without an area
        model it is 0.
        """
        fields: dict[str, Any] = {
            "name": self.name,
            "f_clk_hz": self.f_clk_hz,
            "switch_cycles": self.switch_cycles(),
        }
        fields.update(kw)
        cfg = CpuConfig(n_cores=n_cores, **fields)
        if "static_power_mw" in kw:
            return cfg
        try:
            leak = self.area_model().estimate(cfg)["leak_mw"].value
        except FileNotFoundError:
            return cfg
        cfg.static_power_mw = leak / n_cores
        return cfg
