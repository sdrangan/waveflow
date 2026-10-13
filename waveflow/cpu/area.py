"""area.py — :class:`CpuAreaModel`, the area and leakage of a processor configuration.

Area is a property of a configuration, not of a run, so it is priced from :class:`CpuConfig` alone:
its core count, cache sizes and clock become the features a model (calibrated against McPAT in
step 12 of ``plans/cpu_model.md``) maps to mm² and mW.  Each number comes back as an
:class:`~waveflow.calib.confidence.Estimate`, so a design-space exploration sees how far a
configuration is from the ones that were measured.
"""

from __future__ import annotations

from dataclasses import dataclass

from waveflow.calib.confidence import Estimate
from waveflow.cpu.config import CpuConfig, CycleCost
from waveflow.cpu.report import cost_confidence
from waveflow.cpu.task import eval_cost


def config_features(config: CpuConfig) -> dict[str, float]:
    """The features an area or leakage model reads: cores, cache sizes (KiB), clock (MHz)."""
    return {
        "n_cores": float(config.n_cores),
        "l1i_kb": config.l1i_bytes / 1024,
        "l1d_kb": config.l1d_bytes / 1024,
        "l2_kb": config.l2_bytes / 1024,
        "f_mhz": config.f_clk_hz / 1e6,
    }


@dataclass(kw_only=True)
class CpuAreaModel:
    """Area (mm²) and static power (mW) of a whole configuration, each a number or a model.

    ``leak_mw`` is the leakage of every core and the shared L2 together;
    :meth:`CpuPlatform.cpu_config <waveflow.cpu.platform.CpuPlatform.cpu_config>` divides it by
    ``n_cores`` for :attr:`CpuConfig.static_power_mw <waveflow.cpu.config.CpuConfig.static_power_mw>`.
    """

    area_mm2: CycleCost
    leak_mw: CycleCost
    #: Names the source in each estimate (a platform, a McPAT node).
    source: str = ""

    def estimate(self, config: CpuConfig) -> dict[str, Estimate]:
        """``{"area_mm2": Estimate, "leak_mw": Estimate}`` for *config*."""
        feats = config_features(config)
        return {
            name: Estimate(
                value=eval_cost(cost, feats),
                source=self.source,
                confidence=cost_confidence(cost, feats),
            )
            for name, cost in (("area_mm2", self.area_mm2), ("leak_mw", self.leak_mw))
        }
