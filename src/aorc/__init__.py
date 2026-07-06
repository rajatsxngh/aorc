"""AORC — Autonomous Orchestrated Repo Contributor.

The orchestrator core depends only on the ``GitHubClient`` and ``LLMClient``
interfaces (see :mod:`aorc.interfaces`), never on a provider or GitHub SDK
directly. Configuration (models, providers, toolchain) comes from ``.aorc.yml``.
"""

from .interfaces import (
    Comment,
    Completion,
    GitHubClient,
    Issue,
    LLMClient,
    Message,
    PullRequest,
)

__version__ = "0.1.0"

__all__ = [
    "Comment",
    "Completion",
    "GitHubClient",
    "Issue",
    "LLMClient",
    "Message",
    "PullRequest",
    "__version__",
]
