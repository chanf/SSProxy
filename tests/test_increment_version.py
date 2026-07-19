"""Characterization tests for :func:`build_ipk.increment_version`.

``increment_version`` rewrites ``__file__`` in place, so every test points the
module's ``__file__`` at a throwaway fixture (via monkeypatch) and snapshots the
``PKG_VERSION`` / ``IPK_FILENAME`` globals so the real module is left untouched.
"""
import pytest

import build_ipk


@pytest.fixture(autouse=True)
def restore_module_globals(monkeypatch):
    orig_version = build_ipk.PKG_VERSION
    orig_filename = build_ipk.IPK_FILENAME
    orig_file = build_ipk.__file__
    yield
    build_ipk.PKG_VERSION = orig_version
    build_ipk.IPK_FILENAME = orig_filename
    build_ipk.__file__ = orig_file


def _point_at(monkeypatch, tmp_path, body):
    """Create a temp script containing ``body`` and make increment_version target it."""
    script = tmp_path / "fake_build_ipk.py"
    script.write_text(body, encoding="utf-8")
    monkeypatch.setattr(build_ipk, "__file__", str(script))
    return script


def _read(script):
    return script.read_text(encoding="utf-8")


def test_bumps_revision(monkeypatch, tmp_path):
    script = _point_at(monkeypatch, tmp_path, 'PKG_VERSION = "1.0.0-81"\n')
    build_ipk.increment_version()

    text = _read(script)
    assert 'PKG_VERSION = "1.0.0-82"' in text
    assert "1.0.0-81" not in text
    assert build_ipk.PKG_VERSION == "1.0.0-82"
    assert build_ipk.IPK_FILENAME == "luci-app-ssproxy_1.0.0-82_all.ipk"


def test_bumps_dotted_form(monkeypatch, tmp_path):
    script = _point_at(monkeypatch, tmp_path, 'PKG_VERSION = "1.0.0"\n')
    build_ipk.increment_version()
    assert 'PKG_VERSION = "1.0.1"' in _read(script)


def test_bumps_large_revision(monkeypatch, tmp_path):
    script = _point_at(monkeypatch, tmp_path, 'PKG_VERSION = "1.0.0-132"\n')
    build_ipk.increment_version()
    assert 'PKG_VERSION = "1.0.0-133"' in _read(script)


def test_non_numeric_revision_appends_dot1(monkeypatch, tmp_path):
    script = _point_at(monkeypatch, tmp_path, 'PKG_VERSION = "1.0.0-abc"\n')
    build_ipk.increment_version()
    assert 'PKG_VERSION = "1.0.0-abc.1"' in _read(script)


def test_non_numeric_dotted_appends_dash1(monkeypatch, tmp_path):
    script = _point_at(monkeypatch, tmp_path, 'PKG_VERSION = "1.0.x"\n')
    build_ipk.increment_version()
    assert 'PKG_VERSION = "1.0.x-1"' in _read(script)


def test_single_quoted_source_is_normalized_to_double(monkeypatch, tmp_path):
    """The rewriter always emits double quotes (a quirk worth pinning)."""
    script = _point_at(monkeypatch, tmp_path, "PKG_VERSION = '1.0.0-5'\n")
    build_ipk.increment_version()
    assert 'PKG_VERSION = "1.0.0-6"' in _read(script)


def test_no_match_leaves_file_untouched(monkeypatch, tmp_path, capsys):
    before_ver = build_ipk.PKG_VERSION
    script = _point_at(monkeypatch, tmp_path, "# no version here\n")
    before = _read(script)
    build_ipk.increment_version()

    assert _read(script) == before
    captured = capsys.readouterr()
    assert "not found" in captured.out
    # globals untouched (compare to the pre-call snapshot, not a hard-coded value,
    # since real builds bump PKG_VERSION in the source file).
    assert build_ipk.PKG_VERSION == before_ver


def test_only_first_match_is_replaced(monkeypatch, tmp_path):
    """count=1 — a later stray occurrence in the file must survive untouched."""
    body = (
        'PKG_VERSION = "1.0.0-81"\n'
        'echo "PKG_VERSION = \\"1.0.0-999\\""\n'
    )
    script = _point_at(monkeypatch, tmp_path, body)
    build_ipk.increment_version()
    text = _read(script)
    assert 'PKG_VERSION = "1.0.0-82"' in text
    assert "1.0.0-999" in text  # the stray occurrence is preserved
