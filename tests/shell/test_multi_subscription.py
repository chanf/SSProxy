"""Tests for multi-subscription migration, merging and scheduling."""

import json
from pathlib import Path


BASE = """proxies:
  - name: "Base A"
    type: ss
    server: 192.0.2.1
    port: 443
proxy-groups:
  - name: "PROXY"
    type: select
    proxies:
      - "Base A"
rules:
  - MATCH,PROXY
"""


EXTRA = """proxies:
  - name: "Extra B"
    type: vmess
    server: 192.0.2.2
    port: 443
    ws-opts:
      headers:
        - "nested-list-must-stay-with-proxy"
  - { name: "Base A", type: ss, server: 192.0.2.9, port: 443 }
  - { name: "Extra C", type: trojan, server: 192.0.2.3, port: 443 }
proxy-groups:
  - { name: "IGNORED", type: select, proxies: ["Extra B"] }
rules:
  - MATCH,IGNORED
"""


FLOW_GROUP_BASE = """proxies:
  - { name: "Flow A", type: ss, server: 192.0.2.4, port: 443 }
proxy-groups:
  - { name: "PROXY", type: select, proxies: ["Flow A"] }
rules:
  - MATCH,PROXY
"""


INLINE_PROXIES_BASE = """proxies:
  - { name: "Inline A", type: ss, server: 192.0.2.5, port: 443 }
proxy-groups:
  - name: "PROXY"
    type: select
    proxies: []
rules:
  - MATCH,PROXY
"""


def test_merge_keeps_base_policy_and_adds_unique_nodes(run_fn, tmp_path):
    base = tmp_path / "base.yaml"
    extra = tmp_path / "extra.yaml"
    merged = tmp_path / "merged.yaml"
    base.write_text(BASE)
    extra.write_text(EXTRA)

    result = run_fn(f'merge_subscription_configs "{merged}" "{base}" "{extra}"')
    output = merged.read_text()

    assert result.returncode == 0, result.stderr
    assert output.count('name: "Base A"') == 1
    assert 'name: "Extra B"' in output
    assert 'name: "Extra C"' in output
    assert "nested-list-must-stay-with-proxy" in output
    assert 'name: "SSProxy - 全部订阅"' in output
    assert output.count('- "SSProxy - 全部订阅"') >= 1
    assert "MATCH,PROXY" in output
    assert "MATCH,IGNORED" not in output


def test_merge_injects_aggregate_into_flow_map_select_group(run_fn, tmp_path):
    base = tmp_path / "base.yaml"
    extra = tmp_path / "extra.yaml"
    merged = tmp_path / "merged.yaml"
    base.write_text(FLOW_GROUP_BASE)
    extra.write_text(EXTRA)

    result = run_fn(f'merge_subscription_configs "{merged}" "{base}" "{extra}"')
    output = merged.read_text()

    assert result.returncode == 0, result.stderr
    assert 'proxies: ["SSProxy - 全部订阅", "Flow A"]' in output


def test_merge_injects_aggregate_into_inline_proxies_select_group(run_fn, tmp_path):
    base = tmp_path / "base.yaml"
    extra = tmp_path / "extra.yaml"
    merged = tmp_path / "merged.yaml"
    base.write_text(INLINE_PROXIES_BASE)
    extra.write_text(EXTRA)

    result = run_fn(f'merge_subscription_configs "{merged}" "{base}" "{extra}"')
    output = merged.read_text()

    assert result.returncode == 0, result.stderr
    assert 'proxies: ["SSProxy - 全部订阅"]' in output


def test_single_subscription_merge_preserves_original_config(run_fn, tmp_path):
    base = tmp_path / "base.yaml"
    merged = tmp_path / "merged.yaml"
    base.write_text(BASE)

    result = run_fn(f'merge_subscription_configs "{merged}" "{base}"')

    assert result.returncode == 0, result.stderr
    assert merged.read_text() == BASE


