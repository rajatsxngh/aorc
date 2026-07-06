#!/bin/bash
# ---------------------------------------------------------------------------
# afk.sh — the Ralph loop. Runs up to N iterations of: feed Claude the issues
# + recent commits + prompt, let it do ONE task, commit, repeat.
#
# Runs INSIDE the Docker sandbox (launched by run-sandbox.sh). The container
# is the security boundary — never run this directly on your host machine,
# because it auto-approves file edits and shell commands.
#
# Usage:  ./afk.sh 3         # run up to 3 iterations
#         Stops early if the agent outputs NO MORE TASKS.
# ---------------------------------------------------------------------------

set -eo pipefail

# Require an iteration count so you never accidentally launch an unbounded loop.
if [ -z "$1" ]; then
  echo "Usage: $0 <iterations>"
  exit 1
fi

for ((i=1; i<=$1; i++)); do
  echo ""
  echo "=================== ITERATION $i of $1 ==================="
  echo ""

  # Remember the commit we're on, so we can tell if the agent actually committed.
  head_before=$(git rev-parse HEAD 2>/dev/null || echo "none")

  # MEMORY: last 5 commits tell this fresh Claude what prior iterations did.
  commits=$(git log -n 5 --format="%H%n%ad%n%B---" --date=short 2>/dev/null || echo "No commits found")

  # WORK: all open (not-done) issue files.
  issues=$(cat issues/*.md 2>/dev/null || echo "No issues found")

  # INSTRUCTIONS.
  prompt=$(cat ralph/prompt.md)

  tmpfile=$(mktemp)

  # Run Claude Code headless. --print runs once and exits.
  printf '%s' "Previous commits: $commits Issues: $issues $prompt" \
    | claude \
        --print \
        --permission-mode acceptEdits \
        --allowedTools "Read,Edit,Write,Bash" \
    | tee "$tmpfile"

  # Stop early if the agent reports there's nothing left to do.
  if grep -q "NO MORE TASKS" "$tmpfile"; then
    echo ""
    echo "Ralph complete after $i iterations (agent reported NO MORE TASKS)."
    rm -f "$tmpfile"
    exit 0
  fi
  rm -f "$tmpfile"

  # ---- SCRIPT-ENFORCED SAFETY GATE (does not trust the agent's goodwill) ----

  # 1. Did the agent actually make a commit this iteration?
  head_after=$(git rev-parse HEAD 2>/dev/null || echo "none")
  if [ "$head_before" == "$head_after" ]; then
    echo ""
    echo "STOP: no new commit this iteration. Agent may be stuck or blocked."
    echo "Review the output above before continuing. Halting the loop."
    exit 1
  fi

  # 2. Do the tests actually pass on what was just committed?
  #    (The prompt tells the agent not to commit on red, but we verify.)
  if command -v pytest >/dev/null 2>&1; then
    if ! pytest -q; then
      echo ""
      echo "STOP: tests are RED after the agent's commit (HEAD $head_after)."
      echo "The agent committed failing code. Halting so nothing builds on top of it."
      exit 1
    fi
  else
    echo "(pytest not found yet — skipping test gate this early iteration.)"
  fi

  echo ""
  echo "Iteration $i OK: new commit + tests green."
done

echo ""
echo "Reached iteration cap ($1). Stopping."