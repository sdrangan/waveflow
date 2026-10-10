"""``waveflow blind-test``: run a fresh Claude Code agent on a spec, unattended.

The question a blind test answers is *what does an assistant that has never
seen Waveflow do, given only a spec and the MCP server?*  So everything the
student would not have is kept out:

* the agent starts in a fresh folder holding only the spec (plus the files the
  spec links to, such as ``frame.md``), outside any tree with a ``CLAUDE.md``;
* ``--strict-mcp-config`` gives it the Waveflow server and nothing else;
* ``--setting-sources project`` drops the operator's saved permissions,
  plugins and settings; the fresh folder has none of its own;
* ``--permission-prompts none`` denies anything outside the allowlist instead
  of waiting for a person, and every denial is reported.

``claude -p`` runs the agent to completion and exits, so there is nothing to
poll.  Its stream is echoed as it arrives (the agent's text, each tool call
with its key argument, a line per result); ``--silent`` turns that off.

A spec that says "stop for review after Stage 1" ends the first run;
``--approve`` then resumes the same session with a fixed reply, as many times
as ``--rounds`` says.

Nothing the agent writes goes into the run log, and nothing the run log holds
is inside the trial folder: the log lives beside it, in ``<folder>.blindtest/``,
so the agent cannot read its own transcript.  ``summary.md`` there is the
first thing to read afterwards: which Waveflow tools it called and in what
order, what it read outside its folder (a read of the repo's ``plans/`` is a
leak a student could not have), what it wrote, what was denied, and the model and tokens it used.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

__all__ = ["run_blind_test", "summarize", "DEFAULT_ALLOWED_TOOLS"]

#: What a student would reasonably click "allow" for: the Waveflow tools, file
#: work, and the commands a Waveflow build runs.  Anything else is denied and
#: shows up in the summary, which is itself a finding.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__waveflow",
    # The only way to wait on a long background run (csynth, cosim) in -p
    # mode -- HARNESS_NOTE tells the agent to use it.  It was missing from the
    # first rotate run's list, so the wait was denied and the csim killed.
    "Monitor",
    "Read",
    "Glob",
    "Grep",
    "Edit",
    "Write",
    "Bash(python:*)",
    "Bash(python3:*)",
    "Bash(waveflow:*)",
    "Bash(pytest:*)",
    "Bash(vitis-run:*)",
    "Bash(vitis_hls:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(diff:*)",
    "Bash(find:*)",
    "Bash(grep:*)",
    "Bash(mkdir:*)",
    "Bash(cp:*)",
    "Bash(mv:*)",
    # Looking around.  An agent's first move is to check its environment
    # (which python, where Vitis is); a student clicks "allow" on that, so a
    # denial here says nothing about the tools and only costs the agent turns.
    "Bash(which:*)",
    "Bash(echo:*)",
    "Bash(pwd:*)",
    "Bash(env:*)",
    "Bash(printenv:*)",
    "Bash(test:*)",
    "Bash(sort:*)",
    "Bash(uniq:*)",
    # The same, for the PowerShell tool Claude Code uses on Windows.  A
    # chained command is allowed only if every part is, so PowerShell needs
    # its own python / waveflow rules, not just the Bash ones.
    "PowerShell(python:*)",
    "PowerShell(waveflow:*)",
    "PowerShell(pytest:*)",
    "PowerShell(vitis-run:*)",
    "PowerShell(Get-ChildItem:*)",
    "PowerShell(Get-Command:*)",
    "PowerShell(Get-Content:*)",
    "PowerShell(Get-Location:*)",
    "PowerShell(Test-Path:*)",
    "PowerShell(Resolve-Path:*)",
    "PowerShell(Select-String:*)",
    "PowerShell(Select-Object:*)",
    "PowerShell(Measure-Object:*)",
    "PowerShell(Write-Output:*)",
    "PowerShell(New-Item:*)",
    "PowerShell(Copy-Item:*)",
    "PowerShell(Move-Item:*)",
)

#: The no-Waveflow arm (``--no-waveflow``): the same harness and allowlist, less every
#: Waveflow entry, so the two arms differ only in Waveflow.  The plan's Stage 0 baseline.
#: The arm is chosen here, in the first message, not in the spec: both arms read the SAME
#: spec file (``examples/mcp_test/rotate_func.md``), which names no tooling.
def baseline_allowed(allowed) -> list[str]:
    return [a for a in allowed if "waveflow" not in a.lower()]


#: Names no frame and no example: choosing the architecture from the spec is part of
#: what a blind test measures (plans/mcp_frames.md).  A spec that wants a particular
#: one names it itself; ``--message`` can still name one for a run.
WAVEFLOW_FIRST = (
    "Build the accelerator specified in {spec}, in this folder, with Waveflow: its MCP "
    "server is available."
)

NO_WAVEFLOW_FIRST = (
    "Build the accelerator specified in {spec}, in this folder. Use Vitis HLS directly: "
    "write the kernel and its testbench in C++, and use Python with numpy for models, test "
    "vectors and analysis."
)

_MD_LINK = re.compile(r"\]\(([^)#\s]+\.md)\)")

#: Tools that only make sense with a person or a scheduler behind the session.
#: ScheduleWakeup was the measured failure: in ``-p`` mode ending the turn ends
#: the process, the wakeup never fires, and the background csim it was
#: waiting on was killed.
DISALLOWED_TOOLS: tuple[str, ...] = ("ScheduleWakeup", "CronCreate", "CronDelete", "CronList")

#: What the agent is told about its *environment* -- nothing about Waveflow.
#: Measured in ``-p`` mode (Claude Code 2.1.285): a pending Monitor keeps the
#: session alive and wakes the agent when it fires; a bare background command
#: is killed the moment the turn ends.
HARNESS_NOTE = (
    "This session is non-interactive. Nobody will answer questions or approve "
    "anything, and the session ends as soon as you end your turn with nothing "
    "pending. A command running in the background is killed when your turn ends "
    "unless a Monitor is watching it. For a command that may take longer than the "
    "command timeout (synthesis, co-simulation), run it in the background and wait "
    "for it with Monitor, repeating the Monitor if it expires, before you end your "
    "turn. ScheduleWakeup is not available."
)

def _vitis_bin() -> Path | None:
    """The Vitis ``bin`` directory, or None when Vitis is not installed."""
    try:
        from waveflow.toolchain.toolchain import find_vitis_path

        exe = find_vitis_path()
    except Exception:
        return None
    return Path(exe).parent if exe else None


def _vivado_bin() -> Path | None:
    """The Vivado ``bin`` directory (``xvlog``, ``xelab``, ``xsim``), or None."""
    try:
        from waveflow.toolchain.toolchain import find_vivado_path

        exe = find_vivado_path()
    except Exception:
        return None
    return Path(exe).parent if exe else None


def vitis_allowed() -> list[str]:
    """Allow ``vitis-run`` however the agent spells it.

    The agent of the first no-Waveflow rotate run (2026-10-01) found Vitis off PATH,
    called it by full path, and was denied every time: a rule matches a command's
    *prefix*, and ``Bash(vitis-run:*)`` is not a prefix of
    ``/c/Xilinx/2025.1/Vitis/bin/vitis-run.bat``.  The Waveflow arm never noticed,
    because its builds start Vitis from ``python``.
    """
    rules = []
    for b, tools in ((_vitis_bin(), _VITIS_TOOLS), (_vivado_bin(), _VIVADO_TOOLS)):
        if b is None:
            continue
        for tool in tools:
            for name in (tool, tool + ".bat"):
                win = str(b / name)
                fwd = win.replace("\\", "/")
                drive = fwd[0].lower()
                msys = f"/{drive}{fwd[2:]}" if fwd[1:2] == ":" else fwd
                for spelled in {win, fwd, msys, name}:
                    rules += [f"Bash({spelled}:*)", f"PowerShell({spelled}:*)"]
    return sorted(set(rules))


_VITIS_TOOLS = ("vitis-run", "vitis_hls")
#: Vivado's RTL simulator and the tool itself: a system-level spec cannot be checked
#: at RTL without them, and the baseline arm has no other way to reach them.
_VIVADO_TOOLS = ("vivado", "xvlog", "xvhdl", "xelab", "xsim")


def harness_note() -> str:
    """HARNESS_NOTE, plus where Vitis is -- environment facts, the same for both arms."""
    b = _vitis_bin()
    if b is None:
        return HARNESS_NOTE
    note = (HARNESS_NOTE + f" Vitis HLS is installed and its bin directory ({b}) is on "
            "PATH: run it as `vitis-run --mode hls --tcl <script>`.")
    vb = _vivado_bin()
    if vb is not None:
        note += (f" Vivado is installed and its bin directory ({vb}) is on PATH too "
                 "(`vivado`, and its simulator `xvlog` / `xelab` / `xsim`).")
    return note


#: How many times one run may be put back to work after a killed background
#: command, so an agent that keeps doing it cannot loop forever.
MAX_CONTINUES = 3

#: Sent when a phase ended with a background command killed anyway.
CONTINUE_AFTER_KILL = (
    "Your turn ended while a background command was still running, so it was "
    "stopped before it finished. Rerun it, wait for it with Monitor, and continue "
    "the task."
)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def _companions(prompt: Path) -> list[Path]:
    """Markdown files the prompt links to relatively, that exist beside it."""
    out: list[Path] = []
    for rel in _MD_LINK.findall(prompt.read_text(encoding="utf-8")):
        if "://" in rel:
            continue
        p = (prompt.parent / rel).resolve()
        if p.is_file() and p != prompt.resolve() and p not in out:
            out.append(p)
    return out


def _context_leaks(folder: Path) -> list[str]:
    """Files above *folder* that Claude Code would load as project context."""
    leaks = []
    for d in [folder, *folder.parents]:
        for name in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md"):
            if (d / name).is_file():
                leaks.append(str(d / name))
    return leaks


def _repo_root() -> Path | None:
    try:
        from waveflow.mcp.knowledge.roots import repo_root

        return repo_root()
    except Exception:
        return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop the agent and everything it started.

    ``proc.kill()`` is not enough.  On Windows ``claude`` is a ``.cmd`` shim,
    so the process we hold is ``cmd.exe`` and the agent is its child: killing
    the shim left the agent running to completion (measured: a 25 s limit that
    stopped nothing, the agent finishing at 91 s).  The agent's own children --
    Vitis runs, the MCP server -- need stopping too.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import signal

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def _waveflow_importable(no_waveflow: bool) -> bool | None:
    """Can the agent's ``python`` import Waveflow?  (Only asked for the baseline arm.)"""
    if not no_waveflow:
        return None
    exe = shutil.which("python", path=_agent_env(True).get("PATH"))
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, "-c", "import waveflow"], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode == 0


