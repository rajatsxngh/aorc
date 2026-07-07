# AORC v1 — Issue Breakdown

18 tracer-bullet slices from `AORC-PRD-v1-FINAL-tagged.md`. Spine = S1→S8 (one actionable issue → review-approved PR). Rest thicken: parallelism, refinement, guards, install.

## Dependency order

```
S1  two seams (GitHubClient + LLMClient)        ← blocks everything
├─ S2  label state machine + board
├─ S3  triage
│   ├─ S11 clarification (grill-me)
│   │   └─ S12 epic decomposition
├─ S9  graphify integration
S2 + S3 →
└─ S4  container harness + checkpoint spine
    ├─ S5  design stage + schema + gate
    │   └─ S6  tester + critic + red/error gate
    │       └─ S7  coder bounded fix loop
    │           └─ S8  reviewer + coverage + PR open
    ├─ S13 cost + compute guards
    ├─ S15 credential & token model
    └─ S4 + S9 →
        └─ S10 collision + dispatch + concurrency
            └─ S16 liveness & idempotency
S1 →
└─ S14 failure escalation + backoff
S8 + S10 →
└─ S17 merge-time + auto-rollback
S2 + S17 →
└─ S18 github app install
```

## Slices

| # | Slice | Blocked by | Stories |
|---|---|---|---|
| S1 | Two seams: GitHubClient + LLMClient | — | 4, 48–50 |
| S2 | Label state machine + board | S1 | 10, 55–57 (A1) |
| S3 | Triage (orchestrator-side) | S1 | 7–9 |
| S4 | Container harness + checkpoint spine | S2, S3 | 24, 40–43 |
| S5 | Design stage + schema + actionability gate | S4 | 27–29 (B12, B13) |
| S6 | Tester + critic + red/error + iface coverage | S5 | 30–32 (safety 2,4,5a) |
| S7 | Coder bounded fix loop | S6 | 33–36 (safety 3,7) |
| S8 | Reviewer + coverage + smoke + PR open | S7 | 37–39, 51 (B14, safety 5b,6) |
| S9 | Graphify integration | S1 | 20, 21 |
| S10 | Collision + dispatch selector + concurrency | S4, S9 | 22, 23, 25, 26 (A3, B19) |
| S11 | Clarification (grill-me) | S3 | 11–15 (B20, B21) |
| S12 | Epic decomposition | S3, S11 | 16–19 (B8, B9) |
| S13 | Cost + compute guards | S4 | — (B1–B4) |
| S14 | Failure escalation + backoff | S1 | 44–47 (B25, B26) |
| S15 | Credential & token model | S4 | — (B10, B11, safety 10) |
| S16 | Liveness & idempotency | S10 | — (B5–B7) |
| S17 | Merge-time + auto-rollback | S8, S10 | 52–54 (B16–B18, B22, safety 9) |
| S18 | GitHub App install | S2, S17 | 1–3, 5, 6 (B23, B27) |

## v1.5 — go-live glue (open, from the post-S19 full-system readiness review)

All v1 components are built and unit-tested, but AORC cannot run live: no
entry point, no build-stage driver, no real token minter, no webhook
receiver, no executed Actions surface. S21 + S22 unblock the rest.

```
S21 composition root / entry point               ← unblocks everything live
└─ S22 pipeline driver + worktree/API split-brain fix
    ├─ S23 real token minter (App-JWT → installation token)
    ├─ S24 webhook receiver + HMAC verification
    └─ S23 + S24 →
        └─ S25 real GitHub Actions execution wiring
```

| # | Slice | Blocked by |
|---|---|---|
| S21 | Composition root / entry point | — |
| S22 | Pipeline driver + split-brain fix | S21 |
| S23 | Real token minter | S21, S22 |
| S24 | Webhook receiver + HMAC | S21, S22 |
| S25 | Actions execution wiring | S21–S24 |

## Known open limitation (post-v1)

A container holding its per-issue `GITHUB_TOKEN` can push or call the API
directly, bypassing the orchestrator-side `ScrubbingGitHubClient` (layer-2
scrubbing covers orchestrator-mediated writes only). Carried S15 → S16 → S18
→ S19; S19 closed the `docker run -e` host-`ps` exposure half (env now via a
0600 temp `--env-file`) but push mediation (orchestrator-side push /
scrubbing egress proxy) requires the real in-container agent execution path,
which v1 never builds. First work item for any v2.
