"""Reproducibility regression tests.

Before the gzip-header mtime was pinned, two consecutive builds produced
byte-different archives (the gzip wrapper embedded the wall clock). These
tests pin the fix: the same inputs must yield byte-identical output every time.
"""
import build_ipk


def _tiny_tree(base):
    base.mkdir(parents=True, exist_ok=True)
    (base / "etc").mkdir(exist_ok=True)
    (base / "etc" / "init.d").mkdir(parents=True, exist_ok=True)
    (base / "etc" / "init.d" / "mihomo").write_text("#!/bin/sh\n")
    (base / "etc" / "config").mkdir(parents=True, exist_ok=True)
    (base / "etc" / "config" / "mihomo").write_text("config mihomo 'main'\n")
    (base / "usr").mkdir(exist_ok=True)
    (base / "usr" / "share").mkdir(parents=True, exist_ok=True)
    (base / "usr" / "share" / "mihomo").mkdir(parents=True, exist_ok=True)
    (base / "usr" / "share" / "mihomo" / "helper.sh").write_text("#!/bin/sh\n")
    return base


def test_make_tar_gz_is_byte_reproducible(builder, tmp_path):
    src = _tiny_tree(tmp_path / "root")
    out_a = tmp_path / "a.tar.gz"
    out_b = tmp_path / "b.tar.gz"

    builder.make_tar_gz(str(src), str(out_a), is_control=False)
    builder.make_tar_gz(str(src), str(out_b), is_control=False)

    assert out_a.read_bytes() == out_b.read_bytes()


def test_make_tar_gz_reproducible_across_independent_trees(builder, tmp_path):
    """Different source directories with identical contents must still match."""
    src_a = _tiny_tree(tmp_path / "root_a")
    src_b = _tiny_tree(tmp_path / "root_b")

    out_a = tmp_path / "a.tar.gz"
    out_b = tmp_path / "b.tar.gz"
    builder.make_tar_gz(str(src_a), str(out_a), is_control=False)
    builder.make_tar_gz(str(src_b), str(out_b), is_control=False)

    assert out_a.read_bytes() == out_b.read_bytes()


def test_outer_archive_is_byte_reproducible(builder, tmp_path):
    flist = [
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", b"\x1f\x8bcontrol"),
        ("data.tar.gz", b"\x1f\x8bdata"),
    ]
    out_a = tmp_path / "a.ipk"
    out_b = tmp_path / "b.ipk"

    builder.write_tar_gz_outer_archive(str(out_a), flist)
    builder.write_tar_gz_outer_archive(str(out_b), flist)

    assert out_a.read_bytes() == out_b.read_bytes()
