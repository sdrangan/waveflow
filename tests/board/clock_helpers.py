"""Synthetic clock evidence for software tests; never physical-board evidence."""
from pathlib import Path

from waveflow.board import Board, ClockProfile, ClockRequest, ClockResolution
from waveflow.board.clock import _context
from waveflow.build.build import BuildConfig


def synthetic_clock(board, requested_hz=100e6, hls_target_hz=None, *, source="ps_pl0"):
    request = ClockRequest(source, requested_hz,
                           requested_hz if hls_target_hz is None else hls_target_hz)
    return ClockResolution(
        request=request, profile=ClockProfile(requested_hz, request.hls_target_hz),
        board_fingerprint=_context(board, source)[1],
        generator_parameters={"clock_source": "IOPLL", "reference_hz": 33_333_330,
                              "divisor0": 15, "divisor1": 1},
        work_dir=Path.cwd(), resolved_at="synthetic test fixture",
    )


def resolved_config(root, *, board=None, requested_hz=100e6, hls_target_hz=None, **kwargs):
    board = Board.rfsoc4x2() if board is None else board
    return BuildConfig(root_dir=root, board=board,
                       resolved_clock=synthetic_clock(board, requested_hz, hls_target_hz),
                       **kwargs)
