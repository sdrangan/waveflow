"""Parse Vitis HLS cosim reports.

``CosimReportParser`` mirrors :class:`waveflow.utils.csynthparse.CsynthParser`:
construct it with a solution path, and it locates + parses the cosim report
file Vitis HLS writes after ``cosim_design``.

Vitis 2025.1+ writes a structured table at
``<sol>/sim/report/<top>_cosim.rpt``; older versions wrote a flat
``<sol>/sim/report/cosim.log`` with a ``Total Execution Time`` line.  Both
shapes are handled transparently — public callers only see
:meth:`get_transaction_cycles`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


class CosimReportParser:
    """Parse a Vitis HLS cosim report and extract the transaction cycle count.

    Parameters
    ----------
    sol_path : str | Path | None
        Path to the solution directory (e.g. ``waveflow_poly_w32_proj/solution1``).
        The cosim report is discovered under ``<sol>/sim/report/``.
    report_path : str | Path | None
        Explicit path to the cosim report file.  Takes precedence over
        ``sol_path``.  Useful for fixture-based tests that don't have a
        full Vitis solution layout.
    top : str | None
        Top module name; needed to find the ``<top>_cosim.rpt`` Vitis 2025.1+
        report.  When ``sol_path`` is given but ``top`` is not, the parser
        falls back to globbing for ``*_cosim.rpt`` and using the first match,
        then to ``cosim.log``.

    Raises
    ------
    FileNotFoundError
        If no cosim report can be located.  The error message lists every
        candidate path the parser tried.
    """

    # Vitis 2025.1+ table column layout (first numeric column after the
    # status word is "Latency(Clock Cycles) min").  We grab all numeric
    # columns from the matched row.
    _TABLE_ROW_RE = re.compile(
        r"^\|\s*\w+\s*\|\s*(Pass|NA)\s*\|(?P<numbers>.+)\|\s*$"
    )

    # Legacy cosim.log line, e.g.: "Total Execution Time: 144 cycles"
    _TOTAL_TIME_RE = re.compile(
        r"Total\s+Execution\s+Time\s*:?\s*(\d+)\s*(?:clock\s*)?cycles",
        re.IGNORECASE,
    )

    def __init__(
        self,
        sol_path: str | Path | None = None,
        report_path: str | Path | None = None,
        top: str | None = None,
    ) -> None:
        if sol_path is None and report_path is None:
            raise ValueError("Either sol_path or report_path must be provided.")
        self.sol_path = Path(sol_path) if sol_path is not None else None
        self.top = top
        self._candidates: list[Path] = []
        if report_path is not None:
            self.report_path = Path(report_path)
            if not self.report_path.exists():
                raise FileNotFoundError(
                    f"Cosim report not found: {self.report_path}"
                )
            return
        assert self.sol_path is not None
        sim_report_dir = self.sol_path / "sim" / "report"
        # Vitis 2025.1+ layout — preferred.
        if top is not None:
            self._candidates.append(sim_report_dir / f"{top}_cosim.rpt")
        # Glob fallback when top is unknown — newer Vitis emits one rpt.
        self._candidates.extend(sorted(sim_report_dir.glob("*_cosim.rpt")))
        # Legacy layout.
        self._candidates.append(sim_report_dir / "cosim.log")
        for cand in self._candidates:
            if cand.exists():
                self.report_path = cand
                return
        raise FileNotFoundError(
            "Cosim report not found. Tried:\n  "
            + "\n  ".join(str(c) for c in self._candidates)
        )

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def get_transaction_cycles(self) -> int | None:
        """Return the kernel's measured per-transaction latency in cycles.

        For both report formats the parser uses the same definition: the
        first numeric latency column for a Verilog or VHDL row that
        reports ``Pass``.  When the report is the legacy ``cosim.log``,
        the value is pulled from the ``Total Execution Time`` line.

        Returns
        -------
        int | None
            Cycle count, or ``None`` if no passing-status row could be
            parsed (e.g. cosim failed before emitting a latency row).
        """
        if self.report_path.suffix == ".rpt":
            return self._parse_rpt_table()
        return self._parse_log()

    # ------------------------------------------------------------------
    # Format-specific implementations
    # ------------------------------------------------------------------

    def _parse_rpt_table(self) -> int | None:
        """Parse the 2025.1+ ``<top>_cosim.rpt`` table.

        Returns the first row's first numeric Latency column (== ``min``)
        for a row marked ``Pass``.  Vitis reports min == avg == max for
        single-transaction kernels like poly, so any of the three columns
        would do; ``min`` is the conservative choice.
        """
        for line in self.report_path.read_text(encoding="utf-8").splitlines():
            m = self._TABLE_ROW_RE.match(line)
            if m is None or m.group(1) != "Pass":
                continue
            numbers = [
                int(p.strip())
                for p in m.group("numbers").split("|")
                if p.strip().isdigit()
            ]
            if not numbers:
                continue
            return numbers[0]   # Latency(Clock Cycles) min
        return None

    def _parse_log(self) -> int | None:
        """Parse the legacy ``cosim.log``: scan for ``Total Execution Time``."""
        text = self.report_path.read_text(encoding="utf-8")
        m = self._TOTAL_TIME_RE.search(text)
        if m is None:
            return None
        return int(m.group(1))
