import math
import re
from dataclasses import dataclass, field
from enum import IntEnum
from sys import prefix
from typing import Literal
import matplotlib.pyplot as plt
import numpy as np
from vcdvcd import VCDVCD

from waveflow.utils.timing import SigTimingInfo


class AximmBeatType(IntEnum):
    """Per-cycle data-phase status for an extracted AXI-MM burst."""

    TRANSFER = 0
    IDLE = 1
    STALL = 2


@dataclass
class AxisBurst:
    """One AXI4-Stream burst as a per-beat timeline.

    The canonical burst structure, produced by two sources so timing tooling is source-agnostic:
    a live capture (the pysim ``StreamSink`` / its XSI ``AxisSlave`` twin) *or* a VCD post-analysis
    (:meth:`VcdParser.extract_axis_bursts`).  Per-burst begin/end are just derived views of the
    per-beat timeline (``tstart``, ``tstart + len(beat_type) * clk_period``); the valuable part is
    ``beat_type`` — the occupancy timeline (where the gaps and backpressure are).

    Attributes
    ----------
    data : np.ndarray
        The transferred words (uint32), one per *transfer* beat.
    start_idx : int
        Clock-edge index of the first beat of the burst.
    tstart : float
        Time of the first beat (same units as the extractor's ``clk_period``).
    beat_type : list[int]
        Per-beat status over the whole burst span, each an :class:`AximmBeatType`:
        ``0`` transfer, ``1`` idle (``tvalid=0``), ``2`` stall (``tready=0``).
    """

    data: np.ndarray
    start_idx: int
    tstart: float
    beat_type: list[int] = field(default_factory=list)

    @property
    def n_transfers(self) -> int:
        """Number of transfer beats (``== len(data)``)."""
        return int(np.asarray(self.data).size)

    @property
    def n_beats(self) -> int:
        """Total beats spanned, including idle/stall bubbles."""
        return len(self.beat_type)

    @classmethod
    def from_dict(cls, d: dict) -> "AxisBurst":
        """Bridge a legacy ``extract_axis_bursts`` dict (``data``/``start_idx``/``tstart``/
        ``beat_type``) into an ``AxisBurst`` — lets callers converge without the extractor itself
        being rewritten yet (see ``plans/stream_tb_file_vectors.md``, Stage 4)."""
        return cls(
            data=np.asarray(d["data"], dtype=np.uint32),
            start_idx=int(d["start_idx"]),
            tstart=float(d["tstart"]),
            beat_type=list(d["beat_type"]),
        )


def vcd_trace(trace_level: str) -> str:
    """Map a user trace level to the HLS cosim VCD trace pattern.

    ``"port"`` and ``"all"`` pass through unchanged; any other value maps to
    ``"*"`` (the xsim "trace everything" pattern).
    """
    return trace_level if trace_level in ("port", "all") else "*"


def binary_str_to_numeric(
        bin_str : str,
        dtype : str,
        wid : int) -> int | float:
    """
    Converts a binary string to a numeric value of the specified type.
    Parameters
    ----------
    bin_str : str
        Binary string to convert (e.g., '1101').
    dtype : str
        Target data type ('int', 'uint' or 'float').
    wid : int
        Width of the data type string. (e.g, 8, 16, 32, 64).
    Returns
    -------
    value : int | float
        Converted numeric value.
    """
    # Check signal is binary having only '0' and '1'
    if not re.fullmatch(r'[01]+', bin_str):
        raise ValueError(f"Invalid binary string: {bin_str}")

    # Zero pad the binary string to the specified width
    bin_str = bin_str.zfill(wid)

    if dtype == 'int':
        # Signed integer conversion
        if bin_str[0] == '1':  # negative number
            value = int(bin_str, 2) - (1 << wid)
        else:
            value = int(bin_str, 2)
    elif dtype == 'uint':
        # Unsigned integer conversion
        value = int(bin_str, 2)
    elif dtype == 'float':
        # Float conversion (assuming IEEE 754 format)
        if wid == 32:
            int_value = int(bin_str, 2)
            value = np.frombuffer(int_value.to_bytes(4, byteorder='big'), dtype=np.float32)[0]
        elif wid == 64:
            int_value = int(bin_str, 2)
            value = np.frombuffer(int_value.to_bytes(8, byteorder='big'), dtype=np.float64)[0]
    else:
        raise ValueError(f"Unsupported data type: {dtype}")
    return value

