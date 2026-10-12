"""xbar_profile_build.py — measure one crossbar configuration: area, timing closure, latency.

The rungs::

    ip -> synth -> latency -> records

``ip`` runs ``create_ip`` (~30 s, cached by configuration).  ``synth`` synthesizes, places and routes
the IP out of context at the platform clock (a few minutes, cached).  ``latency`` runs the IP under
XSI between host and memory BFMs and reads each probe's span in cycles.  ``records`` files both into
the platform's module record store -- ``vivado_synth`` / ``vivado_impl`` resource records and ``xsi``
timing records under the crossbar's content key -- and writes ``results/points/<name>.json``.

Driven by :func:`~waveflow.build.cli.run_dag_cli`::

    python -m examples.xbar_profile.xbar_profile_build --list-steps
    python -m examples.xbar_profile.xbar_profile_build --shape 2x2 --mode SAMD --dw 64
    python -m examples.xbar_profile.xbar_profile_build --through latency --shape 4x4 --mode SASD

Needs Vivado (``create_ip``, synthesis, xsim) and a C++ compiler for the XSI testbench.  The whole
grid is :mod:`examples.xbar_profile.xbar_profile_sweep`.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent

from waveflow.build.axi_xbar import (  # noqa: E402
    generate_axi_xbar,
    parse_latency_output,
    render_xbar_bfm_top,
    render_xbar_latency_tb,
    synth_axi_xbar,
    xbar_latency_probes,
    xbar_synth_records,
    xbar_timing_records,
)
from waveflow.build.build import BuildConfig, BuildDag, BuildStep  # noqa: E402
from waveflow.build.cli import run_dag_cli  # noqa: E402
from waveflow.build.xsi_workspace import XsiWorkspace  # noqa: E402

from examples.xbar_profile.xbar_profile import (  # noqa: E402
    BEATS,
    CLK_FREQ,
    PART,
    PERIOD_NS,
    xbar_config,
)

#: Everything generated lands here, keyed by configuration -- untracked.
WORK = "work"
#: The work-tier platform a single build files into when ``--platform`` is given (the sweep sets it).
#: Named for the part the numbers are measured on, which is not the RFSoC's (see ``xbar_profile.PART``).
PLATFORM = "zu7ev_xbar_250mhz"
PLATFORMS_ROOT = HERE / "calib" / "work"


def platform_name(part: str) -> str:
    """The work-tier platform for measurements on *part*: ``PLATFORM`` for the default stand-in,
    ``<device>_xbar_250mhz`` otherwise -- a platform holds one part, so another part is another."""
    return PLATFORM if part == PART else f"{part.split('-')[0].removeprefix('xc')}_xbar_250mhz"

POINT_PARAMS = {"shape": "2x2", "mode": "SAMD", "dw": 64}


def _target(config: BuildConfig) -> tuple[str, float]:
    """``(part, period_ns)``: the platform's when one is selected, else the config's ``part`` /
    ``clk_freq``, else the stand-in part at 250 MHz."""
    plat = getattr(config, "platform_info", None)
    if plat is not None and plat.part:
        return plat.part, float(plat.synth_period_ns or PERIOD_NS)
    clk = getattr(config, "clk_freq", None)
    return (getattr(config, "part", None) or PART), (1e9 / clk if clk else PERIOD_NS)


@dataclass(kw_only=True)
class _PointStep(BuildStep):
    """A step whose outputs are in memory and whose cache is its own.

    The DAG's freshness rule is file mtimes, which cannot see which *point* a file belongs to; each
    of these steps instead caches under the configuration's digest (``generate_axi_xbar``,
    ``synth_axi_xbar``, ``XsiSnapshot``), so it always runs and a repeat costs a cache lookup.
    """

    def is_fresh(self, config: BuildConfig, paths: dict) -> bool:
        return False


@dataclass(kw_only=True)
class GenIpStep(_PointStep):
    """Generate the crossbar IP for this point with ``create_ip`` (cached by configuration)."""

    description = "Generate the axi_crossbar IP for the point (create_ip, cached)."
    consumes = []
    produces = {"xbar_cfg": None, "xbar_ip": None}
    params = dict(POINT_PARAMS)

    def run(self, config: BuildConfig, shape, mode, dw, **_) -> dict:
        part, _period = _target(config)
        cfg = xbar_config(shape, mode, int(dw), part=part)
        started = time.perf_counter()
        ip = generate_axi_xbar(cfg, Path(config.root_dir) / WORK / "ip")
        print(f"  {cfg.name}: IP ready ({time.perf_counter() - started:.1f}s) -> {ip.root}")
        return {"xbar_cfg": cfg, "xbar_ip": ip}


@dataclass(kw_only=True)
class SynthStep(_PointStep):
    """Out-of-context synthesis, place and route at the platform clock; utilization + slack."""

    description = "Synthesize, place and route the crossbar out of context at the platform clock."
    consumes = ["xbar_cfg", "xbar_ip"]
    produces = {"xbar_synth": None}
    params = {"impl": True}

    def run(self, config: BuildConfig, xbar_cfg, xbar_ip, impl=True, **_) -> dict:
        _part, period = _target(config)
        res = synth_axi_xbar(xbar_cfg, xbar_ip, Path(config.root_dir) / WORK / "synth", period,
                             impl=bool(impl))
        util = res.impl or res.synth
        verdict = ("" if res.met is None else
                   f"  WNS {res.wns_ns:+.3f} ns ({'met' if res.met else 'FAILED'} at {period:g} ns)")
        print(f"  {xbar_cfg.name}: LUT {util.get('lut')}  FF {util.get('ff')}  "
              f"BRAM18 {util.get('bram', 0)}{verdict}")
        return {"xbar_synth": res}


@dataclass(kw_only=True)
class LatencyStep(_PointStep):
    """Run the probe set through the crossbar under XSI; each probe's span in cycles."""

    description = "Measure the crossbar's latencies under XSI (host and memory BFMs)."
    consumes = ["xbar_cfg", "xbar_ip"]
    produces = {"xbar_latency": None}
    params = {"beats": BEATS}

    def run(self, config: BuildConfig, xbar_cfg, xbar_ip, beats=BEATS, **_) -> dict:
        cfg, ip = xbar_cfg, xbar_ip
        probes = xbar_latency_probes(cfg, beats=int(beats))
        top = "xbar_lat_top"
        ws = XsiWorkspace(Path(config.root_dir) / WORK / "xsi" / f"{cfg.name}_{cfg.digest()}",
                          top=top)
        ws.prepare(rtl_files=list(ip.sim_files) + [f"{top}.v"], include_dirs=ip.include_dirs,
                   tb_name="xbar_lat_tb",
                   tb_cpp=render_xbar_latency_tb(cfg, probes, ws.design_dll),
                   extra_files={f"{top}.v": render_xbar_bfm_top(cfg, top)})
        started = time.perf_counter()
        out = ws.run()
        spans = parse_latency_output(out, probes)
        print(f"  {cfg.name}: " + "  ".join(f"{k}={v}" for k, v in spans.items()))
        return {"xbar_latency": {"spans": spans, "probes": probes,
                                 "seconds": time.perf_counter() - started}}


