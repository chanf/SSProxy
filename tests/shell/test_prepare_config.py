"""Tests for the ``prepare_config`` subcommand — the controlled-config merge.

Feeds a subscription-style source config and asserts that prepare_config strips
the originals and substitutes the controlled ports / DNS / TUN blocks, then
injects UCI access rules. Output is written to the hardcoded /tmp/mihomo_run.yaml.
"""
import os

SRC = (
    "mixed-port: 7890\n"
    "port: 7891\n"
    "socks-port: 7892\n"
    "allow-lan: false\n"
    "external-controller: 127.0.0.1:9999\n"
    "proxies:\n"
    "  - name: testnode\n"
    "    type: ss\n"
    "    server: 1.2.3.4\n"
    "dns:\n"
    "  enable: false\n"
    "  nameserver:\n"
    "    - 8.8.8.8\n"
    "tun:\n"
    "  enable: false\n"
    "rules:\n"
    "    - DOMAIN,example.com,DIRECT\n"
)

RUN_CONFIG = "/tmp/mihomo_run.yaml"


def _run(run, uci_env, src_path, **uci_vals):
    gets = {"mihomo.config.config_path": str(src_path)}
    gets.update({f"mihomo.config.{k}": v for k, v in uci_vals.items()})
    return run("prepare_config", env=uci_env(gets=gets))


def test_missing_source_errors(run, uci_env, tmp_path):
    res = _run(run, uci_env, tmp_path / "nope.yaml")
    assert res.returncode == 1
    assert "not found" in res.stderr


def test_substitutes_controlled_ports_and_dns(run, uci_env, tmp_path):
    src = tmp_path / "src.yaml"
    src.write_text(SRC)
    res = _run(run, uci_env, src,
               dns_port="1053", tproxy_port="7893", mix_port="7890", tun_enabled="0")
    assert res.returncode == 0, res.stderr
    out = open(RUN_CONFIG, encoding="utf-8").read()

    # Controlled block prepended at the very top.
    assert out.startswith("mixed-port: 7890\n")
    assert "tproxy-port: 7893" in out
    assert "allow-lan: true" in out
    assert "external-controller: 0.0.0.0:9090" in out
    # Controlled DNS block appended with the configured port.
    assert "listen: 0.0.0.0:1053" in out
    assert "enhanced-mode: fake-ip" in out
    # IPv6 enabled so whitelisted v6 clients get AAAA for tproxy.
    assert "ipv6: true" in out
    assert "ipv6: false" not in out
    # Original top-level ports / controller stripped.
    assert "127.0.0.1:9999" not in out
    assert "socks-port:" not in out
    # Original dns/tun blocks replaced (no duplicate enable:false nameserver 8.8.8.8).
    assert "8.8.8.8" not in out
    # TUN disabled form.
    assert "tun:" in out and "enable: false" in out
    # Existing rules section preserved + re-indented to 2 spaces.
    assert "rules:" in out
    assert "  - DOMAIN,example.com,DIRECT" in out


def test_tun_enabled_block(run, uci_env, tmp_path):
    src = tmp_path / "src.yaml"
    src.write_text(SRC)
    res = _run(run, uci_env, src, tun_enabled="1",
               dns_port="1053", tproxy_port="7893", mix_port="7890")
    assert res.returncode == 0, res.stderr
    out = open(RUN_CONFIG, encoding="utf-8").read()
    assert "tun:\n  enable: true" in out
    assert "stack: system" in out
    assert "auto-route: true" in out


def test_injects_uci_access_rules(run, uci_env, tmp_path):
    src = tmp_path / "src.yaml"
    # The rule's proxy target must exist as a `name:` in the config for the
    # rule to be kept; use a flow-map entry so the name is followed by a comma.
    src.write_text(SRC + 'proxy-groups:\n  - {name: "MYGROUP", type: select}\n')
    env = uci_env(
        gets={
            "mihomo.config.config_path": str(src),
            "mihomo.config.dns_port": "1053",
            "mihomo.config.tproxy_port": "7893",
            "mihomo.config.mix_port": "7890",
            "mihomo.config.tun_enabled": "0",
            "mihomo.r1.enabled": "1",
            "mihomo.r1.domain": "ads.example.com",
            "mihomo.r1.rule_type": "suffix",
            "mihomo.r1.action": "proxy",
            "mihomo.r1.group": "MYGROUP",
        },
        show=["mihomo.r1=mihomo_rule"],
    )
    res = run("prepare_config", env=env)
    assert res.returncode == 0, res.stderr
    out = open(RUN_CONFIG, encoding="utf-8").read()
    # The injected rule lands in the rules: block (highest priority, first match).
    assert "DOMAIN-SUFFIX,ads.example.com,MYGROUP" in out
