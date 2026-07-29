"""cli.py — ``waveflow_calib``: create, inspect and publish a calibration platform.

A platform accumulated four kinds of thing without ever acquiring a way to *look at* one, and could
only be created as a side effect of running a build.  This is the missing front door::

    waveflow_calib new    calib/platforms/myboard --part xc7z020clg484-1 --clk 100e6
    waveflow_calib show   calib/platforms/myboard
    waveflow_calib publish calib/work/myboard calib/platforms/myboard --apply

``publish`` delegates to :mod:`waveflow.calib.publish` (still available as ``publish_calib``); the
other two are new.

**Not built: ``retime``** — sweeping every registered fixture to refit a whole platform end to end.
The registry it needs exists (:func:`~waveflow.calib.fixture.all_fixtures`); the driving is
per-example today.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _fmt_counters(d: dict) -> str:
    return "  ".join(f"{k}={v}" for k, v in sorted(d.items())) if d else "(none)"


def cmd_new(args) -> int:
    """Create a platform directory and seed its manifest."""
    from waveflow.calib.platform import Platform

    root, name = Path(args.path).parent, Path(args.path).name
    res_types = tuple(args.res_types) if args.res_types else None
    p = Platform.resolve(root, name, part=args.part, clk_freq=args.clk, res_types=res_types)

    print(f"platform {p.name!r} at {p.dir}")
    print(f"  part      : {p.part}")
    print(f"  clock     : {p.clk_freq/1e6:.1f} MHz" if p.clk_freq else "  clock     : (unset)")
    print(f"  res_types : {' '.join(p.res_types)}"
          f"{'' if args.res_types else '   (the FPGA default — not written to the manifest)'}")
    print(f"  manifest  : {p.manifest_path}")
    if p.dir.parts and "work" in p.dir.parts:
        print("\n  NOTE: this looks like a work-tier directory.  Sweeps write here freely; promote to a"
              "\n  tracked library with `waveflow_calib publish` when the numbers are worth keeping.")
    return 0


def cmd_show(args) -> int:
    """Inventory a platform: identity, bus law, component residuals, module records."""
    from waveflow.calib.module_key import ModuleIdentity  # noqa: F401  (documents the record shape)
    from waveflow.calib.platform import PLATFORM_MANIFEST, Platform
    from waveflow.calib.record_store import ModuleStore

    pdir = Path(args.path)
    if not (pdir / PLATFORM_MANIFEST).is_file():
        print(f"no platform at {pdir} (no {PLATFORM_MANIFEST})")
        return 1
    p = Platform.resolve(pdir.parent, pdir.name)

    print(f"platform {p.name!r}   {pdir}")
    print(f"  part {p.part}   clock {p.clk_freq/1e6:.1f} MHz" if p.clk_freq
          else f"  part {p.part}")
    print(f"  measured in: {' '.join(p.res_types)}")

    bus = pdir / "mm_bus.json"
    pts = len(list((pdir / "points").glob("*.json")))
    print(f"\nbus law      : {'fitted' if bus.is_file() else 'absent'}"
          f"   ({pts} corpus point{'' if pts == 1 else 's'})")

    comps = sorted(d for d in (pdir / "components").glob("*") if d.is_dir())
    print(f"\ntiming residuals ({len(comps)}):")
    for c in comps:
        params = c / "params.json"
        rows = 0
        corpus = c / "corpus.csv"
        if corpus.is_file():
            rows = max(0, len(corpus.read_text(encoding="utf-8").splitlines()) - 1)
        state = "fitted" if params.is_file() else "NOT fitted"
        print(f"  {c.name:42s} {state:11s} {rows} corpus row(s)")
    if not comps:
        print("  (none)")

    store = ModuleStore(pdir)
    keys = store.keys()
    print(f"\nmodule records ({len(keys)} configuration(s)):")
    by_class: dict = {}
    for k in keys:
        ident = store.get_identity(k)
        by_class.setdefault(ident.cls_name if ident else "?", []).append(k)
    coverage = store.coverage("resource")        # one pass over the store, not one per class
    for cls, ks in sorted(by_class.items()):
        cov: dict = {}
        for k in ks:
            for src, n in coverage.get(k, {}).items():
                cov[src] = cov.get(src, 0) + n
        print(f"  {cls:22s} {len(ks):>3} config(s)   {_fmt_counters(cov)} record(s)")
    if not keys:
        print("  (none)")

    cost = store.total_cost_seconds()
    if cost:
        print(f"\nsynthesis time these records represent: {cost/60:.1f} min")

    if args.verbose:
        print("\nper-configuration detail:")
        for k in keys:
            ident = store.get_identity(k)
            best = store.best(k, "resource")
            got = _fmt_counters({c: best.payload[c] for c in p.res_types
                                 if best and c in best.payload}) if best else "(no record)"
            print(f"  {k:26s} {json.dumps(ident.params if ident else {}, sort_keys=True)}")
            print(f"  {'':26s} {got}")
    return 0


def cmd_publish(args) -> int:
    from waveflow.calib.publish import main as publish_main

    argv = [args.work_dir, args.tracked_dir]
    if args.apply:
        argv.append("--apply")
    if args.force:
        argv.append("--force")
    return publish_main(argv)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        prog="waveflow_calib",
        description="Create, inspect and publish a Waveflow calibration platform.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="create a platform directory and seed its manifest")
    n.add_argument("path", help="the platform directory to create, e.g. calib/platforms/myboard")
    n.add_argument("--part", required=True, help="FPGA part / technology id the calibration is valid for")
    n.add_argument("--clk", type=float, required=True, help="synthesis clock in Hz, e.g. 100e6")
    n.add_argument("--res-types", nargs="+", default=None,
                   help="resource counters this technology is measured in (default: the FPGA set)")
    n.set_defaults(func=cmd_new)

    s = sub.add_parser("show", help="inventory a platform's calibration data")
    s.add_argument("path", help="the platform directory")
    s.add_argument("-v", "--verbose", action="store_true", help="list every module configuration")
    s.set_defaults(func=cmd_show)

    pb = sub.add_parser("publish", help="promote a work-tier platform into a tracked library")
    pb.add_argument("work_dir")
    pb.add_argument("tracked_dir")
    pb.add_argument("--apply", action="store_true")
    pb.add_argument("--force", action="store_true")
    pb.set_defaults(func=cmd_publish)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
