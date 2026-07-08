"""S5 -- Design stage: strict schema + actionability gate.

The first pipeline stage inside the container. A design agent reads the
issue (+ clarification Q&A + relevant repo files -- fresh, scoped context,
nothing carried over from other stages) and must emit a design doc in a
strict, machine-readable (JSON) schema -- not freeform -- so the tester and
coder consume it identically every run.

Design *is* the actionability gate: it either bounds the work (proceed) or
it can't (needs-clarification / agent-blocked). Routing on an invalid
response is a pure, mechanical schema check -- no LLM judgment:
  - fails to parse / missing required fields (a *format* miss) -> retry the
    design agent, then agent-blocked if still unparseable after N attempts.
  - parses with all required fields but low `confidence` -> needs-clarification
    (the gate working as intended, not a format problem).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .graphify import GraphifyClient
from .harness import CheckpointReport, InFlightRegistry
from .interfaces import GitHubClient, LLMClient, Message, strip_code_fences
from .pipeline import branch_name

REQUIRED_FIELDS = ("interface", "test_specs", "task_list", "files", "confidence")

DEFAULT_CONFIDENCE_THRESHOLD = 0.5
DEFAULT_MAX_RETRIES = 3

_SYSTEM_PROMPT = (
    "You are the design agent for a software issue. Produce a design doc as a "
    "single JSON object with exactly these fields:\n"
    '  "interface": [{"name": ..., "inputs": [...], "outputs": ...}, ...]\n'
    '  "test_specs": [ "behavior to test", ... ]\n'
    '  "task_list": [ "ordered implementation step", ... ]\n'
    '  "files": [ "exact/file/path.py", ... ]\n'
    "Every `files` entry must be the exact repo-relative path of the file "
    "(e.g. src/pkg/module.py for a src-layout package), matching an existing "
    "file's real location when the file already exists -- never a bare "
    "filename.\n"
    '  "confidence": 0.0-1.0\n'
    "If the issue cannot be bounded into a finite interface, test list, and task "
    "list, still reply with the JSON schema but set confidence low. Reply with "
    "the JSON object only, no surrounding prose."
)


def design_doc_path(issue_number: int) -> str:
    return f"aorc/issue-{issue_number}/design.md"


@dataclass
class DesignDoc:
    interface: list
    test_specs: list
    task_list: list
    files: list
    confidence: float
    raw: dict = field(default_factory=dict)


@dataclass
class DesignResult:
    status: str  # "proceed" | "needs-clarification" | "agent-blocked"
    doc: DesignDoc | None = None
    attempts: int = 0


def parse_design_response(text: str) -> DesignDoc | None:
    """Pure schema check, no LLM judgment: `None` on any format miss (invalid
    JSON, not an object, or missing a required field)."""
    try:
        data = json.loads(strip_code_fences(text))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or any(f not in data for f in REQUIRED_FIELDS):
        return None
    try:
        confidence = float(data["confidence"])
    except (TypeError, ValueError):
        return None
    return DesignDoc(
        interface=data["interface"],
        test_specs=data["test_specs"],
        task_list=data["task_list"],
        files=data["files"],
        confidence=confidence,
        raw=data,
    )


# Directories never containing design-relevant source files.
_RESOLVE_SKIP_DIRS = {".git", "__pycache__", ".aorc", ".aorc-worktrees", "node_modules", ".venv"}


def resolve_design_files(files: list, cwd: str) -> list:
    """S42: snap the design's `files` entries to real worktree paths. The
    design LLM emits unqualified paths ("math_utils.py" for a module that
    actually lives at src/sandbox/math_utils.py), and every downstream
    consumer -- module derivation, stub seeding, import header/normalizer,
    the coder's writes -- inherits the error consistently, so nothing ever
    fails loudly. An entry that exists at its stated path is kept; a
    missing entry whose basename matches exactly one file in the tree is
    snapped to that file; new or ambiguous entries are kept verbatim
    (never guessed)."""
    resolved = []
    for entry in files:
        if not isinstance(entry, str) or os.path.exists(os.path.join(cwd, entry)):
            resolved.append(entry)
            continue
        basename = os.path.basename(entry)
        matches = []
        for root, dirs, names in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in _RESOLVE_SKIP_DIRS]
            if basename in names:
                matches.append(
                    os.path.relpath(os.path.join(root, basename), cwd).replace(os.sep, "/")
                )
        resolved.append(matches[0] if len(matches) == 1 else entry)
    return resolved


def checkpoint_report(issue_number: int, doc: DesignDoc) -> CheckpointReport:
    """The design doc's `files` field feeds the S4 checkpoint report."""
    return CheckpointReport(issue_number=issue_number, files=list(doc.files))