def test_legacy_url_migrates_to_subscription_section(run, uci_env, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(BASE)
    env = uci_env(gets={
        "mihomo.config.subscription_url": "https://example.com/legacy",
        "mihomo.config.config_path": str(config),
        "mihomo.config.work_dir": str(tmp_path / "work"),
    })

    result = run("restore_subscription_url", env=env)

    assert result.returncode == 0, result.stderr
    ops = uci_env.ops()
    assert "add mihomo subscription" not in ops  # add returns an ID but is not recorded
    assert "set mihomo.cfg0001.name=订阅 1" in ops
    assert "set mihomo.cfg0001.url=https://example.com/legacy" in ops
    assert (tmp_path / "work" / "subscriptions" / "cfg0001.yaml").read_text() == BASE


def test_schedule_counts_enabled_subscription_sections(run, uci_env):
    env = uci_env(
        gets={
            "mihomo.config.auto_update": "1",
            "mihomo.config.update_interval": "12",
            "mihomo.config.last_update": "1000",
            "mihomo.sub1.enabled": "1",
            "mihomo.sub2.enabled": "0",
        },
        show=["mihomo.sub1=subscription", "mihomo.sub2=subscription"],
    )

    result = run("get_schedule", env=env)
    payload = json.loads(result.stdout)

    assert payload["subscription_count"] == 1
    assert payload["has_url"] == "1"
    assert payload["next_update"] == str(1000 + 12 * 3600)


def test_batch_update_downloads_all_and_reuses_failed_cache(run, uci_env, bin_dir, tmp_path):
    one = tmp_path / "one.yaml"
    two = tmp_path / "two.yaml"
    one.write_text(BASE)
    two.write_text(EXTRA)
    curl = Path(bin_dir) / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "out=''\nurl=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in -o) shift; out=\"$1\" ;; http*) url=\"$1\" ;; esac\n"
        "  shift\n"
        "done\n"
        "case \"$url\" in\n"
        "  *one*) cat \"$SUB_ONE_FILE\" > \"$out\" ;;\n"
        "  *two*) [ \"$FAIL_TWO\" = 1 ] && exit 22; cat \"$SUB_TWO_FILE\" > \"$out\" ;;\n"
        "  *) exit 22 ;;\n"
        "esac\n"
    )
    curl.chmod(0o755)
    work = tmp_path / "work dir"
    merged = tmp_path / "merged config.yaml"
    env = uci_env(
        gets={
            "mihomo.config.config_mode": "subscription",
            "mihomo.config.work_dir": str(work),
            "mihomo.config.config_path": str(merged),
            "mihomo.sub1.enabled": "1",
            "mihomo.sub1.name": "One",
            "mihomo.sub1.url": "https://example.com/one",
            "mihomo.sub2.enabled": "1",
            "mihomo.sub2.name": "Two",
            "mihomo.sub2.url": "https://example.com/two",
        },
        show=["mihomo.sub1=subscription", "mihomo.sub2=subscription"],
    )
    env.update({"SUB_ONE_FILE": str(one), "SUB_TWO_FILE": str(two)})

    first = run("update_subscriptions", env=env)
    first_payload = json.loads(first.stdout)

    assert first.returncode == 0, first.stderr
    assert first_payload["updated"] == 2
    assert first_payload["available"] == 2
    assert 'name: "Extra B"' in merged.read_text()

    env["FAIL_TWO"] = "1"
    second = run("update_subscriptions", env=env)
    second_payload = json.loads(second.stdout)

    assert second.returncode == 0, second.stderr
    assert second_payload["updated"] == 1
    assert second_payload["cached"] == 1
    assert 'name: "Extra B"' in merged.read_text()


def test_frontend_manages_multiple_subscriptions_and_tests_merged_nodes(src_files):
    settings = src_files["root/www/luci-static/resources/view/mihomo/settings.js"]
    dashboard = src_files["root/www/luci-static/resources/view/mihomo/dashboard.js"]

    assert "form.GridSection, 'subscription'" in settings
    assert "subscriptions.addremove = true" in settings
    assert "subscriptions.sortable = true" in settings
    assert "['update_subscriptions']" in settings
    assert "form.Value, 'subscription_url'" not in settings
    assert "['update_subscriptions']" in dashboard
    assert "_('全部订阅节点')" in dashboard
    assert "['test_all_nodes']" in dashboard
