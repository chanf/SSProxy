"""End-to-end test of :func:`build_ipk.main`.

Runs the builder against a throwaway copy of itself (loaded from a temp dir via
``importlib``) so the real repo is never mutated, then validates the produced
``.ipk`` inside-out: outer layout, the ``debian-binary`` blob, the nested
``control.tar.gz`` (with the freshly-bumped version), and the ``data.tar.gz``
payload.
"""
import importlib.util
import io
import os
import shutil
import tarfile

import build_ipk


def _load_fresh_builder(tmp_path):
    """Copy build_ipk.py into tmp_path and import it as an isolated module."""
    src = os.path.abspath(build_ipk.__file__)
    dst = tmp_path / "build_ipk.py"
    shutil.copy(src, dst)

    spec = importlib.util.spec_from_file_location("build_ipk_under_test", dst)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _open_tar(data):
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")


def test_main_produces_valid_ipk(tmp_path):
    mod = _load_fresh_builder(tmp_path)
    original_version = mod.PKG_VERSION  # before main() bumps it

    mod.main()

    # The version line in the temp copy was bumped by one revision.
    assert mod.PKG_VERSION != original_version
    assert "-" in original_version
    base, rev = original_version.rsplit("-", 1)
    expected_version = f"{base}-{int(rev) + 1}"
    assert mod.PKG_VERSION == expected_version

    # Exactly one .ipk produced, named after the (bumped) version.
    ipks = list((tmp_path / "dist").glob("*.ipk"))
    assert [p.name for p in ipks] == [mod.IPK_FILENAME]

    with tarfile.open(ipks[0], "r:gz") as outer:
        outer_names = sorted(m.name for m in outer.getmembers())
        assert outer_names == ["./control.tar.gz", "./data.tar.gz", "./debian-binary"]

        debian = outer.extractfile("./debian-binary").read()
        assert debian == b"2.0\n"

        control_blob = outer.extractfile("./control.tar.gz").read()
        data_blob = outer.extractfile("./data.tar.gz").read()

    # control.tar.gz → ./control carries the freshly-bumped Version.
    with _open_tar(control_blob) as ctar:
        control = ctar.extractfile("./control").read().decode("utf-8")
    assert f"Version: {mod.PKG_VERSION}" in control

    # data.tar.gz → representative payload files are present.
    with _open_tar(data_blob) as dtar:
        data_names = {m.name for m in dtar.getmembers()}
    for expected in [
        "./etc/init.d/mihomo",
        "./usr/share/mihomo/helper.sh",
        "./etc/config/mihomo",
        "./www/luci-static/resources/view/mihomo/dashboard.js",
    ]:
        assert expected in data_names, f"missing {expected} in data.tar.gz"


def test_main_uses_isolated_workspace(tmp_path):
    """main() must operate entirely under the temp copy's directory."""
    mod = _load_fresh_builder(tmp_path)
    mod.main()

    workspace = os.path.dirname(os.path.abspath(mod.__file__))
    assert workspace == str(tmp_path)
    assert (tmp_path / "src").is_dir()
    assert (tmp_path / "build").is_dir()
    assert (tmp_path / "dist").is_dir()
