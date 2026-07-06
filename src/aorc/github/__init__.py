"""GitHub seam: the `build_github_client` factory constructs the real SDK-backed
client from a per-issue token and repo name. Unit tests use `MockGitHubClient`
instead; orchestrator logic only ever sees the `GitHubClient` interface."""

from __future__ import annotations

from ..interfaces import GitHubClient


def build_github_client(token: str, repo: str) -> GitHubClient:
    """Construct the real GitHub SDK-backed client for a single repository."""
    from .sdk_adapter import SdkGitHubClient

    return SdkGitHubClient(token=token, repo=repo)


__all__ = ["build_github_client"]
