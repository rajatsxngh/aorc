"""S9 -- Graphify integration: knowledge-graph seam + blast-radius surface."""

from __future__ import annotations

from aorc.graphify import BlastRadiusResult, MockGraphifyClient


def test_build_and_reindex_recorded():
    client = MockGraphifyClient()

    client.build()
    client.reindex()
    client.reindex()

    assert client.build_calls == 1
    assert client.reindex_calls == 2


def test_blast_radius_returns_dependents():
    client = MockGraphifyClient(edges={"a.py": {"b.py", "c.py"}})

    result = client.blast_radius(["a.py"])

    assert result.ok is True
    assert result.files == {"b.py", "c.py"}
    assert result.error is None


def test_blast_radius_unknown_file_is_ok_but_empty():
    client = MockGraphifyClient()

    result = client.blast_radius(["never-heard-of-this.py"])

    assert result.ok is True
    assert result.files == set()


def test_blast_radius_failure_is_an_explicit_signal_not_empty():
    client = MockGraphifyClient(edges={"a.py": {"b.py"}})
    client.fail_next = True

    result = client.blast_radius(["a.py"])

    assert result.ok is False
    assert result.error
    assert result.files == set()
    # Distinct dataclass identity check: ok=False is not interchangeable
    # with a legitimate empty-set result.
    assert result != BlastRadiusResult(ok=True, files=set())

    # Failure is scripted per-call, not sticky.
    retried = client.blast_radius(["a.py"])
    assert retried.ok is True
    assert retried.files == {"b.py"}


def test_mcp_adapter_surfaces_import_failure_as_signal():
    from aorc.graphify_adapter import MCPGraphifyClient

    client = MCPGraphifyClient("http://localhost:1234")

    result = client.blast_radius(["a.py"])

    assert result.ok is False
    assert result.error