def _claude_exe() -> str:
    exe = shutil.which("claude")
    if exe is None:
        raise FileNotFoundError("the `claude` command is not on PATH")
    return exe


def _agent_env(no_waveflow: bool = False) -> dict[str, str]:
    """The operator's environment, with this venv first on PATH.

    For the no-Waveflow arm, the operator's environment as it is: whatever ``python`` the
    shell resolves (``waveflow_importable`` in ``config.json`` records whether that one can
    import Waveflow, which would make the baseline less blind).

    A student runs the agent with their venv active, so when it types
    ``python build.py`` it gets the Python that has Waveflow.  Reproduce that
    rather than trusting whatever ``python`` the operator's shell resolves.
    """
    env = dict(os.environ)
    # Both arms: Vitis and Vivado on PATH, like a student who has sourced the Xilinx
    # settings.  Vivado matters to the baseline arm: a system-level spec needs its
    # simulator (xvlog / xelab / xsim), and the Waveflow arm reaches it through the
    # toolchain finder whatever PATH says.
    for b in (_vivado_bin(), _vitis_bin()):
        if b is not None:
            env["PATH"] = str(b) + os.pathsep + env.get("PATH", "")
    if not no_waveflow:
        bindir = str(Path(sys.executable).parent)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = sys.prefix
    # A blind test launched from inside a Claude Code session must not look
    # like a nested one.
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(var, None)
    return env


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _run_claude(
    *,
    message: str,
    folder: Path,
    logdir: Path,
    phase: int,
    mcp_config: Path,
    allowed: list[str],
    permission_mode: str,
    model: str | None,
    resume: str | None,
    timeout: float,
    silent: bool,
    no_waveflow: bool = False,
) -> dict[str, Any]:
    cmd = [
        _claude_exe(),
        "-p",
        message,
        "--output-format", "stream-json",
        "--verbose",
        "--strict-mcp-config",
        "--mcp-config", str(mcp_config),
        "--setting-sources", "project",
        "--permission-mode", permission_mode,
        "--permission-prompts", "none",
        "--append-system-prompt", harness_note(),
        "--disallowedTools", *DISALLOWED_TOOLS,
        "--allowedTools", *allowed,
    ]
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]

    transcript = logdir / f"transcript-{phase}.jsonl"
    stderr = logdir / f"stderr-{phase}.txt"
    t0 = time.monotonic()
    timed_out = False
    if not silent:
        _echo(f"\n=== phase {phase}: {_short(message, 100)}")
    with transcript.open("w", encoding="utf-8") as out, stderr.open(
        "w", encoding="utf-8"
    ) as err:
        proc = subprocess.Popen(
            cmd,
            cwd=folder,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            env=_agent_env(no_waveflow),
            encoding="utf-8",
            errors="replace",
            # Its own process group, so the whole tree can be stopped (POSIX).
            start_new_session=(os.name != "nt"),
        )

        # The stream is read line by line so it can be shown live, which means
        # the read blocks; a timer enforces the timeout from outside it.
        def _kill() -> None:
            nonlocal timed_out
            timed_out = True
            _kill_tree(proc)

        timer = threading.Timer(timeout, _kill)
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                out.write(line)
                out.flush()
                if not silent:
                    _live(line, folder)
            proc.wait()
        except BaseException:
            # Ctrl-C (or anything else) must not leave the agent running on
            # its own; the caller records the phase as interrupted.
            _kill_tree(proc)
            proc.wait()
            raise
        finally:
            timer.cancel()
        code = proc.returncode
    result = _result_record(transcript)
    # The transcript is the authority: a run that reached its result finished,
    # even if the timer fired while it was writing it.
    if result is not None:
        timed_out = False
    if not silent:
        r = result or {}
        _echo(
            f"=== phase {phase} done: {'TIMED OUT' if timed_out else r.get('subtype', f'exit {code}')}, "
            f"{r.get('num_turns', '?')} turns, {(time.monotonic() - t0) / 60:.1f} min, "
            f"{_models(r) or '?'}, {_fmt_tokens(_tokens(r))}"
        )
    return {
        "phase": phase,
        "message": message,
        "transcript": str(transcript),
        "exit_code": code,
        "timed_out": timed_out,
        "wall_s": round(time.monotonic() - t0, 1),
        # A run cut off has no result record; its init record still names the session.
        "session_id": (result or {}).get("session_id") or _session_of(transcript),
        "result": result,
    }


