from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from waveflow.board import Board
from waveflow.toolchain import find_vivado_path, run_vivado


@pytest.mark.vivado
def test_xczu48dr_synthesis_license_smoke(tmp_path):
    """Fail separately from board/BD validation when xczu48dr synthesis is unavailable."""
    if find_vivado_path() is None:
        pytest.skip("Vivado installation not found; skipping synthesis-license smoke test")

    board = Board.rfsoc4x2()
    script = Path(
        str(files("waveflow.board").joinpath("tcl", "synthesis_license_smoke.tcl"))
    )
    result = run_vivado(
        script,
        tmp_path,
        args=[board.part],
        required_version=board.vivado_version,
    )
    assert f"WAVEFLOW_SYNTH_LICENSE_OK: {board.part}" in result.stdout
