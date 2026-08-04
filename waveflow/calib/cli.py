"""cli.py — ``waveflow_calib``: create, inspect and publish a calibration platform.

A platform accumulated four kinds of thing without ever acquiring a way to *look at* one, and could
only be created as a side effect of running a build.  This is the missing front door::

    waveflow_calib list                       # what platforms can I see, and for which target?
    waveflow_calib new     calib/platforms/myboard --from zynq7020_bfm_100mhz
    waveflow_calib show    calib/platforms/myboard
    waveflow_calib publish calib/work/myboard calib/platforms/myboard --apply

``publish`` delegates to :mod:`waveflow.calib.publish` (still available as ``publish_calib``); the rest
are new.  ``list`` exists because the question you ask *before* creating one — is there already a
calibrated platform for my target? — had no answer short of knowing where the package installed itself.

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
    """Create a platform directory, optionally seeded from an existing one."""
    from waveflow.calib.platform import Platform, platform_fallback_path

    root, name = Path(args.path).parent, Path(args.path).name

    if not args.from_platform and not (args.part and args.clk):
        # A platform without an identity cannot gate anything: every stored number is only valid for
        # a particular part at a particular clock, and the mismatch check has nothing to compare.
        print("error: --part and --clk are required unless seeding with --from "
              "(which inherits the identity being copied)")
        return 2

    if args.from_platform:
        # Seed BEFORE resolve, so resolve then confirms against the inherited manifest rather than
        # seeding a fresh one and immediately overwriting it.
        from waveflow.calib.publish import seed_platform

        upstream = Platform.resolve(root, args.from_platform, fallbacks=platform_fallback_path())
        if upstream.dir.resolve() == (root / name).resolve():
            print(f"error: --from {args.from_platform!r} resolved to the platform being created")
            return 1
        written = seed_platform(upstream.dir, root / name, force=args.force)
        print(f"seeded from {upstream.dir}  ({len(written)} file(s))")

    res_types = tuple(args.res_types) if args.res_types else None
    p = Platform.resolve(root, name, part=args.part, clk_freq=args.clk, res_types=res_types,
                         allow_mismatch=bool(args.from_platform))

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


def cmd_list(args) -> int:
    """Every platform visible from here, in resolution order, with where each one comes from.

    The question this answers is the one you ask before seeding: *is there already a calibrated
    platform for my target?*  So it prints the identity of each — a name alone does not tell you
    whether its numbers are valid for the part you are building.
    """
    from waveflow.calib.platform import PLATFORM_MANIFEST, Platform, platform_fallback_path

    roots = [Path(args.platforms_root)] + [
        Path(r) for r in platform_fallback_path()]

    seen: dict = {}
    rows = []
    for root in roots:
        if not Path(root).is_dir():
            continue
        for d in sorted(p for p in Path(root).iterdir() if p.is_dir()):
            if not (d / PLATFORM_MANIFEST).is_file():
                continue
            shadowed = d.name in seen
            try:
                plat = Platform.resolve(root, d.name)
            except Exception:
                continue
            rows.append((d.name, plat, root, shadowed))
            seen.setdefault(d.name, d)

    if not rows:
        print(f"no platforms found under {args.platforms_root} or the fallback path")
        return 1

    print(f"{'name':28s} {'part':22s} {'clock':>9}   source")
    for name, plat, root, shadowed in rows:
        clk = f"{plat.clk_freq/1e6:.0f} MHz" if plat.clk_freq else "?"
        mark = "  (shadowed)" if shadowed else ""
        print(f"{name:28s} {str(plat.part):22s} {clk:>9}   {root}{mark}")
    print("\nSeed from one with:  waveflow_calib new <dir> --from <name>")
    if any(sh for _, _, _, sh in rows):
        print("A shadowed entry is never resolved — an earlier root already owns that name.")
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
    n.add_argument("--part", help="FPGA part / technology id the calibration is valid for")
    n.add_argument("--clk", type=float, help="synthesis clock in Hz, e.g. 100e6")
    n.add_argument("--from", dest="from_platform", metavar="PLATFORM",
                   help="seed from an existing platform (resolved through the usual search path), so "
                        "the new library inherits its bus law, residuals and module records")
    n.add_argument("--force", action="store_true",
                   help="allow seeding into a directory that is not empty")
    n.add_argument("--res-types", nargs="+", default=None,
                   help="resource counters this technology is measured in (default: the FPGA set)")
    n.set_defaults(func=cmd_new)

    ls = sub.add_parser("list", help="list every platform visible from here, with its identity")
    ls.add_argument("--platforms-root", default="calib/platforms",
                    help="this project's library (searched first; default calib/platforms)")
    ls.set_defaults(func=cmd_list)

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
