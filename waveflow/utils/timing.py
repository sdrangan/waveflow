# timing.py: Helper functions for timing analysis of Xilinx designs

from matplotlib import patches
import numpy as np
import matplotlib.pyplot as plt

class SigTimingInfo(object):
    """    
    Holds timing information for a single signal.
    """
    def __init__(
            self, 
            name : str, 
            times : list[float], 
            values : list[str], 
            is_clock : bool =False):
        """
        Constructor
        
        Parameters
        ----------
        name : str
            Display name of the signal.
        times : list of float
            List of time points where the signal changes value.
        values : list of str
            List of signal values corresponding to the time points.
        is_clock : bool, optional
            If True, indicates that this signal is a clock.
        """
        self.name = name
        self.times = times
        self.values = values
        self.is_clock = is_clock


        self.two_level = all(v in {'0', '1', 'x', 'z', 'X', 'Z'} for v in values)

class ClkSig(SigTimingInfo):
    def __init__(
            self,
            clk_name : str = 'clk',
            period : float = 10.0,
            ncycles : int = 10,
            start_rising : bool = True): 
        """
        Constructor for clock signal information.
        Parameters
        ----------
        clk_name : str, optional
            Name of the clock signal.
        period : float, optional
            Clock period in time units.
        ncycles : int, optional
            Number of clock cycles.
        """
        self.ncycles = ncycles
        self.period = period
        times = np.arange(0, 2*ncycles) * (period / 2)
        if start_rising:
            values = ['1', '0'] * ncycles
        else:
            values = ['0', '1'] * ncycles 
        super().__init__(name=clk_name, times=times, values=values, is_clock=True)

    def clk_periods(self):
        """
        Returns the start of each clock period, as defined by the rising edges.
        """
        edges = []
        if self.values[0] == '1':
            edges.append(self.times[0])
        for i in range(1, len(self.values)):
            if self.values[i-1] == '0' and self.values[i] == '1':
                edges.append(self.times[i])
        return edges



