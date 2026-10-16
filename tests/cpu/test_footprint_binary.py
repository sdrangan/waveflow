"""AC9 of ``plans/cpu_model.md`` (code-bytes part): a kernel's code bytes are read from the binary.

The runner's ``code_bytes`` must equal an independent reading of the cross-compiled binary's symbol
table (``aarch64-linux-gnu-nm -S``): every measured symbol present, clones included, sizes summed.
"""

from __future__ import annotations

import subprocess

import pytest

from waveflow.cpu.calib.gem5 import Gem5Runner
from waveflow.cpu.calib.kernels import KERNELS


@pytest.mark.gem5
def test_code_bytes_equal_the_binarys_symbol_sizes(tmp_path):
    runner = Gem5Runner(workdir=tmp_path)
    why = runner.unavailable()
    if why:
        pytest.skip(why)
    nm = str(runner.cc).removesuffix("gcc") + "nm"
    for name, k in KERNELS.items():
        exe = runner.build(k)
        table = subprocess.run(
            [nm, "-S", str(exe)], capture_output=True, text=True, check=True
        )
        expect = 0
        found = set()
        for line in table.stdout.splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[3].split(".")[0] in k.symbols:
                expect += int(parts[1], 16)
                found.add(parts[3].split(".")[0])
        assert found == set(k.symbols), name
        assert runner.code_bytes(exe, k) == expect > 0, name