def _tokens(result: dict[str, Any] | None) -> tuple[int, int, int]:
    """(input, of which read from cache, output) tokens for one run.

    From the result's ``usage``, which covers that run alone.  (``modelUsage``
    is cumulative over a resumed session, so it is used only for the model
    names.)  Input includes the conversation context re-read on every turn,
    most of it from cache, which is why it dwarfs the output.
    """
    u = (result or {}).get("usage") or {}
    cached = int(u.get("cache_read_input_tokens") or 0)
    inp = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + cached
    return inp, cached, int(u.get("output_tokens") or 0)


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _fmt_tokens(t: tuple[int, int, int]) -> str:
    inp, cached, out = t
    return f"{_k(inp)} tokens in ({_k(cached)} cached), {_k(out)} out"


def _models(result: dict[str, Any] | None) -> str:
    return ", ".join(((result or {}).get("modelUsage") or {}).keys())


def _echo(s: str) -> None:
    print(s, flush=True)


def _show_path(p: str, folder: Path | None) -> str:
    """A path as a person scanning the log needs it: relative to the trial
    folder when inside it, otherwise its tail (the end is the informative part)."""
    try:
        rp = Path(p).resolve()
        if folder is not None and (rp == folder or folder in rp.parents):
            return "./" + rp.relative_to(folder).as_posix()
    except (OSError, ValueError):
        pass
    return p if len(p) <= 100 else "…" + p[-99:]


def _brief_input(name: str, a: dict[str, Any], folder: Path | None = None) -> str:
    """The one argument that says what a tool call is doing."""
    for key in ("file_path", "path"):
        if a.get(key):
            return _show_path(str(a[key]), folder)
    for key in ("command", "pattern", "query", "section", "symbol", "name", "frame", "url"):
        if a.get(key):
            return _short(a[key], 120)
    return _short(a, 120) if a else ""


def _live(line: str, folder: Path | None = None) -> None:
    """Print one stream-json record as a compact log line."""
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        return
    t = r.get("type")
    if t == "system" and r.get("subtype") == "init":
        wf = sum(1 for x in r.get("tools", []) if x.startswith("mcp__waveflow"))
        _echo(f"  [init] model {r.get('model')}, MCP {r.get('mcp_servers')}, {wf} Waveflow tools")
    elif t == "assistant":
        for c in r.get("message", {}).get("content", []):
            if c.get("type") == "text" and c.get("text", "").strip():
                for ln in c["text"].strip().splitlines():
                    _echo(f"  {ln}")
            elif c.get("type") == "tool_use":
                name = c.get("name", "?").removeprefix("mcp__waveflow__")
                _echo(f"  -> {name}  {_brief_input(name, c.get('input') or {}, folder)}")
    elif t == "user":
        content = r.get("message", {}).get("content")
        for c in content if isinstance(content, list) else []:
            if c.get("type") == "tool_result":
                body = c.get("content")
                if isinstance(body, list):
                    body = " ".join(str(x.get("text", "")) for x in body if isinstance(x, dict))
                tag = "  <- ERROR " if c.get("is_error") else "  <- "
                _echo(tag + _short(str(body or ""), 100))


