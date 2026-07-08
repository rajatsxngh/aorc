"""S15 -- Credential & token model."""

from __future__ import annotations

import subprocess

import pytest

from aorc.credentials import (
    GITHUB_TOKEN_ENV,
    LLM_API_KEY_ENV,
    MINIMAL_PERMISSIONS,
    REDACTED,
    TOKEN_TTL_SECONDS,
    CredentialBroker,
    CredentialLeakError,
    PermissionCeilingError,
    ScrubbingGitHubClient,
    handle_token_expiry,
    scrub,
)
from aorc.github.mock import MockGitHubClient
from aorc.harness import ContainerHarness, MockContainerRuntime, WorktreeManager
from aorc.interfaces import Issue
from aorc.pipeline import branch_name

PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEfakefakefakefakefakefakefake\n"
    "-----END RSA PRIVATE KEY-----"
)
MINTED_TOKEN = "ghs_" + "a" * 36


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "a@a.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=repo, check=True)
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


class RecordingMinter:
    """Stands in for the orchestrator-side App-JWT -> installation-token
    exchange (real HTTP lives behind this seam; S18/S19's job)."""

    def __init__(self, token: str = MINTED_TOKEN) -> None:
        self.token = token
        self.calls: list[tuple] = []

    def __call__(self, private_key: str, repo: str, permissions: dict) -> str:
        self.calls.append((private_key, repo, dict(permissions)))
        return self.token


def make_broker(minter=None, **kwargs):
    return CredentialBroker(
        PRIVATE_KEY, minter or RecordingMinter(), clock=lambda: 1000.0, **kwargs
    )


# --------------------------------------------------------------------------- #
# Per-issue token minting
# --------------------------------------------------------------------------- #


def test_mint_produces_single_repo_minimal_permission_1h_token():
    minter = RecordingMinter()
    broker = make_broker(minter)

    token = broker.mint(issue_number=5, repo="acme/widget")

    assert token.token == MINTED_TOKEN
    assert token.repo == "acme/widget"
    assert token.permissions == MINIMAL_PERMISSIONS
    assert token.expires_at == 1000.0 + TOKEN_TTL_SECONDS
    # the exchange itself is the only place the private key is used
    assert minter.calls == [(PRIVATE_KEY, "acme/widget", MINIMAL_PERMISSIONS)]


def test_mint_accepts_narrower_permissions():
    broker = make_broker()

    token = broker.mint(issue_number=5, repo="acme/widget", permissions={"issues": "read"})

    assert token.permissions == {"issues": "read"}


def test_mint_rejects_permissions_outside_the_minimal_set():
    minter = RecordingMinter()
    broker = make_broker(minter)

    with pytest.raises(PermissionCeilingError):
        broker.mint(
            issue_number=5, repo="acme/widget", permissions={"administration": "write"}
        )
    # rejected before the exchange -- no token was minted
    assert minter.calls == []


def test_mint_rejects_escalation_above_the_minimal_level():
    minter = RecordingMinter()
    broker = make_broker(minter)

    with pytest.raises(PermissionCeilingError):
        broker.mint(
            issue_number=5, repo="acme/widget", permissions={"contents": "admin"}
        )
    assert minter.calls == []


def test_token_expiry_boundary():
    broker = make_broker()
    token = broker.mint(issue_number=5, repo="acme/widget")

    assert not token.expired(now=1000.0 + TOKEN_TTL_SECONDS - 1)
    assert token.expired(now=1000.0 + TOKEN_TTL_SECONDS)


# --------------------------------------------------------------------------- #
# Container env: token + LLM key in separate slots, never the private key
# --------------------------------------------------------------------------- #


def test_container_env_has_separate_github_and_llm_slots():
    broker = make_broker(llm_api_key="sk-ant-" + "b" * 30)
    token = broker.mint(issue_number=5, repo="acme/widget")

    env = broker.container_env(token)

    assert env[GITHUB_TOKEN_ENV] == MINTED_TOKEN
    assert env[LLM_API_KEY_ENV] == "sk-ant-" + "b" * 30
    assert set(env) == {GITHUB_TOKEN_ENV, LLM_API_KEY_ENV}  # nothing else rides along


def test_container_env_omits_llm_slot_when_no_key_configured():
    broker = make_broker()
    token = broker.mint(issue_number=5, repo="acme/widget")

    assert set(broker.container_env(token)) == {GITHUB_TOKEN_ENV}


def test_container_env_never_contains_private_key():
    broker = make_broker(llm_api_key="sk-ant-" + "b" * 30)
    token = broker.mint(issue_number=5, repo="acme/widget")

    for value in broker.container_env(token).values():
        assert PRIVATE_KEY not in value


