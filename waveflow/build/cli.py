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
    dag_factory: Callable[[], BuildDag] | Callable[[argparse.Namespace], BuildDag],
    *,
    description: str,
    default_through: str | None,
    root_dir: Path,
    extra_args: Iterable[tuple[tuple, dict]] = (),
    params_from_args: Callable[[argparse.Namespace], dict] | None = None,
    config_from_args: Callable[[argparse.Namespace], dict] | None = None,
) -> None:
    """Run an example's ``BuildDag`` with the standard introspection CLI.

    Parameters
    ----------
    dag_factory:
        Callable returning the assembled :class:`BuildDag`: zero-arg, or taking the parsed
        ``argparse`` namespace when the DAG's *shape* depends on an ``extra_args`` knob (e.g. a
        ``--probes`` flag that adds timing probes to a system top).
    description:
        ``argparse`` description (the example's one-liner).
    default_through:
        Default ``--through`` target when none is given; ``None`` runs the whole DAG (a DAG with
        more than one sink, e.g. two systems' ``compare`` steps).
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

    import inspect
    dag = dag_factory(args) if inspect.signature(dag_factory).parameters else dag_factory()

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
            if not entry["stale"]:
                stale_note = ""
            elif not entry["exists"]:
                stale_note = "  STALE (missing)"
            elif entry["stale_because"]:
                stale_note = f"  STALE ({', '.join(entry['stale_because'])} newer)"
            else:                                   # the step's is_fresh hook said so
                stale_note = "  STALE (the step's own check)"
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
        # The elapsed time is on the status line so a log -- or a blind test's transcript of
        # one -- says where a build's wall clock went, step by step (`csynth` vs `system_xsi`).
        took = f"({result.elapsed_seconds:.1f} s)"
        _log_step_time(config.root_dir, step.name, result)
        if not result.success:
            print(f"    FAILED {took}: {result.message}")
            # The message is str(exc); for a toolchain error that is often a bare string with no
            # file or line.  Print the traceback too so the failure is actionable.
            if result.traceback:
                print(indent(result.traceback.rstrip(), "    "))
        elif result.skipped:
            print("    UP-TO-DATE")
        else:
            print(f"    PASSED {took}")

    dag.run(config, through=args.through, force=force,
            on_step_begin=on_step_begin, on_step_end=on_step_end)


#: Where :func:`run_dag_cli` appends one line per step it ran: name, start, end, outcome.
#: Read by ``waveflow blind-test``'s summary to split a build's wall clock into synthesis,
#: RTL simulation and pysim; harmless anywhere else.
STEP_LOG = Path(".waveflow") / "build_steps.jsonl"


def _log_step_time(root_dir, name: str, result) -> None:
    """Append one step's timing to ``<root>/.waveflow/build_steps.jsonl``; never fail a build."""
    if result.skipped:
        return
    import json

    end = time.time()
    try:
        path = Path(root_dir) / STEP_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"step": name, "start": end - result.elapsed_seconds, "end": end,
                                "success": bool(result.success)}) + "\n")
    except OSError:
        pass