def _records(transcript: Path) -> list[dict[str, Any]]:
    out = []
    if not transcript.is_file():
        return out
    for line in transcript.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _killed_at_end(transcript: Path) -> list[str]:
    """Background commands that were killed after the agent's last turn ended.

    The signature of an agent that went to wait for a background job without
    a Monitor: the result record, then ``task_updated`` with status ``killed``.
    """
    recs = _records(transcript)
    last = max((i for i, r in enumerate(recs) if r.get("type") == "result"), default=None)
    if last is None:
        return []
    return [
        r.get("task_id", "?")
        for r in recs[last + 1:]
        if r.get("subtype") == "task_updated"
        and (r.get("patch") or {}).get("status") == "killed"
    ]


_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
               "output_tokens")


def _result_record(transcript: Path) -> dict[str, Any] | None:
    """The phase's result: the LAST result record, with usage, turns and time SUMMED.

    One ``claude -p`` phase can write several result records.  Each time a Monitor
    wakes an agent whose turn had ended, the session runs another turn and writes
    another result covering that turn alone.  Taking only the last undercounted the
    first no-Waveflow rotate run about 30-fold (91k tokens reported, 3.1M used).
    """
    results = [r for r in _records(transcript) if r.get("type") == "result"]
    if not results:
        return None
    merged = dict(results[-1])
    usage = dict(merged.get("usage") or {})
    for k in _USAGE_KEYS:
        usage[k] = sum(int((r.get("usage") or {}).get(k) or 0) for r in results)
    merged["usage"] = usage
    merged["num_turns"] = sum(int(r.get("num_turns") or 0) for r in results)
    merged["duration_ms"] = sum(int(r.get("duration_ms") or 0) for r in results)
    merged["result_records"] = len(results)
    return merged


def default_folder(prompt: str | Path) -> Path | None:
    """``<clone>/../waveflow_blind_tests/<spec stem>``, or None with no clone.

    Beside the clone, never in it: a folder inside the clone would load the
    repository's ``CLAUDE.md`` into the agent.  Outside git, too, so there is
    nothing to ignore.
    """
    repo = _repo_root()
    if repo is None:
        return None
    return repo.parent / "waveflow_blind_tests" / Path(prompt).stem


RESUME_MESSAGE = (
    "Your previous session was interrupted before you finished. Continue the task "
    "from where you left off; the files you already wrote are in this folder."
)
_DEFAULTS = {"permission_mode": "acceptEdits", "timeout": 4 * 3600.0}


def _log_dir(folder: Path) -> Path:
    return folder.parent / f"{folder.name}.blindtest"


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _session_of(transcript: Path) -> str | None:
    """The last session id a transcript names -- from its result, or failing
    that its init record, which is written first, so even a run killed a few
    seconds in can be resumed."""
    sid = None
    for r in _records(transcript):
        if r.get("session_id") and (r.get("type") == "result" or r.get("subtype") == "init"):
            sid = r["session_id"]
    return sid


def _load_phases(logdir: Path) -> list[dict[str, Any]]:
    """Every phase so far, rebuilt from ``phases.json`` and the transcripts.

    ``phases.json`` is updated as each phase starts and ends, so a phase that
    was running when the process died is still listed; the transcript supplies
    its session id and whatever result it reached.  A run from before
    ``phases.json`` existed is rebuilt from the transcripts alone.
    """
    saved = _read_json(logdir / "phases.json")
    if saved is None:
        # The first version of this script wrote run.json, at the end only.
        saved = (_read_json(logdir / "run.json") or {}).get("phases", [])
    recorded = {p["phase"]: p for p in saved}
    phases = []
    for t in sorted(logdir.glob("transcript-*.jsonl"),
                    key=lambda p: int(p.stem.split("-")[1])):
        n = int(t.stem.split("-")[1])
        rec = recorded.get(n, {"phase": n, "message": "(not recorded)"})
        result = _result_record(t)
        status = rec.get("status")
        if result is None and status != "timed out":
            status = "interrupted"
        phases.append({
            "phase": n,
            "message": rec.get("message", "(not recorded)"),
            "transcript": str(t),
            "exit_code": rec.get("exit_code"),
            "timed_out": status == "timed out",
            "status": status or "done",
            "wall_s": rec.get("wall_s") or 0.0,
            "session_id": (result or {}).get("session_id") or _session_of(t),
            "result": result,
        })
    return phases


def _save_phases(logdir: Path, phases: list[dict[str, Any]]) -> None:
    _write_json(logdir / "phases.json",
                [{k: v for k, v in p.items() if k != "result"} for p in phases])


