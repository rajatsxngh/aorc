"""S1 architecture invariant #1 — orchestrator/core code depends only on the
`GitHubClient` / `LLMClient` interfaces, never on a provider or GitHub SDK.

Provider/GitHub SDK imports are permitted *only* inside adapter modules
(files named `*_adapter.py`). Everywhere else in the package, importing such
an SDK is a violation. Enforced statically over the AST so it can't regress.
"""

import ast
import pathlib

SDK_MODULES = {"anthropic", "openai", "github"}  # PyGithub imports as `github`
SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "aorc"


def _imported_modules(tree):
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                mods.add(node.module.split(".")[0])
    return mods


def test_only_adapters_touch_sdks():
    offenders = []
    for py in SRC.rglob("*.py"):
        if py.name.endswith("_adapter.py"):
            continue  # adapters are the sanctioned seam
        used = _imported_modules(ast.parse(py.read_text()))
        bad = used & SDK_MODULES
        if bad:
            offenders.append(f"{py.relative_to(SRC)}: {sorted(bad)}")
    assert not offenders, f"non-adapter modules import an SDK: {offenders}"
