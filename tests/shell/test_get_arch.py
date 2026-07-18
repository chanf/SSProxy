"""Tests for the ``get_arch`` subcommand.

Architecture detection is driven by ``uname -m`` (and, for unknown machines,
``opkg print-architecture``) — both stubbed here. The ``x86_64`` branch also
consults ``/proc/cpuinfo`` for v3 flags, which doesn't exist on macOS, so it
exercises the ``amd64-compatible`` fallback rather than the v3 detection.
"""
import pytest


@pytest.mark.parametrize("uname_m, expected", [
    ("aarch64", "arm64"),
    ("armv7l", "armv7"),
    ("mips", "mips-softfloat"),
    ("mipsel", "mipsle-softfloat"),
])
def test_arch_mapping(run, uname_m, expected):
    res = run("get_arch", env={"UNAME_M": uname_m})
    assert res.returncode == 0
    assert res.stdout.strip() == expected


def test_x86_64_without_cpuinfo_falls_back(run):
    """No /proc/cpuinfo on macOS → not v3 → amd64-compatible."""
    res = run("get_arch", env={"UNAME_M": "x86_64"})
    assert res.returncode == 0
    assert res.stdout.strip() == "amd64-compatible"


@pytest.mark.parametrize("opkg_out, expected", [
    ("arch aarch64 2\narch all 1", "arm64"),
    ("arch x86_64 2", "amd64"),
    ("arch armv7a 2", "armv7"),
    ("arch mipsel 2", "mipsle-softfloat"),
])
def test_unknown_arch_uses_opkg(run, opkg_out, expected):
    res = run("get_arch", env={"UNAME_M": "totally-unknown", "OPKG_ARCH_OUT": opkg_out})
    assert res.returncode == 0
    assert res.stdout.strip() == expected


def test_unknown_arch_no_opkg_match(run):
    res = run("get_arch", env={"UNAME_M": "totally-unknown", "OPKG_ARCH_OUT": "arch zaphod 2"})
    assert res.returncode == 0
    assert res.stdout.strip() == "unknown"