def run_blind_test(
    prompt: str | Path | None,
    folder: str | Path | None = None,
    *,
    message: str | None = None,
    approve: str | None = "Approved. Continue.",
    rounds: int = 1,
    model: str | None = None,
    extra_allowed: list[str] | None = None,
    permission_mode: str | None = None,
    timeout: float | None = None,
    force: bool = False,
    silent: bool = False,
    resume: bool = False,
    no_waveflow: bool = False,
) -> dict[str, Any]:
    """Run the blind test and write ``<folder>.blindtest/summary.md``.

    *folder* defaults to :func:`default_folder` of *prompt*.  With *resume*,
    continue the interrupted or finished run in *folder* instead of starting
    one: same session, same settings (``--allow`` adds to them; ``model``,
    ``permission_mode`` and ``timeout`` override them when given), and the
    summary covers every phase.

    *no_waveflow* runs the baseline arm: no MCP server, no Waveflow tools on the allowlist,
    the operator's own Python, and a first message that says to use Vitis directly.
    """
    if folder is None:
        if prompt is None:
            return {"error": "give --prompt or --folder"}
        folder = default_folder(prompt)
        if folder is None:
            return {"error": "no Waveflow clone found to put the default folder beside; "
                    "pass --folder"}
    folder = Path(folder).resolve()
    if resume:
        return _resume(folder, message=message, approve=approve, rounds=rounds,
                       model=model, extra_allowed=extra_allowed,
                       permission_mode=permission_mode, timeout=timeout, silent=silent)
    if prompt is None:
        return {"error": "a new run needs --prompt"}
    prompt = Path(prompt).resolve()
    if not prompt.is_file():
        return {"error": f"no such prompt file: {prompt}"}

    repo = _repo_root()
    if repo is not None and (folder == repo or repo in folder.parents):
        return {"error": f"{folder} is inside the Waveflow clone {repo}; "
                "the agent would see the repo's CLAUDE.md and plans/"}
    leaks = _context_leaks(folder)
    if leaks:
        return {"error": "these files would load as context for the agent; "
                "choose a folder outside them", "files": leaks}
    if folder.exists() and any(folder.iterdir()) and not force:
        return {"error": f"{folder} is not empty; pass --force to reuse it"}

    logdir = _log_dir(folder)
    if logdir.exists() and any(logdir.iterdir()) and not force:
        return {"error": f"{logdir} already holds a run; pass --resume to continue it, "
                "or --force to overwrite it"}
    folder.mkdir(parents=True, exist_ok=True)
    if logdir.exists():
        for old in logdir.iterdir():
            if old.is_file():
                old.unlink()
    logdir.mkdir(parents=True, exist_ok=True)

    copied = []
    for f in [prompt, *_companions(prompt)]:
        shutil.copy2(f, folder / f.name)
        copied.append(f.name)

    mcp_config = logdir / "mcp.json"
    servers = {} if no_waveflow else {
        "waveflow": {
            "type": "stdio",
            "command": Path(sys.executable).as_posix(),
            "args": ["-m", "waveflow.mcp.server"],
        }
    }
    mcp_config.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")

    first = message or (NO_WAVEFLOW_FIRST if no_waveflow else WAVEFLOW_FIRST).format(
        spec=prompt.name)
    allowed = [*DEFAULT_ALLOWED_TOOLS, *vitis_allowed(), *(extra_allowed or [])]
    config = {
        "prompt": str(prompt),
        "first_message": first,
        "folder": str(folder),
        "copied": copied,
        "allowed": baseline_allowed(allowed) if no_waveflow else allowed,
        "no_waveflow": no_waveflow,
        "waveflow_importable": _waveflow_importable(no_waveflow),
        "permission_mode": permission_mode or _DEFAULTS["permission_mode"],
        "model": model,
        "timeout": timeout or _DEFAULTS["timeout"],
        "approve": approve,
        "rounds": rounds,
    }
    _write_json(logdir / "config.json", config)
    return _drive(folder, logdir, config, phases=[], first=first, resume_sid=None,
                  approvals_left=rounds if approve else 0, silent=silent)


def _resume(
    folder: Path,
    *,
    message: str | None,
    approve: str | None,
    rounds: int,
    model: str | None,
    extra_allowed: list[str] | None,
    permission_mode: str | None,
    timeout: float | None,
    silent: bool,
) -> dict[str, Any]:
    logdir = _log_dir(folder)
    phases = _load_phases(logdir)
    if not phases:
        return {"error": f"nothing to resume: no transcripts in {logdir}"}
    sid = next((p["session_id"] for p in reversed(phases) if p["session_id"]), None)

    # A run from before config.json existed gets the defaults; that is what it ran with.
    # The spec is the file named in the first message; the folder by now also
    # holds whatever the agent wrote, AGENTS.md from a scaffold included.
    m = re.search(r"specified in (\S+\.md)", phases[0]["message"])
    config = _read_json(logdir / "config.json") or {
        "folder": str(folder),
        "copied": [m.group(1)] if m else [],
        "allowed": list(DEFAULT_ALLOWED_TOOLS),
        "permission_mode": _DEFAULTS["permission_mode"],
        "model": None,
        "timeout": _DEFAULTS["timeout"],
        "approve": approve,
        "rounds": rounds,
    }
    # Saved list, plus defaults added since the run began (Monitor was one),
    # plus this call's --allow.
    defaults = [*DEFAULT_ALLOWED_TOOLS, *vitis_allowed(), *(extra_allowed or [])]
    if config.get("no_waveflow"):
        defaults = baseline_allowed(defaults)
    for a in defaults:
        if a not in config["allowed"]:
            config["allowed"].append(a)
    for key, val in (("model", model), ("permission_mode", permission_mode), ("timeout", timeout)):
        if val is not None:
            config[key] = val
    if not (logdir / "mcp.json").is_file():
        return {"error": f"{logdir / 'mcp.json'} is missing; cannot restart the server config"}
    _write_json(logdir / "config.json", config)

    # Approvals still owed: the configured number, less those already sent.
    appr = config.get("approve")
    sent = sum(1 for p in phases if appr and p["message"] == appr and p["result"])
    left = max(0, int(config.get("rounds") or 0) - sent) if appr else 0

    if sid is None:
        # Stopped before Claude Code created a session (measured: a start-up
        # can take over 25 s), so there is no conversation to continue --
        # and the agent did nothing.  Start the phase over.
        spec = next((c for c in config.get("copied", []) if c.endswith(".md")), "the spec")
        first = message or config.get("first_message") or (
            NO_WAVEFLOW_FIRST if config.get("no_waveflow") else WAVEFLOW_FIRST).format(spec=spec)
        if not silent:
            _echo("No session was started before the interruption; starting over.")
    else:
        first = message or RESUME_MESSAGE
    return _drive(folder, logdir, config, phases=phases, first=first,
                  resume_sid=sid, approvals_left=left, silent=silent)


