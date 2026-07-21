"""Tests for landing-node assets and fixed two-hop device data links."""

import json
from pathlib import Path


BASE_CONFIG = (
    "proxies:\n"
    '  - name: "Airport A"\n'
    "    type: ss\n"
    "    server: 1.2.3.4\n"
    "proxy-groups:\n"
    '  - name: "PROXY"\n'
    "    type: select\n"
    "    proxies:\n"
    '      - "Airport A"\n'
    "rules:\n"
    "  - MATCH,PROXY\n"
)

FOUR_SPACE_FLOW_CONFIG = (
    "proxies:\n"
    '    - { name: "Airport A", type: ss, server: 1.2.3.4 }\n'
    "proxy-groups:\n"
    '    - { name: "PROXY", type: select, proxies: ["Airport A"] }\n'
    "rules:\n"
    "    - MATCH,PROXY\n"
)


def _landing_gets(sid="ln1", node_type="socks5"):
    values = {
        f"mihomo.{sid}.enabled": "1",
        f"mihomo.{sid}.name": "Landing A",
        f"mihomo.{sid}.type": node_type,
        f"mihomo.{sid}.server": "203.0.113.10",
        f"mihomo.{sid}.port": "1080",
        f"mihomo.{sid}.username": "user",
        f"mihomo.{sid}.password": "pass",
        f"mihomo.{sid}.cipher": "aes-256-gcm",
        f"mihomo.{sid}.uuid": "11111111-1111-1111-1111-111111111111",
        f"mihomo.{sid}.alter_id": "0",
        f"mihomo.{sid}.tls": "1",
        f"mihomo.{sid}.sni": "landing.example.com",
        f"mihomo.{sid}.flow": "xtls-rprx-vision",
        f"mihomo.{sid}.skip_cert_verify": "1",
    }
    return values


def _link_gets(sid="dl1", device="192.168.66.158", landing="ln1"):
    return {
        f"mihomo.{sid}.enabled": "1",
        f"mihomo.{sid}.device_ip": device,
        f"mihomo.{sid}.subscription_node": "Airport A",
        f"mihomo.{sid}.landing_node": landing,
    }


def _metrics_env(env, tmp_path):
    paths = {
        "MIHOMO_DATA_LINK_HEALTH_FILE": tmp_path / "health",
        "MIHOMO_DATA_LINK_METRICS_FILE": tmp_path / "metrics",
        "MIHOMO_DATA_LINK_CONN_STATE_FILE": tmp_path / "conn_state",
        "MIHOMO_DATA_LINK_LAST_POLL_FILE": tmp_path / "last_poll",
        "MIHOMO_DATA_LINK_FLUSH_FILE": tmp_path / "flush",
        "MIHOMO_DATA_LINK_PERSIST_FILE": tmp_path / "persist",
    }
    env.update({key: str(value) for key, value in paths.items()})
    return paths


def test_emits_type_specific_landing_proxies(run_fn, uci_env):
    show = []
    gets = {}
    for index, node_type in enumerate(("socks5", "http", "ss", "trojan", "vmess", "vless"), 1):
        sid = f"ln{index}"
        show.append(f"mihomo.{sid}=landing_node")
        gets.update(_landing_gets(sid, node_type))

    out = run_fn("emit_landing_proxies_yaml", env=uci_env(gets=gets, show=show)).stdout

    for index, node_type in enumerate(("socks5", "http", "ss", "trojan", "vmess", "vless"), 1):
        assert f'name: "ssproxy-landing-ln{index}"' in out
        assert f"type: {node_type}" in out
    assert 'username: "user"' in out
    assert 'cipher: "aes-256-gcm"' in out
    assert 'uuid: "11111111-1111-1111-1111-111111111111"' in out
    assert 'flow: "xtls-rprx-vision"' in out


def test_emits_dialer_proxy_and_ipv4_rule(run_fn, uci_env, tmp_path):
    cfg = tmp_path / "run.yaml"
    cfg.write_text(BASE_CONFIG + '  - name: "ssproxy-landing-ln1"\n    type: socks5\n')
    gets = {}
    gets.update(_landing_gets())
    gets.update(_link_gets())
    env = uci_env(gets=gets, show=["mihomo.ln1=landing_node", "mihomo.dl1=data_link"])

    proxies = run_fn(f'emit_data_link_proxies_yaml "{cfg}"', env=env).stdout
    rules = run_fn(f'emit_data_link_rules_yaml "{cfg}"', env=env).stdout

    assert 'name: "ssproxy-chain-dl1"' in proxies
    assert "type: socks5" in proxies
    assert 'dialer-proxy: "Airport A"' in proxies
    assert "type: relay" not in proxies
    assert "SRC-IP-CIDR,192.168.66.158/32,ssproxy-chain-dl1" in rules


