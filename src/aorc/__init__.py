"""AORC — Autonomous Orchestrated Repo Contributor.

The orchestrator core depends only on the ``GitHubClient`` and ``LLMClient``
interfaces (see :mod:`aorc.clients`), never on a provider or GitHub SDK
directly. Configuration (models, providers, toolchain) comes from ``.aorc.yml``.
"""

__version__ = "0.1.0"