class TimingDiagram(object):
    def __init__(
            self, 
            time_unit='ns'):
        self.sig_info = dict()
        self.time_unit = time_unit

    def add_signal(self, sig_info : SigTimingInfo):
        """
        Adds a signal's timing information to the diagram.

        Parameters
        ----------
        sig_info : SigTimingInfo
            Signal timing information to add.
        """
        self.sig_info[sig_info.name] = sig_info

    def add_signals(self,
                    sig_info_list : list[SigTimingInfo]):
        """
        Adds multiple signals' timing information to the diagram.

        Parameters
        ----------
        sig_info_list : list of SigTimingInfo
            List of signal timing information to add.
        """
        for si in sig_info_list:
            self.add_signal(si)

    def plot_signals(
            self,
            add_clk_grid = True,
            ax = None,
            fig_width = 10,
            row_height = 0.5,
            row_step = 0.8,
            trange = None,
            text_mode = 'always',
            text_scale_factor = 1000):
        """
        Plots the timing diagram for the selected signals.

        Parameters
        ----------
        add_clk_grid : bool, optional
            If True, adds vertical grid lines at clock edges. 
        ax : matplotlib.axes.Axes, optional
            Axes object to plot on. If None, a new figure and axes are created.
        fig_width : float, optional
            Width of the figure in inches (if ax is None).
        row_height : float, optional    
            Height of each row in inches.  
        row_step : float, optional
            Vertical spacing between rows.
        trange : tuple(float, float), optional
            Time range (tmin, tmax) to plot. If None, uses full range of signals.
        text_mode : str, optional
            Mode for drawing text labels. Options are 'always', 'never', 'auto'.
            In 'auto' mode, text is drawn only if there is enough space.
        text_scale_factor : float, optional
            Scale factor to determine if there is enough space to draw text labels.

        Returns 
        -------
        None         
        ax : matplotlib.axes.Axes
            Axes object with the plotted signals.   
        """

        # Determine signals to plot
        signals_to_plot = list(self.sig_info.keys())    

    
        # Create figure and axis if not provided
        nsig = len(signals_to_plot)
        ymax = row_step * nsig
        if ax is None:
            ax_provided = False
            fig_height = row_height * nsig
            fig, ax = plt.subplots(figsize=(fig_width, fig_height))
        else:
            ax_provided = True


        # Get min and max times.  If not provided, compute from signals
        if trange is not None:
            tmin, tmax = trange
        else:
            for i, s in enumerate(signals_to_plot):
                si = self.sig_info[s]
                if i == 0:
                    tmin = si.times[0]
                    tmax = si.times[-1]
                else:
                    tmin = min(tmin, si.times[0])
                    tmax = max(tmax, si.times[-1])   

        # Save the top and bottom y positions for each signal
        self.ytop = dict()
        self.ybot = dict()

        for i, s in enumerate(signals_to_plot):
            y =  ymax - (i + 0.5) * row_step  # vertical position for signal s
            si = self.sig_info[s]
            t_list = si.times 

            # Draw signal name
            ax.text(tmin - 0.5, y, s, ha='right', va='center', fontsize=10)

            # Set the top and bottom y positions for the signal
            ybot = y - 0.4 * row_step
            ytop = y + 0.4 * row_step
            self.ytop[s] = ytop
            self.ybot[s] = ybot


            # Draw horizontal segments between value changes
            vlast = None
            for j in range(len(t_list)):
                t_start = t_list[j]
                if j + 1 < len(t_list):
                    t_end = t_list[j + 1]
                else:
                    t_end = tmax  # Extend to the right edge

                # Skip if outside time range
                if t_end < tmin or t_start > tmax:
                    continue
                t_start = max(tmin, t_start)
                t_end = min(tmax, t_end)

                v = si.values[j]

                # Set default drawing options
                draw_top = True
                draw_bot = True
                draw_text = True
                fill_gray = False
                draw_vert = True

                # Adjust drawing options based on value
                if (v in {'x', 'X', 'z', 'Z'}):
                    draw_text = False
                    fill_gray = True
                if (si.two_level):
                    if v == '1':
                        draw_bot = False
                        draw_text = False
                    elif v == '0':
                        draw_top = False
                        draw_text = False
                    if vlast is not None:
                        if v == vlast:
                            draw_vert = False
                    elif si.two_level:  # For two level signals, no vertical line at start
                        draw_vert = False
                vlast = v
    
                # Draw a vertical line at the start of the segment                
                if draw_vert:
                    ax.vlines(t_start, ybot, ytop, color='black', linewidth=1)
                if draw_bot:
                    ax.hlines(ybot, t_start, t_end, color='black', linewidth=1)
                if draw_top:
                    ax.hlines(ytop, t_start, t_end, color='black', linewidth=1)

                # Fill gray for unknown values
                if fill_gray:
                    ax.fill_betweenx([ybot, ytop], t_start, t_end, color='lightgray')

                # Place text label in the middle of the segment
                # Check if there is enough space to draw the text
                if text_mode == 'never':
                    draw_text = False
                if draw_text and text_mode == 'auto':
                    idx_start = ax.transData.transform((t_start, y))
                    idx_end = ax.transData.transform((t_end, y))
                    if idx_end[0] - idx_start[0] < len(v)*text_scale_factor:
                        draw_text = False
                if draw_text:
                    ax.text((t_start + t_end) / 2, y, v, ha='center', va='center',
                            fontsize=10, color='black')


        # Add clock grid lines if requested
        if add_clk_grid:
            clk_signal = None
            for s, si in self.sig_info.items():
                if si.is_clock:
                    clk_signal = si.name
                    break
            if not clk_signal:
                add_clk_grid = False
                
        if add_clk_grid:
            for i, t in enumerate(si.times):
                v = si.values[i]
                if v == '1' and (tmin <= t) and (t <= tmax):
                    ax.axvline(x=t, color='gray', linestyle='--', linewidth=0.5)

        ax.set_yticks([])
        ax.set_xlim(tmin, tmax)
        ax.set_ylim(0, ymax)

        # Save axis and time range of the plot
        self.ax = ax
        self.tmin = tmin
        self.tmax = tmax


        return ax
    
    def add_patch(
            self,
            sig_name : str | list[str],
            ind : int | list[int] | None = None,
            time : list[float] | None = None,
            **kwargs):
        """
        Adds a colored patch to highlight a time interval for a specific signal.


        Parameters
        ----------
        sig_name : str or [start_sig, end_sig]
            Name of the signal to highlight.  If a list
            of two signal names is provided, the patch
            will span from the first signal to the second.
        time : [t_start, t_end] or None
            Time interval to highlight.  If None, the indices are used to 
            determine the time interval.
        ind : int or [start_ind, end_ind] or None
            Index or indices of the time points to highlight.  If a single index is provided,
            the patch will span from that index to the next index.  If None, the time parameter
            must be provided.
        **kwargs : keyword arguments
            Additional keyword arguments passed to the patches.Rectangle function.
            Common options include 'color' and 'alpha'.

        Returns
        -------
        None
        """

        # Get the signal names
        if isinstance(sig_name, list):
            if len(sig_name) != 2:
                raise ValueError("sig_name must be a string or a list of two strings.")
            start_sig, end_sig = sig_name
        else:
            start_sig = sig_name 
            end_sig = sig_name
        if not start_sig in self.sig_info:
            raise ValueError(f"Signal {start_sig} not found in timing diagram.")
        if not end_sig in self.sig_info:
            raise ValueError(f"Signal {end_sig} not found in timing diagram.")
        
        # Get the time values
        use_ind = True
        if ind is None:
            if time is None or len(time) != 2:
                raise ValueError("Either ind or time must be provided.")
            t_start, t_end = time
            use_ind = False
        elif isinstance(ind, list):
            if len(ind) != 2:
                raise ValueError("ind must be an integer or a list of two integers.")
            start_ind, end_ind = ind    
        else:
            start_ind = ind
            end_ind = ind+1
        if use_ind:
            t_start = self.sig_info[start_sig].times[start_ind]
            if end_ind >= len(self.sig_info[end_sig].times):
                t_end = self.tmax
            else:
                t_end = self.sig_info[end_sig].times[end_ind]

        # Get the vertical positions
        ybot = self.ybot[start_sig]
        ytop = self.ytop[end_sig]

        # Add the patch
        rect = patches.Rectangle(
            xy=(t_start, ybot), width=(t_end - t_start), 
            height=(ytop - ybot),
            **kwargs)

        self.ax.add_patch(rect)


