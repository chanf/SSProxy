"""Tests for the internal ``urlencode`` helper.

Driven by sourcing the dispatcher-stripped library (urlencode is not a
subcommand). On any system with ``od`` (the fast path), every byte is emitted
as ``%XX`` — including ASCII alphanumerics — which is the behaviour the router
relies on. These tests pin that exact output.
"""
import pytest

CASES = [
    ("abc", "%61%62%63"),
    ("a b", "%61%20%62"),
    ("a/b", "%61%2f%62"),
    ("a.b-c~d_e", "%61%2e%62%2d%63%7e%64%5f%65"),
    ("中文", "%e4%b8%ad%e6%96%87"),          # UTF-8 bytes, each percent-encoded
    ("", ""),
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_urlencode(run_fn, raw, expected):
    res = run_fn('urlencode "%s"' % raw)
    assert res.returncode == 0
    assert res.stdout.rstrip("\n") == expected