def test_unified_front_node_overrides_each_link_without_rewriting_it(run_fn, uci_env, tmp_path):
    cfg = tmp_path / "run.yaml"
    cfg.write_text(
        BASE_CONFIG.replace(
            "proxy-groups:\n",
            '  - name: "Airport B"\n    type: ss\n    server: 5.6.7.8\nproxy-groups:\n',
        )
    )
    gets = {"mihomo.config.chain_front_node": "Airport B"}
    gets.update(_landing_gets())
    gets.update(_link_gets())
    env = uci_env(gets=gets, show=["mihomo.ln1=landing_node", "mihomo.dl1=data_link"])

    proxies = run_fn(f'emit_data_link_proxies_yaml "{cfg}"', env=env).stdout

    assert 'dialer-proxy: "Airport B"' in proxies
    assert 'dialer-proxy: "Airport A"' not in proxies
    assert uci_env.ops() == ""


def test_invalid_unified_front_node_does_not_fall_back_to_row(run_fn, uci_env, tmp_path):
    cfg = tmp_path / "run.yaml"
    cfg.write_text(BASE_CONFIG)
    gets = {"mihomo.config.chain_front_node": "Removed Node"}
    gets.update(_landing_gets())
    gets.update(_link_gets())
    env = uci_env(gets=gets, show=["mihomo.ln1=landing_node", "mihomo.dl1=data_link"])

    proxies = run_fn(f'emit_data_link_proxies_yaml "{cfg}"', env=env).stdout

    assert "ssproxy-chain-dl1" not in proxies
    assert 'dialer-proxy: "Airport A"' not in proxies


def test_ipv6_rule_and_invalid_reference_handling(run_fn, uci_env, tmp_path):
    cfg = tmp_path / "run.yaml"
    cfg.write_text(BASE_CONFIG + '  - name: "ssproxy-landing-ln1"\n    type: socks5\n')
    gets = {}
    gets.update(_landing_gets())
    gets.update(_link_gets(device="fd00::158"))
    gets.update(_link_gets("dl2", device="192.168.66.2", landing="missing"))
    env = uci_env(
        gets=gets,
        show=["mihomo.ln1=landing_node", "mihomo.dl1=data_link", "mihomo.dl2=data_link"],
    )

    proxies = run_fn(f'emit_data_link_proxies_yaml "{cfg}"', env=env).stdout
    rules = run_fn(f'emit_data_link_rules_yaml "{cfg}"', env=env).stdout

    assert "ssproxy-chain-dl1" in proxies
    assert "ssproxy-chain-dl2" not in proxies
    assert "SRC-IP-CIDR6,fd00::158/128,ssproxy-chain-dl1" in rules