class SigInfo(object):
    """
    Class to hold information about a VCD signal.

    Attributes
    ----------
    name : str
        Full name of the signal.
    two_level : bool
        True if the signal is two-level (0 and 1).
    numeric_type  : str
        Type of numeric data ('str', 'int', 'uint', 'float').
    numeric_fmt_str : str
        Format string for numeric display.   If None, a default format is used.
    is_clock : bool
        True if the signal is identified as a clock.
    values : list of str
        List of signal values from the VCD file
    times : list of int
        List of time points corresponding to the signal values.
    disp_vals : list of str
        List of signals values for display (after formatting).
    short_name : str
        Short name of the signal (e.g., last part of full name).
    wid : int | None
        Bitwidth of the signal.  If None, the bitwidth will be inferred
        from the name.  For example, a signal named 'data[15:0]' has a bitwidth of 16.
    """
    def __init__(
            self,
            name : str,
            tv : list[tuple[int, str]],
            time_scale : float = 1e3,
            numeric_type : str = 'uint',
            numeric_fmt_str : None | str = None,
            wid : int | None = None):
        self.name = name
        self.two_level = False
        self.numeric_type = numeric_type # 'int', 'uint', 'float'
        self.numeric_fmt_str = numeric_fmt_str  
        self.is_clock = False
        self.time_scale = time_scale
        self.wid = wid
        #: Samples that were X/Z and converted to 0; set by `get_values`.  Nonzero is normal for
        #: an internal net (X before reset) and suspicious for a driven boundary port.
        self.unknown_count = 0

        # Get time and value lists
        n  = len(tv)
        self.times = np.zeros(n, dtype=float)
        self.values = []
        for i, (t, v) in enumerate(tv):
            self.values.append(v)
            self.times[i] = t / self.time_scale  # Scale time
        self.short_name = name.split('.')[-1]
        self.disp_values = None
        self.numeric_values = None

        self.set_format()

    def set_format(self):
        """
        Auto-detects the format of the signal based on its values.
        Right now this only works for Vivado-generated VCDs where the
        values are text strings.  
        
        The format can be over-written later if needed.
        """

        # Estimate the bitwidth if not provided
        if self.wid is None:
            parts = self.name.split('[')
            if len(parts) > 1:
                bit_range = parts[-1].strip(']')
                msb_lsb = bit_range.split(':')
                if len(msb_lsb) == 2:
                    msb = int(msb_lsb[0])
                    lsb = int(msb_lsb[1])
                    self.wid = abs(msb - lsb) + 1
                else:
                    self.wid = 1
            else:
                self.wid = 1
    
        # Remove un-specified values
        filtered = [v for v in self.values if v not in {'x', 'X', 'z', 'Z'}]

        # Check if all values are single-bit '0' or '1'
        if all(v in {'0', '1'} for v in filtered):
            self.two_level = True
            self.numeric_type  = 'uint'
            self.numeric_fmt_str = '%d'

        # Check if clock signal
        if self.name:
            name_lower = self.name.lower()
            if 'clock' in name_lower or 'clk' in name_lower:
                self.is_clock = True

        # Set the numeric format string if not provided
        if self.numeric_fmt_str is None:
            if self.numeric_type == 'int':
                self.numeric_fmt_str = f"%d"
            elif self.numeric_type == 'uint':
                self.numeric_fmt_str = f"%X"
            elif self.numeric_type == 'float':
                self.numeric_fmt_str = f"%.3f"
            else:
                self.numeric_fmt_str = "%s"  # default to string

    def get_values(self):
        """
        Converts the signal numeric and display values based on the format.

        For ``numeric_type == 'uint'`` the storage convention matches the
        waveflow serialization methods:

        * ``wid <= 32``  → ``np.ndarray`` with dtype ``np.uint32``
        * ``wid <= 64``  → ``np.ndarray`` with dtype ``np.uint64``
        * ``wid > 64``   → ``np.ndarray`` with dtype ``np.uint64`` and shape
          ``(n, k)`` where ``k = ceil(wid / 64)``.  Word 0 holds the least-
          significant 64 bits (LSW-first order).
        """

        # Return if already computed
        if self.disp_values is not None and self.numeric_values is not None:
            return

        self.disp_values = []
        raw_values = []
        self.unknown_count = 0
        for v in self.values:
            d = str(v)  # Default is to display original value
            num_value = 0
            # Unknown samples convert to 0.  The test is on the WHOLE string, not membership in
            # {'x','z'}: a vector can be partially unknown ('x0000...'), which the scalar-only
            # test missed -- it fell through to binary_str_to_numeric and raised.  Every internal
            # RTL net is X before reset, so this is the normal case for anything except a
            # BFM-driven boundary port.  `unknown_count` keeps it from being silent.
            if re.fullmatch(r'[01]+', str(v)):
                num_value = binary_str_to_numeric(
                    v, self.numeric_type, self.wid)
                d = self.numeric_fmt_str % num_value
            else:
                self.unknown_count += 1
            raw_values.append(num_value)
            self.disp_values.append(d)

        if self.numeric_type == 'uint':
            n = len(raw_values)
            if self.wid <= 32:
                self.numeric_values = np.array(raw_values, dtype=np.uint32)
            elif self.wid <= 64:
                self.numeric_values = np.array(raw_values, dtype=np.uint64)
            else:
                # Pack each wide integer into k 64-bit words (LSW first)
                k = math.ceil(self.wid / 64)
                arr = np.zeros((n, k), dtype=np.uint64)
                _mask64 = (1 << 64) - 1  # Python int mask — avoids overflow
                for i, val in enumerate(raw_values):
                    for j in range(k):
                        arr[i, j] = np.uint64((val >> (j * 64)) & _mask64)
                self.numeric_values = arr
        else:
            self.numeric_values = np.array(raw_values)

    


