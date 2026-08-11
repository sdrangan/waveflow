"""Shared introspection CLI runner for ``BuildDag`` examples.

Each example's ``main()`` was a copy of the same boilerplate: the
``--through`` / ``--list-steps`` / ``--list-steps-verbose`` / ``--list-artifacts``
/ ``--status`` / ``--force`` / ``--force-step`` flags plus the step-progress
callbacks, all built on the ``BuildDag`` introspection API. :func:`run_dag_cli`
extracts that once; an example supplies its DAG factory, root directory, default
``--through`` target, and its own knobs (the test vector / parameters) via
``extra_args`` + ``params_from_args``.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from textwrap import indent
from typing import Callable, Iterable

from waveflow.build.build import BuildConfig, BuildDag


def run_dag_cli(
    dag_factory: Callable[[], BuildDag],
    *,
    description: str,
    default_through: str,
    root_dir: Path,
    extra_args: Iterable[tuple[tuple, dict]] = (),
    params_from_args: Callable[[argparse.Namespace], dict] | None = None,
    config_from_args: Callable[[argparse.Namespace], dict] | None = None,
) -> None:
    """Run an example's ``BuildDag`` with the standard introspection CLI.

    Parameters
    ----------
    dag_factory:
        Zero-arg callable returning the assembled :class:`BuildDag`.
    description:
        ``argparse`` description (the example's one-liner).
    default_through:
        Default ``--through`` target when none is given.
    root_dir:
        Build root for the :class:`BuildConfig`.
    extra_args:
        Iterable of ``(flags_tuple, kwargs_dict)`` added to the parser between the
        generic listing flags and ``--force`` — the example-specific knobs.
    params_from_args:
        Maps the parsed ``argparse`` namespace to the ``BuildConfig`` params dict.
    config_from_args:
        Maps the namespace to **non-param** :class:`BuildConfig` fields — ``platform``,
        ``platforms_root``, ``part``, ``clk_freq``.  Return ``{}`` for an uncalibrated build.

        Params alone could not express this: a platform is not a knob the steps read, it is where a
        calibrated build *files what it measured*.  Without this hook no example CLI could target a
        platform at all, so a single point of a calibration sweep could only be run by the sweep.
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--through", metavar="STEP", default=default_through,
                        help="Run the DAG up to and including this step.")
    parser.add_argument("--list-steps", action="store_true",
                        help="Print all step names in execution order and exit.")
    parser.add_argument("--list-steps-verbose", action="store_true",
                        help="Print step names with descriptions and artifacts.")
    parser.add_argument("--list-artifacts", action="store_true",
                        help="Print all artifacts with their producer and file path.")
    parser.add_argument("--status", action="store_true",
                        help="Print pre-build freshness status and exit.")
    for flags, kwargs in extra_args:
        parser.add_argument(*flags, **kwargs)
    parser.add_argument("--force", action="store_true",
                        help="Force all steps to rebuild.")
    parser.add_argument("--force-step", metavar="STEP", action="append", default=[],
                        help="Force a specific step to rebuild (repeatable).")
    args = parser.parse_args()

    dag = dag_factory()

    if args.list_steps:
        for name in dag.step_names():
            print(name)
        return

    if args.list_steps_verbose:
        for step in dag.steps():
            consumes = ", ".join(step.consumes) if step.consumes else "(none)"
            produces = ", ".join(step.produces) if step.produces else "(none)"
            print(f"{step.name}")
            if step.description:
                print(f"    {step.description}")
            print(f"    consumes: {consumes}")
            print(f"    produces: {produces}")
        return

    config = BuildConfig(
        root_dir=Path(root_dir),
        params=(params_from_args(args) if params_from_args else {}),
        **(config_from_args(args) if config_from_args else {}),
    )

    if args.list_artifacts:
        all_paths = dag.artifact_paths(config)
        for artifact, step_name in dag.artifact_owners().items():
            p = all_paths.get(artifact)
            if p is not None:
                try:
                    display = p.relative_to(config.root_dir)
                except ValueError:
                    display = p
                print(f"  {artifact:<24} {step_name:<26} {display}")
            else:
                print(f"  {artifact:<24} {step_name:<26} (object)")
        return

    if args.status:
        now = time.time()
        for entry in dag.results_status(config):
            age = f"{(now - entry['mtime']) / 3600:.1f}h ago" if entry["mtime"] else "—"
            exists_mark = "✓" if entry["exists"] else "✗"
            stale_note = (f"  STALE ({', '.join(entry['stale_because'])} newer)"
                          if entry["stale"] else "")
            print(f"  {entry['artifact']:<16} {entry['produced_by']:<22} "
                  f"{exists_mark}  {age:<12}{stale_note}")
        return

    force: bool | list[str] = True if args.force else (args.force_step or False)

    def on_step_begin(step, will_run, paths):
        print(f"{step.name}:")
        for artifact, p in paths.items():
            try:
                display = p.relative_to(config.root_dir)
            except ValueError:
                display = p
            print(f"    {display}")
        if will_run:
            print("    RUNNING...")

    def on_step_end(step, result):
        if not result.success:
            print(f"    FAILED: {result.message}")
            # The message is str(exc); for a toolchain error that is often a bare string with no
            # file or line.  Print the traceback too so the failure is actionable.
            if result.traceback:
                print(indent(result.traceback.rstrip(), "    "))
        elif result.skipped:
            print("    UP-TO-DATE")
        else:
            print("    PASSED")

    dag.run(config, through=args.through, force=force,
            on_step_begin=on_step_begin, on_step_end=on_step_end)
