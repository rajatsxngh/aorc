#!/bin/bash
# ---------------------------------------------------------------------------
# afk.sh — the Ralph loop. Runs up to N iterations of: feed Claude the issues
# + recent commits + prompt, let it do ONE task, commit, repeat.
#
# Runs INSIDE the Docker sandbox (launched by run-sandbox.sh). The container
# is the security boundary — never run this directly on your host machine.
#
# Usage:  ./afk.sh 3         # run up to 3 iterations
# ---------------------------------------------------------------------------

set -eo pipefail

if [ -z "$1" ]; then
  echo "Usage: $0 <iterations>"
  exit 1
fi

# ---- Install the project's declared dependencies before the loop ----
# The image has Python + pytest baked in, but the PROJECT's own deps
# (pyyaml, etc. from pyproject.toml) must be installed into this fresh
# container. -e . reads pyproject.toml and installs them.
if [ -f "pyproject.toml" ]; then
  echo "Installing project dependencies (pip install -e .)..."
  pip install --break-system-packages -e . >/dev/null 2>&1 || \
    echo "(dep install skipped/failed — continuing; the agent may set it up)"
fi

for ((i=1; i<=$1; i++)); do
  echo ""
  echo "=================== ITERATION $i of $1 ==================="
  echo ""

  head_before=$(git rev-parse HEAD 2>/dev/null || echo "none")
  commits=$(git log -n 5 --format="%H%n%ad%n%B---" --date=short 2>/dev/null || echo "No commits found")
  issues=$(cat issues/*.md 2>/dev/null || echo "No issues found")
  prompt=$(cat ralph/prompt.md)

  tmpfile=$(mktemp)

  # --model sonnet keeps cost down (Opus is the pricey default).
  printf '%s' "Previous commits: $commits Issues: $issues $prompt" \
    | claude \
        --print \
        --model sonnet \
        --permission-mode acceptEdits \
        --allowedTools "Read,Edit,Write,Bash" \
    | tee "$tmpfile"

  if grep -q "NO MORE TASKS" "$tmpfile"; then
    echo ""
    echo "Ralph complete after $i iterations (agent reported NO MORE TASKS)."
    rm -f "$tmpfile"
    exit 0
  fi
  rm -f "$tmpfile"

  # ---- SCRIPT-ENFORCED SAFETY GATE ----

  # Re-install deps in case the agent added a new one this iteration.
  if [ -f "pyproject.toml" ]; then
    pip install --break-system-packages -e . >/dev/null 2>&1 || true
  fi

  head_after=$(git rev-parse HEAD 2>/dev/null || echo "none")
  if [ "$head_before" == "$head_after" ]; then
    echo ""
    echo "STOP: no new commit this iteration. Agent may be stuck or blocked."
    echo "Review the output above before continuing. Halting the loop."
    exit 1
  fi

  # Find pytest whether on PATH or inside a project .venv.
  PYTEST_BIN=""
  if [ -x ".venv/bin/pytest" ]; then
    PYTEST_BIN=".venv/bin/pytest"
  elif command -v pytest >/dev/null 2>&1; then
    PYTEST_BIN="pytest"
  fi

  if [ -n "$PYTEST_BIN" ]; then
    if ! "$PYTEST_BIN" -q; then
      echo ""
      echo "STOP: tests are RED after the agent's commit (HEAD $head_after)."
      echo "The agent committed failing code. Halting so nothing builds on top of it."
      exit 1
    fi
  else
    echo "(pytest not found — skipping test gate this iteration.)"
  fi

  echo ""
  echo "Iteration $i OK: new commit + tests green."
done

echo ""
echo "Reached iteration cap ($1). Stopping."