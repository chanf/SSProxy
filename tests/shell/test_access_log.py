"""Tests for the simplified structured network access log."""

import json
from pathlib import Path


def _env(tmp_path):
    return {
        "MIHOMO_ACCESS_LOG_FILE": str(tmp_path / "access.log"),
        "MIHOMO_ACCESS_SEEN_FILE": str(tmp_path / "access.seen"),
    }


def _record(index):
    return {
        "ts": 1700000000 + index,
        "id": f"conn-{index}",
        "ip": "192.168.66.151",
        "device": "phone",
        "domain": f"example-{index}.com",
        "dst": "203.0.113.1",
        "policy": "PROXY",
        "rule": "Match",
        "up": index * 100,
        "down": index * 200,
        "start": "2026-07-21T00:00:00Z",
    }


def test_get_history_returns_valid_json_in_reverse_order(run, tmp_path):
    env = _env(tmp_path)
    log_file = Path(env["MIHOMO_ACCESS_LOG_FILE"])
    log_file.write_text("".join(json.dumps(_record(i)) + "\n" for i in range(1, 4)))

    result = run("get_history", "2", env=env)
    payload = json.loads(result.stdout)

    assert result.returncode == 0, result.stderr
    assert [row["id"] for row in payload] == ["conn-3", "conn-2"]


def test_get_history_discards_legacy_concatenated_stream(run, tmp_path):
    env = _env(tmp_path)
    log_file = Path(env["MIHOMO_ACCESS_LOG_FILE"])
    log_file.write_text(json.dumps(_record(1)) + json.dumps(_record(2)))

    result = run("get_history", "300", env=env)

    assert json.loads(result.stdout) == []
    assert log_file.read_text() == ""


def test_clear_access_log_preserves_seen_connection_ids(run, tmp_path):
    env = _env(tmp_path)
    log_file = Path(env["MIHOMO_ACCESS_LOG_FILE"])
    seen_file = Path(env["MIHOMO_ACCESS_SEEN_FILE"])
    log_file.write_text(json.dumps(_record(1)) + "\n")
    seen_file.write_text("conn-1\n")

    result = run("clear_access_log", env=env)

    assert result.returncode == 0
    assert result.stdout.strip() == "OK"
    assert log_file.read_text() == ""
    assert seen_file.read_text() == "conn-1\n"


def test_clear_access_log_marks_current_connections_seen(run, bin_dir, tmp_path):
    env = _env(tmp_path)
    log_file = Path(env["MIHOMO_ACCESS_LOG_FILE"])
    seen_file = Path(env["MIHOMO_ACCESS_SEEN_FILE"])
    log_file.write_text(json.dumps(_record(1)) + "\n")
    seen_file.write_text("old-connection\n")
    jsonfilter = Path(bin_dir) / "jsonfilter"
    jsonfilter.write_text("#!/bin/sh\nprintf 'active-1\\nactive-2\\n'\n")
    jsonfilter.chmod(0o755)
    env["CURL_RESPONSE"] = '{"connections":[{"id":"active-1"},{"id":"active-2"}]}'

    result = run("clear_access_log", env=env)

    assert result.returncode == 0, result.stderr
    assert log_file.read_text() == ""
    assert seen_file.read_text().splitlines() == ["old-connection", "active-1", "active-2"]


def test_frontend_uses_dom_nodes_for_data_link_metrics(src_files):
    helper = src_files["root/usr/share/mihomo/helper.sh"]
    chain = src_files["root/www/luci-static/resources/view/mihomo/chain.js"]
    access_log = src_files["root/www/luci-static/resources/view/mihomo/accesslog.js"]

    assert "$.connections[@].chains[0]" in helper
    assert '$3==ip && $4!="*"' in helper
    assert "'class': 'ssproxy-data-link-dot'" in chain
    assert "'class': 'ssproxy-data-link-speed'" in chain
    assert "'class': 'ssproxy-data-link-total'" in chain
    assert "return '<span class=\"ssproxy-data-link" not in chain
    assert "['clear_access_log']" in access_log
    assert "_('网络访问日志')" in access_log
    assert "get_connections" not in access_log
    assert "add_access_rule" not in access_log
    assert "IP列表" not in access_log
    assert "实时连接" not in access_log