class VcdParser(object):
    """
    Class to parse VCD signals and extract information.

    Attributes
    ----------
    sig_info : dict[str, SigInfo]
        Information for each signal to be processed.
    time_scale : float
        Time scaling factor (default: 1e3 for ns).
    """
    def __init__(
            self, 
            vcd : VCDVCD):
        """
        Parameters
        ----------
        vcd : VCDVCD
            Parsed VCD object to initialize the viewer.
        """

        self.vcd = vcd        
        self.sig_info = dict()
        self.time_scale = 1e3  # default to ns

  
    def add_signal(
            self,
            name : str,
            short_name : str | None = None,
            numeric_type : str = 'int',
            numeric_fmt_str : str | None = None):
        """ 
        Adds a signal to be processed

        Parameters
        ----------
        name : str
            Full name of the signal to add.
        short_name : str | None
            Short name to use for the signal.  If None, the last part of the full name is used.
        numeric_type : str
            Numeric type of the signal ('int', 'uint', 'float').
        numeric_fmt_str : str | None
            Format string for displaying the numeric values.
        """
        for s in self.vcd.signals:
            if s == name:
                sig_info = SigInfo(
                    name, 
                    self.vcd[s].tv, 
                    time_scale=self.time_scale,
                    numeric_type=numeric_type,
                    numeric_fmt_str=numeric_fmt_str)
                self.sig_info[s] = sig_info                
                if short_name is not None:
                    self.sig_info[s].short_name = short_name
                return
        raise ValueError(f"Signal '{name}' not found in VCD.")

    def add_signals_prefix(self, prefix : str ='s_axi_ctrl'):
        """ 
        Adds all signals with a prefix to sig_info.  If the signal is of the form:

           *{prefix}_{short_name}  or *{prefix}.{short_name}

        The signal will be added with short_name as the short name.
        """
        pattern = re.compile(re.escape(prefix) + r"[_\.]?(.*)", flags=re.IGNORECASE)

        found = False
        for s in self.vcd.signals:
            if prefix in s.lower():
                m = pattern.search(s)
                self.add_signal(s)
                self.sig_info[s].short_name = m.group(1)
                print(f"Added signal with prefix '{prefix}': {s} as {m.group(1)}")
                found = True
        if not found:
            print(f"No signals with prefix '{prefix}' found in VCD.")
       
    def add_clock_signal(
            self, 
            name : str | None = None) -> str:
        """
        Adds a clock signal to sig_info and marks it as a clock.
        Parameters
        ----------
        name : str
            Name that must be contained in the signal along with 'clock' or 'clk' to be added.

        Returns
        -------
        full_name : str
            Full name of the clock signal added.
        """
        for s in self.vcd.signals:
            name_lower = s.lower()
            if (('clock' in name_lower) or ('clk' in name_lower)) and (name is None or name in s):
                name = s
                break
        if name is None:
            raise ValueError("No clock signal found in VCD.")
        self.add_signal(name)
        self.sig_info[name].is_clock = True
        self.sig_info[name].short_name =  'clk'

        return name

    def add_status_signals(
            self, 
            prefix : str ='AESL_'):
        """
        Adds the status signals to disp_signals.

        Following the Vivado HLS naming convention, the signals added 
        are those ending with {prefix} + one of
        'clock', 'start', 'done', 'idle', 'ready'.

        Parameters
        ----------
        prefix : str
            Prefix for the status signals
        """
        suffixes = ['clock', 'start', 'done', 'idle', 'ready']
        for s in self.vcd.signals:
            for suf in suffixes:
                if s.endswith(f"{prefix}{suf}"):
                    self.add_signal(s)
                    self.sig_info[s].short_name = suf

    def add_axiss_signals(
            self,
            name : str | None = None,
            short_name_prefix : str | None = None,
            ignore_multiple : bool = False) -> dict[str, str]:
        """
        Adds signals that are part of an AXI4-Stream interface.

        Parameters
        ----------
        name : str | None
            If provided, only signals containing this substring are considered.
        short_name_prefix : str | None
            If provided, this prefix is added to the short names of the signals.
        ignore_multiple : bool
            If True, if multiple signals are found for an AXI4-Stream keyword, the first one is used and a warning is printed.  If False, an error is raised.

        Returns
        -------
        axi_sigs : dict[str, str]
            Dictionary mapping AXI4-Stream keywords to signal names.
        bitwidth : int
            Bitwidth of the TDATA signal.
        """
        axi4s_keywords = ['tdata', 'tvalid', 'tready', 'tlast']
        axi_sigs = dict()
        for kw in axi4s_keywords:
            axi_sigs[kw] = None
            for s in self.vcd.signals:
                if kw in s.lower() and (name is None or name in s):
                    if axi_sigs[kw] is not None:
                        if ignore_multiple:
                            print(f"Warning: Multiple signals found for AXI4-Stream keyword '{kw}'. Using '{axi_sigs[kw]}' and ignoring '{s}'.")
                            continue
                        else:
                            raise ValueError(f"Multiple signals found for AXI4-Stream keyword '{kw}'.")
                    axi_sigs[kw] = s
                    self.add_signal(s)
                    if short_name_prefix:
                        short_name = f"{short_name_prefix}_{kw.upper()}"
                    elif name:
                        short_name = f"{name}_{kw.upper()}"
                    else:  
                        short_name = kw.upper()
                    self.sig_info[s].short_name = short_name

            # Check if signal is found, except 'tlast' which is optional.
            if (axi_sigs[kw] is None) and (kw != 'tlast'):
                raise ValueError(f"No signal found for AXI4-Stream keyword '{kw}'.")
            
        # Get the bitwidth from the TDATA signal.
        # The signal ends in [N:0], so the width is N+1
        tdata_sig = axi_sigs['tdata']
        tdata_parts = tdata_sig.split('[')
        bitwidth = None
        if len(tdata_parts) > 1:
            bit_range = tdata_parts[-1].strip(']')
            msb_lsb = bit_range.split(':')
            if len(msb_lsb) == 2:
                msb = int(msb_lsb[0])
                bitwidth = msb + 1       

        if bitwidth is None:
            raise ValueError(f"Could not determine bitwidth from TDATA signal '{tdata_sig}'.")   
                  
        return axi_sigs, bitwidth

    def add_aximm_signals(
            self,
            prefix : str | None = None,
            dir : Literal['read', 'write', 'both'] = 'both',
            lite_only : bool = False,
            short_name_prefix : str | None = None,
            ignore_multiple : bool = False,
            confirm_exists : bool = True) -> dict[str, str]:
        """
        Adds signals that are part of an AXI4-MM interfaces (either AXI4-Lite or AXI4-Full)
        
        Parameters
        ----------
        prefix : str | None
            If provided, only signals containing this `prefix{kw}`.
        short_name_prefix : str | None
            If provided, this prefix is added to the short names of the signals.
        dir : Literal['read', 'write', 'both']
            Direction of the AXI4-MM interface to consider.  
        lite_only : bool
            If True, only consider AXI4-Lite signals (i.e., without burst signals 
            like AWLEN, ARLEN, etc.).  If False, consider all AXI4-MM signals.
        ignore_multiple : bool
            If True, if multiple signals are found for an AXI4-Stream keyword, the first one is used and a warning is printed.  If False, an error is raised.
        confirm_exists : bool
            If True, an error is raised if any expected signal is not found.  If False, missing signals 
            are ignored.

        Returns
        -------
        axi_sigs : dict[str, str]
            Dictionary mapping AXI4-Stream keywords to signal names for the signals.
        bitwidths: dict[str, int]
            Dictionary mapping AXI4-MM data signal keywords to their bitwidths.
            The signal keywords are 'RDATA', 'WDATA', 'AWADDR' and 'ARADDR' if they are found.
        """


        # Determine signals to look for
        axi4s_keywords = []
        if dir in ['write', 'both']:
            axi4s_keywords += ['AWADDR', 'AWVALID', 'AWREADY', 'WDATA', 'WVALID', 'WREADY', 'BVALID', 'BREADY']
            if not lite_only:
                 axi4s_keywords += ['AWLEN', 'WLAST']
        if dir in ['read', 'both']:
            axi4s_keywords += ['ARADDR', 'ARVALID', 'ARREADY', 'RDATA', 'RVALID', 'RREADY', ]
            if not lite_only:
                axi4s_keywords += ['ARLEN', 'RLAST']

        axi_sigs = dict()
        for kw in axi4s_keywords:
            axi_sigs[kw] = None
            tgt = f"{prefix}{kw}" if prefix else kw
            tgt = tgt.lower()
            for s in self.vcd.signals:
                if tgt in s.lower():
                    if axi_sigs[kw] is not None:
                        if ignore_multiple:
                            print(f"Warning: Multiple signals found for AXI4-Stream keyword '{kw}'. Using '{axi_sigs[kw]}' and ignoring '{s}'.")
                            continue
                        else:
                            err_str = f"Multiple signals found for AXI4-Stream keyword '{kw}': '{axi_sigs[kw]}' and '{s}'."
                            raise ValueError(err_str)
                    axi_sigs[kw] = s
                    self.add_signal(s)
                    if short_name_prefix:
                        short_name = f"{short_name_prefix}{kw.upper()}"
                    elif prefix:
                        short_name = f"{prefix}_{kw.upper()}"
                    else:  
                        short_name = kw.upper()
                    self.sig_info[s].short_name = short_name

            # Check if signal is found, except 'tlast' which is optional.
            if (axi_sigs[kw] is None) and (confirm_exists):
                raise ValueError(f"No signal found for AXI4- keyword '{kw}'.")
            
        # Get the bitwidth from the TDATA signal.
        # The signal ends in [N:0], so the width is N+1
        sig_bwid = ['RDATA', 'WDATA', 'AWADDR', 'ARADDR']  # Signals for which to get bitwidths
        bitwidths = dict()
        for sig in sig_bwid:
            if sig in axi_sigs and axi_sigs[sig] is not None:
                axi_sig = axi_sigs[sig]
                parts = axi_sig.split('[')
                bitwidth = None
                if len(parts) > 1:
                    bit_range = parts[-1].strip(']')
                    msb_lsb = bit_range.split(':')
                    if len(msb_lsb) == 2:
                        msb = int(msb_lsb[0])
                        bitwidth = msb + 1       

                        if bitwidth is None:
                            raise ValueError(f"Could not determine bitwidth from signal '{axi_sig}'.")
                        bitwidths[sig] = bitwidth
        return axi_sigs, bitwidths

   
    def full_name(
            self, 
            short_name : str) -> str:
        """
        Returns the full signal name for a given short name.
        Parameters
        ----------
        short_name : str
            Short name of the signal
        Returns
        -------
        full_name : str
            Full signal name if found, else None
        """
        for s, si in self.sig_info.items():
            if si.short_name == short_name:
                return s
        return None
    
    def get_values(
            self):
        """
        Converts the signal values for all added signals.
        """
        for s, si in self.sig_info.items():
            si.get_values()

    def get_td_signals(
            self) -> dict[str, SigTimingInfo]:
        """
        Returns the information for all added signals so that this can be 
        used for the timing diagram plotting.

        Example
        -------
        from vcd import VcdParser
        from timing import TimingDiagram

        vp = VcdParser(vcd)
        vp.add_signal(...)  # Add all signals to be plotted
        ...
        sig_list = vp.get_td_signals()
        td = TimingDiagram()
        td.add_signals(sig_list)
        td.plot()
        

        Returns
        -------
        sig_list : list[SigTimingInfo]
            List of signal timing information.
        """

        self.get_values()

        sig_list = []
        for si in self.sig_info.values():
            td_si = SigTimingInfo(
                name = si.short_name,
                times = si.times,
                values = si.disp_values,
                is_clock = si.is_clock)
            sig_list.append(td_si)
        return sig_list

    

    def extract_axis_bursts(
            self,
            clk_name : str,
            axis_sigs : dict[str, str]) -> list[dict]:
        """
        Extract bursts from AXI4-Stream signals.
        
        Parameters
        ----------
        clk_name: str
            Name of the clock signal.
        axis_sigs : dict[str, str]
            Dictionary of AXI4-Stream signal names with keys 'tdata', 'tvalid', 'tready' and
            optionally 'tlast'.  ``tlast`` may be absent or ``None``: a boundary port declared as
            a plain ``hls::stream<ap_uint<W> >`` has no TLAST wire at all (mem_copy's ``s_cmd`` is
            one), and framing begins only on the internal channel.  See
            :func:`walk_handshake_bursts` for what an unframed channel returns.

        Returns
        -------
        bursts : list of dict
            Each dict has:
            - 'data': np.ndarray of tdata values in the burst, in the signal's own dtype (a
              64-bit TDATA stays 64-bit; this used to be cast to uint32, silently truncating)
            - 'start_idx': index of first beat in burst
            - 'beat_type':  list of status of each beat.
            beat_type[i] can be 0 (transfer, tvalid=tready=1), 1 (idle (tvalid=0)), 2 (stall (tready=0))
            - 'tstart': time of first beat in burst
            - 'complete': True only if closed by an observed TLAST
        clk_period : float
            Estimated clock period in ns.
            Hence the time for beat i is tstart + i * clk_period
        """
        tlast_name = axis_sigs.get('tlast')

        # Ensure numeric values are computed for all required signals
        for sig_name in [clk_name, axis_sigs['tdata'], axis_sigs['tvalid'],
                         axis_sigs['tready'], tlast_name]:
            if sig_name is not None and sig_name in self.sig_info:
                self.sig_info[sig_name].get_values()

        # Extract clock times and resample AXI-Stream signals.  Beats stay labelled by the true
        # rising-edge time (`clk_times`), but the signals are READ just before the edge -- see
        # `clock_sample_times` for why sampling on the edge miscounts handshakes.
        clk_sig = self.sig_info[clk_name]
        clk_times = extract_clock_times(clk_sig)
        sample_times = clock_sample_times(clk_times)
        tdata = resample_signal(self.sig_info[axis_sigs['tdata']], sample_times)
        tvalid = resample_signal(self.sig_info[axis_sigs['tvalid']], sample_times)
        tready = resample_signal(self.sig_info[axis_sigs['tready']], sample_times)
        tlast = (resample_signal(self.sig_info[tlast_name], sample_times)
                 if tlast_name is not None and tlast_name in self.sig_info else None)

        bursts = walk_handshake_bursts(tdata, tvalid, tready, tlast, clk_times)
        clk_period = np.median(np.diff(clk_times))

        return bursts, clk_period

    def extract_fifo_bursts(
            self,
            clk_name : str,
            fifo_sigs : dict[str, str],
            side : Literal['write', 'read'] = 'write',
            data_width : int | None = None) -> tuple[list[dict], float]:
        """
        Extract bursts from an **internal HLS FIFO** channel.

        The same valid/ready handshake as AXI4-Stream, in the vocabulary Vitis emits for a
        dataflow channel between two ``hls::task`` bodies:

        ===========  =============================  =============================
        role         write side (producer)          read side (consumer)
        ===========  =============================  =============================
        data         ``<producer>_<ch>_din``        ``<ch>_dout``
        valid        ``<producer>_<ch>_write``      ``<ch>_empty_n``
        ready        ``<ch>_full_n``                ``<consumer>_<ch>_read``
        ===========  =============================  =============================

        These nets live in the **top** scope of an HLS DATAFLOW design -- Vitis lifts inter-task
        channel wires up beside the task instances -- so they are observable from a level-1
        ``$dumpvars`` of the top, with no hierarchical path to resolve.

        Parameters
        ----------
        clk_name : str
            Name of the clock signal.
        fifo_sigs : dict[str, str]
            Exact net names.  Write side needs 'din', 'write', 'full_n'; read side needs
            'dout', 'read', 'empty_n'.  Bind these by exact name: a trace contains both
            ``ywords_fifo_cap`` and ``il_store_..._U0_ywords_fifo_cap``, so substring matching
            picks the wrong one.
        side : {'write', 'read'}
            Which end of the FIFO to observe.  The two differ: the write side shows when the
            producer offered a word, the read side when the consumer took it, and the gap
            between them is the channel's occupancy.
        data_width : int | None
            ``W`` of a ``framed_word<W>`` channel, whose net is ``W+1`` bits wide with ``last``
            on top (see :func:`split_framed_word`).  ``None`` for an unframed channel, in which
            case all accepted beats come back as one burst.

        Returns
        -------
        bursts : list of dict
            As :meth:`extract_axis_bursts`.
        clk_period : float
            Estimated clock period in ns.
        """
        if side == 'write':
            data_key, valid_key, ready_key = 'din', 'write', 'full_n'
        elif side == 'read':
            data_key, valid_key, ready_key = 'dout', 'empty_n', 'read'
        else:
            raise ValueError(f"side must be 'write' or 'read', got {side!r}")

        missing = [k for k in (data_key, valid_key, ready_key) if not fifo_sigs.get(k)]
        if missing:
            raise ValueError(
                f"extract_fifo_bursts(side={side!r}) needs {data_key}/{valid_key}/{ready_key}; "
                f"missing {missing}. Present: {sorted(k for k, v in fifo_sigs.items() if v)}")

        for sig_name in [clk_name, fifo_sigs[data_key], fifo_sigs[valid_key],
                         fifo_sigs[ready_key]]:
            if sig_name in self.sig_info:
                self.sig_info[sig_name].get_values()

        clk_times = extract_clock_times(self.sig_info[clk_name])
        sample_times = clock_sample_times(clk_times)
        raw   = resample_signal(self.sig_info[fifo_sigs[data_key]], sample_times)
        valid = resample_signal(self.sig_info[fifo_sigs[valid_key]], sample_times)
        ready = resample_signal(self.sig_info[fifo_sigs[ready_key]], sample_times)

        if data_width is None:
            data, last = raw, None
        else:
            data, last = split_framed_word(raw, data_width)

        bursts = walk_handshake_bursts(data, valid, ready, last, clk_times)
        clk_period = np.median(np.diff(clk_times))

        return bursts, clk_period

    def extract_aximm_bursts(
            self,
            clk_name: str,
            aximm_sigs: dict[str, str]) -> tuple[list[dict], list[dict], float]:
        """
        Extract write and read bursts from AXI4-MM signals.

        Supports both AXI4-Lite (without ``AWLEN``/``ARLEN``/``WLAST``/``RLAST``)
        and AXI4-Full (with burst-length and last-beat signals).  The method
        resamples all signals just before each rising clock edge (using
        :func:`extract_clock_times`, :func:`clock_sample_times` and
        :func:`resample_signal`) and then walks the resampled arrays to identify
        accepted handshakes.

        Parameters
        ----------
        clk_name : str
            Name of the clock signal.
        aximm_sigs : dict[str, str]
            Dictionary of AXI4-MM signal names, as returned by
            :meth:`add_aximm_signals <VcdParser.add_aximm_signals>`.

            Write-side keys: ``AWADDR``, ``AWVALID``, ``AWREADY``,
            ``WDATA``, ``WVALID``, ``WREADY``.
            Optional write keys: ``AWLEN``, ``WLAST``, ``BVALID``,
            ``BREADY``.

            Read-side keys: ``ARADDR``, ``ARVALID``, ``ARREADY``,
            ``RDATA``, ``RVALID``, ``RREADY``.
            Optional read keys: ``ARLEN``, ``RLAST``.

        Returns
        -------
        write_bursts : list of dict
            Each dict has:

            - ``'addr'``      : accepted AWADDR value
            - ``'data'``      : np.ndarray of accepted WDATA beats
            - ``'start_idx'`` : clock-edge index of the address phase
                        - ``'data_start_idx'`` : first clock-edge index represented in
                            ``beat_type`` for this burst's data phase
                        - ``'data_end_idx'`` : final clock-edge index represented in
                            ``beat_type`` for this burst's data phase
            - ``'beat_type'`` : list of per-beat status after the address
                            phase while this burst is the active burst on the data channel.
                            Values use :class:`AximmBeatType`: 0 = transfer
                            (WVALID & WREADY), 1 = idle (WVALID=0), 2 = stall (WREADY=0)
            - ``'tstart'``    : time (ns) of the address phase
                        - ``'data_tstart'`` : time (ns) of the first cycle represented in
                            ``beat_type``
                        - ``'data_tend'`` : time (ns) of the final cycle represented in
                            ``beat_type``
                        - ``'queue_wait_cycles'`` : number of cycles between address
                            acceptance and the start of this burst's data phase
            - ``'awlen'``     : AWLEN value if available, else ``None``

        read_bursts : list of dict
            Each dict has:

            - ``'addr'``      : accepted ARADDR value
            - ``'data'``      : np.ndarray of accepted RDATA beats
            - ``'start_idx'`` : clock-edge index of the address phase
                        - ``'data_start_idx'`` : first clock-edge index represented in
                            ``beat_type`` for this burst's data phase
                        - ``'data_end_idx'`` : final clock-edge index represented in
                            ``beat_type`` for this burst's data phase
            - ``'beat_type'`` : list of per-beat status after the address
                            phase while this burst is the active burst on the data channel.
                            Values use :class:`AximmBeatType`: 0 = transfer
                            (RVALID & RREADY), 1 = idle (RVALID=0), 2 = stall (RREADY=0)
            - ``'tstart'``    : time (ns) of the address phase
                        - ``'data_tstart'`` : time (ns) of the first cycle represented in
                            ``beat_type``
                        - ``'data_tend'`` : time (ns) of the final cycle represented in
                            ``beat_type``
                        - ``'queue_wait_cycles'`` : number of cycles between address
                            acceptance and the start of this burst's data phase
            - ``'arlen'``     : ARLEN value if available, else ``None``

        clk_period : float
            Estimated clock period in ns.
        """

        # Ensure numeric values are computed for all present signals
        for sig_name in [clk_name] + [v for v in aximm_sigs.values() if v is not None]:
            if sig_name in self.sig_info:
                self.sig_info[sig_name].get_values()

        # Extract clock times.  Bursts stay labelled by the true rising-edge time (`clk_times`),
        # but the signals are READ just before the edge -- see `clock_sample_times`.
        clk_sig = self.sig_info[clk_name]
        clk_times = extract_clock_times(clk_sig)
        sample_times = clock_sample_times(clk_times)

        def _resample_key(key):
            """Resample aximm_sigs[key] just before each clock edge, or return None."""
            name = aximm_sigs.get(key)
            if name and name in self.sig_info:
                return resample_signal(self.sig_info[name], sample_times)
            return None

        # Resample write-side signals
        awaddr  = _resample_key('AWADDR')
        awvalid = _resample_key('AWVALID')
        awready = _resample_key('AWREADY')
        awlen   = _resample_key('AWLEN')
        wdata   = _resample_key('WDATA')
        wvalid  = _resample_key('WVALID')
        wready  = _resample_key('WREADY')
        wlast   = _resample_key('WLAST')

        # Resample read-side signals
        araddr  = _resample_key('ARADDR')
        arvalid = _resample_key('ARVALID')
        arready = _resample_key('ARREADY')
        arlen   = _resample_key('ARLEN')
        rdata   = _resample_key('RDATA')
        rvalid  = _resample_key('RVALID')
        rready  = _resample_key('RREADY')
        rlast   = _resample_key('RLAST')

        n = len(clk_times)

        write_bursts = []
        read_bursts  = []

        # FIFO queues of bursts whose address phase was accepted but whose
        # data beats have not yet all arrived.
        pending_writes = []
        pending_reads  = []

        def _is_new_addr_handshake(
                handshake: bool,
                prev_handshake: bool,
                addr_now,
                addr_prev,
                len_now,
                len_prev) -> bool:
            """Return True only for a newly observed address acceptance.

            Address channels in some traces can hold VALID/READY high across
            multiple cycles. Treating every such cycle as a fresh handshake
            creates duplicate overlapping bursts. We start a new burst when the
            accepted handshake is new, or when the accepted address metadata
            changes while the channel remains accepted across consecutive cycles.
            """
            if not handshake:
                return False
            if not prev_handshake:
                return True
            if addr_now is not None and addr_prev is not None and addr_now != addr_prev:
                return True
            if len_now is not None and len_prev is not None and len_now != len_prev:
                return True
            return False

        def _append_active_cycle(active: dict, beat_type: AximmBeatType, clk_idx: int) -> None:
            if active['data_start_idx'] is None:
                active['data_start_idx'] = clk_idx
                active['data_tstart'] = clk_times[clk_idx]
            active['beat_type'].append(beat_type)
            active['data_end_idx'] = clk_idx
            active['data_tend'] = clk_times[clk_idx]
            active['queue_wait_cycles'] = int(active['data_start_idx'] - active['start_idx'])

        for i in range(n):
            prev_i = i - 1

            # --- Write address channel ---
            aw_handshake = bool(
                awvalid is not None and awready is not None and awvalid[i] and awready[i]
            )
            prev_aw_handshake = bool(
                i > 0 and awvalid is not None and awready is not None and awvalid[prev_i] and awready[prev_i]
            )
            if _is_new_addr_handshake(
                    aw_handshake,
                    prev_aw_handshake,
                    awaddr[i] if awaddr is not None else None,
                    awaddr[prev_i] if (i > 0 and awaddr is not None) else None,
                    awlen[i] if awlen is not None else None,
                    awlen[prev_i] if (i > 0 and awlen is not None) else None):
                burst = {
                    'addr':      awaddr[i] if awaddr is not None else None,
                    'awlen':     int(awlen[i]) if awlen is not None else None,
                    'start_idx': i,
                    'tstart':    clk_times[i],
                    'data_start_idx': None,
                    'data_end_idx': None,
                    'data_tstart': None,
                    'data_tend': None,
                    'queue_wait_cycles': None,
                    'data':      [],
                    'beat_type': [],
                }
                pending_writes.append(burst)

            # --- Write data channel ---
            if wvalid is not None and wready is not None and pending_writes:
                active = pending_writes[0]
                if wvalid[i] and wready[i]:
                    _append_active_cycle(active, AximmBeatType.TRANSFER, i)
                    active['data'].append(wdata[i] if wdata is not None else None)
                    # Determine end-of-burst
                    if wlast is not None:
                        is_last = bool(wlast[i])
                    elif active['awlen'] is not None:
                        is_last = len(active['data']) == active['awlen'] + 1
                    else:
                        is_last = True  # AXI4-Lite: single beat per burst
                    if is_last:
                        active['data'] = np.array(active['data'])
                        write_bursts.append(active)
                        pending_writes.pop(0)
                else:
                    if not wvalid[i]:
                        _append_active_cycle(active, AximmBeatType.IDLE, i)
                    elif not wready[i]:
                        _append_active_cycle(active, AximmBeatType.STALL, i)

            # --- Read address channel ---
            ar_handshake = bool(
                arvalid is not None and arready is not None and arvalid[i] and arready[i]
            )
            prev_ar_handshake = bool(
                i > 0 and arvalid is not None and arready is not None and arvalid[prev_i] and arready[prev_i]
            )
            if _is_new_addr_handshake(
                    ar_handshake,
                    prev_ar_handshake,
                    araddr[i] if araddr is not None else None,
                    araddr[prev_i] if (i > 0 and araddr is not None) else None,
                    arlen[i] if arlen is not None else None,
                    arlen[prev_i] if (i > 0 and arlen is not None) else None):
                burst = {
                    'addr':      araddr[i] if araddr is not None else None,
                    'arlen':     int(arlen[i]) if arlen is not None else None,
                    'start_idx': i,
                    'tstart':    clk_times[i],
                    'data_start_idx': None,
                    'data_end_idx': None,
                    'data_tstart': None,
                    'data_tend': None,
                    'queue_wait_cycles': None,
                    'data':      [],
                    'beat_type': [],
                }
                pending_reads.append(burst)

            # --- Read data channel ---
            if rvalid is not None and rready is not None and pending_reads:
                active = pending_reads[0]
                if rvalid[i] and rready[i]:
                    _append_active_cycle(active, AximmBeatType.TRANSFER, i)
                    active['data'].append(rdata[i] if rdata is not None else None)
                    # Determine end-of-burst
                    if rlast is not None:
                        is_last = bool(rlast[i])
                    elif active['arlen'] is not None:
                        is_last = len(active['data']) == active['arlen'] + 1
                    else:
                        is_last = True  # AXI4-Lite: single beat per burst
                    if is_last:
                        active['data'] = np.array(active['data'])
                        read_bursts.append(active)
                        pending_reads.pop(0)
                else:
                    if not rvalid[i]:
                        _append_active_cycle(active, AximmBeatType.IDLE, i)
                    elif not rready[i]:
                        _append_active_cycle(active, AximmBeatType.STALL, i)

        # Estimate clock period
        clk_diffs = np.diff(clk_times)
        clk_period = np.median(clk_diffs)

        return write_bursts, read_bursts, clk_period


