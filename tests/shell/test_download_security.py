"""Security regressions for Mihomo core and database downloads."""

import hashlib


def test_custom_core_download_requires_https_and_sha256(run, uci_env, tmp_path):
    core = tmp_path / "mihomo"
    env = uci_env(gets={"mihomo.config.core_path": str(core)})
    env["UNAME_M"] = "x86_64"

    missing = run("download_core", "https://example.com/mihomo.gz", env=env)
    assert missing.returncode == 1
    assert "SHA256" in missing.stderr

    insecure = run("download_core", "http://example.com/mihomo.gz", "0" * 64, env=env)
    assert insecure.returncode == 1
    assert "HTTPS" in insecure.stderr


def test_custom_core_download_verifies_before_installing(run, uci_env, tmp_path):
    core = tmp_path / "mihomo"
    core.write_text("old-core")
    payload = "new-core\n"
    digest = hashlib.sha256(payload.encode()).hexdigest()
    env = uci_env(gets={"mihomo.config.core_path": str(core)})
    env["UNAME_M"] = "x86_64"
    env["CURL_RESPONSE"] = payload.rstrip("\n")

    result = run("download_core", "https://example.com/mihomo", digest, env=env)

    assert result.returncode == 0, result.stderr
    assert core.read_text() == payload
    assert core.stat().st_mode & 0o111

    env["CURL_RESPONSE"] = "tampered"
    mismatch = run("download_core", "https://example.com/mihomo", digest, env=env)
    assert mismatch.returncode == 1
    assert "verification failed" in mismatch.stderr
    assert core.read_text() == payload


def test_default_core_assets_have_pinned_digests(src_files):
    helper = src_files["root/usr/share/mihomo/helper.sh"]

    for digest in (
        "70d01cfb8cb7bf7a92fd1af16cb4b9553d90bb4eecde3b5c4849103e27c80ddb",
        "d5967e079d9f793515a5a8193aabda455f7e012427eccd567dbc4f2f15498204",
        "2474450cd1c41dfa53036a54a4e85579f493d3af524d86c3d4b8e2b240b56cd2",
        "661a64466f79ab9c39cd3a1c1ece5371a4d93f87cb2d6610ff8c0dacaaa9f180",
        "cfe16b8422198831b6e8d002a93786b0c39fe58a1e240ee4c38d1692d71865b0",
        "cb181a3464310055a0c39c3fe8453c7ad9ad657cb24fbf1cadc2218899d0ec13",
    ):
        assert digest in helper

    assert "curl -fsSL -k" not in helper
    assert "curl -fsSL --proto '=https' --tlsv1.2" in helper
    assert "curl -H \"@$auth_file\"" in helper
