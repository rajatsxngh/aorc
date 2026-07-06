"""S15 -- Credential & token model.

The blast-radius bound for unattended agents, in three mechanical pieces:

- `CredentialBroker`: the orchestrator-side owner of the App private key --
  the master credential, held here and nowhere else (GitHub Actions
  encrypted secrets in v1). It mints one short-lived installation token per
  issue, scoped to a single repository and a minimal permission set, ~1h
  expiry. Only the token (plus the separately-slotted LLM provider key)
  ever reaches a container; `container_env` fails closed with
  `CredentialLeakError` if any slot value would carry a private-key block,
  so the key structurally cannot be injected. The actual App-JWT ->
  installation-token HTTP exchange sits behind the injectable `minter`
  callable, the same seam style as everything else here -- the real
  exchange is S18/S19's job.
- `handle_token_expiry`: the ~1h token *will* expire on hard issues. On
  expiry: tear down through the harness's existing branch-preserving path
  and report "re-queue" -- the issue resumes from its last committed
  artifact (S4's checkpoint machinery, zero new security surface). There
  is deliberately no refresh path: a container can never re-authenticate,
  because minting requires the private key it was never given.
- Secret scrubbing, layer 2 of two (layer 1 is instructing the agent not
  to print its env): `scrub` runs a hardcoded regex set drawn from the
  gitleaks/trufflehog pattern lists -- GitHub token prefixes
  (`ghp_`/`ghs_`/`gho_`/`ghu_`/`ghr_`/`github_pat_`), LLM keys
  (`sk-ant-`, `sk-`), and PEM private-key blocks -- and blanks matches.
  Fully deterministic, no LLM judgment. `ScrubbingGitHubClient` decorates
  any `GitHubClient` (same style as S14's `RateLimitedGitHubClient`) so
  every agent-authored text surface -- comments, issue bodies, PR text,
  committed file content -- is scrubbed before it reaches GitHub.

GitHub and LLM credentials stay in separate slots with separate lifetimes:
`GITHUB_TOKEN` holds the per-issue ~1h installation token; `AORC_LLM_API_KEY`
holds the long-lived provider key from `.aorc.yml` (env-expanded there --
invariant #2, never hardcoded). Expiry of one never touches the other.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from .harness import ContainerHandle, ContainerHarness
from .interfaces import Comment, GitHubClient, Issue, PullRequest

TOKEN_TTL_SECONDS = 3600.0  # ~1 hour, per the credential model

# The minimal per-issue permission set: enough to branch, commit, comment,
# label, and open the PR -- nothing org- or admin-shaped. Also the enforced
# ceiling: `mint` rejects any request for a permission outside this set or at
# a level above it.
MINIMAL_PERMISSIONS = {"contents": "write", "issues": "write", "pull_requests": "write"}

_PERMISSION_LEVELS = {"read": 0, "write": 1, "admin": 2}

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
LLM_API_KEY_ENV = "AORC_LLM_API_KEY"

REDACTED = "[REDACTED]"

# Hardcoded scrub set, shapes drawn from the gitleaks/trufflehog rule lists.
# Order is irrelevant (every match blanks to the same token); each pattern is
# anchored on word boundaries so prose like "github_patterns" survives.
SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),      # ghp_/gho_/ghu_/ghs_/ghr_
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"),    # fine-grained PAT
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{24,}\b"),         # Anthropic
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),             # OpenAI / compatible
    re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)

_PRIVATE_KEY_BLOCK_RE = SECRET_PATTERNS[-1]


class PermissionCeilingError(Exception):
    """A mint request asked for permissions broader than the minimal per-issue
    set. Raised instead of minting -- the ceiling is enforced, not advisory."""


class CredentialLeakError(Exception):
    """A container env slot was about to carry a private key. Raised instead
    of injecting -- the master credential never leaves the orchestrator."""


@dataclass
class IssueToken:
    """One issue's installation access token: single repo, minimal
    permissions, short-lived. The only GitHub credential a container sees."""

    token: str
    repo: str
    permissions: dict[str, str]
    expires_at: float  # epoch seconds

    def expired(self, now: float) -> bool:
        return now >= self.expires_at


class CredentialBroker:
    """Holds the App private key and mints per-issue tokens. `minter` is the
    injectable App-JWT -> installation-token exchange
    (`minter(private_key, repo, permissions) -> token string`); `clock` is
    injectable the same way S14's `sleep` is, so expiry tests never wait."""

    def __init__(
        self,
        private_key: str,
        minter: Callable[[str, str, dict], str],
        *,
        llm_api_key: str | None = None,
        token_ttl_seconds: float = TOKEN_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._private_key = private_key
        self._minter = minter
        self._llm_api_key = llm_api_key
        self._token_ttl_seconds = token_ttl_seconds
        self._clock = clock

    def mint(
        self, issue_number: int, repo: str, permissions: dict[str, str] | None = None
    ) -> IssueToken:
        """Mint one issue's token: single repo, minimal (or narrower)
        permissions, expiring `token_ttl_seconds` from now. A request broader
        than `MINIMAL_PERMISSIONS` raises `PermissionCeilingError` before the
        exchange -- the ceiling is enforced, never advisory."""
        perms = dict(permissions if permissions is not None else MINIMAL_PERMISSIONS)
        for scope, level in perms.items():
            ceiling = MINIMAL_PERMISSIONS.get(scope)
            if ceiling is None:
                raise PermissionCeilingError(
                    f"permission scope {scope!r} is outside the minimal per-issue set"
                )
            if _PERMISSION_LEVELS.get(level, max(_PERMISSION_LEVELS.values())) > (
                _PERMISSION_LEVELS[ceiling]
            ):
                raise PermissionCeilingError(
                    f"permission {scope}:{level} exceeds the ceiling {scope}:{ceiling}"
                )
        token = self._minter(self._private_key, repo, perms)
        return IssueToken(
            token=token,
            repo=repo,
            permissions=perms,
            expires_at=self._clock() + self._token_ttl_seconds,
        )

    def container_env(self, token: IssueToken) -> dict[str, str]:
        """The exact env a container receives: the per-issue GitHub token and
        the LLM provider key, in separate slots -- and provably nothing
        key-shaped. Fails closed rather than ever injecting a private key."""
        env = {GITHUB_TOKEN_ENV: token.token}
        if self._llm_api_key is not None:
            env[LLM_API_KEY_ENV] = self._llm_api_key
        for slot, value in env.items():
            if (self._private_key and self._private_key in value) or (
                _PRIVATE_KEY_BLOCK_RE.search(value)
            ):
                raise CredentialLeakError(
                    f"container env slot {slot} would carry a private key; "
                    "the signing key never enters a container"
                )
        return env


def handle_token_expiry(
    harness: ContainerHarness, handle: ContainerHandle, token: IssueToken, *, now: float
) -> str:
    """Tear-down-and-resume on token expiry. `now` is caller-supplied (same
    seam style as S11's `elapsed_days`). Expired -> teardown through the
    existing branch-preserving path (outcome "held": branch kept, in-flight
    claim cleared) and report "re-queue"; the wake loop (S16/S18) owns the
    actual re-dispatch, where the issue resumes from the last committed
    artifact with a freshly minted token. Nothing here (or anywhere) refreshes
    a live container's token -- there is no such path."""
    if not token.expired(now):
        return "continue"
    harness.teardown(handle, outcome="held")
    return "re-queue"


def scrub(text: str) -> str:
    """Blank every known credential shape. Pure regex -- deterministic, no
    LLM judgment anywhere in the decision."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class ScrubbingGitHubClient(GitHubClient):
    """Decorator: scrubs every agent-authored text argument on the mutating
    surfaces (comments, issue text, PR text, committed content) before it
    reaches GitHub. Reads and non-text mutations delegate untouched."""

    def __init__(self, inner: GitHubClient) -> None:
        self._inner = inner

    # ---- issues ---------------------------------------------------------- #
    def get_issue(self, number: int) -> Issue:
        return self._inner.get_issue(number)

    def list_issues(self, state: str = "open") -> list[Issue]:
        return self._inner.list_issues(state)

    def close_issue(self, number: int) -> None:
        return self._inner.close_issue(number)

    def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> Issue:
        return self._inner.create_issue(scrub(title), scrub(body), labels)

    # ---- comments -------------------------------------------------------- #
    def post_comment(self, issue_number: int, body: str) -> Comment:
        return self._inner.post_comment(issue_number, scrub(body))

    def list_comments(self, issue_number: int) -> list[Comment]:
        return self._inner.list_comments(issue_number)

    # ---- labels ---------------------------------------------------------- #
    def get_labels(self, issue_number: int) -> list[str]:
        return self._inner.get_labels(issue_number)

    def add_label(self, issue_number: int, label: str) -> None:
        return self._inner.add_label(issue_number, label)

    def remove_label(self, issue_number: int, label: str) -> None:
        return self._inner.remove_label(issue_number, label)

    def set_labels(self, issue_number: int, labels: list[str]) -> None:
        return self._inner.set_labels(issue_number, labels)

    def create_label(
        self, name: str, color: str = "ededed", description: str = ""
    ) -> None:
        return self._inner.create_label(name, color, scrub(description))

    # ---- pull requests --------------------------------------------------- #
    def open_pull_request(
        self, title: str, body: str, head: str, base: str = "main"
    ) -> PullRequest:
        return self._inner.open_pull_request(scrub(title), scrub(body), head, base)

    def get_pull_request(self, number: int) -> PullRequest:
        return self._inner.get_pull_request(number)

    def list_pull_requests(self, state: str = "open") -> list[PullRequest]:
        return self._inner.list_pull_requests(state)

    def merge_pull_request(self, number: int, method: str = "merge") -> None:
        return self._inner.merge_pull_request(number, method)

    def delete_branch(self, branch: str) -> None:
        return self._inner.delete_branch(branch)

    # ---- repo contents ---------------------------------------------------- #
    def get_file(self, path: str, ref: str) -> str | None:
        return self._inner.get_file(path, ref)

    def commit_file(self, branch: str, path: str, content: str, message: str) -> None:
        return self._inner.commit_file(branch, path, scrub(content), scrub(message))

    # ---- projects board -------------------------------------------------- #
    def set_board_column(self, issue_number: int, column: str) -> None:
        return self._inner.set_board_column(issue_number, column)

    def get_board_column(self, issue_number: int) -> str | None:
        return self._inner.get_board_column(issue_number)
