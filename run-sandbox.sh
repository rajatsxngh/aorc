#!/bin/bash
# ---------------------------------------------------------------------------
# run-sandbox.sh — builds the sandbox image once, then runs the Ralph loop
# inside it. The container is your safety boundary: the auto-approving agent
# only touches what this script mounts, and (for now) has no credential that
# authenticates to your org repo.
#
# Usage:
#   ./run-sandbox.sh build     # build the image (once, or after Dockerfile edits)
#   ./run-sandbox.sh shell     # open a shell inside the box to poke around
#   ./run-sandbox.sh loop 2    # run the ralph loop for up to N iterations
# ---------------------------------------------------------------------------
set -euo pipefail

IMAGE_NAME="ralph-sandbox"

# The folder shared into the container = wherever you run this script from.
# Run it from your project root (the folder containing issues/, ralph/, .env).
PROJECT_DIR="$(pwd)"

case "${1:-}" in
  build)
    # Turn the Dockerfile recipe into a frozen image named ralph-sandbox.
    docker build -t "$IMAGE_NAME" .
    ;;

  shell|loop)
    MODE="$1"

    # Require an API key file so we fail early with a clear message.
    if [ ! -f "$PROJECT_DIR/.env" ]; then
      echo "ERROR: no .env file found in $PROJECT_DIR"
      echo "Create it with one line:  ANTHROPIC_API_KEY=sk-ant-...your-key..."
      echo "And add .env to your .gitignore so the key is never committed."
      exit 1
    fi

    # Decide what runs inside the box.
    if [ "$MODE" = "shell" ]; then
      CMD="/bin/bash"
    else
      ITERATIONS="${2:-2}"   # default to 2 iterations if none given
      CMD="bash ralph/afk.sh $ITERATIONS"
    fi

    # --- The important flags, explained ---
    #
    # --rm                 : delete the container when it exits (boxes are disposable)
    # -it                  : interactive, so you can watch and Ctrl-C to stop
    # --env-file .env      : inject your API key at RUN time (never baked into the image)
    # -v "$PROJECT_DIR":/work : share ONLY this project folder into /work.
    #                          THIS IS ESSENTIAL — without it the agent runs in an
    #                          empty box and any commits vanish when the box is deleted.
    # -w /work             : start inside the shared project folder
    # --name ralph-running : a friendly name while it runs
    #
    # NETWORK NOTE (read once):
    #   By default the box has full outbound internet, which includes github.com.
    #   For your FIRST slices (S1-S2: pure Python, no GitHub) this is fine, because
    #   there is no org token in .env and no org repo wired in — so nothing can
    #   authenticate to your org even with network access. The only outbound the
    #   agent needs right now is api.anthropic.com so Claude can think.
    #
    #   When real GitHub access enters (around S9 Graphify / S18 GitHub App),
    #   switch to the HARD network wall described at the bottom of this file.
    docker run --rm -it \
      --env-file .env \
      -v "$PROJECT_DIR":/work \
      -w /work \
      --name ralph-running \
      "$IMAGE_NAME" \
      $CMD

    echo ""
    echo "Sandbox exited. Container removed."
    echo "Commits the agent made persist in $PROJECT_DIR (the folder is shared)."
    ;;

  *)
    echo "Usage: $0 {build|shell|loop [iterations]}"
    echo "  $0 build       # build the image once"
    echo "  $0 shell       # open a shell inside the box"
    echo "  $0 loop 2      # run the loop for up to 2 iterations"
    exit 1
    ;;
esac

# ---------------------------------------------------------------------------
# LATER — the HARD network wall (do this before slices that touch real GitHub):
#
#   1. Create an internal (no-outbound) network:
#        docker network create --internal ralph-net
#
#   2. Run a tiny proxy that allowlists ONLY api.anthropic.com and your
#      SANDBOX repo host, and attach the container to ralph-net with
#      --network ralph-net so the org GitHub is physically unreachable at
#      the network layer (not just the credential layer).
#
#   Not needed for your first runs — the no-org-token guard above is enough
#   to start safely on S1-S2.
# ---------------------------------------------------------------------------
