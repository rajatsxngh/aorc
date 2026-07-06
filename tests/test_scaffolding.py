"""Sanity checks that the project scaffolding and test runner are wired up."""

import aorc


def test_package_importable():
    assert aorc.__version__ == "0.1.0"
