"""Regression tests for backend validation at shell/UCI boundaries."""


def test_validate_acl_ip_accepts_ipv4_and_ipv6(run, uci_env):
    assert run("validate_acl_ip", "192.168.1.0/24", env=uci_env()).returncode == 0
    assert run("validate_acl_ip", "fd00::5", env=uci_env()).returncode == 0


def test_validate_acl_ip_rejects_malformed_values(run, uci_env):
    assert run("validate_acl_ip", "192.168.1.999", env=uci_env()).returncode != 0
    assert run("validate_acl_ip", "192.168.1.1/33", env=uci_env()).returncode != 0
    assert run("validate_acl_ip", "not-an-ip", env=uci_env()).returncode != 0


def test_adblock_source_rejects_unsafe_parameters(run, uci_env):
    env = uci_env()
    assert run("add_adblock_source", "bad name", "https://example.com/rules", "domain", "yaml", env=env).returncode == 1
    assert run("add_adblock_source", "ok", "ftp://example.com/rules", "domain", "yaml", env=env).returncode == 1
    assert run("add_adblock_source", "ok", "https://example.com/rules", "unknown", "yaml", env=env).returncode == 1
    assert run("add_adblock_source", "ok", "https://example.com/rules", "domain", "json", env=env).returncode == 1
