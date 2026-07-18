"""Characterization tests for :func:`build_ipk.make_tar_gz`.

The function must produce a *reproducible* tarball: root:root ownership, a fixed
mtime, ``./``-prefixed names, a ``.`` root entry, sorted entries, and the
correct per-file mode depending on the ``is_control`` flag.
"""
import os
import stat
import tarfile

import build_ipk

FIXED_MTIME = 1700000000


def _build_control_tree(base):
    """Mirror a CONTROL/ layout: maintainer scripts + plain files."""
    layout = {
        "postinst": "#!/bin/sh\n",
        "postrm": "#!/bin/sh\n",
        "preinst": "#!/bin/sh\n",
        "prerm": "#!/bin/sh\n",
        "control": "Package: x\nVersion: 1.0.0-1\n",
        "conffiles": "/etc/config/mihomo\n",
    }
    base.mkdir(parents=True, exist_ok=True)
    for name, body in layout.items():
        path = base / name
        path.write_text(body)
    return base


def _build_root_tree(base):
    """Mirror a root/ layout: init script, helper, config, a view, json."""
    layout = {
        "etc/init.d/mihomo": "#!/bin/sh /etc/rc.common\n",
        "etc/config/mihomo": "config mihomo 'main'\n",
        "usr/share/mihomo/helper.sh": "#!/bin/sh\n",
        "usr/share/luci/menu.d/luci-app-mihomo.json": "{}\n",
        "www/luci-static/resources/view/mihomo/dashboard.js": "view{}\n",
    }
    for rel, body in layout.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return base


def _members(tarball):
    with tarfile.open(tarball, "r:gz") as tar:
        return tar.getmembers()


# --- structural invariants (is_control agnostic) ---------------------------


def test_has_dot_root_entry(builder, tmp_path):
    src = _build_root_tree(tmp_path / "root")
    out = tmp_path / "data.tar.gz"
    builder.make_tar_gz(str(src), str(out), is_control=False)

    names = [m.name for m in _members(out)]
    assert "." in names


def test_all_entries_are_root_owned_with_fixed_mtime(builder, tmp_path):
    src = _build_root_tree(tmp_path / "root")
    out = tmp_path / "data.tar.gz"
    builder.make_tar_gz(str(src), str(out), is_control=False)

    for m in _members(out):
        assert m.uid == 0 and m.gid == 0
        assert m.uname == "root" and m.gname == "root"
        assert m.mtime == FIXED_MTIME


def test_all_names_use_dot_prefix(builder, tmp_path):
    src = _build_root_tree(tmp_path / "root")
    out = tmp_path / "data.tar.gz"
    builder.make_tar_gz(str(src), str(out), is_control=False)

    for m in _members(out):
        assert m.name == "." or m.name.startswith("./"), m.name


def test_directories_are_0755(builder, tmp_path):
    src = _build_root_tree(tmp_path / "root")
    out = tmp_path / "data.tar.gz"
    builder.make_tar_gz(str(src), str(out), is_control=False)

    for m in _members(out):
        if m.isdir():
            assert stat.S_IMODE(m.mode) == 0o755, (m.name, oct(m.mode))


def test_entries_are_sorted(builder, tmp_path):
    """Reproducibility depends on lexicographic ordering of arcnames."""
    src = _build_root_tree(tmp_path / "root")
    out = tmp_path / "data.tar.gz"
    builder.make_tar_gz(str(src), str(out), is_control=False)

    names = [m.name for m in _members(out)]
    assert names == sorted(names)


# --- is_control=False mode policy -----------------------------------------


def test_data_mode_policy(builder, tmp_path):
    src = _build_root_tree(tmp_path / "root")
    out = tmp_path / "data.tar.gz"
    builder.make_tar_gz(str(src), str(out), is_control=False)

    modes = {m.name: stat.S_IMODE(m.mode) for m in _members(out) if m.isfile()}
    assert modes["./etc/init.d/mihomo"] == 0o755
    assert modes["./usr/share/mihomo/helper.sh"] == 0o755
    # everything else in the data tarball is 0o644
    for name in [
        "./etc/config/mihomo",
        "./usr/share/luci/menu.d/luci-app-mihomo.json",
        "./www/luci-static/resources/view/mihomo/dashboard.js",
    ]:
        assert modes[name] == 0o644, (name, oct(modes[name]))


# --- is_control=True mode policy ------------------------------------------


def test_control_mode_policy(builder, tmp_path):
    src = _build_control_tree(tmp_path / "CONTROL")
    out = tmp_path / "control.tar.gz"
    builder.make_tar_gz(str(src), str(out), is_control=True)

    modes = {m.name: stat.S_IMODE(m.mode) for m in _members(out) if m.isfile()}
    for script in ["postinst", "postrm", "preinst", "prerm"]:
        assert modes[f"./{script}"] == 0o755, (script, oct(modes[f"./{script}"]))
    # control + conffiles are NOT maintainer scripts → 0o644
    assert modes["./control"] == 0o644
    assert modes["./conffiles"] == 0o644


# --- round-trip content ----------------------------------------------------


def test_file_contents_round_trip(builder, tmp_path):
    src = _build_root_tree(tmp_path / "root")
    out = tmp_path / "data.tar.gz"
    builder.make_tar_gz(str(src), str(out), is_control=False)

    with tarfile.open(out, "r:gz") as tar:
        helper = tar.extractfile("./usr/share/mihomo/helper.sh").read().decode()
    assert helper == "#!/bin/sh\n"