class ActivityDiagram(object):
    """Labeled horizontal lanes on a common cycle axis, showing *activity* rather than values.

    A sibling of :class:`TimingDiagram`, not an extension of it.  Both draw labeled rows against a
    shared time axis, but they answer different questions and their draw loops share nothing.
    ``TimingDiagram`` is a *waveform* renderer: one row per signal, a value-labeled box per
    transition, and its ``text_mode`` / ``text_scale_factor`` logic exists solely to decide whether
    a box is wide enough to hold its text -- a model that only reads at ~10-50 cycle zoom.
    ``ActivityDiagram`` deliberately *discards* per-transition values and shows when each lane was
    busy, which is the one thing that stays legible across thousands of cycles.

    A lane is a ``(label, event_cycles, colour)`` triple: ``event_cycles`` is the (sorted) integer
    cycles at which that lane saw an event -- a handshake, a beat, a fire.  Two render modes decide
    what those events look like:

    * ``"band"`` -- collapse contiguous events into filled bars (:meth:`runs` +
      ``broken_barh``).  The whole-run view: at 2900 cycles a per-beat figure is thousands of
      sub-pixel hairlines that antialias to a grey smear, so the bands carry the same information
      (when each stage was busy) at a fraction of the paths.
    * ``"beat"`` -- one hairline per event (``vlines``).  The zoomed, per-firing view, where the
      individual beats are the point.

    An optional occupancy sub-panel (:meth:`set_occupancy`) draws a level-vs-capacity step line
    beneath the lanes, shaded where the level sits *at* capacity -- a FIFO whose producer is
    blocked.  A write-enable metric shows nothing there (HLS gates the enable), so the counter is
    the only place that congestion is visible.

    Parameters
    ----------
    lanes : list of (str, array-like of int, str)
        ``(label, event_cycles, colour)`` per lane, drawn top to bottom in the order given.
    time_unit : str, optional
        Axis unit label stem; the x-axis is labeled ``"clock {time_unit}"`` by default.
    """

    def __init__(self, lanes, time_unit="cycle"):
        # Normalise event arrays to numpy once, so both draw modes and the run-collapse can index
        # and mask them without re-wrapping.
        self.lanes = [(label, np.asarray(ev), colour) for label, ev, colour in lanes]
        self.time_unit = time_unit
        #: Occupancy sub-panel config, or None.  Set by set_occupancy().
        self._occupancy = None
        # Populated by plot().
        self.ax = None
        self.ax_occupancy = None

    # -- construction from a trace ---------------------------------------------------------------
    @classmethod
    def from_trace(cls, bt, spec, time_unit="cycle"):
        """Build lanes by walking a :class:`~waveflow.utils.trace.BoundTrace`.

        The reusable half of what used to be ``mem_copy``'s bespoke ``_observations``: the mechanism
        that turns a manifest's channels and ports into event lanes.  *Which* channels and ports a
        given design cares about (and in what order, and what colour) stays with that design -- it is
        the design's topology, not something to generalise -- and arrives here as *spec*.

        Parameters
        ----------
        bt : BoundTrace
            A manifest bound to a waveform.
        spec : list of (str, source, str)
            ``(label, source, colour)`` per lane.  *source* names where the lane's events come from:

            * ``("port", port_id, valid, ready)`` -- cycles where an AXI-Stream / m_axi boundary
              port's ``valid`` and ``ready`` both fire (e.g. ``("port", "gmem0", "ARVALID",
              "ARREADY")``).
            * ``("chan", channel_id, side)`` -- cycles where one end of an internal FIFO channel
              hands over a word.  ``side="write"`` reads the producer end (``write`` & ``full_n``),
              ``side="read"`` the consumer end (``empty_n`` & ``read``).
        time_unit : str, optional
            Passed through to the constructor.

        Returns
        -------
        ActivityDiagram
        """
        ch = {c["id"]: c for c in bt.manifest["channels"]}
        hs = bt._handshakes
        lanes = []
        for label, source, colour in spec:
            kind = source[0]
            if kind == "port":
                _, pid, valid, ready = source
                sig = bt.port(pid)["signals"]
                ev = hs(sig[valid], sig[ready])
            elif kind == "chan":
                _, cid, side = source
                c = ch[cid]
                if side == "write":
                    ev = hs(c["write"]["write"], c["write"]["full_n"])
                elif side == "read":
                    ev = hs(c["read"]["empty_n"], c["read"]["read"])
                else:
                    raise ValueError(
                        f"lane {label!r}: channel side must be 'write' or 'read', got {side!r}")
            else:
                raise ValueError(
                    f"lane {label!r}: source kind must be 'port' or 'chan', got {kind!r}")
            lanes.append((label, ev, colour))
        return cls(lanes, time_unit=time_unit)

    @staticmethod
    def occupancy_from_trace(bt, channel):
        """``(level, capacity)`` series for a channel's FIFO, or ``(None, 0)`` if not exposed.

        The extraction half of the occupancy sub-panel: reads a channel's own level / capacity
        counters off the waveform.  ``level`` is the depth series sampled on the clock grid;
        ``capacity`` is the (constant) FIFO depth.  Feed the pair to :meth:`set_occupancy`.
        """
        d = bt.channel(channel).get("depth", {})
        lvl, cap = d.get("level"), d.get("cap")
        if not lvl or not cap or lvl not in bt.resolved or cap not in bt.resolved:
            return None, 0
        from waveflow.utils.vcd import resample_signal

        def _s(bare):
            n = bt.resolved[bare]
            bt.parser.sig_info[n].get_values()
            return np.asarray(resample_signal(bt.parser.sig_info[n], bt._grid()))

        level = _s(lvl)
        return level, int(_s(cap).max())

    def set_occupancy(self, level, cap, colour, ylabel="occupancy", note=None):
        """Attach an occupancy sub-panel drawn beneath the lanes by :meth:`plot`.

        Parameters
        ----------
        level : array-like of int or None
            Per-cycle occupancy of the channel, indexed by cycle.  ``None`` still draws the (empty)
            panel -- a design that exposes no counters keeps the axis rather than silently dropping
            it, so the figure's shape does not depend on the run.
        cap : int
            FIFO capacity; the dashed reference line, and the level at which the panel shades.
        colour : str
            Colour of the step line and the at-capacity shading (usually the producing stage's).
        ylabel : str, optional
            Y-axis label for the panel.
        note : str or None, optional
            In-panel annotation.  ``None`` uses ``"shaded = at capacity ({cap}) -> producer
            blocked"``.
        """
        self._occupancy = {
            "level": level,
            "cap": cap,
            "colour": colour,
            "ylabel": ylabel,
            "note": note,
        }
        return self

    @staticmethod
    def runs(events, gap=3):
        """Collapse event cycles into contiguous activity runs ``(start, width)``.

        Events within *gap* cycles of each other belong to the same run.  Used by the ``"band"``
        render mode; a 1-cycle run comes back with width 1 (never 0) so it still draws.
        """
        ev = np.asarray(events)
        if not len(ev):
            return []
        breaks = np.nonzero(np.diff(ev) > gap)[0]
        starts = np.concatenate(([ev[0]], ev[breaks + 1]))
        ends = np.concatenate((ev[breaks], [ev[-1]]))
        return [(int(s), max(int(e - s), 1)) for s, e in zip(starts, ends)]

    # -- rendering -------------------------------------------------------------------------------
    def plot(
            self,
            mode="band",
            trange=None,
            gap=3,
            title=None,
            fig_width=11,
            fig_height=4.2,
            label_fontsize=8,
            title_fontsize=10,
            band_height=0.68,
            beat_half=0.38,
            occupancy_ratio=(3, 1)):
        """Render the lanes (and the occupancy sub-panel, if set) to a fresh figure.

        Parameters
        ----------
        mode : {'band', 'beat'}, optional
            ``'band'`` collapses events into filled bars; ``'beat'`` draws one hairline per event.
        trange : (int, int) or None, optional
            ``(lo, hi)`` cycle window.  Required for ``'beat'`` (it masks events to the window);
            for ``'band'`` it sets the x-limits (default ``(0, max_event + 40)``).
        gap : int, optional
            Run-merge gap for ``'band'`` mode (see :meth:`runs`).
        title : str or None, optional
            Axis title.
        fig_width, fig_height : float, optional
            Figure size in inches.
        label_fontsize, title_fontsize : int, optional
            Font sizes for the lane labels and the title.
        band_height : float, optional
            Total height of a band bar (``'band'`` mode).
        beat_half : float, optional
            Half-height of a beat hairline (``'beat'`` mode).
        occupancy_ratio : (int, int), optional
            Height ratio of the lane panel to the occupancy panel, when an occupancy panel is set.

        Returns
        -------
        (fig, ax, ax_occupancy)
            The figure, the lane axis, and the occupancy axis (``None`` if none was set).
        """
        n = len(self.lanes)
        xlabel = f"clock {self.time_unit}"

        # The x-axis (limits + label) belongs to the bottom-most panel: the occupancy panel when
        # there is one, else the lane panel itself.
        if self._occupancy is not None:
            fig, (ax, ax2) = plt.subplots(
                2, 1, figsize=(fig_width, fig_height), sharex=True,
                gridspec_kw={"height_ratios": list(occupancy_ratio)})
        else:
            fig, ax = plt.subplots(figsize=(fig_width, fig_height))
            ax2 = None

        if mode == "band":
            hi = trange[1] if trange is not None else (
                int(max((e[-1] for _, e, _ in self.lanes if len(e)), default=1)) + 40)
            lo = trange[0] if trange is not None else 0
            for row, (label, ev, colour) in enumerate(self.lanes):
                y = n - 1 - row
                bars = self.runs(ev, gap=gap)
                if bars:
                    # edgecolor == facecolor with a real linewidth: a 1-cycle run is well under a
                    # pixel across a multi-thousand-cycle axis, and with no stroke it antialiases to
                    # a grey smear instead of the lane's colour.
                    ax.broken_barh(bars, (y - band_height / 2, band_height), facecolors=colour,
                                   edgecolors=colour, linewidth=0.5)
        elif mode == "beat":
            if trange is None:
                raise ValueError("mode='beat' needs a trange=(lo, hi) window to draw into.")
            lo, hi = trange
            for row, (label, ev, colour) in enumerate(self.lanes):
                y = n - 1 - row
                e = ev[(ev >= lo) & (ev <= hi)]
                if len(e):
                    ax.vlines(e, y - beat_half, y + beat_half, color=colour, linewidth=1.1)
        else:
            raise ValueError(f"mode must be 'band' or 'beat', got {mode!r}")

        ax.set_yticks(range(n))
        ax.set_yticklabels([label for label, _, _ in self.lanes][::-1], fontsize=label_fontsize)
        if ax2 is None:
            ax.set_xlim(lo, hi)
            ax.set_xlabel(xlabel)
        if title is not None:
            ax.set_title(title, fontsize=title_fontsize)
        ax.set_axisbelow(True)          # else the grid draws over the bars and reads as extra events
        ax.grid(axis="x", alpha=0.25, linewidth=0.4)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)

        if ax2 is not None:
            self._draw_occupancy(ax2, lo, hi, xlabel)

        self.ax = ax
        self.ax_occupancy = ax2
        return fig, ax, ax2

    def _draw_occupancy(self, ax2, lo, hi, xlabel):
        """Draw the occupancy sub-panel into *ax2* over ``[lo, hi]``."""
        occ = self._occupancy
        level, cap = occ["level"], occ["cap"]
        colour = occ["colour"]
        if level is not None and cap:
            # x is clipped to whatever the level series actually covers: a trace grid runs past hi
            # (so this is a no-op there), but a hand-authored level may stop exactly at the window
            # end, and step() needs x and y the same length.
            window = np.asarray(level)[lo:hi + 1]
            x = np.arange(lo, lo + len(window))
            ax2.step(x, window, where="post", color=colour, linewidth=1.0)
            ax2.axhline(cap, color="0.4", linestyle="--", linewidth=0.8)
            blocked = window >= cap
            ax2.fill_between(x, 0, cap, where=blocked, color=colour, alpha=0.25, step="post")
            ax2.set_ylim(0, cap + 0.5)
            ax2.set_ylabel(occ["ylabel"], fontsize=8)
            note = occ["note"]
            if note is None:
                note = f"shaded = at capacity ({cap}) → producer blocked"
            ax2.text(0.005, 0.86, note, transform=ax2.transAxes, fontsize=7.5, color="0.25")
        ax2.set_xlim(lo, hi)
        ax2.set_xlabel(xlabel)
        ax2.set_axisbelow(True)
        ax2.grid(axis="x", alpha=0.25, linewidth=0.4)
        for s in ("top", "right"):
            ax2.spines[s].set_visible(False)
