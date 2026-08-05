"""migrate_keys.py — re-address a record store after a deliberate signature change.

A record is addressed by a digest of its module's structure.  When what *enters* that signature
changes on purpose — an attribute recognised as context, a structural field added — every key moves
and a committed store stops resolving.  The measurements are still correct; only their addresses are
stale.

This is the second such move in two months, so it is a tool rather than a one-off script.

WHY MAP BY ``(cls_name, params)`` AND NOT BY RE-ELABORATING EACH MODULE.  A module with ports has no
settled structure until those ports are wired: an unbound stream endpoint has ``queue_size=None`` until
:meth:`Interface.bind` supplies a depth.  Elaborating a leaf standalone therefore produces a *third*
key belonging to no real design — the trap ``docs/guide/calib/corpus.md`` documents.  So the new keys
come from walking the **assembled design** over the points its corpus covers, exactly as the sweep that
wrote the store did.

WHY THE MAPPING MUST BE ONE-TO-ONE.  Two stored keys claiming one new key means two configurations were
measured that the new signature cannot tell apart — a real loss of resolution, not a merge to perform
quietly.  A stored key claiming none means the design no longer produces that configuration, and its
measurement is about hardware nobody builds.  Either is a stop, because the alternative is a store that
looks migrated and answers for the wrong module.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from waveflow.calib.module_key import walk_modules
from waveflow.calib.record_store import MODULES_SUBDIR, ModuleStore


def _param_key(cls_name: str, params: dict) -> tuple:
    """The identity a measurement is matched on across a signature change.

    Class plus resolved parameters: what the *design* said, independent of how the signature happened
    to be serialized on the day it was written.
    """
    return (cls_name, tuple(sorted((str(k), str(v)) for k, v in params.items())))


@dataclass
class KeyMigration:
    """The old -> new key mapping for one store, and what is wrong with it."""

    mapping: dict = field(default_factory=dict)          #: old key -> new key
    unclaimed_old: list = field(default_factory=list)     #: stored keys no current point produces
    unclaimed_new: list = field(default_factory=list)     #: current keys with no stored measurement
    collisions: dict = field(default_factory=dict)        #: new key -> [old keys], when >1

    @property
    def ok(self) -> bool:
        """Whether the mapping is safe to apply: one-to-one, nothing stranded."""
        return not (self.unclaimed_old or self.collisions)

    def report(self) -> str:
        lines = [f"{len(self.mapping)} key(s) map cleanly"]
        if self.collisions:
            lines.append(f"  {len(self.collisions)} COLLISION(S) — two configurations the new "
                         f"signature cannot distinguish:")
            lines += [f"    {new} <- {olds}" for new, olds in sorted(self.collisions.items())]
        if self.unclaimed_old:
            lines.append(f"  {len(self.unclaimed_old)} stored key(s) no current point produces:")
            lines += [f"    {k}" for k in sorted(self.unclaimed_old)[:8]]
        if self.unclaimed_new:
            lines.append(f"  {len(self.unclaimed_new)} current key(s) with no stored measurement "
                         f"(expected for a composite until its integration term is filed)")
        return "\n".join(lines)


def plan_migration(store: ModuleStore, designs: "list[tuple[type, dict, str]]") -> KeyMigration:
    """Work out how a store's keys move, without touching it.

    *designs* is ``[(top_class, params, name), ...]`` — the points the store's corpus covers.  Each is
    elaborated and walked, so every key is computed the way a real composite computes it.
    """
    current: dict = {}
    for cls, params, name in designs:
        elaborated = _elaborate(cls, params, name)
        for _path, _comp, ident in walk_modules(elaborated):
            current.setdefault(_param_key(ident.cls_name, ident.params), set()).add(ident.key)

    mig = KeyMigration()
    claimed: dict = {}
    for old in store.keys():
        ident = store.get_identity(old)
        if ident is None:
            mig.unclaimed_old.append(old)
            continue
        new_keys = current.get(_param_key(ident.cls_name, ident.params))
        if not new_keys:
            mig.unclaimed_old.append(old)
            continue
        if len(new_keys) > 1:
            # One (class, params) producing several structures is not a migration problem -- it is a
            # design whose structure depends on more than its parameters, which the purity gate exists
            # to prevent.  Refuse rather than pick.
            mig.collisions[sorted(new_keys)[0]] = [old]
            continue
        new = next(iter(new_keys))
        mig.mapping[old] = new
        claimed.setdefault(new, []).append(old)

    for new, olds in claimed.items():
        if len(olds) > 1:
            mig.collisions[new] = sorted(olds)
            for o in olds:
                mig.mapping.pop(o, None)
    mig.unclaimed_new = sorted(
        k for keys in current.values() for k in keys if k not in claimed)
    return mig


def _elaborate(cls, params: dict, name: str):
    from waveflow.build.elaborate import elaborate

    return elaborate(cls, params, name=name)


def apply_migration(store: ModuleStore, mig: KeyMigration, *, dry_run: bool = True) -> list:
    """Rename each module directory to its new key and rewrite the identity inside it.

    Refuses a mapping that is not one-to-one (:attr:`KeyMigration.ok`): a partially-applied
    re-addressing is worse than none, because the store then looks migrated.

    Renames via a temporary name so an old key that is also a *new* key cannot be clobbered mid-run.
    """
    if not mig.ok:
        raise RuntimeError("refusing to apply an unsafe key migration:\n" + mig.report())

    root = Path(store.root)
    moved = []
    staged: list = []
    for old, new in sorted(mig.mapping.items()):
        if old == new:
            continue
        src = root / old
        if not src.is_dir():
            continue
        tmp = root / f".migrating-{new}"
        staged.append((tmp, root / new, old, new))
        if not dry_run:
            shutil.move(str(src), str(tmp))
    for tmp, dst, old, new in staged:
        if not dry_run:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(tmp), str(dst))
            _rewrite_identity(dst, new)
        moved.append((old, new))
    return moved


def _rewrite_identity(module_dir: Path, new_key: str) -> None:
    """Point ``module.json`` at the new key.

    The stored ``signature`` is deliberately **left alone**: it is the digest of the structure as it
    was measured, and rewriting it would assert a provenance this migration cannot verify.  Read-time
    verification compares it against the identity it was read for, so a genuinely different module
    still raises.
    """
    path = module_dir / "module.json"
    if not path.is_file():
        return
    blob = json.loads(path.read_text(encoding="utf-8"))
    blob["key"] = new_key
    path.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")


def store_root(platform_dir: "str | Path") -> Path:
    return Path(platform_dir) / MODULES_SUBDIR


__all__ = ["KeyMigration", "plan_migration", "apply_migration", "store_root"]
