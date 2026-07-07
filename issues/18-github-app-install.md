# S18 — GitHub App install

## What to build

The install experience that ties the whole system to a repo — built last so it wires together real pipeline behavior rather than stubs.

- Ship AORC as a GitHub App installable on any repository or organization, with fine-grained permissions: issues (r/w), pull requests (r/w), contents (r/w), projects (r/w), actions (r/w). Register webhooks for issue events (opened/edited/labeled/comment), PR events (merged/opened/review-comment), and push-to-main (for auto-rollback).
- **On install:** open a PR adding `.aorc.yml` to the target repo, and create a GitHub Projects board with the standard columns (Backlog, Needs Clarification, In Progress, Blocked, In Review, Done).
- **Triage/clarify/decompose run immediately** (orchestrator-side, minimal config), including the first-run backfill sweep — the backlog starts organizing itself.
- **Any issue reaching dispatch is held** (`blocked: awaiting-config`) until the `.aorc.yml` PR is merged, then released. **Never** run the build pipeline against baked-in defaults (guessing `pytest`/`pip install`) — the toolchain-hallucination the config file exists to prevent. The install-PR text states the shown defaults are a template and the pipeline won't build until merged.
- **Config validation:** on a malformed/partial `.aorc.yml`, fail closed with a clear error; missing required fields (setup/test) block the build pipeline for that repo.
- **Missing `smoke:` block:** run the pipeline normally, skip reviewer smoke gate, but permanently disqualify the repo from auto-merge until smoke tests exist. Warn once in the config PR.
- **Local-LLM constraint:** a local `base_url` is only reachable on a self-hosted runner sharing the model's host. If a local model is configured on a cloud runner, surface a clear, fail-fast error (not treated as a transient provider error).

## Acceptance criteria

- [ ] App installs with the fine-grained permission set and registers all listed webhooks
- [ ] On install: `.aorc.yml` PR opened + Projects board created with the six standard columns
- [ ] Triage/clarify/decompose + backfill run immediately on install
- [ ] Dispatch-ready issues held `blocked: awaiting-config` until the config PR merges; never build on baked-in defaults
- [ ] Malformed/partial `.aorc.yml` fails closed; missing setup/test blocks the repo's build pipeline
- [ ] Missing `smoke:` → run + skip smoke gate + permanent auto-merge disqualification + one-time warning
- [ ] Local `base_url` on a cloud runner → clear fail-fast error

## Known limitation inherited from S15/S16 (address here or in S19)

A container holding `GITHUB_TOKEN` can push or hit the API **directly**,
bypassing the orchestrator-side `ScrubbingGitHubClient` entirely — layer-2
scrubbing only covers orchestrator-mediated writes. When wiring real container
plumbing: route agent pushes through an orchestrator-mediated path or a
scrubbing egress proxy, and stop passing secrets as `docker run -e KEY=value`
(visible in host `ps`; `DockerContainerRuntime.start` does this today).

## Wiring obligation inherited from S17

S17 introduced the `GitOps` seam (`merge.py`: rebase for PR-open/stale-PR
handling, revert for auto-rollback) with **only `MockGitOps`** behind it — no
real implementation exists. The real surface lands here: the push-to-main
Actions workflow that detects a red main and performs the revert, plus real
rebase execution against actual branches. Webhook wiring note: the full merge
handler is `merge.MergeTimeHandler.on_pr_merged` / `.on_pr_comment` /
`.on_main_broken` — not `WakeLoop.on_pr_merged` (the bare held-sweep), which
the handler already calls internally.

## Blocked by

- S2 (labels/board), S17 (full pipeline + push-to-main rollback behavior to wire webhooks against)
