"""Table-driven tests for the pure :func:`build_ipk._bump_version_string` helper."""
import pytest

import build_ipk


@pytest.mark.parametrize(
    "current, expected",
    [
        ("1.0.0-1", "1.0.0-2"),
        ("1.0.0-81", "1.0.0-82"),
        ("1.0.0-132", "1.0.0-133"),
        ("1.0.0-999", "1.0.0-1000"),
        # dotted form: last numeric segment bumps
        ("1.0.0", "1.0.1"),
        ("2.5.0", "2.5.1"),
        ("10", "11"),
        # non-numeric revision tail → append ".1"
        ("1.0.0-abc", "1.0.0-abc.1"),
        ("1.0.0-beta", "1.0.0-beta.1"),
        # non-numeric dotted tail → append "-1"
        ("1.0.x", "1.0.x-1"),
    ],
)
def test_bump_version_string(builder, current, expected):
    assert builder._bump_version_string(current) == expected


def test_bump_is_pure(builder):
    """No side effects — same input always yields the same output."""
    assert builder._bump_version_string("1.0.0-5") == "1.0.0-6"
    assert builder._bump_version_string("1.0.0-5") == "1.0.0-6"
