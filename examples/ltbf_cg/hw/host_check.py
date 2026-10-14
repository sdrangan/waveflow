"""host_check.py -- the engine body compiled with g++ (no Vitis) against the model, word for word.

A fast functional check of ``hw/cpp/ltbf_engine_task.h`` before any csynth: the scenario's input
words go through ``ltbf_engine_task`` once per job (the ``hls::stream`` C model), and every output
word must equal :func:`examples.ltbf_cg.hw.engine.golden`.  The RTL gate is the XSI system run.

    python -m examples.ltbf_cg.hw.host_check
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

from examples.ltbf_cg.hw import engine as eg

HLS_INCLUDE = Path("/tools/Xilinx/Vitis_HLS/2024.1/include")

MAIN = r"""
#include <cstdio>
#include <fstream>
#include "ltbf_engine_task.h"
int main(int argc, char** argv) {
    std::ifstream in(argv[1]);
    std::ofstream out(argv[2]);
    int jobs;
    in >> jobs;
    for (int j = 0; j < jobs; ++j) {
        hls::stream<ap_uint<64> > s_in, m_out;
        long nw;
        in >> nw;
        for (long k = 0; k < nw; ++k) { unsigned long long v; in >> v; s_in.write(v); }
        ltbf_engine_task(s_in, m_out);
        while (!m_out.empty()) out << (unsigned long long)m_out.read() << "\n";
    }
    return 0;
}
"""


def host_check(system: eg.LtbfSystem, work: Path) -> int:
    """Build and run the body on *system*'s scenario in *work*; return the mismatching words."""
    work = Path(work)
    eg.generate(work, system.knobs())
    f = eg.hw_formats(int(system.W))
    jobs = system.jobs()
    lines = [str(len(jobs))]
    for job in jobs:
        words = np.concatenate(eg.job_words(job, f, int(system.R), int(system.C)))
        lines.append(str(len(words)))
        lines += [str(int(w)) for w in words]
    (work / "in.txt").write_text("\n".join(lines) + "\n")
    (work / "main.cpp").write_text(MAIN)
    exe = work / "host_tb"
    subprocess.run(
        ["g++", "-O2", "-std=c++14", f"-I{HLS_INCLUDE}", f"-I{work / 'include'}",
         str(work / "main.cpp"), "-o", str(exe)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    subprocess.run([str(exe), str(work / "in.txt"), str(work / "out.txt")], check=True)
    got = np.array(
        [int(t) for t in (work / "out.txt").read_text().split()], dtype=np.uint64
    )
    want = np.concatenate(system.expected())
    if got.size != want.size:
        return max(got.size, want.size)
    return int(np.sum(got != want))


if __name__ == "__main__":
    bad = 0
    for explicit in (False, True):
        s = eg.LtbfSystem(explicit=explicit)
        n = host_check(s, eg.HERE / "build" / ("host_e64" if explicit else "host_f64"))
        print("explicit" if explicit else "factored", "mismatching words:", n)
        bad += n
    sys.exit(1 if bad else 0)
