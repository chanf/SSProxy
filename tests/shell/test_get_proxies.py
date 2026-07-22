"""Tests for the ``get_proxies`` subcommand — the awk YAML parser.

This is the highest-value target: it historically broke helper.sh entirely on
the router due to a quote-escaping bug. Covered here: every error path
(not_found / empty / html / no_nodes / parse_failed) plus both supported node
forms (block and inline flow-map) and CRLF tolerance.
"""
import json
import os

BLOCK = (
    "proxies:\n"
    '  - name: "节点A"\n'
    "    type: ss\n"
    "    server: 1.2.3.4\n"
    "  - name: 节点B\n"
    "    type: vmess\n"
    "    server: 5.6.7.8\n"
)

FLOW = (
    "proxies:\n"
    '  - {name: "节点A", type: ss, server: 1.2.3.4}\n'
    "  - {name: 节点B, type: vmess, server: 5.6.7.8}\n"
)


def _run(run, uci_env, config_path):
    # get_proxies intentionally prefers the live merged config. Tests in this
    # suite create that global runtime file, so isolate parser cases explicitly.
    try:
        os.unlink("/tmp/mihomo_run.yaml")
    except FileNotFoundError:
        pass
    env = uci_env(gets={"mihomo.config.config_path": str(config_path)})
    return run("get_proxies", env=env)


def test_not_found(run, uci_env, tmp_path):
    res = _run(run, uci_env, tmp_path / "missing.yaml")
    assert json.loads(res.stdout)["error"] == "not_found"


def test_empty(run, uci_env, tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("short\n")  # 6 bytes < 10
    res = _run(run, uci_env, p)
    assert json.loads(res.stdout)["error"] == "empty"


def test_html_interception(run, uci_env, tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("<html><title>Blocked</title></html>\n" + "x" * 20)
    res = _run(run, uci_env, p)
    d = json.loads(res.stdout)
    assert d["error"] == "html"
    assert "Blocked" in d["msg"]


def test_no_nodes(run, uci_env, tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("proxies: []\n")
    res = _run(run, uci_env, p)
    assert json.loads(res.stdout)["error"] == "no_nodes"


def test_parse_failed(run, uci_env, tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("something-else: 1\n" + "x" * 20)
    res = _run(run, uci_env, p)
    assert json.loads(res.stdout)["error"] == "parse_failed"


def test_block_form_nodes(run, uci_env, tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(BLOCK)
    nodes = json.loads(_run(run, uci_env, p).stdout)
    assert [n["name"] for n in nodes] == ["节点A", "节点B"]
    assert nodes[0] == {"name": "节点A", "type": "ss", "server": "1.2.3.4"}


def test_flow_map_nodes(run, uci_env, tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(FLOW)
    nodes = json.loads(_run(run, uci_env, p).stdout)
    assert [n["name"] for n in nodes] == ["节点A", "节点B"]
    assert nodes[1]["server"] == "5.6.7.8"


def test_crlf_line_endings(run, uci_env, tmp_path):
    p = tmp_path / "c.yaml"
    p.write_bytes(BLOCK.replace("\n", "\r\n").encode("utf-8"))
    nodes = json.loads(_run(run, uci_env, p).stdout)
    assert [n["name"] for n in nodes] == ["节点A", "节点B"]


def test_json_is_valid_for_all_paths(run, uci_env, tmp_path):
    """Every get_proxies exit must emit parseable JSON (regression: bare-quote bug)."""
    for body, path in [
        (BLOCK, tmp_path / "a.yaml"),
        ("proxies: []\n", tmp_path / "b.yaml"),
        ("short\n", tmp_path / "c.yaml"),
    ]:
        path.write_text(body)
        # Must not raise:
        json.loads(_run(run, uci_env, path).stdout)


def test_special_characters_in_node_fields_remain_valid_json(run, uci_env, tmp_path):
    p = tmp_path / "quoted.yaml"
    p.write_text(
        "proxies:\n"
        "  - name: 'A\"B\\\\C'\n"
        "    type: ss\n"
        "    server: 'host\"name\\\\1'\n"
    )
    nodes = json.loads(_run(run, uci_env, p).stdout)
    assert nodes == [{"name": 'A"B\\C', "type": "ss", "server": 'host"name\\1'}]
