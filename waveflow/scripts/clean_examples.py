"""Remove regenerable Vitis/Vivado HLS build artifacts under examples/.

Cross-platform (Windows + Unix). Dry-run by default — pass --force to delete.

This targets *known artifact directory names* rather than "everything git-ignored"
(`git clean -fdx`). The git approach is hazardous here because some .gitignore rules
ignore whole example directories, which would sweep up hand-written source files that
happen to be uncommitted (e.g. examples/vecunit/vecunit.py). Matching artifact dir
names keeps the cleanup intent-revealing and leaves source alone.
"""

import argparse
import re
import shutil
from pathlib import Path

# Directory names (matched on the leaf name) that are pure regenerable build output.
ARTIFACT_DIR_PATTERNS = [
    r".*_proj$",       # Vitis HLS project trees (the bulk of the footprint)
    r"solution\d*$",   # HLS solutions
    r"__pycache__$",   # Python bytecode cache
    r"\.Xil$",         # Vivado scratch
    r"\.autopilot$",   # Vitis HLS scratch
    r"\.ipcache$",     # Vivado IP cache
]
_ARTIFACT_RE = re.compile("|".join(f"(?:{p})" for p in ARTIFACT_DIR_PATTERNS))


def find_repo_root(start: Path) -> Path:
    """Walk upward from `start` until a directory containing pyproject.toml is found."""
    for parent in [start, *start.parents]:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise SystemExit("Could not locate repo root (no pyproject.toml found above cwd).")


def dir_size_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def find_artifact_dirs(examples_root: Path) -> list[Path]:
    """Top-level artifact dirs (don't descend into an already-matched dir)."""
    matches: list[Path] = []
    for path in sorted(examples_root.rglob("*")):
        if not path.is_dir():
            continue
        # Skip anything already inside a matched artifact dir.
        if any(path.is_relative_to(m) for m in matches):
            continue
        if _ARTIFACT_RE.fullmatch(path.name):
            matches.append(path)
    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force", action="store_true",
        help="Actually delete. Without this flag, only prints what would be removed.",
    )
    parser.add_argument(
        "--examples-dir", default=None,
        help="Path to the examples/ directory (default: <repo-root>/examples).",
    )
    args = parser.parse_args()

    repo_root = find_repo_root(Path.cwd().resolve())
    examples_root = (
        Path(args.examples_dir).resolve() if args.examples_dir
        else repo_root / "examples"
    )
    if not examples_root.is_dir():
        raise SystemExit(f"No examples directory at {examples_root}")

    targets = find_artifact_dirs(examples_root)
    if not targets:
        print(f"Nothing to clean under {examples_root}")
        return

    total = 0
    for d in targets:
        size = dir_size_bytes(d)
        total += size
        print(f"{size / 1024 / 1024:8.1f} MB  {d.relative_to(repo_root)}")

    print(f"\n{len(targets)} dirs, {total / 1024 / 1024:.1f} MB "
          f"({total / 1024 / 1024 / 1024:.2f} GB) total")

    if not args.force:
        print("\nDry run. Re-run with --force to delete.")
        return

    removed = 0
    for d in targets:
        try:
            shutil.rmtree(d)
            removed += 1
        except OSError as e:
            print(f"  ! failed to remove {d}: {e}")
    print(f"\nRemoved {removed}/{len(targets)} dirs.")


if __name__ == "__main__":
    main()