def extract_clock_times(
        sig_info : SigInfo) -> list[float]:
    """
    Extracts the clock edge times from a VCD object for a given clock signal.

    Parameters
    ----------
    vcd : VCDVCD
        Parsed VCD object.
    clk_name : str
        Name of the clock signal.

    Returns
    -------
    clk_times : list of float
        List of times (in ns) when the clock signal transitions to '1'.
    """
    
    clk_times = []
    for t, v in zip(sig_info.times, sig_info.values):
        if v == '1':
            clk_times.append(t)  # Convert to ns

    clk_times = np.array(clk_times)

    return clk_times


#: Where inside the cycle to sample, as a fraction of the clock period *before* the rising edge.
#: 0.25 puts the sample in the middle of the clock-low phase.  Half a period lands on the falling
#: edge, which is another transition boundary and is ambiguous again.
CLK_SAMPLE_FRAC = 0.25


def clock_sample_times(
        clk_times : np.ndarray,
        frac : float = CLK_SAMPLE_FRAC) -> np.ndarray:
    """
    Sample points for reading synchronous signals, given the rising-edge times.

    A flop samples the value its inputs held *during* the cycle leading up to the edge, but a VCD
    records a change caused by that edge at the edge timestamp itself -- and
    :func:`resample_signal` advances while ``sig_times[j] <= t``, so sampling AT ``clk_times``
    returns the POST-edge value.  For a handshake (``VALID & READY``) that is not a clean
    one-cycle shift: the two wires can move at the same timestamp in opposite directions, so
    coincidences are both invented and destroyed.  On the mem_copy XSI trace this read AXI-MM
    ``AW`` as 16 accepted addresses instead of 128, and ``W`` as 2032 beats instead of 2048.

    Sampling slightly *before* the edge reads the values the flops actually captured.

    Parameters
    ----------
    clk_times : np.ndarray
        Rising-edge times, as returned by :func:`extract_clock_times`.
    frac : float
        Fraction of a clock period to step back from each edge.  Defaults to
        :data:`CLK_SAMPLE_FRAC`.

    Returns
    -------
    sample_times : np.ndarray
        Times at which to resample.  Same length as *clk_times*, so cycle indices are unchanged
        and results stay labelled by the true edge time.  Values before the first signal event
        are safe: :func:`resample_signal` yields the initial value there.
    """
    clk_times = np.asarray(clk_times)
    if len(clk_times) < 2:
        return clk_times
    period = np.median(np.diff(clk_times))
    return clk_times - frac * period