def _drive(
    folder: Path,
    logdir: Path,
    config: dict[str, Any],
    *,
    phases: list[dict[str, Any]],
    first: str,
    resume_sid: str | None,
    approvals_left: int,
    silent: bool,
) -> dict[str, Any]:
    """Run *first*, then the approvals owed, recording each phase as it goes."""
    common = dict(
        folder=folder,
        logdir=logdir,
        mcp_config=logdir / "mcp.json",
        allowed=config["allowed"],
        permission_mode=config["permission_mode"],
        model=config.get("model"),
        timeout=float(config["timeout"]),
        silent=silent,
        no_waveflow=bool(config.get("no_waveflow")),
    )
    queue = [first] + [config["approve"]] * approvals_left
    interrupted = False
    continues_left = MAX_CONTINUES
    started = False
    while queue:
        msg = queue.pop(0)
        if started:
            last = phases[-1]
            if not (last["result"] and not last["result"].get("is_error") and last["session_id"]):
                break
        n = (phases[-1]["phase"] + 1) if phases else 1
        sid = phases[-1]["session_id"] if (phases and started) else resume_sid
        started = True
        phases.append({"phase": n, "message": msg, "status": "running",
                       "transcript": str(logdir / f"transcript-{n}.jsonl"),
                       "exit_code": None, "timed_out": False, "wall_s": 0.0,
                       "session_id": sid, "result": None})
        _save_phases(logdir, phases)
        try:
            done = _run_claude(message=msg, phase=n, resume=sid, **common)
        except KeyboardInterrupt:
            interrupted = True
            t = Path(phases[-1]["transcript"])
            phases[-1].update(status="interrupted", result=_result_record(t),
                              session_id=_session_of(t) or sid)
            _save_phases(logdir, phases)
            break
        done["status"] = ("done" if done["result"]
                          else "timed out" if done["timed_out"] else "interrupted")
        killed = _killed_at_end(Path(done["transcript"]))
        if killed:
            done["killed_background"] = killed
        phases[-1] = done
        _save_phases(logdir, phases)
        # The safety net: it went to wait without a Monitor and its job was
        # killed.  Put it back to work before any approval is sent.
        if killed and done["result"] and continues_left > 0:
            continues_left -= 1
            if not silent:
                _echo(f"\nA background command was killed when the turn ended "
                      f"({', '.join(killed)}); continuing the session.")
            queue.insert(0, CONTINUE_AFTER_KILL)

    repo = _repo_root()
    summary = summarize(phases, folder=folder, repo=repo,
                        copied=config.get("copied", []), allowed=config["allowed"])
    (logdir / "summary.md").write_text(summary, encoding="utf-8")
    if interrupted and not silent:
        _echo(f"\nInterrupted. Continue later with:\n"
              f"  waveflow blind-test --resume --folder \"{folder}\"")
    return {
        "folder": str(folder),
        "log_dir": str(logdir),
        "summary": str(logdir / "summary.md"),
        "phases": len(phases),
        "models": sorted({m for p in phases for m in _models(p["result"]).split(", ") if m}),
        "tokens_in": sum(_tokens(p["result"])[0] for p in phases),
        "tokens_in_cached": sum(_tokens(p["result"])[1] for p in phases),
        "tokens_out": sum(_tokens(p["result"])[2] for p in phases),
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _toc_example_dirs(repo: Path | None) -> set[str]:
    if repo is None:
        return set()
    dirs = set()
    for idx in (repo / "docs" / "examples").glob("*/index.md"):
        m = re.search(r"(?m)^example_dir:\s*(\S+)", idx.read_text(encoding="utf-8"))
        if m:
            dirs.add(m.group(1).strip().strip("/").removeprefix("examples/"))
    return dirs


def _classify_read(path: str, folder: Path, repo: Path | None, toc: set[str]) -> str:
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return "other"
    if p == folder or folder in p.parents:
        return "own folder"
    if repo is not None and (p == repo or repo in p.parents):
        rel = p.relative_to(repo).as_posix()
        if rel.startswith("plans/"):
            return "LEAK: repo plans/"
        if rel.startswith("examples/"):
            ex = rel.split("/")[1] if "/" in rel else ""
            return "repo example (TOC)" if ex in toc else "repo example NOT in TOC"
        if rel.startswith("docs/"):
            return "repo docs"
        return "repo other"
    return "outside"


def _short(v: Any, n: int = 110) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _login_note(api_key_source: str | None) -> str:
    """How the run was paid for.

    Claude Code also reports a dollar figure (``total_cost_usd``) whatever the
    login -- the tokens priced at API rates -- which reads as a charge on a
    subscription, where there is none.  So the report shows tokens and says
    which login was used, and never shows dollars.
    """
    if not api_key_source or api_key_source == "none":
        return ("Login: claude.ai subscription. The runs count against the plan's "
                "usage limits; nothing is charged per token.")
    return f"Login: API key (`{api_key_source}`). **Every token is billed.**"


def _choice(
    calls: list[tuple[int, str, dict[str, Any]]],
    folder: Path,
    repo: Path | None,
    toc: set[str],
) -> list[str]:
    """The summary's lead: how the agent chose its architecture.

    Choosing is the first part of building from a spec, so the report opens
    with it: whether the menu (frames or example cards) was consulted before
    the first write, which frame the process was fetched for, which reference
    designs were read, and whether -- and in which frame -- a scaffold was
    asked for.
    """
    def tool(n: str) -> str:
        return n.removeprefix("mcp__waveflow__")

    writes = [i for i, (_, n, a) in enumerate(calls) if n in ("Write", "Edit")]
    first_write = writes[0] if writes else len(calls)

    def before_write(name: str) -> str:
        at = [i for i, (_, n, _) in enumerate(calls) if tool(n) == name]
        if not at:
            return "**never called**"
        return "before the first write" if at[0] < first_write else "**only after the first write**"

    frames = [a.get("frame") for _, n, a in calls if tool(n) == "waveflow_get_process"]
    scaffolds = [a.get("frame") for _, n, a in calls if tool(n) == "waveflow_new_accel_project"]
    refs: dict[str, None] = {}
    for _, n, a in calls:
        if tool(n) == "waveflow_get_example" and a.get("name"):
            refs.setdefault(str(a["name"]), None)
        elif n == "Read" and repo is not None:
            path = a.get("file_path") or ""
            if _classify_read(path, folder, repo, toc) == "repo example (TOC)":
                rel = Path(path).resolve().relative_to(repo).as_posix()
                refs.setdefault(f"{rel.split('/')[1]} (read from the checkout)", None)

    def frame_name(f: Any) -> str:
        return "generic (no frame)" if not f else f"`{f}`"

    L = ["## Choice", ""]
    L.append(f"- first write: tool call #{first_write + 1} of {len(calls)}"
             if writes else "- first write: **none**")
    L.append(f"- `waveflow_list_frames`: {before_write('waveflow_list_frames')}")
    L.append(f"- `waveflow_list_examples`: {before_write('waveflow_list_examples')}")
    L.append("- `waveflow_get_process`: " + (
        ", ".join(frame_name(f) for f in frames) if frames else "**never called**"))
    L.append("- references read: " + (", ".join(f"`{r}`" for r in refs) if refs else "none"))
    L.append("- scaffold: " + (
        ", ".join(frame_name(f) for f in scaffolds) if scaffolds else "not requested"))
    # Both arms are told where Vitis and Vivado are.  A Waveflow-arm agent that
    # calls them itself has left the Waveflow flow for the vendor's -- worth
    # seeing at the top, not buried in the command list.
    direct = [a.get("command", "") for _, n, a in calls
              if n in ("Bash", "PowerShell") and runs_toolchain(a.get("command", ""))]
    L.append(f"- direct Vitis / Vivado commands: {len(direct)}" if direct
             else "- direct Vitis / Vivado commands: none")
    for c in direct[:5]:
        L.append(f"    - `{_short(c, 120)}`")
    L.append("")
    return L


#: A command that runs a Vitis or Vivado tool itself, rather than through a build
#: script.  Reading or grepping a generated script that mentions one does not count.
_TOOLCHAIN_CALL = re.compile(
    # In command position (start, or after ; & | ( ), optionally by full path,
    # and not a file name that merely starts with the tool's (`xsim.dir/`).
    r"(?:^|[;&|(])\s*(?:[\w:./\\-]*[/\\])?"
    r"(?:vitis-run|vitis_hls|vivado|xvlog|xvhdl|xelab|xsim)(?:\.bat)?(?![\w.\-])",
    re.IGNORECASE,
)

#: Quoted text: a grep pattern or an echo, never a command being run.
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")


def runs_toolchain(command: str) -> bool:
    """Whether *command* runs a Vitis or Vivado tool itself."""
    return bool(_TOOLCHAIN_CALL.search(_QUOTED.sub('""', command)))



# ---------------------------------------------------------------------------
# Where the time went
# ---------------------------------------------------------------------------

#: The time categories, most specific first: where two overlap, the earlier wins.
TIME_CATEGORIES = ("synth", "rtl sim", "pysim", "other build / test", "build (unsplit)",
                   "other tools")

#: A shell command's category, first match wins.  A build that runs several kinds of work
#: in one call (a DAG through `compare`) is "build (unsplit)" unless the project's step log
#: (`.waveflow/build_steps.jsonl`, written by run_dag_cli) splits it.
_COMMAND_TIME_RULES = (
    ("rtl sim", r"\bxsim\b|\bxelab\b|\bxvlog\b|run_sim\.py|run\.bat|_bfm_tb|--through\s+"
                r"(system_xsi|check_cosim|cosim\w*|rtlsim|rtl_timing|xsi)\b"),
    ("synth", r"--through\s+(csynth\w*|system_rtl)\b|run_hls\.tcl|csynth|vitis-run|vitis_hls"),
    ("pysim", r"--through\s+(pysim|py_sim|check_pysim|check_model)\b|pysim"),
    ("build (unsplit)", r"_build\.py(?!\s+--through\s+(codegen|gen_\w+|golden|report|figures)\b)"),
    ("other build / test", r"pytest|python\s|g\+\+|--through"),
)

#: A build step's category, from its name.
_STEP_TIME_RULES = (
    ("synth", r"csynth|synth|system_rtl|impl"),
    ("rtl sim", r"xsi|cosim|rtlsim|rtl_sim|xsim|rtl_timing"),
    ("pysim", r"pysim|py_sim"),
)


def _time_category(text: str, rules) -> str | None:
    for cat, pat in rules:
        if re.search(pat, text, re.IGNORECASE):
            return cat
    return None


def _stamp(r: dict[str, Any]):
    import datetime as dt

    t = r.get("timestamp")
    try:
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() if t else None
    except ValueError:
        return None


def _time_split(phases: list[dict[str, Any]], folder: Path) -> list[str]:
    """Where the wall clock went: synthesis, RTL simulation, pysim, other tools, the agent.

    Each tool call is the interval from its issue to its result.  A background command
    returns at once, so it is charged from its launch to the ``end_time`` of its task.
    Builds run through ``run_dag_cli`` log each step (``.waveflow/build_steps.jsonl``),
    which splits a build that ran synthesis, pysim and RTL in one call.  Every second is
    charged once, to the most specific category running then; a second in which no tool
    ran is the agent's own.
    """
    intervals: list[tuple[float, float, str]] = []
    first = last = None
    for p in phases:
        uses: dict[str, tuple[float | None, str, dict[str, Any]]] = {}
        launched: dict[str, tuple[float, str]] = {}
        task_of: dict[str, str] = {}
        for r in _records(Path(p["transcript"])):
            kind, sub = r.get("type"), r.get("subtype")
            if kind == "system" and sub == "task_started":
                task_of[r.get("task_id")] = r.get("tool_use_id")
            elif kind == "system" and sub == "task_updated":
                end = (r.get("patch") or {}).get("end_time")
                use = task_of.get(r.get("task_id"))
                if end and use in launched:
                    t0, cat = launched.pop(use)
                    intervals.append((t0, end / 1000, cat))
                    last = max(last or 0, end / 1000)
            when = _stamp(r)
            if when is not None:
                first = when if first is None else first
                last = when if last is None else max(last, when)
            content = (r.get("message") or {}).get("content") if isinstance(r.get("message"), dict) else None
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    uses[c.get("id", "")] = (when, c.get("name", ""), c.get("input") or {})
                elif c.get("type") == "tool_result" and c.get("tool_use_id") in uses:
                    t0, name, inp = uses.pop(c["tool_use_id"])
                    cat = "other tools"
                    if name in ("Bash", "PowerShell"):
                        cat = _time_category(str(inp.get("command", "")), _COMMAND_TIME_RULES) or cat
                        if inp.get("run_in_background") and t0 is not None:
                            launched[c["tool_use_id"]] = (t0, cat)
                            cat = "other tools"
                    if t0 is not None and when is not None and when > t0:
                        intervals.append((t0, when, cat))

    for log in folder.rglob("build_steps.jsonl") if folder.is_dir() else ():
        if log.parent.name != ".waveflow":
            continue
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            cat = _time_category(str(e.get("step", "")), _STEP_TIME_RULES) or "other build / test"
            if first is not None and e.get("start", 0) >= first - 60:
                intervals.append((float(e["start"]), float(e["end"]), cat))

    L = ["## Where the time went", ""]
    if first is None or last is None or last <= first:
        return L + ["(the transcript has no timestamps)", ""]
    order = list(TIME_CATEGORIES)
    total = {k: 0.0 for k in order}
    edges = sorted({x for a, b, _ in intervals for x in (a, b)} | {first, last})
    for a, b in zip(edges, edges[1:]):
        live = [c for s, e, c in intervals if s <= a and e >= b]
        if live:
            total[min(live, key=order.index)] += b - a
    wall = last - first
    total["agent (no tool running)"] = max(0.0, wall - sum(total.values()))
    L += ["| | minutes | share |", "| --- | --- | --- |"]
    for k, v in total.items():
        if v >= 1 or k.startswith("agent"):
            L.append(f"| {k} | {v / 60:.1f} | {100 * v / wall:.0f}% |")
    L += [f"| **wall** | **{wall / 60:.1f}** | |", ""]
    return L

def summarize(
    phases: list[dict[str, Any]],
    *,
    folder: Path,
    repo: Path | None,
    copied: list[str],
    allowed: list[str],
) -> str:
    toc = _toc_example_dirs(repo)
    calls: list[tuple[int, str, dict[str, Any]]] = []
    init: dict[str, Any] | None = None
    for p in phases:
        for r in _records(Path(p["transcript"])):
            if r.get("type") == "system" and r.get("subtype") == "init" and init is None:
                init = r
            if r.get("type") == "assistant":
                for c in r.get("message", {}).get("content", []):
                    if c.get("type") == "tool_use":
                        calls.append((p["phase"], c.get("name", "?"), c.get("input") or {}))

    L: list[str] = [f"# Blind test: {folder.name}", ""]
    L += ["| Phase | Message | Turns | Wall | Model | Tokens in (cached) | Tokens out | Outcome |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for p in phases:
        r = p["result"] or {}
        if p["timed_out"]:
            outcome = "TIMED OUT"
        elif not r:
            outcome = "**interrupted**"
        else:
            outcome = r.get("subtype") or f"exit {p['exit_code']}"
        inp, cached, out = _tokens(r)
        L.append(
            f"| {p['phase']} | {_short(p['message'], 50)} | {r.get('num_turns', '?')} | "
            f"{p['wall_s'] / 60:.1f} min | {_models(r) or '?'} | {_k(inp)} ({_k(cached)}) | "
            f"{_k(out)} | {outcome} |"
        )
    L.append("")
    L.append(f"Copied into the folder: {', '.join(copied)}.")
    if init:
        L.append(_login_note(init.get("apiKeySource")))
        wf = [t for t in init.get("tools", []) if t.startswith("mcp__waveflow")]
        L.append(
            f"Model `{init.get('model')}`; MCP {init.get('mcp_servers')}; "
            f"{len(wf)} Waveflow tools offered; skills {len(init.get('skills') or [])}, "
            f"plugins {len(init.get('plugins') or [])}."
        )
    L.append("")

    wf_calls = [(ph, n, a) for ph, n, a in calls if n.startswith("mcp__waveflow")]
    L += _choice(calls, folder, repo, toc)
    L += _time_split(phases, folder)

    L += ["## Waveflow tools", ""]
    if not wf_calls:
        L.append("**None were called.**")
    else:
        first = next(i for i, (_, n, _) in enumerate(calls) if n.startswith("mcp__waveflow"))
        L.append(f"First Waveflow call at tool call #{first + 1} of {len(calls)}.")
        L += ["", "| # | Phase | Tool | Arguments |", "| --- | --- | --- | --- |"]
        for i, (ph, n, a) in enumerate(wf_calls, 1):
            L.append(f"| {i} | {ph} | `{n.removeprefix('mcp__waveflow__')}` | {_short(a, 90)} |")
    L.append("")

    L += ["## All tool calls by name", ""]
    for name, n in Counter(n for _, n, _ in calls).most_common():
        L.append(f"- `{name}`: {n}")
    L.append("")

    reads = Counter()
    read_paths: dict[str, list[str]] = {}
    for _, n, a in calls:
        if n in ("Read", "Glob", "Grep"):
            path = a.get("file_path") or a.get("path") or ""
            if not path:
                continue
            cls = _classify_read(path, folder, repo, toc)
            reads[cls] += 1
            read_paths.setdefault(cls, []).append(path)
    L += ["## What it read", ""]
    for cls, n in reads.most_common():
        L.append(f"- **{cls}**: {n}")
        if cls != "own folder":
            for path in sorted(set(read_paths[cls]))[:15]:
                L.append(f"    - `{path}`")
    if not reads:
        L.append("(no file reads)")
    L.append("")

    L += ["## What it wrote", ""]
    wrote = sorted({a.get("file_path", "") for _, n, a in calls if n in ("Write", "Edit")})
    for path in wrote:
        cls = _classify_read(path, folder, repo, toc)
        flags = []
        if cls != "own folder":
            flags.append(f"**outside its folder ({cls})**")
        if "/gen/" in Path(path).as_posix() or Path(path).parent.name == "gen":
            flags.append("**hand-edited a generated file**")
        L.append(f"- `{_show_path(path, folder)}` {' '.join(flags)}".rstrip())
    if not wrote:
        L.append("(nothing)")
    L.append("")

    L += ["## Commands", ""]
    for ph, n, a in calls:
        if n in ("Bash", "PowerShell"):
            L.append(f"- [{ph}] `{_short(a.get('command', ''), 140)}`")
    L.append("")

    L += ["## Denied", ""]
    denied = [d for p in phases for d in ((p["result"] or {}).get("permission_denials") or [])]
    for d in denied:
        L.append(f"- `{d.get('tool_name')}` {_short(d.get('tool_input', {}), 120)}")
    if not denied:
        L.append("(nothing)")
    L += ["", f"Allowlist: {', '.join(f'`{a}`' for a in allowed)}", ""]

    L += ["## Final message of each phase", ""]
    for p in phases:
        L += [f"### Phase {p['phase']}", "", str((p["result"] or {}).get("result", "(none)")), ""]

    L += ["## Folder afterwards", ""]
    for f in sorted(folder.rglob("*")):
        rel = f.relative_to(folder)
        if len(rel.parts) <= 2 and not any(part.startswith(".") for part in rel.parts):
            L.append(f"- `{rel.as_posix()}{'/' if f.is_dir() else ''}`")
    L.append("")
    return "\n".join(L)
