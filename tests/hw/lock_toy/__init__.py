"""The minimal consumer of :class:`~waveflow.hw.locked_mem.LockedT2pMemIF`.

``plans/t2p_lock_chan.md`` S1, checkpoint 2.  A fixture rather than an example: what it is for is the
*lowering*, and the first real consumer (infinite play on ``RfShotTx``) is checkpoint 3's.  Keeping
it here rather than in ``examples/`` is the honest filing — an example teaches a design, and this
teaches nothing a user wants; it asks whether a graph holding a lock reaches C++ and through Vitis.
"""
