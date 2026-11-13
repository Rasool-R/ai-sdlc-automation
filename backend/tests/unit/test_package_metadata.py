"""Smoke test verifying package initialization and Python runtime requirement."""

import sys

import app


def test_package_version_and_python_runtime() -> None:
    assert app.__version__ == "0.1.0"
    assert sys.version_info >= (3, 11)