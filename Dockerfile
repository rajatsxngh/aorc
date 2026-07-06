# ============================================================
#  AORC base image
#  A sealed room, pre-stocked with everything an agent needs.
#  Every per-issue container (and your AFK Ralph sandbox) is
#  born from this image, so the skills are ALWAYS present.
# ============================================================

# 1. Start from a ready-made Node.js machine.
#    "node" already has Node + npm installed. "22-slim" = version 22,
#    the "slim" (smaller) variant so the image isn't huge.
FROM node:22-slim

# 2. Install a few basic system tools the agents rely on:
#    git (version control), ca-certificates (lets it talk to
#    the internet securely), and curl (fetch things over the web).
#    The "rm -rf" line at the end just deletes install leftovers
#    to keep the image small.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 3. Make a non-root user called "agent" and work as them.
#    Running as root (full admin) inside a container is risky;
#    a normal user is a safer default. The agent lives in /home/agent.
RUN useradd --create-home --shell /bin/bash agent
USER agent
WORKDIR /home/agent

# 4. Install Claude Code globally inside THIS image.
#    (This is the container's own copy — nothing to do with the
#    copy on your laptop. That's the whole point.)
RUN npm install -g @anthropic-ai/claude-code

# 5. Bake in Matt Pocock's skills, inside the image.
#    Same install command you ran on your machine — but now it
#    runs during the image build, so every container starts
#    with /grill-me, /to-prd, /to-issues, /tdd, etc. already there.
RUN npx skills@latest add mattpocock/skills --yes --global

# 6. (Optional) A place for the repo to live when a container runs.
#    When you start a container you'll drop the code into /work.
WORKDIR /work

# 7. Default command when a container starts.
#    Left as bash so you can poke around; AORC / Ralph will
#    override this to actually launch the agent.
CMD ["/bin/bash"]

# ------------------------------------------------------------
#  NOTES (not run — just for you):
#
#  * The ANTHROPIC API KEY is deliberately NOT written in here.
#    Never bake a secret into an image. You pass it in when you
#    RUN a container, like this (one line in your terminal):
#
#      docker run -it \
#        -e ANTHROPIC_API_KEY="sk-ant-...your-key..." \
#        -v "$PWD":/work \
#        aorc-base
#
#    -e  = hand it the key as an environment variable (safe, temporary)
#    -v  = share your current folder into the container's /work
#    aorc-base = the name we'll give this image (see build command below)
#
#  * This same image is what AORC's "pre-baked base image" decision
#    in the PRD refers to. Build it once; reuse it for every issue.
# ------------------------------------------------------------