def split_framed_word(
        values : np.ndarray,
        data_width : int) -> tuple[np.ndarray, np.ndarray]:
    """
    Split ``streamutils::framed_word<W>`` samples into their data and ``last`` parts.

    An internal Waveflow channel between two ``hls::task`` bodies cannot use ``ap_axis`` (Vitis HLS
    214-208 forbids it on an internal FIFO), so the packet boundary rides as one extra bit above
    the payload: a ``framed_word<64>`` is a 65-bit RTL net with ``last`` in bit 64.  There is no
    TLAST wire to read -- it has to be unpacked from the data word.

    Parameters
    ----------
    values : np.ndarray
        Resampled samples of the ``*_din`` / ``*_dout`` net.  Either 1-D (``W+1 <= 64``) or, per
        :class:`SigInfo`'s storage convention, 2-D ``(n, k)`` uint64 LSW-first (``W+1 > 64``).
    data_width : int
        ``W`` -- the payload width, i.e. the bit index of ``last``.

    Returns
    -------
    data : np.ndarray
        Payload, with the framing bit removed.
    last : np.ndarray
        The framing bit, 0 or 1 per sample.
    """
    v = np.asarray(values)
    if v.ndim == 1:
        if data_width >= 64:
            raise ValueError(
                f"framed_word<{data_width}> needs {data_width + 1} bits but the samples are 1-D "
                f"({v.dtype}); SigInfo stores >64-bit signals as 2-D. Was the signal added with "
                f"the right width?")
        return v & ((1 << data_width) - 1), (v >> data_width) & 1

    word, bit = divmod(data_width, 64)
    if bit != 0:
        raise ValueError(
            f"framed_word<{data_width}>: `last` falls at bit {bit} of word {word}, so the payload "
            f"straddles a 64-bit word boundary. Only whole-word payloads are unpacked today -- "
            f"add the masking here if a non-multiple-of-64 wide channel appears.")
    last = (v[:, word] >> 0) & 1
    data = v[:, 0] if word == 1 else v[:, :word]
    return data, last