@dataclass(kw_only=True)
class RecordsStep(_PointStep):
    """File the measurements into the platform's module store and write the point's JSON.

    A record identical to one already filed (same source, same payload) is not appended again, so a
    re-run of a cached point leaves the store as it was rather than doubling its rows.
    """

    description = "File resource + timing records; write results/points/<name>.json."
    consumes = ["xbar_cfg", "xbar_synth", "xbar_latency"]
    produces = {"xbar_point": None}

    def run(self, config: BuildConfig, xbar_cfg, xbar_synth, xbar_latency, **_) -> dict:
        cfg, res, lat = xbar_cfg, xbar_synth, xbar_latency
        ident = cfg.identity()
        point = {"name": cfg.name, "key": ident.key, "params": ident.params,
                 "part": res.part, "period_ns": res.period_ns, "synth": res.to_json(),
                 "latency": lat["spans"],
                 "probes": {p.name: p.what for p in lat["probes"]}}
        out = Path(config.root_dir) / "results" / "points" / f"{cfg.name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(point, indent=2), encoding="utf-8")

        plat = getattr(config, "platform_info", None)
        if plat is None:
            print(f"  (no platform selected -- wrote {out.relative_to(config.root_dir)} only)")
            return {"xbar_point": point}

        from waveflow.calib.record_store import ModuleStore

        store = ModuleStore(plat.dir)
        recs = (xbar_synth_records(cfg, res)
                + xbar_timing_records(cfg, lat["spans"], lat["probes"], part=res.part,
                                      cost_seconds=lat["seconds"]))
        filed = 0
        for rec in recs:
            have = store.read(rec.key, rec.target, identity=ident, source=rec.source)
            if any(h.payload == rec.payload for h in have):
                continue
            store.append(rec, identity=ident)
            filed += 1
        print(f"  filed {filed} new record(s) under {ident.key} in {plat.dir}")
        return {"xbar_point": point}


def build_xbar_profile_dag() -> BuildDag:
    """The DAG for one point; the point arrives through ``config.params`` (shape, mode, dw)."""
    dag = BuildDag()
    dag.add(GenIpStep(name="ip"))
    dag.add(SynthStep(name="synth"))
    dag.add(LatencyStep(name="latency"))
    dag.add(RecordsStep(name="records"))
    return dag


def main(argv: "list[str] | None" = None) -> None:
    import sys

    if argv is not None:
        sys.argv = [sys.argv[0], *argv]
    run_dag_cli(
        build_xbar_profile_dag,
        description="Measure one axi_crossbar configuration: area, timing closure, latency.",
        default_through="records",
        root_dir=HERE,
        extra_args=[
            (("--shape",), {"default": "2x2", "help": "masters x slaves, e.g. 2x2 (default 2x2)"}),
            (("--mode",), {"default": "SAMD", "choices": ["SAMD", "SASD"]}),
            (("--dw",), {"type": int, "default": 64, "help": "data width in bits (default 64)"}),
            (("--no-impl",), {"action": "store_true", "help": "synthesis only, no place and route"}),
            (("--part",), {"default": PART,
                           "help": f"part to measure on (default {PART}, a stand-in for the RFSoC)"}),
            (("--platform",), {"action": "store_true",
                               "help": "file records into the work-tier platform for the part"}),
        ],
        params_from_args=lambda a: {"shape": a.shape, "mode": a.mode, "dw": a.dw,
                                    "impl": not a.no_impl},
        config_from_args=lambda a: ({"platform": platform_name(a.part),
                                     "platforms_root": PLATFORMS_ROOT,
                                     "part": a.part, "clk_freq": CLK_FREQ} if a.platform
                                    else {"part": a.part, "clk_freq": CLK_FREQ}),
    )


if __name__ == "__main__":
    main()
