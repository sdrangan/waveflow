"""hls_ip.py — the vendor IP a csynth'd kernel instantiates, generated for an XSI run.

Some operators csynth does not write as Verilog: a ``float`` add or multiply becomes an instance of
Xilinx's ``floating_point`` core, and ``syn/verilog`` holds only a thin Verilog shell around it plus a
``<name>_ip.tcl`` saying how to make the core (``create_ip`` and its configuration).  Vitis's own
cosim runs that script; the system XSI flow (``system_rtl`` -> ``system_xsi``) must too, or ``xelab``
stops at ``Module <..._ip> not found``.

    ip = generate_hls_ips(tcls, out_dir, part="xc7z020-clg484-1")
    ip.vhdl_files     # the cores' simulation wrappers, for xvhdl
    ip.libs           # the precompiled libraries they bind (floating_point_v7_1_20), for xelab -L

The wrappers are VHDL and bind behavioral models Vivado ships **precompiled** for xsim
(``<Vivado>/data/xsim/ip``), so nothing beyond the wrapper is compiled: ``xvhdl`` the wrapper into
``work``, then ``xelab -L <lib>``.  Generation is cached like the crossbar's
(:func:`~waveflow.build.axi_xbar.generate_axi_xbar`): by a digest of the scripts and the part, so an
unchanged kernel reuses the cores without starting Vivado.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Libraries every VHDL file may name that are not IP (the compiler's own, and ``work``).
_STD_LIBS = {"ieee", "std", "work"}


@dataclass
class HlsIp:
    """Generated cores: the VHDL wrappers xvhdl compiles and the libraries xelab links."""

    vhdl_files: list[Path] = field(default_factory=list)
    libs: list[str] = field(default_factory=list)


def ip_tcls(vdir: Path) -> list[Path]:
    """The ``<name>_ip.tcl`` csynth left in a module's ``syn/verilog`` -- one per vendor core."""
    return sorted(Path(vdir).glob("*_ip.tcl"))


def csynth_part(solution: Path) -> str | None:
    """The part a solution was synthesized for (``syn/report/csynth.xml``'s ``<Part>``), or None."""
    rpt = Path(solution) / "syn" / "report" / "csynth.xml"
    if not rpt.is_file():
        return None
    m = re.search(r"<Part>([^<]+)</Part>", rpt.read_text(encoding="utf-8", errors="replace"))
    return m.group(1).strip() if m else None


def _module_name(tcl: str) -> str:
    m = re.search(r"-module_name\s+(\S+)", tcl)
    if not m:
        raise ValueError("an _ip.tcl without -module_name")
    return m.group(1)


def _collect(root: Path, names: list[str]) -> HlsIp:
    files = [root / n / "sim" / f"{n}.vhd" for n in names]
    missing = [f for f in files if not f.is_file()]
    if missing:
        raise FileNotFoundError(f"generated HLS IP is missing {missing}")
    libs: list[str] = []
    for f in files:
        for lib in re.findall(r"^\s*library\s+(\w+)\s*;", f.read_text(encoding="utf-8"), re.I | re.M):
            if lib.lower() not in _STD_LIBS and lib not in libs:
                libs.append(lib)
    return HlsIp(vhdl_files=files, libs=libs)


def generate_hls_ips(tcls, out_dir: str | Path, *, part: str, vivado: str | None = None,
                     force: bool = False) -> HlsIp:
    """Generate (or reuse) the cores of *tcls* (``_ip.tcl`` files) for *part* under
    ``out_dir/hls_ip_<digest>/``, in one Vivado batch.  Raises ``RuntimeError`` with Vivado's output
    tail if generation fails.  *part* may be csynth's spelling (``xc7z020-clg484-1``) or Vivado's
    (``xc7z020clg484-1``)."""
    tcls = [Path(t) for t in tcls]
    texts = [t.read_text(encoding="utf-8") for t in tcls]
    names = [_module_name(t) for t in texts]
    h = hashlib.sha256(part.encode())
    for t in texts:
        h.update(t.encode())
    work = Path(out_dir) / f"hls_ip_{h.hexdigest()[:12]}"
    root = work / "ip"
    stamp = work / "done.json"
    if stamp.exists() and not force:
        return _collect(root, names)

    if vivado is None:
        from waveflow.toolchain.toolchain import find_vivado_path
        vivado = find_vivado_path()
    if not vivado:
        raise RuntimeError("generate_hls_ips: Vivado not found (set WAVEFLOW_VIVADO_PATH or PATH)")

    work.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    # csynth writes the part as device-package-speed; Vivado wants device+package for 7-series and
    # keeps the dashes for UltraScale+.  Ask Vivado which one it knows.
    alt = re.sub(r"^(\w+)-(\w+)-", r"\1\2-", part)
    lines = [f"set p [lindex [get_parts -quiet {{{part}}}] 0]",
             f"if {{$p eq \"\"}} {{ set p [lindex [get_parts -quiet {{{alt}}}] 0] }}",
             "if {$p eq \"\"} { error \"no such part\" }",
             "create_project -in_memory -part $p",
             "set_property target_language Verilog [current_project]"]
    for tcl, name in zip(texts, names):
        lines.append(re.sub(r"(-module_name\s+\S+)", rf"\1 -dir {{{root.as_posix()}}}", tcl, count=1))
    script = work / "gen_hls_ip.tcl"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    from waveflow import events

    with events.span("tool", "vivado", script=script.name):
        proc = subprocess.run([vivado, "-mode", "batch", "-nojournal", "-nolog", "-source",
                               script.name], cwd=str(work), capture_output=True, text=True,
                              timeout=900)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(f"generate_hls_ips: Vivado failed:\n{out[-3000:]}")
    ip = _collect(root, names)
    stamp.write_text(json.dumps({"part": part, "cores": names}, indent=1) + "\n", encoding="utf-8")
    return ip