def test_container_env_fails_closed_if_a_slot_would_carry_the_private_key():
    # A misconfigured minter handing back the signing key itself must raise,
    # not silently inject the master credential into a container.
    broker = make_broker(RecordingMinter(token=PRIVATE_KEY))
    token = broker.mint(issue_number=5, repo="acme/widget")

    with pytest.raises(CredentialLeakError):
        broker.container_env(token)


def test_container_env_fails_closed_on_any_private_key_block():
    # Not just *this* broker's key: any PEM private-key block in a slot value
    # is a leak (e.g. an env-expanded config value that turned out to be a key).
    other_key = "-----BEGIN PRIVATE KEY-----\nZZZZ\n-----END PRIVATE KEY-----"
    broker = make_broker(llm_api_key=other_key)
    token = broker.mint(issue_number=5, repo="acme/widget")

    with pytest.raises(CredentialLeakError):
        broker.container_env(token)


# --------------------------------------------------------------------------- #
# Token expiry mid-pipeline: teardown + re-queue, never in-container refresh
# --------------------------------------------------------------------------- #


def test_unexpired_token_continues_without_teardown(tmp_path):
    repo = _init_repo(tmp_path)
    runtime = MockContainerRuntime()
    harness = ContainerHarness(
        runtime, WorktreeManager(str(repo), str(tmp_path / "wt")),
        MockGitHubClient(issues=[Issue(number=5)]),
    )
    handle = harness.dispatch(5)
    broker = make_broker()
    token = broker.mint(issue_number=5, repo="acme/widget")

    verdict = handle_token_expiry(harness, handle, token, now=1000.0)

    assert verdict == "continue"
    assert handle.status == "running"


def test_expired_token_tears_down_and_requeues_from_last_artifact(tmp_path):
    repo = _init_repo(tmp_path)
    runtime = MockContainerRuntime()
    gh = MockGitHubClient(issues=[Issue(number=5)])
    harness = ContainerHarness(runtime, WorktreeManager(str(repo), str(tmp_path / "wt")), gh)
    handle = harness.dispatch(5)
    harness._checkpoint.registry.record(5, ["src/x.py"])
    minter = RecordingMinter()
    broker = make_broker(minter)
    token = broker.mint(issue_number=5, repo="acme/widget")

    verdict = handle_token_expiry(harness, handle, token, now=1000.0 + TOKEN_TTL_SECONDS)

    assert verdict == "re-queue"
    assert handle.status == "stopped"
    # branch preserved -- the re-queued issue resumes from the last committed
    # artifact on it -- and the in-flight claim is cleared
    assert ("delete_branch", branch_name(5)) not in gh.calls
    assert harness._checkpoint.registry.claimed_by_others(0) == {}
    # and no refresh path: expiry never re-mints (that would be the
    # in-container re-auth surface the issue forbids)
    assert len(minter.calls) == 1


# --------------------------------------------------------------------------- #
# Harness env threading: only the built env reaches the container
# --------------------------------------------------------------------------- #


def test_dispatch_passes_credential_env_to_the_container(tmp_path):
    repo = _init_repo(tmp_path)
    runtime = MockContainerRuntime()
    harness = ContainerHarness(
        runtime, WorktreeManager(str(repo), str(tmp_path / "wt")),
        MockGitHubClient(issues=[Issue(number=5)]),
    )
    broker = make_broker(llm_api_key="sk-ant-" + "b" * 30)
    env = broker.container_env(broker.mint(issue_number=5, repo="acme/widget"))

    harness.dispatch(5, env=env)

    assert runtime.envs[5] == env
    assert PRIVATE_KEY not in str(runtime.envs[5])


def test_dispatch_without_env_passes_none(tmp_path):
    repo = _init_repo(tmp_path)
    runtime = MockContainerRuntime()
    harness = ContainerHarness(
        runtime, WorktreeManager(str(repo), str(tmp_path / "wt")),
        MockGitHubClient(issues=[Issue(number=5)]),
    )

    harness.dispatch(5)

    assert runtime.envs[5] is None


# --------------------------------------------------------------------------- #
# Deterministic secret scrubbing (layer 2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "A1b2" * 9,                       # classic PAT
        "ghs_" + "C3d4" * 9,                       # installation token (what we mint)
        "gho_" + "E5f6" * 9,                       # OAuth token
        "github_pat_" + "G7" * 11 + "_" + "h8" * 30,  # fine-grained PAT
        "sk-ant-api03-" + "x" * 40,                # Anthropic key
        "sk-" + "y" * 40,                          # OpenAI-style key
    ],
)
def test_scrub_blanks_known_token_shapes(secret):
    text = f"the run failed with credential {secret} in the log"

    scrubbed = scrub(text)

    assert secret not in scrubbed
    assert REDACTED in scrubbed
    assert scrubbed.startswith("the run failed")


