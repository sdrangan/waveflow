"""The ray-traced RF environment: multipath channels on the RF sample interface.

A design that talks to an :class:`~waveflow.hw.rfdc.Rfdc` sees the world through
:class:`~waveflow.hw.rf_sample_if.RFSampIF` edges.  This package puts a propagation environment on
those edges: a channel computed by ray tracing a 3D scene (NVIDIA Sionna RT), applied block by block
to complex-baseband samples, as a pysim signal source or as a channel between a transmitter and a
receiver.  The plan, its decisions and their evidence: ``plans/sionna_rt/sionna_rt.md``.

**Sionna is optional.**  The channel lives in a *channel file* -- every path's amplitude, delay and
Doppler shift, per antenna pair (:class:`~waveflow.rfenv.paths.PathSet`) -- and applying it needs
only numpy.  Tracing a scene into one needs ``pip install -e ".[sionna]"``, and every Sionna import
in the package lives in :mod:`waveflow.rfenv.sionna_backend`, inside functions.  So a committed
channel file runs anywhere, and the XSI testbench can replay what a source rendered from it.

Modules:

* :mod:`~waveflow.rfenv.paths` -- :class:`PathSet`, the channel file and its manifest;
* :mod:`~waveflow.rfenv.apply` -- :class:`ChannelApplier`, the channel applied block by block, and
  :func:`taps`, the discrete taps a receiver sees;
* :mod:`~waveflow.rfenv.waveforms` -- what a transmitter sends (tone, Zadoff-Chu, PN, a user's array);
* :mod:`~waveflow.rfenv.noise` -- power conversions, dBFS, and thermal noise drawn by sample index.
* :mod:`~waveflow.rfenv.sionna_backend` -- :class:`SceneSpec` and :func:`trace`: a scene traced by
  Sionna RT into a :class:`PathSet` (imports Sionna only when it traces; not imported here).
"""

from waveflow.rfenv.apply import ChannelApplier, reference_apply, taps
from waveflow.rfenv.paths import PathSet

__all__ = ["ChannelApplier", "PathSet", "reference_apply", "taps"]
