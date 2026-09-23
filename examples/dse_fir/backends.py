"""Toolchain-free FIR evidence: frozen HLS replay and canonical resource models.

HLS estimates are not placed/routed measurements. The committed grid has no
candidate-level RTL trace/verdict, so this backend cannot certify RTL correctness.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from examples.fir_block.fir_block import FirBlock
from examples.fir_block.fir_block_corpus import COMMITTED_CALIB, GRID
from waveflow.build.elaborate import elaborate
from waveflow.calib.module_key import identify_instance
from waveflow.calib.platform import Platform

_ROOT = Path(__file__).resolve().parents[2]
_COUNTERS = ("dsp", "lut", "ff", "bram")
_FIELDS = {"ntap", "samp_w", "samp_i", "unroll_lane", "mem_dwidth"}


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _params(params):
    if set(params) != _FIELDS:
        raise ValueError("exactly ntap, samp_w, samp_i, unroll_lane, mem_dwidth are required")
    p = dict(params)
    if type(p["unroll_lane"]) is not bool:
        raise ValueError("unroll_lane must be a boolean")
    if any(type(p[k]) is not int for k in _FIELDS - {"unroll_lane"}):
        raise ValueError("numeric FIR parameters must be integers")
    if not (p["ntap"] >= 1 and 1 <= p["samp_i"] <= p["samp_w"] <= p["mem_dwidth"]):
        raise ValueError("require ntap >= 1 and 1 <= samp_i <= samp_w <= mem_dwidth")
    return p


class ReplayBackend:
    """Read-only backend, pinned to the example platform (never user fallbacks).

    ``calib_dir`` can name a copy of the committed library for offline inspection.
    Content hashes, not timestamps, identify every calibration/model input. Source
    changes during the life of an instance are refused; construct a fresh backend.
    """

    def __init__(self, calib_dir=None):
        self.calib_dir = Path(calib_dir or COMMITTED_CALIB).resolve()
        if not (self.calib_dir / "platform.json").is_file():
            raise FileNotFoundError(f"missing frozen platform: {self.calib_dir / 'platform.json'}")
        self.platform = Platform.resolve(self.calib_dir.parent, self.calib_dir.name, fallbacks=[])
        self._grid = deepcopy(GRID)
        self._hashes = self._source_hashes()
        self._identity = {
            "backend": "frozen_fir_replay", "schema_version": 1,
            "platform": self.platform.name, "part": self.platform.part,
            "clock_hz": self.platform.clk_freq, "source_hashes": self._hashes,
            "source_version": _digest(json.dumps(self._hashes, sort_keys=True).encode()),
        }

    def _source_hashes(self):
        # Broad upstream coverage is intentional: elaboration, device rules, module
        # identity and feature extraction can all change predictions without a corpus edit.
        paths = set()
        for package in ("calib", "hw", "build", "simulation", "utils"):
            paths.update((_ROOT / "waveflow" / package).rglob("*.py"))
        example = _ROOT / "examples" / "fir_block"
        paths.update(example.glob("*.py"))
        paths.update(example.glob("*.h"))
        paths.add(Path(__file__).resolve())
        hashes = {str(p.relative_to(_ROOT)): _digest(p.read_bytes()) for p in sorted(paths)}
        for p in sorted(self.calib_dir.rglob("*")):
            if p.is_file() and p.suffix in {".json", ".jsonl", ".csv"}:
                hashes["calibration/" + str(p.relative_to(self.calib_dir))] = _digest(p.read_bytes())
        return hashes

    @property
    def identity(self) -> dict:
        return deepcopy(self._identity)

    def _result(self, status, kind, metrics=None, **provenance):
        # The complete hash manifest lives once in the experiment store. Returning
        # it for every observation buries numerical evidence in repeated context.
        backend_ref = {key: value for key, value in self._identity.items() if key != "source_hashes"}
        return {"status": status, "evidence_kind": kind, "metrics": metrics or {},
                "provenance": {"backend": backend_ref, **provenance}}

    def _check_sources(self):
        if self._source_hashes() != self._hashes:
            raise RuntimeError("backend source content changed; create a new ReplayBackend")

    def capabilities(self) -> dict:
        return {"synth": {"available": True, "mode": "replay", "points": len(self._grid),
                          "evidence_kind": "hls_estimate_replay", "samp_i": 2, "mem_dwidth": 32,
                          "supported_params": [{"ntap": n, "samp_w": w, "unroll_lane": u,
                                                "samp_i": 2, "mem_dwidth": 32}
                                               for n, w, u in sorted(self._grid)]},
                "rtlsim": {"available": False, "reason": "no per-candidate RTL evidence"},
                "predict_resource": {"available": True, "mode": "composed_model"},
                "toolchain_required": False}

    def predict_resource(self, params: dict) -> dict:
        from waveflow.calib.resource_model import compose

        p = _params(params)
        self._check_sources()
        if (p["samp_i"] != 2 or p["mem_dwidth"] not in {32, 64}
                or self.platform.part != "xc7z020clg484-1"
                or self.platform.clk_freq != 100_000_000):
            return self._result("uncalibrated", "none", params=p,
                                reason="outside calibrated format/memory/platform domain",
                                extrapolating=True)
        top = elaborate(FirBlock, p, name="fir_block")
        # add_rm recurses and calls FirCompute.get_rm(platform): no private
        # refit, invented features, hand-summed child costs, or top-total training.
        top.add_rm(self.platform)
        estimate = compose(top)
        composition = estimate.to_json()
        if (composition["level"] == "UNCALIBRATED"
                or any(counter not in resources for _, _, resources, _ in estimate.per_module
                       for counter in _COUNTERS)):
            # Canonical compose intentionally includes zero placeholders for missing
            # models. They are diagnostics, never usable whole-design estimates.
            return self._result("uncalibrated", "resource_model_prediction", params=p,
                                composition=composition, reason="missing module/counter calibration")
        compute = next(c for _, n, _, c in estimate.per_module if n == "FirCompute")
        per_target = compute.facts.get("per_target", {})
        metrics = {}
        for counter in _COUNTERS:
            facts = per_target.get(counter, compute.to_json())
            contributions = [c.facts.get("per_target", {}).get(counter, c.to_json())
                             for _, _, _, c in estimate.per_module]
            # Analytical counters do not retain their own fitted support. Apply
            # the hierarchy's known domain warning to them too; keep the original
            # per-counter model claim in confidence for inspection.
            extrapolating = composition["level"] == "EXTRAPOLATED"
            exact = not extrapolating and all(c.get("level") == "EXACT" for c in contributions)
            metrics["top_" + counter] = {
                "est": estimate.total[counter], "interval": None,
                "exact": exact, "source": "canonical_compose:" + facts.get("model", "fit"),
                "n_support": facts.get("n_points"), "extrapolating": extrapolating,
                "uncertainty": "exact_model_rule" if exact else "unknown_predictive_interval",
                "max_abs_residual": facts.get("max_abs_residual"),
                "max_rel_residual": facts.get("max_rel_residual"),
                "fitted_ranges": facts.get("fitted_ranges"),
                "confidence": facts,
            }
        return self._result("ok", "resource_model_prediction", metrics,
                            params=p, composition=composition,
                            uncertainty_note="Residuals describe the calibration fit, not a predictive CI; "
                                             "exact means model/lookup exactness, not physical measurement.")

    def rtlsim(self, params: dict) -> dict:
        p = _params(params)
        self._check_sources()
        return self._result("unavailable", "none", params=p,
                            reason="no per-candidate RTL trace or verification verdict in frozen corpus")

    def synth(self, params: dict) -> dict:
        p = _params(params)
        self._check_sources()
        key = (p["ntap"], p["samp_w"], p["unroll_lane"])
        if p["samp_i"] != 2 or p["mem_dwidth"] != 32 or key not in self._grid:
            return self._result("out_of_corpus", "none", reason="not in the fixed I=2, memory=32 HLS grid")
        if self.platform.part != "xc7z020clg484-1" or self.platform.clk_freq != 100_000_000:
            return self._result("unavailable", "none", reason="platform differs from frozen HLS corpus")
        top = elaborate(FirBlock, p, name="fir_block")
        compute = next(c for c in top.sub_comps.values() if type(c).__name__ == "FirCompute")
        ident = identify_instance(compute)
        module_dir = self.calib_dir / "modules" / ident.key
        manifest_path = module_dir / "module.json"
        records_path = module_dir / "resource" / "records.jsonl"
        if not manifest_path.exists() or not records_path.exists():
            return self._result("unavailable", "none", reason="exact compute module record is missing")
        manifest = json.loads(manifest_path.read_text())
        if (manifest.get("key") != ident.key or manifest.get("params") != ident.params
                or manifest.get("cls_name") != ident.cls_name
                or manifest.get("cls_module") != ident.cls_module):
            return self._result("unavailable", "none", reason="compute module identity mismatch")
        expected = self._grid[key]
        for line in records_path.read_text().splitlines():
            record = json.loads(line)
            prov = record.get("provenance", {})
            if (record.get("target") == "resource" and record.get("source") == "hls_estimate"
                    and prov.get("signature") == manifest.get("signature")
                    and prov.get("part") == self.platform.part and prov.get("period_ns") == 10.0
                    and all(record["payload"].get(c) == expected[c] for c in ("lut", "ff", "dsp"))):
                return self._result("ok", "hls_estimate_replay",
                                    {k: v for k, v in expected.items() if k.startswith("top_")},
                                    module_key=ident.key, params=p, source="hls_estimate",
                                    interpretation="Vitis HLS estimate; not physical implementation measurement",
                                    record_path=str(records_path.relative_to(self.calib_dir)),
                                    record_sha256=self._hashes["calibration/" + str(records_path.relative_to(self.calib_dir))],
                                    module_sha256=self._hashes["calibration/" + str(manifest_path.relative_to(self.calib_dir))])
        return self._result("unavailable", "none", reason="no matching HLS resource evidence")
