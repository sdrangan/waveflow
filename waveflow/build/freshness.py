"""freshness.py — when a system's steps have nothing to do (``plans/unit_harness.md`` Stage 6).

A system's steps (scenario, pysim, the RTL run, compare, a calibration) used to run every time: each
reads Python and C++ the DAG cannot see.  Stage 6 asks the opposite of a unit nobody changed -- **no
work** -- so each step of a system is fresh while one digest of everything its run depends on is the
one it last ran with (:func:`system_digest`):

* the DUT's **structure** (``structure_signature`` of its root) and the testbench participants'
  init-time knobs (their ``DynParam``\\ s: bundles, policies);
* the **source** of every class in the system that is not the framework's, and the framework itself
  (every ``waveflow/`` source: a framework change re-verifies everything);
* the **scenario** (``scenario_digest``: the bytes both runs read);
* the **RTL** of the system's HLS tops (their csynth source stamps);
* for a step whose result they change (``uses_records``: pysim, whose timing they are), the
  **calibration records** its modules load -- each module's own, by its identity, so a changed
  generator record leaves the core's harness fresh, and makes the pysim of every system holding the
  generator stale: a parent's estimate follows its units' records.  The RTL run does not read them.

The static part is computed once per system object, before it runs (a run changes counters a
signature would see); the RTL stamps and the records are read each time, since a csynth or a
calibration earlier in the same build changes them.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

_FRAMEWORK: str | None = None
_SUFFIXES = (".py", ".h", ".hpp", ".v", ".tcl", ".cpp", ".bat", ".sh")


def framework_digest() -> str:
    """Every source file of the ``waveflow`` package, hashed once per process."""
    global _FRAMEWORK
    if _FRAMEWORK is None:
        import waveflow

        root = Path(waveflow.__file__).resolve().parent
        h = hashlib.sha256()
        for p in sorted(root.rglob("*")):
            if p.suffix in _SUFFIXES and p.is_file() and "__pycache__" not in p.parts:
                h.update(p.relative_to(root).as_posix().encode())
                h.update(p.read_bytes())
        _FRAMEWORK = h.hexdigest()
    return _FRAMEWORK


def _class_sources(objs) -> str:
    """The source files of every class (and base) of *objs* outside the framework and the standard
    library, hashed."""
    import waveflow

    fw = Path(waveflow.__file__).resolve().parent
    files: set[Path] = set()
    for o in objs:
        for cls in type(o).__mro__:
            try:
                f = inspect.getsourcefile(cls)
            except (TypeError, OSError):
                continue
            if not f:
                continue
            p = Path(f).resolve()
            if fw in p.parents or "site-packages" in p.parts or "lib" == p.parent.name.lower():
                continue
            files.add(p)
    h = hashlib.sha256()
    for p in sorted(files):
        h.update(p.as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def _static_digest(sysm) -> str:
    """The part of :func:`system_digest` a run cannot change, once per system object."""
    cached = getattr(sysm, "_wf_static_digest", None)
    if cached is not None:
        return cached
    from waveflow.build.elaborate import structure_signature
    from waveflow.calib.phases import scenario_digest
    from waveflow.hw.hw_module import discover_dyn_params

    dut = sysm.dut()
    elems = list(sysm.elements())
    # Only participants that carry a knob: an endpoint left unnamed takes a process-wide counter's
    # name, which would make two builds of one system differ.
    knobs = sorted((o.name, sorted((k, repr(v)) for k, v in discover_dyn_params(o).items()))
                   for o in elems if not _inside(o, dut) and discover_dyn_params(o))
    h = hashlib.sha256()
    for part in (framework_digest(), _class_sources(elems), repr(structure_signature(dut)),
                 repr(knobs), scenario_digest(sysm)):
        h.update(part.encode())
        h.update(b"\0")
    sysm._wf_static_digest = h.hexdigest()
    return sysm._wf_static_digest


def _inside(mod, root) -> bool:
    while mod is not None:
        if mod is root:
            return True
        mod = getattr(mod, "parent", None)
    return False


def _records_digest(sysm) -> str:
    """The calibration records the system's modules load -- each module's own, by identity.  A
    calibrating system (``sysm._wf_calibrating``) leaves them out: it writes them."""
    if getattr(sysm, "_wf_calibrating", False):
        return ""
    from waveflow.calib.record_store import ModuleStore
    from waveflow.hw.memory import MemoryMod

    out = []
    todo = [sysm.dut()]
    while todo:
        m = todo.pop(0)
        todo.extend(getattr(m, "sub_comps", {}).values())
        phases = getattr(m, "timing_phases", None) or {}
        dirs = {ph.calib_dir for ph in phases.values() if ph.calib_dir is not None}
        if isinstance(m, MemoryMod) and m.calib_dir is not None:
            dirs.add(Path(m.calib_dir))
        for d in sorted(dirs, key=str):
            if not (Path(d) / "modules").is_dir():
                continue
            store = ModuleStore(d)
            if phases:
                from waveflow.calib.phases import stored_phases
                rec = stored_phases(store, m)
            else:
                from waveflow.calib.mem_timing import stored_mem_timing
                rec = stored_mem_timing(store, m)
            out.append((m.name, None if rec is None else json.dumps(rec.payload, sort_keys=True)))
    return hashlib.sha256(repr(sorted(out, key=lambda x: x[0])).encode()).hexdigest()


def system_digest(sysm, root, *, records: bool = True, rtl: bool = True) -> str:
    """What a system's run depends on, as one digest (see the module docstring); *records*: with the
    calibration records its modules load; *rtl*: with its tops' RTL stamps (a step that reads no RTL
    -- the scenario, pysim -- leaves them out)."""
    from waveflow.build.rtl_digest import read_stamp

    stamps = [(t, read_stamp(root, t)) for t in getattr(sysm, "_wf_tops", ())] if rtl else []
    h = hashlib.sha256()
    for part in (_static_digest(sysm), repr(sorted(stamps, key=lambda x: x[0])),
                 _records_digest(sysm) if records else ""):
        h.update(part.encode())
        h.update(b"\0")
    return h.hexdigest()


def stamp_file(work: Path, step_name: str) -> Path:
    """Where a system step records the digest it ran with."""
    return Path(work) / ".wf_fresh" / f"{step_name}.txt"


__all__ = ["framework_digest", "stamp_file", "system_digest"]