def rebuild_in_flight_registry(
    issue_numbers: list[int], github: GitHubClient
) -> InFlightRegistry:
    """S10 stateless bookkeeping: the orchestrator holds no memory between
    wakes, so an in-process `InFlightRegistry` can't be trusted to survive a
    restart. Rebuild a throwaway one from what's actually persisted to
    GitHub -- each in-flight issue's design doc `files` field, already
    committed to that issue's own branch by `DesignStage._commit` -- instead
    of a separate state file. An issue with no design doc yet (or an
    unparseable one) simply contributes no claim.
    """
    registry = InFlightRegistry()
    for number in issue_numbers:
        raw = github.get_file(design_doc_path(number), branch_name(number))
        if raw is None:
            continue
        doc = parse_design_response(raw)
        if doc is not None:
            registry.record(number, list(doc.files))
    return registry


class DesignStage:
    """Runs the design agent on fresh, scoped context; validates the strict
    schema mechanically; commits the design doc to the issue's branch when it
    bounds the work."""

    def __init__(
        self,
        llm: LLMClient,
        github: GitHubClient,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        graphify: GraphifyClient | None = None,
    ) -> None:
        self._llm = llm
        self._github = github
        self._max_retries = max_retries
        self._confidence_threshold = confidence_threshold
        self._graphify = graphify

    def _messages(self, issue_number: int, issue_body: str, qa: list[str], repo_files: dict[str, str]) -> list[Message]:
        # Scoped context only: this issue's body, its Q&A, and the requested
        # repo files -- nothing from other stages or other issues.
        parts = [f"Issue #{issue_number}:\n{issue_body}"]
        if qa:
            parts.append("Clarification Q&A:\n" + "\n".join(qa))
        for path, content in repo_files.items():
            parts.append(f"Repo file {path}:\n{content}")
        if self._graphify is not None and repo_files:
            result = self._graphify.blast_radius(list(repo_files))
            if result.ok:
                blast = ", ".join(sorted(result.files)) or "none"
                parts.append(f"Blast radius (files that import/call the above): {blast}")
            else:
                parts.append(f"Blast radius query failed ({result.error}); proceed without it.")
        return [
            Message("system", _SYSTEM_PROMPT),
            Message("user", "\n\n".join(parts)),
        ]

    def run(
        self,
        issue_number: int,
        issue_body: str,
        *,
        qa: list[str] | None = None,
        repo_files: dict[str, str] | None = None,
    ) -> DesignResult:
        messages = self._messages(issue_number, issue_body, qa or [], repo_files or {})
        attempts = 0
        for attempts in range(1, self._max_retries + 1):
            completion = self._llm.complete(messages)
            doc = parse_design_response(completion.text)
            if doc is None:
                continue  # format miss -- retry the design agent
            if doc.confidence < self._confidence_threshold:
                return DesignResult(status="needs-clarification", doc=doc, attempts=attempts)
            self._commit(issue_number, doc)
            return DesignResult(status="proceed", doc=doc, attempts=attempts)
        return DesignResult(status="agent-blocked", doc=None, attempts=attempts)

    def _commit(self, issue_number: int, doc: DesignDoc) -> None:
        self._github.commit_file(
            branch_name(issue_number),
            design_doc_path(issue_number),
            json.dumps(doc.raw, indent=2),
            message=f"design: issue #{issue_number}",
        )
