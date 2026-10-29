"""records.py -- the system's message schemas (spec §5.4; never packed by hand).

The done record every engine and the Ingest DMA push into the Resp FIFO, and the engines' and the
Ingest DMA's register banks.
"""

from __future__ import annotations

from typing import ClassVar

from waveflow.hw.dataschema import DataList
from waveflow.linalg.message import U8, U16, U32

#: The done record's sources.
SRC_SYS, SRC_VEC, SRC_INGEST = 0, 1, 2


class Resp(DataList):
    """One record in the Resp FIFO: which source, its job (or chunk), its status; one 64-bit word."""

    include_filename: ClassVar[str | None] = "ltbf_resp.h"
    elements: ClassVar[dict] = {
        "src": {"schema": U8, "description": "0 systolic, 1 vector unit, 2 Ingest DMA"},
        "status": {"schema": U8, "description": "the job's status (0 OK)"},
        "tx_id": {"schema": U16, "description": "the job's tx_id (a chunk: 0)"},
        "seq": {
            "schema": U16,
            "description": "the Ingest DMA's chunk sequence number (a job: 0)",
        },
    }


class SysCfg(DataList):
    """The systolic wrapper's thin Ctrl: configuration (user, 2026-10-28)."""

    include_filename: ClassVar[str | None] = "ltbf_sys_cfg.h"
    elements: ClassVar[dict] = {
        "enable": {"schema": U32, "description": "1: accept jobs"}
    }


class SysStatus(DataList):
    """The systolic wrapper's thin Ctrl: status, the latest after every job."""

    include_filename: ClassVar[str | None] = "ltbf_sys_status.h"
    elements: ClassVar[dict] = {
        "jobs_done": {"schema": U32, "description": "jobs finished since reset"},
        "last_tx_id": {"schema": U16, "description": "the last finished job's tx_id"},
        "last_status": {"schema": U8, "description": "its status"},
    }
