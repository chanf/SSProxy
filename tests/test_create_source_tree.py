"""Characterization tests for :func:`build_ipk.create_source_tree`.

These lock the *current* on-disk behaviour (which files are written, how the
``Version:`` field and ``__PKG_VERSION__`` placeholder are substituted, and
which paths are marked executable) before any refactor of the builder.
"""
import os
import re
import stat

import build_ipk


def test_writes_every_src_files_entry(builder, tmp_path):
    src_dir = tmp_path / "src"
    builder.create_source_tree(str(src_dir))

    for rel_path in builder.src_files:
        assert (src_dir / rel_path).is_file(), f"missing {rel_path}"


def test_control_version_is_substituted(builder, src_files, tmp_path):
    # The placeholder baked into the source string must differ from PKG_VERSION…
    assert re.search(r"^Version: 1\.0\.0-1$", src_files["CONTROL/control"], re.M)
    src_dir = tmp_path / "src"
    builder.create_source_tree(str(src_dir))

    control = (src_dir / "CONTROL" / "control").read_text(encoding="utf-8")
    m = re.search(r"^Version: (.+)$", control, re.M)
    assert m is not None
    assert m.group(1) == builder.PKG_VERSION  # not the 1.0.0-1 placeholder


def test_pkg_version_placeholder_is_replaced_in_views(builder, src_files, tmp_path):
    # dashboard.js carries the placeholder → the test is non-vacuous.
    assert "__PKG_VERSION__" in src_files[
        "root/www/luci-static/resources/view/mihomo/dashboard.js"
    ]
    src_dir = tmp_path / "src"
    builder.create_source_tree(str(src_dir))

    dashboard = (src_dir / "root/www/luci-static/resources/view/mihomo/dashboard.js").read_text(
        encoding="utf-8"
    )
    assert "__PKG_VERSION__" not in dashboard
    assert builder.PKG_VERSION in dashboard


def test_executable_files_get_0755(builder, tmp_path):
    src_dir = tmp_path / "src"
    builder.create_source_tree(str(src_dir))

    for rel_path in [
        "CONTROL/postinst",
        "CONTROL/postrm",
        "CONTROL/conffiles",          # matches "CONTROL/" and != "CONTROL/control"
        "root/etc/init.d/mihomo",
        "root/usr/share/mihomo/helper.sh",
    ]:
        mode = stat.S_IMODE((src_dir / rel_path).stat().st_mode)
        assert mode == 0o755, f"{rel_path}: expected 0o755, got {oct(mode)}"


def test_non_executable_files_have_no_exec_bit(builder, tmp_path):
    src_dir = tmp_path / "src"
    builder.create_source_tree(str(src_dir))

    for rel_path in [
        "CONTROL/control",            # explicitly excluded from the chmod predicate
        "root/etc/config/mihomo",
        "root/usr/share/luci/menu.d/luci-app-ssproxy.json",
        "root/www/luci-static/resources/view/mihomo/dashboard.js",
    ]:
        mode = stat.S_IMODE((src_dir / rel_path).stat().st_mode)
        assert mode & 0o111 == 0, f"{rel_path}: unexpectedly executable ({oct(mode)})"


def test_wipes_existing_tree_first(builder, tmp_path):
    """A pre-existing file in src_dir that is NOT in src_files must be removed."""
    src_dir = tmp_path / "src"
    stale = src_dir / "stale-file.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("leftover")

    builder.create_source_tree(str(src_dir))
    assert not stale.exists()
    # …and the real tree is written in its place.
    assert (src_dir / "CONTROL" / "control").is_file()
