"""Stream blocks: small, general free-running modules that move framed streams around -- the pieces
a stream-only design reaches for between its own kernels (``docs/guide/streams/``).

Each block is framework code: a Python model, an HLS body under ``src/`` templated on its ports' word
types (so one body serves a block at a top, at a composite's boundary and inside one), a build step that
copies the body, and an RTL gate in its own harness."""
from waveflow.streams.broadcast import StreamBroadcast, StreamBroadcastStep

__all__ = ["StreamBroadcast", "StreamBroadcastStep"]
