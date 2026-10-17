# Waveflow execution tools

Run FIR and MemCopy through the same CLI or MCP interface. Python simulation works;
synthesis and RTL are the next step.

- **[Run the demo](execution/README.md)** — setup, commands and saved results.
- **[Present the work](execution/presentation.html)** — diagrams and measured results. Download and open the HTML.
- **[Add synthesis and RTL](execution/HANDOFF.md)** — the collaborator's starting point.
- [Tool contract](EXECUTION_SURFACE.md) · [Test results](VERIFICATION.md)

## Earlier FIR pilot

The [legacy six-tool service](LEGACY.md) supports FIR replay and agent benchmarks.
It is separate from the four-tool interface above. Its `synth` operation replays
saved HLS results; it does not run synthesis. The Pi adapter still uses this service.
