import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

#: Oldest AMD/Xilinx release Waveflow is tested against.  Older toolchains may work, but the
#: generated HLS pragmas and the Vitis ``vitis-run --mode hls`` entry point are not validated
#: against them.
MIN_TOOL_VERSION: Tuple[int, int] = (2025, 1)

VITIS_PATH_ENV = "WAVEFLOW_VITIS_PATH"
VIVADO_PATH_ENV = "WAVEFLOW_VIVADO_PATH"


def _is_tool_binary(path: Path, binary_name: str) -> bool:
    if not path.is_file():
        return False
    if path.name != binary_name:
        return False
    # On POSIX we also require executable permission.
    if platform.system() == "Windows":
        return True
    return os.access(path, os.X_OK)


def _version_key(version_text: str) -> Tuple:
    """Sort key that prefers numerically newer version strings."""
    parts = re.split(r"(\d+)", version_text)
    key: List[Tuple[int, Union[int, str]]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((1, int(part)))
        else:
            key.append((0, part.lower()))
    return tuple(key)


def _collect_candidates(root: Path, binary_name: str, product: str = "Vitis") -> List[Path]:
    """
    Collect candidate tool executables from a root path.

    Supported layouts:
    - <root>/<binary_name>
    - <root>/bin/<binary_name>
    - <root>/<version>/bin/<binary_name>
    - <root>/<version>/<product>/bin/<binary_name>

    ``product`` is the per-tool install directory ("Vitis" or "Vivado") used by the unified
    installer layout, where both tools sit under a shared ``<root>/<version>/`` directory.
    """
    candidates: List[Path] = []

    direct = root / binary_name
    if _is_tool_binary(direct, binary_name):
        candidates.append(direct)

    bin_direct = root / "bin" / binary_name
    if _is_tool_binary(bin_direct, binary_name):
        candidates.append(bin_direct)

    if root.is_dir():
        for child in root.iterdir():
            if not child.is_dir():
                continue
            p = child / "bin" / binary_name
            if _is_tool_binary(p, binary_name):
                candidates.append(p)

            # Unified installer style: C:\Xilinx\<version>\Vitis\bin\vitis-run.bat, and the
            # matching /eda/xilinx/<version>/Vivado/bin/vivado on Linux.
            p_nested = child / product / "bin" / binary_name
            if _is_tool_binary(p_nested, binary_name):
                candidates.append(p_nested)

    return candidates


#: Per-tool install directories in the unified installer layout.  Used to tell a version
#: directory apart from a product directory when ranking candidates.
_PRODUCT_DIRS = frozenset({"Vitis", "Vivado"})


def _pick_highest_version(candidates: List[Path]) -> Optional[str]:
    if not candidates:
        return None

    def candidate_key(path: Path) -> Tuple:
        version = ""
        if path.parent.name == "bin":
            # Layout: <root>/<version>/bin/<binary>
            version = path.parent.parent.name
            # Layout: <root>/<version>/<product>/bin/<binary>
            if version in _PRODUCT_DIRS:
                version = path.parent.parent.parent.name
        return _version_key(version)

    best = max(candidates, key=candidate_key)
    return str(best.resolve())


def find_vitis_path(top_dir: Optional[Union[str, Path]] = None) -> Optional[str]:
    """
    Locates the Vitis execution entry point for hardware synthesis and simulation.

    On Windows, the function searches for the `vitis-run.bat` batch file, which
    initializes the necessary environment variables. On Linux, it searches for
    the `vitis-run` shell script.

    The search priority is as follows:
    1.  **Environment Variable**: Uses the path specified in `WAVEFLOW_VITIS_PATH`
         if it exists.
    2.  **Explicit Search**: Searches within the provided `top_dir`.
    3.  **Heuristic Search**: Searches standard OS installation paths:
        - Windows: `<top_dir>\\<version>\\Vitis\\bin\\vitis-run.bat`
        - Linux: `<top_dir>/<version>/Vitis/bin/vitis-run` and the older
          `<top_dir>/<version>/bin/vitis-run`

    If multiple versions of Vitis are found in the search path, the version with
    the highest alphanumeric value (e.g., 2025.1 over 2024.2) is returned.

    Parameters
    ----------
    top_dir : Optional[Union[str, Path]]
        The directory to begin the search. On Windows, if None, it defaults to
        `C:\\Xilinx`. On Linux, it defaults to `/tools/Xilinx` and `/opt/Xilinx`
        (and the `Vitis` subdirectory of each, for the older per-product layout).
        Installs outside these locations need `WAVEFLOW_VITIS_PATH`.

    Returns
    -------
    Optional[str]
        The absolute path to the `vitis-run.bat` (Windows) or `vitis-run` (Linux)
        binary. Returns `None` if no valid installation is detected.
    """
    system_name = platform.system()
    binary_name = "vitis-run.bat" if system_name == "Windows" else "vitis-run"

    env_value = os.environ.get(VITIS_PATH_ENV, "").strip()
    if env_value:
        env_path = Path(env_value).expanduser()
        if _is_tool_binary(env_path, binary_name):
            return str(env_path.resolve())

        env_candidates = _collect_candidates(env_path, binary_name)
        env_match = _pick_highest_version(env_candidates)
        if env_match is not None:
            return env_match

    roots: List[Path] = []
    if top_dir is not None:
        roots.append(Path(top_dir).expanduser())

    roots.extend(_default_roots("Vitis"))

    all_candidates: List[Path] = []
    for root in roots:
        all_candidates.extend(_collect_candidates(root, binary_name, product="Vitis"))

    return _pick_highest_version(all_candidates)


def _default_roots(product: str) -> List[Path]:
    """Standard install roots to search for ``product`` ("Vitis" or "Vivado")."""
    if platform.system() == "Windows":
        return [Path(r"C:\Xilinx")]
    # Both the unified layout (<root>/<version>/<product>/bin) and the older per-product
    # layout (<root>/<product>/<version>/bin) are covered by searching each root.
    return [
        Path("/tools/Xilinx"),
        Path("/opt/Xilinx"),
        Path(f"/tools/Xilinx/{product}"),
        Path(f"/opt/Xilinx/{product}"),
    ]


def _vivado_siblings(vitis_exe: Path, binary_name: str) -> List[Path]:
    """
    Candidate Vivado executables that sit alongside an already-resolved ``vitis-run``.

    Vitis and Vivado are installed together and version-locked, so the Vivado matching a
    discovered Vitis is the one Waveflow wants — no second environment variable needed.
    """
    base = vitis_exe.parent.parent  # <...>/Vitis  or  <...>/<version>
    candidates = [
        # <root>/<version>/Vitis/bin/vitis-run -> <root>/<version>/Vivado/bin/vivado
        base.parent / "Vivado" / "bin" / binary_name,
    ]
    if base.parent.name == "Vitis":
        # <root>/Vitis/<version>/bin/vitis-run -> <root>/Vivado/<version>/bin/vivado
        candidates.append(base.parent.parent / "Vivado" / base.name / "bin" / binary_name)
    return [p for p in candidates if _is_tool_binary(p, binary_name)]


def find_vivado_path(top_dir: Optional[Union[str, Path]] = None) -> Optional[str]:
    """
    Locate the Vivado executable used for RTL simulation, synthesis, and implementation.

    Vivado is normally installed alongside Vitis and version-locked to it, so the common case
    needs no configuration beyond ``WAVEFLOW_VITIS_PATH``: whatever Vivado sits next to the
    discovered Vitis is used.  The search order is:

    1.  **Environment variable**: ``WAVEFLOW_VIVADO_PATH``, for split installs where Vivado
        does not live beside Vitis.
    2.  **Explicit search**: the provided ``top_dir``.
    3.  **Alongside Vitis**: the Vivado matching the Vitis found by :func:`find_vitis_path`.
    4.  **PATH**: a ``vivado`` on the current ``PATH`` (what an environment module provides).
    5.  **Heuristic search**: the standard install roots, newest version winning.

    Parameters
    ----------
    top_dir : Optional[Union[str, Path]]
        Directory to begin the search, checked before the automatic locations.

    Returns
    -------
    Optional[str]
        Absolute path to ``vivado.bat`` (Windows) or ``vivado`` (Linux), or ``None`` if no
        installation is detected.
    """
    binary_name = "vivado.bat" if platform.system() == "Windows" else "vivado"

    env_value = os.environ.get(VIVADO_PATH_ENV, "").strip()
    if env_value:
        env_path = Path(env_value).expanduser()
        if _is_tool_binary(env_path, binary_name):
            return str(env_path.resolve())
        env_match = _pick_highest_version(
            _collect_candidates(env_path, binary_name, product="Vivado")
        )
        if env_match is not None:
            return env_match

    if top_dir is not None:
        top_match = _pick_highest_version(
            _collect_candidates(Path(top_dir).expanduser(), binary_name, product="Vivado")
        )
        if top_match is not None:
            return top_match

    vitis_exe = find_vitis_path()
    if vitis_exe:
        siblings = _vivado_siblings(Path(vitis_exe), binary_name)
        if siblings:
            return str(siblings[0].resolve())

    on_path = shutil.which(binary_name)
    if on_path:
        return str(Path(on_path).resolve())

    all_candidates: List[Path] = []
    for root in _default_roots("Vivado"):
        all_candidates.extend(_collect_candidates(root, binary_name, product="Vivado"))

    return _pick_highest_version(all_candidates)


#: ``vitis-run v2026.1 (64-bit)`` / ``vivado v2026.1 (64-bit)``.  The leading ``v`` is required
#: so the banner's release number wins over later lines such as Vivado's
#: ``Tool Version Limit: 2026.06``.
_BANNER_VERSION_RE = re.compile(r"\bv(\d{4})\.(\d+)")

#: A bare ``2026.1`` version directory in an install path.
_PATH_VERSION_RE = re.compile(r"^(\d{4})\.(\d+)$")


def parse_tool_version(text: str) -> Optional[str]:
    """Extract a ``YYYY.N`` release from a tool's ``-version`` banner, or ``None``."""
    match = _BANNER_VERSION_RE.search(text)
    return f"{match.group(1)}.{match.group(2)}" if match else None


def _version_from_path(exe: Path) -> Optional[str]:
    """Recover the release from a ``.../2026.1/...`` install path, newest component last."""
    for part in reversed(exe.parts):
        match = _PATH_VERSION_RE.match(part)
        if match:
            return f"{match.group(1)}.{match.group(2)}"
    return None


def tool_version(
    exe: Union[str, Path],
    version_flag: str,
    timeout: float = 180.0,
) -> Optional[str]:
    """
    Return the ``YYYY.N`` release of an AMD tool, or ``None`` if it cannot be determined.

    The executable is invoked with ``version_flag`` (``--version`` for ``vitis-run``,
    ``-version`` for ``vivado``) and its banner parsed.  Launching these tools costs a couple
    of seconds; if the run fails or times out, the release is recovered from the install path
    instead, so a report is still produced for an installation that cannot start.

    Parameters
    ----------
    exe : Union[str, Path]
        Path to the tool executable.
    version_flag : str
        The tool's version flag, spelled exactly as that tool expects it.
    timeout : float, optional
        Seconds to wait for the tool to print its banner. Defaults to 180.

    Returns
    -------
    Optional[str]
        The release string, e.g. ``"2026.1"``.
    """
    exe_path = Path(exe)
    final_cmd, use_shell = _build_final_cmd([exe_path, version_flag])
    try:
        result = subprocess.run(
            final_cmd,
            shell=use_shell,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        version = parse_tool_version(f"{result.stdout or ''}\n{result.stderr or ''}")
        if version is not None:
            return version
    except (OSError, subprocess.SubprocessError):
        pass

    return _version_from_path(exe_path)


def version_tuple(version: Optional[str]) -> Optional[Tuple[int, int]]:
    """Convert ``"2026.1"`` to ``(2026, 1)`` for comparison, or ``None`` if unparseable."""
    if not version:
        return None
    match = _PATH_VERSION_RE.match(version.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


@dataclass(frozen=True)
class ToolInfo:
    """The outcome of probing for one AMD tool."""

    #: Display name, e.g. ``"Vitis"``.
    name: str
    #: Absolute path to the executable, or ``None`` if it was not found.
    path: Optional[str]
    #: Release string such as ``"2026.1"``, or ``None`` if it could not be determined.
    version: Optional[str]
    #: Name of the environment variable that overrides this tool's location.
    env_var: str

    @property
    def found(self) -> bool:
        """Whether an executable was located."""
        return self.path is not None

    @property
    def meets_min(self) -> Optional[bool]:
        """
        Whether the release is at least :data:`MIN_TOOL_VERSION`.

        ``None`` when the tool is missing or its version could not be determined — an unknown
        version is reported as unknown rather than assumed good or bad.
        """
        parsed = version_tuple(self.version)
        if parsed is None:
            return None
        return parsed >= MIN_TOOL_VERSION


def probe_amd_tools(check_versions: bool = True) -> List[ToolInfo]:
    """
    Locate Vitis and Vivado and report what was found.

    Parameters
    ----------
    check_versions : bool, optional
        If ``True`` (default), each located tool is run to read its release. Set ``False`` to
        skip the subprocess launches and derive versions from the install paths alone, which
        is much faster but only works for standard layouts.

    Returns
    -------
    List[ToolInfo]
        One entry for Vitis and one for Vivado, in that order.
    """
    probes = (
        ("Vitis", find_vitis_path(), VITIS_PATH_ENV, "--version"),
        ("Vivado", find_vivado_path(), VIVADO_PATH_ENV, "-version"),
    )

    infos: List[ToolInfo] = []
    for name, path, env_var, flag in probes:
        if path is None:
            version = None
        elif check_versions:
            version = tool_version(path, flag)
        else:
            version = _version_from_path(Path(path))
        infos.append(ToolInfo(name=name, path=path, version=version, env_var=env_var))

    return infos


def _build_vitis_hls_cmd(
    tcl_script: Union[str, Path],
    args: Optional[Sequence[str]] = None,
) -> tuple[List[str], Path]:
    vitis_path = find_vitis_path()
    if not vitis_path:
        raise RuntimeError(
            "Vitis installation not found. Please set WAVEFLOW_VITIS_PATH "
            "(run `test_amd_tools` to diagnose)."
        )

    tcl_path = Path(tcl_script)
    cmd_list = [str(vitis_path), "--mode", "hls", "--tcl", str(tcl_path)]
    if args:
        cmd_list.append("--tclargs")
        cmd_list.extend(str(arg) for arg in args)

    return cmd_list, tcl_path.parent


def run_vitis_hls(
    tcl_script: Union[str, Path],
    work_dir: Optional[Union[str, Path]] = None,
    args: Optional[List[str]] = None,
    capture_output: bool = True,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """
    Execute a Vitis HLS TCL script using the discovered Vitis launcher.

    The function first resolves the Vitis executable via :func:`find_vitis_path`.
    It then builds and executes a command of the form:

    - Windows: ``vitis-run.bat --mode hls <tcl_script> [--tclargs ...]``
      executed via ``cmd.exe /c call ...``
    - Linux: ``vitis-run --mode hls <tcl_script> [--tclargs ...]``

    Parameters
    ----------
    tcl_script : Union[str, Path]
        Path to the TCL script consumed by Vitis HLS.
        This is passed as the positional ``<input_file>`` argument to
        ``vitis-run``.
    work_dir : Optional[Union[str, Path]], optional
        Working directory for the subprocess. If ``None``, defaults to
        ``Path(tcl_script).parent``.
    args : Optional[List[str]], optional
        Optional values passed to the TCL script through ``--tclargs``.
        If provided, they are appended after ``--tclargs`` in the exact order
        given.
    capture_output : bool, optional
        If ``True`` (default), captures stdout/stderr and stores them in the
        returned :class:`subprocess.CompletedProcess`. If ``False``, output is
        inherited by the current process terminal.
    env : Optional[Dict[str, str]], optional
        Additional environment variables merged into the subprocess
        environment before launching Vitis.

    Returns
    -------
    subprocess.CompletedProcess
        The completed process object from :func:`subprocess.run`, including
        ``args``, ``returncode``, and optionally ``stdout``/``stderr``.

    Raises
    ------
    RuntimeError
        If no Vitis executable could be discovered.
    subprocess.CalledProcessError
        If Vitis returns a non-zero exit status (``check=True`` behavior).
    """
    cmd_list, default_work_dir = _build_vitis_hls_cmd(tcl_script=tcl_script, args=args)
    final_cmd, use_shell = _build_final_cmd(cmd_list)

    final_env = None
    if env is not None:
        final_env = os.environ.copy()
        final_env.update(env)

    return subprocess.run(
        final_cmd,
        cwd=work_dir or default_work_dir,
        shell=use_shell,
        check=True,
        text=True,
        capture_output=capture_output,
        env=final_env,
    )


def _build_final_cmd(cmd_list: Sequence[Union[str, Path]]) -> tuple[Union[str, List[str]], bool]:
    normalized = [str(part) for part in cmd_list]
    is_windows = platform.system() == "Windows"
    if is_windows:
        joined_cmd = " ".join(f'"{part}"' for part in normalized)
        return f'cmd.exe /c "call {joined_cmd}"', True
    return normalized, False


def _write_vitis_hls_result_report(
    output_path: Union[str, Path],
    result: Dict[str, Optional[str]],
) -> Path:
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as handle:
        handle.write("status\n")
        handle.write(f"{result.get('status')}\n\n")
        handle.write("message\n")
        handle.write(f"{result.get('message')}\n\n")
        handle.write("stderr\n")
        handle.write(f"{result.get('stderr')}\n\n")
        handle.write("stdout\n")
        handle.write(f"{result.get('stdout')}\n")

    return out_path


def subprocess_result(
    cmd_list: Sequence[Union[str, Path]],
    work_dir: Optional[Union[str, Path]] = None,
    capture_output: bool = True,
    output_path: Optional[Union[str, Path]] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Optional[str]]:
    """
    Execute a subprocess command and return a structured result dictionary.

    The command is normalized using the same platform-specific wrapping used by
    :func:`run_vitis_hls`: on Windows it is executed via ``cmd.exe /c "call ..."``;
    on non-Windows platforms it is executed directly as a list of arguments.

    Parameters
    ----------
    cmd_list : Sequence[Union[str, Path]]
        Command and arguments to execute.
    work_dir : Optional[Union[str, Path]], optional
        Working directory for the subprocess. If ``None``, the current working
        directory is used.
    capture_output : bool, optional
        If ``True`` (default), captures stdout/stderr in the returned result
        dictionary. If ``False``, output is inherited by the current process
        terminal and the reported ``stdout``/``stderr`` values may be ``None``.
    output_path : Optional[Union[str, Path]], optional
        Optional text-file path where the structured result is written in a
        simple human-readable format containing the ``status``, ``message``,
        ``stderr``, and ``stdout`` fields. When provided, ``capture_output`` is
        forced to ``True`` so the report file can include subprocess output.
    env : Optional[Dict[str, str]], optional
        Additional environment variables merged into the subprocess
        environment before launching the command.

    Returns
    -------
    Dict[str, Optional[str]]
        A dictionary with the fields:

        - ``status``: One of ``"passed"``, ``"subprocess_error"``, or
          ``"runtime_error"``.
        - ``stdout``: Captured standard output when available.
        - ``stderr``: Captured standard error when available.
        - ``message``: Error message for non-subprocess failures, otherwise
          ``None``.
    """
    effective_capture_output = capture_output or (output_path is not None)
    final_cmd, use_shell = _build_final_cmd(cmd_list)
    final_env = None
    if env is not None:
        final_env = os.environ.copy()
        final_env.update(env)

    try:
        result = subprocess.run(
            final_cmd,
            cwd=work_dir,
            shell=use_shell,
            check=True,
            text=True,
            capture_output=effective_capture_output,
            env=final_env,
        )
        out = {
            "status": "passed",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "message": None,
        }
    except subprocess.CalledProcessError as exc:
        out = {
            "status": "subprocess_error",
            "stdout": exc.stdout,
            "stderr": exc.stderr,
            "message": str(exc),
        }
    except Exception as exc:
        out = {
            "status": "runtime_error",
            "stdout": None,
            "stderr": None,
            "message": str(exc),
        }

    if output_path is not None:
        _write_vitis_hls_result_report(output_path, out)

    return out


def run_vitis_hls_result(
    tcl_script: Union[str, Path],
    work_dir: Optional[Union[str, Path]] = None,
    args: Optional[List[str]] = None,
    capture_output: bool = True,
    output_path: Optional[Union[str, Path]] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Optional[str]]:
    """
    Execute a Vitis HLS TCL script and return a structured result dictionary.

    This wrapper preserves the existing behavior of :func:`run_vitis_hls` for
    command construction and subprocess execution, but it normalizes success and
    common failure modes into a plain dictionary for callers that prefer not to
    handle exceptions directly.

    Parameters
    ----------
    tcl_script : Union[str, Path]
        Path to the TCL script consumed by Vitis HLS.
    work_dir : Optional[Union[str, Path]], optional
        Working directory for the subprocess. If ``None``, defaults to
        ``Path(tcl_script).parent``.
    args : Optional[List[str]], optional
        Optional values passed to the TCL script through ``--tclargs``.
        If provided, they are appended after ``--tclargs`` in the exact order
        given.
    capture_output : bool, optional
        If ``True`` (default), captures stdout/stderr in the returned result
        dictionary. If ``False``, output is inherited by the current process
        terminal and the reported ``stdout``/``stderr`` values may be ``None``.
    output_path : Optional[Union[str, Path]], optional
        Optional text-file path where the structured result is written in a
        simple human-readable format containing the ``status``, ``message``,
        ``stderr``, and ``stdout`` fields.
        When provided, ``capture_output`` is forced to ``True`` so the report
        file can include subprocess output.
    env : Optional[Dict[str, str]], optional
        Additional environment variables merged into the subprocess
        environment before launching Vitis.

    Returns
    -------
    Dict[str, Optional[str]]
        A dictionary with the fields:

        - ``status``: One of ``"passed"``, ``"subprocess_error"``, or
          ``"runtime_error"``.
        - ``stdout``: Captured standard output when available.
        - ``stderr``: Captured standard error when available.
        - ``message``: Error message for non-subprocess failures, otherwise
          ``None``.
    """
    effective_capture_output = capture_output or (output_path is not None)

    try:
        cmd_list, default_work_dir = _build_vitis_hls_cmd(tcl_script=tcl_script, args=args)
    except RuntimeError as exc:
        out = {
            "status": "runtime_error",
            "stdout": None,
            "stderr": None,
            "message": str(exc),
        }
        if output_path is not None:
            _write_vitis_hls_result_report(output_path, out)
        return out

    return subprocess_result(
        cmd_list=cmd_list,
        work_dir=work_dir or default_work_dir,
        capture_output=effective_capture_output,
        output_path=output_path,
        env=env,
    )
