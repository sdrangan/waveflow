# The agent's jail (plans/dse_cg.md step 7; run_agent.py): what an agent run's Claude Code process and
# its shell can see.  Python with the usual scientific stack and git; no Waveflow, no Vitis/Vivado, no
# repository.  run_agent.py mounts the run's sandbox and config directory and the Agent SDK's bundled
# Claude Code (read-only) into it, and nothing else.  Its tag (and digest) is pre-registered
# (prereg.AGENT_IMAGE), identical in both arms.
#
#     docker build -t waveflow-dse-agent:1 examples/dse_cg/agent

FROM python:3.12.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy==2.3.4 pandas==2.3.3 scipy==1.16.3 matplotlib==3.10.7

# A writable home is the run's config directory (HOME and CLAUDE_CONFIG_DIR are set by run_agent.py).
ENV PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg
