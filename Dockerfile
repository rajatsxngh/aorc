# ============================================================
#  AORC base image
#  Baked in: Claude Code + Matt's skills + Python 3.12 + uv + pytest
# ============================================================

FROM node:22-slim

# 1. System tools + Python. (python3, pip, and venv come from apt here,
#    so a working Python is ALWAYS present in every container — the agent
#    never has to install it, and it never disappears between runs.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    curl \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# 2. Install global node tools WHILE STILL ROOT.
RUN npm install -g @anthropic-ai/claude-code
RUN npx skills@latest add mattpocock/skills --yes --global

# 3. Install uv (fast Python package manager) system-wide, still root.
#    Placed in /usr/local/bin so every user (incl. 'agent') sees it on PATH.
RUN curl -LsSf https://astral.sh/uv/install.sh | \
    env UV_INSTALL_DIR=/usr/local/bin sh

# 4. Make pytest available system-wide too, so the loop's test gate and
#    manual `pytest` both work without a per-project venv.
RUN pip install --no-cache-dir --break-system-packages pytest

# 5. Create the non-root user and switch to it.
RUN useradd --create-home --shell /bin/bash agent
USER agent
WORKDIR /home/agent

# 6. Work directory for the mounted repo.
WORKDIR /work

# 7. Default command.
CMD ["/bin/bash"]

# ------------------------------------------------------------
#  NOTES
#  * Python, uv, and pytest are now part of the IMAGE, so they exist
#    in every fresh container and survive the --rm teardown. The agent
#    should NOT create its own uv-installed Python anymore.
#  * Authentication inside the container still needs ANTHROPIC_API_KEY
#    (passed at run time via --env-file .env). A host Max/Pro login
#    does not carry into the container.
# ------------------------------------------------------------