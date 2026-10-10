"""xsi_snapshot.py — an incremental XSI build, and cheap re-runs of what it built.

An XSI run is four phases (``waveflow.events`` times each): compile the RTL (``xvlog``), elaborate
it into a design library (``xelab -dll``), compile the testbench (``g++``), simulate.  On the
examples the first three are 20-30 s and the last is a fraction of a second, and ``run.bat`` /
``run.sh`` with no verb redo all four on every call.  :class:`XsiSnapshot` builds only what
changed and runs what is built (``plans/incremental_xsi.md``)::

    snap = XsiSnapshot(xsi_dir, top="mem_copy", tb="mem_copy_bfm_tb")
    snap.build()                          # compile + elaborate + testbench, or nothing if unchanged
    out = snap.run(vectors_dir=xsi_dir / "runs" / "p3")   # ~0.1 s + the simulated cycles

**Skipped by content, never by timestamp.**  After a phase succeeds, a stamp records a SHA-256 of
every input it read: for the design, the runner script, ``rtl_<top>.f`` and each file it lists
(including the files of every ``--include`` directory) and, traced, the VCD dumper; for the
testbench, the runner script, ``<tb>.cpp``, ``xsi_loader.cpp`` and every quoted header either
includes, transitively, plus ``WF_TB_CXXFLAGS``.  A phase is skipped only when its stamp matches
and its output exists.  Timestamps are not enough here: stale RTL has passed for current in this
repository more than once (``rtl_staleness`` in :mod:`waveflow.build.rtl_digest` exists for that).

Each stamp lives beside what it vouches for, and the runner deletes both before rebuilding:

* the design's in ``xsim.dir/<snapshot>/wf_stamp.json`` -- removing the snapshot (as the ``-m xsi``
  gates do, to force a clean build) removes its stamp with it;
* the testbench's in ``<tb>.wf_stamp.json``, which the runner's ``tb`` phase deletes with the
  binary.

So a by-hand ``run.bat`` that rebuilt from other inputs cannot leave a stamp that still matches the
old ones.

**Traced builds are their own snapshot** (``<top>_trace``), so a traced and an untraced build of one
top coexist.  The testbench binary is shared: the runner points it at the traced design through
``WF_XSI_DESIGN`` (``xsi_bfm.h``).

**A run's vectors directory.**  ``run(vectors_dir=...)`` makes the testbench read and write its
``vectors/...`` bundles there instead (``WF_VECTORS_DIR``, ``xsi_bundle.h``), and puts the run's
waveform database there too.  Two runs of one snapshot then do not overwrite each other's outputs;
build once, then run them in parallel with ``build=False``.  A traced run's ``<top>_trace.vcd`` is
still written into the workspace -- its name is fixed in the dumper -- so traced runs share it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from waveflow.build.trace_steps import run_xsi, xsi_runner_cmd, xsi_runner_name


class XsiRunError(RuntimeError):
    """The XSI run did not complete (compile, elaborate, or the testbench exited non-zero)."""


#: ``#include "foo.h"`` -- quoted form only; an angle-bracket include is a system header.
_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.M)

#: The ``.f`` options whose next token is an include DIRECTORY.
_INCDIR_OPTS = ("--include", "-i")


def _elab_only_failed_cleanup(out: str) -> bool:
    """xelab built the library and then failed only to delete its scratch ``obj/`` directory.

    Seen on Windows, twice in one ``-m xsi`` session (``bo_top``, ``mm_fir_top``): ``Built XSI
    simulation shared library ...`` followed by ``Could not remove the obj directory: ... being used
    by another process`` and exit status 1 -- a freshly written ``xsim_N.c`` still held open
    (an indexer or scanner).  The library is complete; a real elaboration error prints ``ERROR:``.
    The runner ignored xelab's status before this module read it, so this was always happening.
    """
    return ("Built XSI simulation shared library" in out and "Could not remove the obj directory" in out
            and not re.search(r"^ERROR:", out, re.M))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(files: dict[str, str]) -> str:
    """One hash over a ``{name: sha}`` map, independent of insertion order."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(f"{name}\0{files[name]}\n".encode())
    return h.hexdigest()


@dataclass
class XsiSnapshot:
    """One elaborated design (*top*, traced or not) and one testbench (*tb*) in *work_dir*.

    *tb_cxxflags* are extra flags for compiling the testbench (``WF_TB_CXXFLAGS``); they are part of
    the testbench's stamp.
    """

    work_dir: Path
    top: str
    tb: str
    trace: bool = False
    tb_cxxflags: str = ""

    def __post_init__(self) -> None:
        self.work_dir = Path(self.work_dir)

    # ---- names ------------------------------------------------------------------------------

    @property
    def snapshot(self) -> str:
        """The ``xelab -s`` name: ``<top>``, or ``<top>_trace`` for the traced elaboration."""
        return f"{self.top}_trace" if self.trace else self.top

    @property
    def design_dir(self) -> Path:
        return self.work_dir / "xsim.dir" / self.snapshot

    @property
    def design_lib(self) -> Path:
        return self.design_dir / f"xsimk.{'dll' if os.name == 'nt' else 'so'}"

    @property
    def tb_binary(self) -> Path:
        return self.work_dir / f"{self.tb}.{'exe' if os.name == 'nt' else 'bin'}"

    @property
    def rtl_stamp(self) -> Path:
        return self.design_dir / "wf_stamp.json"

    @property
    def tb_stamp(self) -> Path:
        return self.work_dir / f"{self.tb}.wf_stamp.json"

    # ---- what each phase reads ------------------------------------------------------------------

    def rtl_inputs(self) -> dict[str, str] | None:
        """``{file: sha256}`` of every input to compile + elaborate, or None if one is missing
        (then nothing can vouch for the design, and it is rebuilt -- where xvlog reports it)."""
        wd = self.work_dir
        files: dict[str, str] = {}
        runner = wd / xsi_runner_name()
        flist = wd / f"rtl_{self.top}.f"
        if not runner.is_file() or not flist.is_file():
            return None
        files[runner.name] = _sha(runner)
        files[flist.name] = _sha(flist)
        for line in flist.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("//", "#")):
                continue
            toks = shlex.split(line, posix=True)
            i = 0
            while i < len(toks):
                tok = toks[i]
                if tok in _INCDIR_OPTS and i + 1 < len(toks):
                    d = (wd / toks[i + 1]).resolve()
                    if not d.is_dir():
                        return None
                    for f in sorted(p for p in d.iterdir() if p.is_file()):
                        files[f.as_posix()] = _sha(f)
                    i += 2
                    continue
                if not tok.startswith("-"):
                    f = (wd / tok).resolve()
                    if not f.is_file():
                        return None
                    files[f.as_posix()] = _sha(f)
                i += 1
        if self.trace:
            dumper = wd / f"vcd_dumper_{self.top}.v"
            if not dumper.is_file():
                return None
            files[dumper.name] = _sha(dumper)
        return files

    def tb_inputs(self) -> dict[str, str] | None:
        """``{file: sha256}`` of every input to the testbench build, or None if its source is
        missing.  Quoted includes are followed transitively; one found nowhere (``xsi.h``, a Vitis
        header behind ``-I``) is a toolchain header and is recorded by name."""
        wd = self.work_dir
        runner = wd / xsi_runner_name()
        roots = [wd / f"{self.tb}.cpp", wd / "xsi_loader.cpp"]
        if not runner.is_file() or not all(r.is_file() for r in roots):
            return None
        inc_dirs = [Path(m) for m in re.findall(r'-I"?([^"\s]+)"?', self.tb_cxxflags)]
        files: dict[str, str] = {runner.name: _sha(runner),
                                 "WF_TB_CXXFLAGS": hashlib.sha256(self.tb_cxxflags.encode()).hexdigest()}
        todo = list(roots)
        while todo:
            f = todo.pop()
            key = f.resolve().as_posix()
            if key in files:
                continue
            files[key] = _sha(f)
            for inc in _INCLUDE.findall(f.read_text(encoding="utf-8", errors="replace")):
                for d in [f.parent, wd, *inc_dirs]:
                    cand = d / inc
                    if cand.is_file():
                        todo.append(cand)
                        break
                else:
                    files[f"<{inc}>"] = "toolchain"
        return files

    # ---- staleness ------------------------------------------------------------------------------

    @staticmethod
    def _stamp_matches(stamp: Path, inputs: dict[str, str] | None, output: Path) -> bool:
        if inputs is None or not output.is_file() or not stamp.is_file():
            return False
        try:
            return json.loads(stamp.read_text(encoding="utf-8")).get("digest") == _digest(inputs)
        except (OSError, ValueError):
            return False

    def stale(self) -> list[str]:
        """The phases a :meth:`build` would run: a subset of ``["rtl", "tb"]``."""
        out = []
        if not self._stamp_matches(self.rtl_stamp, self.rtl_inputs(), self.design_lib):
            out.append("rtl")
        if not self._stamp_matches(self.tb_stamp, self.tb_inputs(), self.tb_binary):
            out.append("tb")
        return out

    # ---- build and run --------------------------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.tb_cxxflags:
            env["WF_TB_CXXFLAGS"] = self.tb_cxxflags
        else:
            env.pop("WF_TB_CXXFLAGS", None)
        return env

    def _invoke(self, verb: str, timeout: int, vectors_dir: str | None = None) -> str:
        r = run_xsi(xsi_runner_cmd(self.top, self.tb, trace=self.trace, verb=verb,
                                   vectors_dir=vectors_dir),
                    cwd=self.work_dir, name=f"xsi {verb}", capture_output=True, text=True,
                    timeout=timeout, env=self._env())
        return (r.stdout or "") + (r.stderr or "")

    def build(self, *, force: bool = False, timeout: int = 1800) -> list[str]:
        """Rebuild the stale phases (all of them with *force*); returns the phases it ran.

        Raises :class:`XsiRunError` if a phase failed; its stamp is then not written, so the next
        build retries it.
        """
        todo = ["rtl", "tb"] if force else self.stale()
        if not todo:
            return []
        rtl_in = self.rtl_inputs() if "rtl" in todo else None
        tb_in = self.tb_inputs() if "tb" in todo else None
        out = self._invoke("build" if len(todo) == 2 else todo[0], timeout)
        if "rtl" in todo:
            ok = "xvlog errorlevel=0" in out and self.design_lib.is_file() and (
                "xelab errorlevel=0" in out or _elab_only_failed_cleanup(out))
            if not ok:
                raise XsiRunError(f"{self.snapshot}: compile/elaborate failed:\n{out[-4000:]}")
            # Stamp what was read only if it is still what is on disk: an input edited while the
            # build ran must not be vouched for by a design built from its previous content.
            if rtl_in is not None and rtl_in == self.rtl_inputs():
                self.rtl_stamp.write_text(json.dumps({"digest": _digest(rtl_in), "inputs": rtl_in},
                                                     indent=1) + "\n", encoding="utf-8")
        if "tb" in todo:
            if "gpp errorlevel=0" not in out or not self.tb_binary.is_file():
                raise XsiRunError(f"{self.tb}: testbench build failed:\n{out[-4000:]}")
            if tb_in is not None and tb_in == self.tb_inputs():
                self.tb_stamp.write_text(json.dumps({"digest": _digest(tb_in), "inputs": tb_in},
                                                    indent=1) + "\n", encoding="utf-8")
        return todo

    def run(self, vectors_dir: str | os.PathLike[str] | None = None, *, build: bool = True,
            check: bool = True, timeout: int = 1800) -> str:
        """Run the testbench (building first, incrementally, unless *build* is False).

        *vectors_dir*, when given, replaces the testbench's ``vectors/`` for this run: its inputs
        are read from it and its outputs written to it.  Returns the run's output; with *check*,
        raises :class:`XsiRunError` unless it printed ``XSI_EXITCODE=0``.
        """
        if build:
            self.build(timeout=timeout)
        vd = None
        if vectors_dir is not None:
            p = Path(vectors_dir).resolve()          # relative = to the caller's cwd, as usual
            p.mkdir(parents=True, exist_ok=True)
            vd = p.as_posix()
        out = self._invoke("run", timeout, vd)
        if check and "XSI_EXITCODE=0" not in out:
            raise XsiRunError(f"{self.top}: XSI run did not complete cleanly:\n{out[-4000:]}")
        return out


__all__ = ["XsiRunError", "XsiSnapshot"]
