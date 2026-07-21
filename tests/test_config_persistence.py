"""Package lifecycle tests for preserving UCI data across reinstallations."""

import os
import subprocess


CHAIN_CONFIG = """config mihomo 'config'
\toption enabled '1'

config landing_node 'landing_a'
\toption name 'Landing A'
\toption type 'socks5'
\toption server '203.0.113.10'
\toption port '1080'

config data_link 'link_a'
\toption device_ip '192.168.66.151'
\toption subscription_node 'Airport A'
\toption landing_node 'landing_a'
"""


def _run_script(tmp_path, name, content, config_file, backup_file):
    script = tmp_path / name
    script.write_text(content)
    script.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "MIHOMO_UCI_CONFIG": str(config_file),
        "MIHOMO_UCI_BACKUP": str(backup_file),
        "SSPROXY_SKIP_RUNTIME_HOOKS": "1",
    })
    return subprocess.run(
        ["sh", str(script)], env=env, capture_output=True, text=True, timeout=10
    )


def test_preinst_and_postinst_preserve_complete_chain_config(src_files, tmp_path):
    config_file = tmp_path / "etc" / "config" / "mihomo"
    backup_file = tmp_path / "etc" / "mihomo" / ".uci_config_backup"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(CHAIN_CONFIG)

    result = _run_script(
        tmp_path, "preinst", src_files["CONTROL/preinst"], config_file, backup_file
    )
    assert result.returncode == 0, result.stderr
    assert backup_file.read_text() == CHAIN_CONFIG

    config_file.write_text("config mihomo 'config'\n\toption enabled '0'\n")
    result = _run_script(
        tmp_path, "postinst", src_files["CONTROL/postinst"], config_file, backup_file
    )
    assert result.returncode == 0, result.stderr
    assert config_file.read_text() == CHAIN_CONFIG


def test_prerm_refreshes_backup_with_latest_records(src_files, tmp_path):
    config_file = tmp_path / "mihomo"
    backup_file = tmp_path / "persist" / ".uci_config_backup"
    config_file.write_text(CHAIN_CONFIG.replace("link_a", "link_latest"))
    backup_file.parent.mkdir(parents=True)
    backup_file.write_text("stale\n")

    result = _run_script(
        tmp_path, "prerm", src_files["CONTROL/prerm"], config_file, backup_file
    )

    assert result.returncode == 0, result.stderr
    assert "link_latest" in backup_file.read_text()
    assert "stale" not in backup_file.read_text()