def test_scrub_blanks_pem_private_key_blocks():
    scrubbed = scrub(f"dumped config:\n{PRIVATE_KEY}\ndone")

    assert "PRIVATE KEY" not in scrubbed
    assert REDACTED in scrubbed


def test_scrub_leaves_innocent_text_alone():
    text = (
        "ghp_short is not a token; the ask-anthropic skill and sk-8 flag "
        "and github_patterns module are fine"
    )
    assert scrub(text) == text


def test_scrub_is_deterministic_plain_regex():
    text = "leak ghp_" + "A1b2" * 9
    assert scrub(text) == scrub(text)


# --------------------------------------------------------------------------- #
# ScrubbingGitHubClient: every agent-posted text surface scrubbed
# --------------------------------------------------------------------------- #

LEAKY = "output was sk-ant-api03-" + "x" * 40 + " end"


def test_post_comment_body_is_scrubbed_before_reaching_github():
    inner = MockGitHubClient(issues=[Issue(number=5)])
    gh = ScrubbingGitHubClient(inner)

    gh.post_comment(5, LEAKY)

    posted = inner.comments[5][0].body
    assert "sk-ant-" not in posted
    assert REDACTED in posted


def test_issue_and_pr_text_is_scrubbed():
    inner = MockGitHubClient()
    gh = ScrubbingGitHubClient(inner)

    issue = gh.create_issue(f"title {LEAKY}", f"body {LEAKY}")
    pr = gh.open_pull_request(f"title {LEAKY}", f"body {LEAKY}", head="aorc/issue-5")

    for text in (issue.title, issue.body, pr.title, pr.body):
        assert "sk-ant-" not in text
        assert REDACTED in text


def test_committed_file_content_and_message_are_scrubbed():
    inner = MockGitHubClient()
    gh = ScrubbingGitHubClient(inner)

    gh.create_branch("aorc/issue-5")
    gh.commit_file("aorc/issue-5", "notes.md", LEAKY, f"msg {LEAKY}")

    assert "sk-ant-" not in inner.files[("aorc/issue-5", "notes.md")]
    commit_call = next(c for c in inner.calls if c[0] == "commit_file")
    assert "sk-ant-" not in commit_call[3]


def test_reads_pass_through_and_innocent_label_names_survive():
    inner = MockGitHubClient(issues=[Issue(number=5, title="t", body="b")])
    gh = ScrubbingGitHubClient(inner)

    gh.add_label(5, "agent-working")

    assert gh.get_issue(5).title == "t"
    assert gh.get_labels(5) == ["agent-working"]


def test_label_names_are_scrubbed_on_every_label_write():
    # S16 closed this gap: a secret can ride in a label *name*, not just a
    # description -- add/set/create/remove all scrub the name.
    inner = MockGitHubClient(issues=[Issue(number=5)])
    gh = ScrubbingGitHubClient(inner)
    leaky_name = "leak-ghp_" + "c" * 36

    gh.add_label(5, leaky_name)
    assert all("ghp_" not in label for label in inner.issues[5].labels)

    gh.remove_label(5, leaky_name)  # scrubbed the same way, so removal matches
    assert inner.issues[5].labels == []

    gh.set_labels(5, [leaky_name, "fine"])
    assert all("ghp_" not in label for label in inner.issues[5].labels)
    assert "fine" in inner.issues[5].labels

    gh.create_label(leaky_name, description="d")
    assert all("ghp_" not in name for name in inner.created_labels)


def test_branch_names_are_scrubbed_on_pr_head_and_commit():
    inner = MockGitHubClient()
    gh = ScrubbingGitHubClient(inner)
    leaky_branch = "leak-ghp_" + "c" * 36

    pr = gh.open_pull_request("t", "b", head=leaky_branch)
    assert "ghp_" not in pr.head

    # create + commit scrub the branch name identically, so they line up
    gh.create_branch(leaky_branch)
    assert all("ghp_" not in b for b in inner.branches)
    gh.commit_file(leaky_branch, "notes.md", "content", "msg")
    assert all("ghp_" not in ref for (ref, _path) in inner.files)


def test_assert_env_clean_rejects_key_shaped_values():
    from aorc.credentials import assert_env_clean

    with pytest.raises(CredentialLeakError):
        assert_env_clean({"GITHUB_TOKEN": PRIVATE_KEY})

    # real broker-shaped env passes
    assert_env_clean({GITHUB_TOKEN_ENV: MINTED_TOKEN, LLM_API_KEY_ENV: "sk-" + "x" * 20})
