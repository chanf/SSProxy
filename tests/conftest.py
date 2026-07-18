"""Pytest configuration shared across the whole test suite.

Puts the repository root on ``sys.path`` so ``import build_ipk`` works from any
test module, and exposes the :mod:`build_ipk` module + its :data:`src_files`
dict via fixtures so tests never reach for hard-coded paths.
"""
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import build_ipk  # noqa: E402  (after sys.path tweak)


@pytest.fixture(scope="session")
def builder():
    """The imported ``build_ipk`` module (builder code + embedded ``src_files``)."""
    return build_ipk


@pytest.fixture(scope="session")
def src_files(builder):
    """The ``src_files`` dict — every file that ships inside the .ipk."""
    return builder.src_files
