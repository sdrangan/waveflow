"""Check accepted AXIS beats at the actual synthesized kernel boundary."""
from bisect import bisect_right
from pathlib import Path
import subprocess

import numpy as np
from vcdvcd import VCDVCD

from waveflow.toolchain import find_vivado_path


def dump_cosim_vcd(sim_dir):
    """Replay the compiled XSim snapshot with a small boundary-only VCD dump."""
    sim_dir = Path(sim_dir)
    script = sim_dir / "frames.tcl"
    script.write_text('open_vcd frames.vcd\n'
        'log_vcd /apatb_poly_top/AESL_inst_poly/*\n'
        'run all\nclose_vcd\nquit\n')
    executable = Path(find_vivado_path()).with_name("xsim")
    result = subprocess.run([str(executable), "poly", "-tclbatch", script.name,
        "-testplusarg", "UVM_VERBOSITY=UVM_NONE", "-testplusarg", "UVM_TESTNAME=poly_test_lib",
        "-testplusarg", "UVM_TIMEOUT=20000000000000"], cwd=sim_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180)
    (sim_dir / "frames.log").write_text(result.stdout)
    if result.returncode or "ERROR:" in result.stdout or not (sim_dir / "frames.vcd").is_file():
        raise RuntimeError(f"XSim VCD replay failed: {sim_dir / 'frames.log'}")
    return sim_dir / "frames.vcd"


def read_frames(path):
    vcd = VCDVCD(str(path))
    prefix = "apatb_poly_top.AESL_inst_poly."
    def trace(name):
        candidates = [s for s in vcd.signals if s.startswith(prefix)
                      and s[len(prefix):].split("[")[0] == name]
        if len(candidates) != 1:
            raise ValueError(f"missing or ambiguous boundary signal: {name}")
        tv = vcd[candidates[0]].tv
        return [t for t, _ in tv], [v for _, v in tv]
    def at(tv, time):
        index = bisect_right(tv[0], time) - 1
        return tv[1][index] if index >= 0 else "x"
    clock = trace("ap_clk")
    edges = [t for t, value in zip(*clock) if value == "1" and t > 0]
    reset = trace("ap_rst_n")
    result = {}
    for port in ("s_in", "m_out"):
        signals = {n: trace(f"{port}_{n}") for n in ("TVALID", "TREADY", "TDATA", "TLAST", "TKEEP", "TSTRB")}
        frames, words = [], []
        for edge in edges:
            # Values immediately before the rising edge define the accepted beat.
            t = edge - 1
            if at(reset, t) != "1" or any(at(signals[n], t) != "1" for n in ("TVALID", "TREADY")):
                continue
            try:
                data = {n: int(at(tv, t), 2) for n, tv in signals.items()}
            except ValueError as exc:
                raise ValueError(f"unknown AXIS bits at {port}:{edge}") from exc
            if data["TKEEP"] != 15 or data["TSTRB"] != 15:
                raise ValueError("partial words are unsupported in the 32-bit reference flow")
            words.append(data["TDATA"])
            if data["TLAST"]:
                frames.append(words)
                words = []
        if words:
            raise ValueError(f"unterminated AXIS frame on {port}")
        result[port] = frames
    return result