def test_prepare_config_injects_complete_chain(run, uci_env, tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(FOUR_SPACE_FLOW_CONFIG)
    gets = {
        "mihomo.config.config_path": str(source),
        "mihomo.config.config_mode": "subscription",
        "mihomo.config.dns_port": "1053",
        "mihomo.config.tproxy_port": "7893",
        "mihomo.config.mix_port": "7890",
        "mihomo.config.tun_enabled": "0",
        "mihomo.config.geo_auto_update": "0",
    }
    gets.update(_landing_gets())
    gets.update(_link_gets())
    env = uci_env(gets=gets, show=["mihomo.ln1=landing_node", "mihomo.dl1=data_link"])

    result = run("prepare_config", env=env)
    assert result.returncode == 0, result.stderr
    output = Path("/tmp/mihomo_run.yaml").read_text()
    assert '    - name: "ssproxy-landing-ln1"' in output
    assert '    - { name: "Airport A", type: ss, server: 1.2.3.4 }' in output
    assert '    - name: "ssproxy-chain-dl1"' in output
    assert '      dialer-proxy: "Airport A"' in output
    assert "type: relay" not in output
    assert "SRC-IP-CIDR,192.168.66.158/32,ssproxy-chain-dl1" in output


def test_landing_node_delay_test_returns_controller_delay(run, uci_env):
    run_config = Path("/tmp/mihomo_run.yaml")
    run_config.write_text('proxies:\n  - name: "ssproxy-landing-ln1"\n    type: socks5\n')
    gets = _landing_gets()
    gets["mihomo.config.test_url"] = "https://example.com/generate_204"
    env = uci_env(gets=gets, show=["mihomo.ln1=landing_node"])
    env["CURL_RESPONSE"] = '{"delay":87}'

    result = run("test_landing_node", "ln1", env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '{"delay":87}'


def test_landing_node_delay_test_requires_applied_config(run, uci_env):
    Path("/tmp/mihomo_run.yaml").unlink(missing_ok=True)
    env = uci_env(gets=_landing_gets(), show=["mihomo.ln1=landing_node"])

    result = run("test_landing_node", "ln1", env=env)

    assert result.returncode == 1
    assert "save and apply first" in result.stderr


def test_data_link_success_marks_health_and_reset_clears_it(run, uci_env, tmp_path):
    gets = _link_gets()
    gets["mihomo.config.test_url"] = "https://example.com/generate_204"
    env = uci_env(gets=gets, show=["mihomo.dl1=data_link"])
    paths = _metrics_env(env, tmp_path)
    env["CURL_RESPONSE"] = '{"delay":42}'

    result = run("test_data_link", "dl1", env=env)
    health = run("get_data_link_health", env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '{"delay":42}'
    assert '"dl1"' in health.stdout

    assert run("reset_data_link_health", env=env).returncode == 0
    assert run("get_data_link_health", env=env).stdout.replace("\n", "") == "[]"
    for key in ("MIHOMO_DATA_LINK_METRICS_FILE", "MIHOMO_DATA_LINK_CONN_STATE_FILE", "MIHOMO_DATA_LINK_PERSIST_FILE"):
        assert Path(env[key]).read_text() == ""


def test_live_connection_marks_data_link_health(run_fn, uci_env, tmp_path):
    env = uci_env(gets=_link_gets(), show=["mihomo.dl1=data_link"])
    paths = _metrics_env(env, tmp_path)
    raw = '{"connections":[{"chains":["ssproxy-chain-dl1"]}]}'

    result = run_fn(f"update_data_link_health_from_connections '{raw}'", env=env)

    assert result.returncode == 0, result.stderr
    assert paths["MIHOMO_DATA_LINK_HEALTH_FILE"].read_text().strip() == "dl1"


def test_data_link_metrics_endpoint_returns_rates_totals_and_health(run, uci_env, tmp_path):
    env = uci_env(gets=_link_gets(), show=["mihomo.dl1=data_link"])
    paths = _metrics_env(env, tmp_path)
    paths["MIHOMO_DATA_LINK_METRICS_FILE"].write_text("dl1|2048|4096|1048576|2097152|123456\n")
    paths["MIHOMO_DATA_LINK_HEALTH_FILE"].write_text("dl1\n")

    result = run("get_data_link_metrics", env=env)
    payload = json.loads(result.stdout)

    assert payload == [{
        "sid": "dl1",
        "up_rate": 2048,
        "down_rate": 4096,
        "total_up": 1048576,
        "total_down": 2097152,
        "updated": 123456,
        "healthy": 1,
    }]


def test_data_link_json_printf_is_busybox_portable(src_files):
    helper = src_files["root/usr/share/mihomo/helper.sh"]

    assert "printf '\\\"%s\\\"'" not in helper
    assert "printf '{\\\"sid\\\":" not in helper
    assert "printf '{\"sid\":\"%s\"" in helper


def test_set_chain_front_node_validates_and_commits(run, uci_env, tmp_path):
    cfg = tmp_path / "source.yaml"
    cfg.write_text(BASE_CONFIG)
    env = uci_env(gets={"mihomo.config.config_path": str(cfg)})

    result = run("set_chain_front_node", "Airport A", env=env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"front_node": "Airport A"}
    assert "set mihomo.config.chain_front_node=Airport A" in uci_env.ops()
    assert "commit mihomo" in uci_env.ops()

    invalid = run("set_chain_front_node", "Missing Node", env=env)
    assert invalid.returncode == 1
    assert "front node not found" in invalid.stderr


def test_chain_frontend_contains_unified_front_control(src_files):
    chain = src_files["root/www/luci-static/resources/view/mihomo/chain.js"]

    assert "_('非统一前置')" in chain
    assert "['set_chain_front_node', selected]" in chain
    assert "o.readonly = front_node !== 'individual'" in chain
    assert "p.name.indexOf('ssproxy-chain-') !== 0" in chain


def test_resolve_data_link_sid_uses_device_ip_or_group(run_fn, uci_env):
    env = uci_env(gets=_link_gets(), show=["mihomo.dl1=data_link"])

    by_ip = run_fn("resolve_data_link_sid 192.168.66.158 '' ''", env=env)
    by_group = run_fn("resolve_data_link_sid 10.0.0.2 ssproxy-chain-dl1 ''", env=env)

    assert by_ip.stdout.strip() == "dl1"
    assert by_group.stdout.strip() == "dl1"
