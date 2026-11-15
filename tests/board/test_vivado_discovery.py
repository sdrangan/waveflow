from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from waveflow.board import Board
from waveflow.toolchain import find_vivado_path, run_vivado


@pytest.mark.vivado
def test_vivado_discovers_packaged_rfsoc4x2_board(tmp_path):
    if find_vivado_path() is None:
        pytest.skip("Vivado installation not found; skipping board-file discovery test")

    board = Board.rfsoc4x2()
    script = Path(str(files("waveflow.board").joinpath("tcl", "discover_board.tcl")))
    result = run_vivado(
        script,
        tmp_path,
        args=[board.board_repo_dir, board.board_part, board.part],
        required_version=board.vivado_version,
    )
    assert f"WAVEFLOW_BOARD_OK: {board.board_part} -> {board.part}" in result.stdout
    assert (tmp_path / "vivado.log").is_file()
    assert (tmp_path / "vivado.jou").is_file()
