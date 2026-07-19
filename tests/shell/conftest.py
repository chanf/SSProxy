"""Pytest harness for driving ``helper.sh`` as a black box from Python.

The shell subcommands run under a stubbed environment: a throwaway ``bin/`` is
prepended to ``PATH`` containing fake ``uci``/``logger``/``sed``/``uname``/
``opkg``/``curl`` (see ``tests/fixtures/stubs/``), and UCI state is fed in via
data files pointed at by environment variables. The exact on-disk ``helper.sh``
string the router runs is pulled straight out of ``build_ipk.src_files``.
"""
import os
import shutil
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import build_ipk  # noqa: E402

_STUBS_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "stubs")
_STUB_NAMES = ["uci", "logger", "sed", "uname", "opkg", "curl", "ip"]


@pytest.fixture(scope="session")
def helper_path(tmp_path_factory):
    """Write helper.sh (exactly as the router runs it) to a temp file, +x."""
    helper_text = build_ipk.src_files["root/usr/share/mihomo/helper.sh"]
    path = tmp_path_factory.mktemp("helper") / "helper.sh"
    path.write_text(helper_text)
    os.chmod(path, 0o755)
    return str(path)


@pytest.fixture(scope="session")
def helper_lib_path(tmp_path_factory):
    """helper.sh with the top-level ``case "$1" in`` dispatcher stripped.

    The dispatcher is split off at its *last* occurrence (the first one lives
    inside the ``rule_target_exists`` function). The remainder is a plain
    function library that can be sourced with ``.`` so internal helpers that
    are NOT exposed as subcommands (``urlencode``, ``resolve_proxy_name``,
    ``emit_access_rules_yaml``) can be invoked directly.
    """
    helper_text = build_ipk.src_files["root/usr/share/mihomo/helper.sh"]
    lib_text = helper_text.rsplit('case "$1" in', 1)[0]
    path = tmp_path_factory.mktemp("helperlib") / "helper_lib.sh"
    path.write_text(lib_text)
    return str(path)


@pytest.fixture
def bin_dir(tmp_path):
    """A fresh dir of stub binaries, prepended to PATH for the test."""
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    for name in _STUB_NAMES:
        src = os.path.join(_STUBS_SRC, name)
        if os.path.exists(src):
            shutil.copy(src, bin_path / name)
            os.chmod(bin_path / name, 0o755)
    return str(bin_path)


@pytest.fixture
def run(helper_path, bin_dir, tmp_path):
    """Return a callable ``run(subcmd, *args, env=None)`` → CompletedProcess."""
    base_env = dict(os.environ)
    base_env["PATH"] = bin_dir + os.pathsep + base_env.get("PATH", "")
    base_env["TMPDIR"] = str(tmp_path)

    def _run(subcmd, *args, env=None, timeout=30):
        full_env = dict(base_env)
        if env:
            full_env.update(env)
        return subprocess.run(
            ["sh", helper_path, subcmd, *args],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    return _run


@pytest.fixture
def run_fn(helper_lib_path, bin_dir, tmp_path):
    """Return a callable ``run_fn(body, env=None)`` for internal helper funcs.

    ``body`` is shell code executed after sourcing the dispatcher-stripped
    library, e.g. ``'urlencode "a b"'`` or ``'emit_access_rules_yaml /tmp/cfg'``.
    """
    base_env = dict(os.environ)
    base_env["PATH"] = bin_dir + os.pathsep + base_env.get("PATH", "")
    base_env["TMPDIR"] = str(tmp_path)

    def _run(body, env=None, timeout=30):
        full_env = dict(base_env)
        if env:
            full_env.update(env)
        cmd = ". " + helper_lib_path + " 2>/dev/null; " + body
        return subprocess.run(
            ["sh", "-c", cmd],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    return _run


@pytest.fixture
def uci_env(tmp_path):
    """Write UCI get/show data files and return the matching env dict.

    ``gets`` maps dotted keys to values; ``show`` is a list of raw `uci show`
    lines (e.g. ``"mihomo.r1=mihomo_rule"``).
    """
    get_file = tmp_path / "uci_get"
    show_file = tmp_path / "uci_show"
    ops_file = tmp_path / "uci_ops"
    add_counter = tmp_path / "uci_add_ctr"
    log_file = tmp_path / "logger.log"

    def _build(gets=None, show=None):
        get_file.write_text(
            "".join(f"{k}={v}\n" for k, v in (gets or {}).items())
        )
        show_file.write_text(
            "".join(line + "\n" for line in (show or []))
        )
        return {
            "UCI_GET_FILE": str(get_file),
            "UCI_SHOW_FILE": str(show_file),
            "UCI_OPS_FILE": str(ops_file),
            "UCI_ADD_COUNTER": str(add_counter),
            "LOGGER_FILE": str(log_file),
        }

    def _ops():
        return ops_file.read_text() if ops_file.exists() else ""

    _build.ops = _ops  # type: ignore[attr-defined]
    _build.log = lambda: log_file.read_text() if log_file.exists() else ""  # type: ignore[attr-defined]
    return _build
