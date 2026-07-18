"""Characterization tests for :func:`build_ipk.write_tar_gz_outer_archive`.

This assembles the final ``.ipk`` (an outer gzipped tar holding ``debian-binary``,
``control.tar.gz`` and ``data.tar.gz`` as opaque blobs).
"""
import io
import stat
import tarfile

import build_ipk

FIXED_MTIME = 1700000000


def _file_list():
    return [
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", b"\x1f\x8bcontrol-bytes"),
        ("data.tar.gz", b"\x1f\x8bdata-bytes"),
    ]


def _members(archive):
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getmembers(), tar


def test_contains_exactly_three_blob_entries(builder, tmp_path):
    out = tmp_path / "pkg.ipk"
    builder.write_tar_gz_outer_archive(str(out), _file_list())

    members, _ = _members(out)
    names = sorted(m.name for m in members)
    assert names == ["./control.tar.gz", "./data.tar.gz", "./debian-binary"]


def test_no_dot_root_entry(builder, tmp_path):
    """Unlike make_tar_gz, the outer archive does not synthesise a '.' entry."""
    out = tmp_path / "pkg.ipk"
    builder.write_tar_gz_outer_archive(str(out), _file_list())

    members, _ = _members(out)
    assert "." not in [m.name for m in members]


def test_blob_sizes_and_contents(builder, tmp_path):
    out = tmp_path / "pkg.ipk"
    flist = _file_list()
    builder.write_tar_gz_outer_archive(str(out), flist)

    with tarfile.open(out, "r:gz") as tar:
        by_name = {m.name: m for m in tar.getmembers()}
        for name, data in flist:
            m = by_name[f"./{name}"]
            assert m.size == len(data)
            assert tar.extractfile(m).read() == data


def test_all_entries_root_owned_fixed_mtime_0644(builder, tmp_path):
    out = tmp_path / "pkg.ipk"
    builder.write_tar_gz_outer_archive(str(out), _file_list())

    members, _ = _members(out)
    for m in members:
        assert m.uid == 0 and m.gid == 0
        assert m.uname == "root" and m.gname == "root"
        assert m.mtime == FIXED_MTIME
        assert stat.S_IMODE(m.mode) == 0o644
        assert m.isfile()