def walk_handshake_bursts(
        data : np.ndarray,
        valid : np.ndarray,
        ready : np.ndarray,
        last : np.ndarray | None,
        clk_times : np.ndarray) -> list[dict]:
    """
    Walk one valid/ready channel and group its accepted beats into bursts.

    The shared core behind :meth:`VcdParser.extract_axis_bursts` and
    :meth:`VcdParser.extract_fifo_bursts`.  Every channel Waveflow observes -- an AXI4-Stream
    boundary port, or an internal HLS FIFO whose handshake is spelled ``write``/``full_n`` and
    ``read``/``empty_n`` -- reduces to the same four arrays, so the burst logic lives here once
    and the protocol-specific naming stays in thin adapters.

    Parameters
    ----------
    data, valid, ready : np.ndarray
        Per-cycle samples, already resampled onto the clock grid.
    last : np.ndarray | None
        Per-cycle framing bit, or ``None`` for a channel with no packet boundary at all (a plain
        ``hls::stream<ap_uint<W> >`` boundary port has no TLAST wire).  With ``None`` there is
        nothing to delimit on, so **all** accepted beats are returned as a single burst: inventing
        boundaries from idle gaps would split a packet at any stall, which is worse than declining
        to guess.  Segment such a stream from the protocol instead (e.g. its job index).
    clk_times : np.ndarray
        Rising-edge times, used to label ``tstart``.

    Returns
    -------
    bursts : list of dict
        Keys ``data``, ``start_idx``, ``beat_type``, ``tstart`` (as consumed by
        :meth:`AxisBurst.from_dict`), plus ``complete`` -- True only when the burst was closed by
        an observed ``last``.  A burst still open when the trace ends is returned with
        ``complete=False`` rather than silently dropped; its trailing idle/stall cycles are
        trimmed, since they describe the end of the trace and not the packet.
    """
    src = np.asarray(data)
    bursts : list[dict] = []
    current : dict | None = None

    def _close(cur : dict, complete : bool) -> None:
        if cur['data']:
            cur['data'] = np.asarray(cur['data'], dtype=src.dtype)
        else:
            shape = (0,) if src.ndim == 1 else (0, src.shape[1])
            cur['data'] = np.empty(shape, dtype=src.dtype)
        while cur['beat_type'] and cur['beat_type'][-1] != 0:
            cur['beat_type'].pop()
        cur['complete'] = complete
        bursts.append(cur)

    for i in range(len(clk_times)):
        if valid[i] and ready[i]:
            if current is None:
                current = {'data': [], 'start_idx': i, 'beat_type': [], 'tstart': clk_times[i]}
            current['data'].append(src[i])
            current['beat_type'].append(0)                      # transfer
            if last is not None and last[i]:
                _close(current, True)
                current = None
        elif current is not None:
            current['beat_type'].append(1 if not valid[i] else 2)   # idle : stall

    if current is not None:
        _close(current, False)
    return bursts


