"""Build and run the g++ check of the HLS bodies on a scenario; compare with the golden."""
import subprocess, sys
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from vmul import W, VmulResp, expected, scenario

import os
# AMD's open-source ap_int: git clone https://github.com/Xilinx/HLS_arbitrary_Precision_Types
APT = Path(os.environ.get("AP_TYPES_INCLUDE", "/tmp/claude-0/apt/include"))


def run(kind: str):
    jobs, image = scenario(kind)
    (HERE / "in").mkdir(exist_ok=True); (HERE / "out").mkdir(exist_ok=True)
    np.concatenate([c.serialize(word_bw=W) for c, *_ in jobs]).astype("<u8").tofile(HERE / "in/cmds.bin")
    image.astype("<u8").tofile(HERE / "in/image.bin")
    exe = HERE / "vmul_csim"
    subprocess.run(["g++", "-std=c++17", "-O1", "-w", f"-I{HERE/'mock'}", f"-I{APT}",
                    f"-I{HERE.parent/'include'}", f"-I{HERE.parent/'src'}", str(HERE / "vmul_csim.cpp"),
                    "-o", str(exe)], check=True)
    out = subprocess.run([str(exe)], cwd=HERE, check=True, capture_output=True, text=True).stdout
    mem = np.fromfile(HERE / "out/mem.bin", "<u8")
    done = np.fromfile(HERE / "out/done.bin", "<u8")
    want_resp, want_mem = expected(kind)
    rw = VmulResp.nwords_per_inst(W)
    got = [VmulResp().deserialize(done[i:i + rw], word_bw=W) for i in range(0, len(done), rw)]
    ok_resp = [(r.tx_id, r.n, r.code) for r in got] == [(r.tx_id, r.n, r.code) for r in want_resp]
    ok_mem = np.array_equal(mem, want_mem)
    print(f"{kind}: {out.strip()} | responses {len(got)}/{len(want_resp)} match={ok_resp} | memory match={ok_mem}")
    return ok_resp and ok_mem


if __name__ == "__main__":
    sys.exit(0 if all([run("test"), run("error")]) else 1)
