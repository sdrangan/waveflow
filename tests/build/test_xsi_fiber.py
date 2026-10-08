"""The C++ software-thread scheduler (``waveflow/build/xsi/xsi_fiber.h``, plans/host_runtime.md S2).

Compiled and run with a plain g++ -- no Vivado, no xsim -- with **every** compiler at hand: Vivado's
MinGW 6.2 (what run.bat builds testbenches with), its MinGW 9.5, and the ``g++`` on PATH.  Each must
print exactly the expected interleaving, so the fiber backend (Windows fibers or ucontext) and the
compiler change nothing.  The interleaving is SimPy's: threads run in start order, a satisfied wait
returns without a switch, each thread's endpoints are stepped just before it runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

XSI_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"

PROGRAM = r"""
#include "xsi_fiber.h"
#include <cstdio>
#include <string>
#include <vector>
using namespace wfbfm;

static std::vector<std::string> LOG;
static SwScheduler S;
static void note(const std::string& s) { LOG.push_back(std::to_string(S.ticks()) + " " + s); }

// An endpoint stand-in: counts its steps and completes an "operation" 2 steps after it is started.
struct Ep : SwStepper {
    std::string name; long left = -1;
    explicit Ep(const char* n) : name(n) {}
    void step() override { note("step " + name); if (left > 0) --left; }
    void op() { S.uses(this); left = 2; S.wait_until([this] { return left == 0; }); left = -1; }
};

int main() {
    Ep ea("a"), eb("b");
    bool flag = false;
    S.start("main", [&] {
        note("main starts writer");
        S.start("writer", [&] {
            note("writer op");
            eb.op();
            note("writer op done, sets flag");
            flag = true;
            S.wait_ticks(3);
            note("writer end");
        });
        note("main waits flag");
        S.wait_until([&] { return flag; });
        note("main saw flag");
        ea.op();
        note("main op done");
        S.wait_until([&] { return flag; });            // already true: no switch
        note("main end");
    });
    for (int c = 0; c < 20 && !S.all_done(); ++c) S.tick();
    for (auto& l : LOG) std::printf("%s\n", l.c_str());
    std::printf("DONE %d ticks=%ld\n", (int)S.all_done(), S.ticks());
    return S.all_done() ? 0 : 1;
}
"""

EXPECTED = """\
1 main starts writer
1 main waits flag
1 writer op
2 step b
3 step b
3 writer op done, sets flag
3 main saw flag
4 step a
4 step b
5 step a
5 main op done
5 main end
5 step b
6 step b
6 writer end
DONE 1 ticks=6"""


def _compilers() -> list[tuple[str, Path]]:
    found = []
    for v in ("6.2.0", "10.0.0"):
        for root in sorted(Path("C:/Xilinx").glob(f"*/Vivado/tps/mingw/{v}/win64.o/nt/bin")):
            gxx = root / "g++.exe"
            if gxx.is_file():
                found.append((f"vivado-mingw-{v}", gxx))
                break
    path_gxx = shutil.which("g++")
    if path_gxx:
        found.append(("path-g++", Path(path_gxx)))
    return found


COMPILERS = _compilers()


@pytest.mark.skipif(not COMPILERS, reason="no g++ found")
@pytest.mark.parametrize("label,gxx", COMPILERS, ids=[c[0] for c in COMPILERS])
def test_scheduler_interleaving_is_simpy_order(label, gxx, tmp_path):
    src = tmp_path / "fiber_test.cpp"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "fiber_test.exe"
    env = dict(os.environ, PATH=f"{gxx.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    std = "-std=c++14" if "6.2" in label else "-std=c++17"
    r = subprocess.run([str(gxx), std, "-O2", "-Wall", f"-I{XSI_SRC}", str(src), "-o", str(exe)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"{label}: does not compile:\n{r.stderr[-3000:]}"
    outs = set()
    for _ in range(5):
        run = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=60)
        assert run.returncode == 0, f"{label}: run failed:\n{run.stdout}{run.stderr}"
        outs.add(run.stdout.strip().replace("\r\n", "\n"))
    assert outs == {EXPECTED}, f"{label}:\n" + "\n---\n".join(outs)