def resample_signal(
        sig_info : SigInfo,
        clk_times : np.ndarray) -> np.ndarray:
    """
    Resamples a signal to new time points using nearest-neighbor interpolation.

    Parameters
    ----------
    sig_info : SigInfo
        Signal information object.
    clk_times : np.ndarray
        Array of new time points to sample the signal at.  Typically these are clock edge times.

    Returns
    -------
    resampled_values : np.ndarray
        Array of signal values at the new time points.  For wide signals
        (``wid > 64`` with ``numeric_type == 'uint'``), this is a 2-D array
        of shape ``(len(clk_times), k)`` with dtype ``np.uint64``.
    """    
    sig_times = sig_info.times
    sig_values = sig_info.numeric_values
    m = len(clk_times)

    # Allocate output with the same trailing shape as sig_values
    if sig_values.ndim == 2:
        sampled = np.empty((m, sig_values.shape[1]), dtype=sig_values.dtype)
    else:
        sampled = np.empty(m, dtype=sig_values.dtype)

    j = 0  # pointer into sig_times and sig_values
    current_val = sig_values[0]

    for i, t_clk in enumerate(clk_times):
        # advance signal pointer while events are before or at this clock
        while j < len(sig_times) and sig_times[j] <= t_clk:
            current_val = sig_values[j]
            j += 1
        sampled[i] = current_val

    return sampled

