"""Tests for the access-rule subcommands and the internal ``emit_access_rules_yaml``.

``emit_access_rules_yaml`` is an internal helper (not in the dispatcher), so it
is exercised by sourcing the library. ``get/add/del/import`` are subcommands.
"""
import json

CONFIG = 'proxy-groups:\n  - {name: "MYGROUP", type: select}\n'


def _emit(run_fn, uci_env, tmp_path, gets, show, config_text=CONFIG):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(config_text)
    env = uci_env(gets=gets, show=show)
    res = run_fn('emit_access_rules_yaml "%s"' % cfg, env=env)
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_emit_block_rule(run_fn, uci_env, tmp_path):
    out = _emit(run_fn, uci_env, tmp_path,
                gets={"mihomo.r1.enabled": "1", "mihomo.r1.domain": "ad.com",
                      "mihomo.r1.rule_type": "suffix", "mihomo.r1.action": "block"},
                show=["mihomo.r1=mihomo_rule"])
    assert "- 'DOMAIN-SUFFIX,ad.com,REJECT'" in out


def test_emit_direct_rule(run_fn, uci_env, tmp_path):
    out = _emit(run_fn, uci_env, tmp_path,
                gets={"mihomo.r1.enabled": "1", "mihomo.r1.domain": "direct.com",
                      "mihomo.r1.rule_type": "suffix", "mihomo.r1.action": "direct"},
                show=["mihomo.r1=mihomo_rule"])
    assert "- 'DOMAIN-SUFFIX,direct.com,DIRECT'" in out


def test_emit_proxy_rule_with_valid_group(run_fn, uci_env, tmp_path):
    out = _emit(run_fn, uci_env, tmp_path,
                gets={"mihomo.r1.enabled": "1", "mihomo.r1.domain": "p.com",
                      "mihomo.r1.rule_type": "suffix", "mihomo.r1.action": "proxy",
                      "mihomo.r1.group": "MYGROUP"},
                show=["mihomo.r1=mihomo_rule"])
    assert "- 'DOMAIN-SUFFIX,p.com,MYGROUP'" in out


def test_emit_proxy_rule_with_unknown_group_is_skipped(run_fn, uci_env, tmp_path):
    out = _emit(run_fn, uci_env, tmp_path,
                gets={"mihomo.r1.enabled": "1", "mihomo.r1.domain": "p.com",
                      "mihomo.r1.rule_type": "suffix", "mihomo.r1.action": "proxy",
                      "mihomo.r1.group": "NOPE"},
                show=["mihomo.r1=mihomo_rule"])
    assert out.strip() == ""


def test_emit_keyword_and_domain_types(run_fn, uci_env, tmp_path):
    out = _emit(run_fn, uci_env, tmp_path,
                gets={"mihomo.r1.enabled": "1", "mihomo.r1.domain": "kw",
                      "mihomo.r1.rule_type": "keyword", "mihomo.r1.action": "block",
                      "mihomo.r2.enabled": "1", "mihomo.r2.domain": "dom",
                      "mihomo.r2.rule_type": "domain", "mihomo.r2.action": "block"},
                show=["mihomo.r1=mihomo_rule", "mihomo.r2=mihomo_rule"])
    assert "- 'DOMAIN-KEYWORD,kw,REJECT'" in out
    assert "- 'DOMAIN,dom,REJECT'" in out


def test_emit_skips_disabled_and_domainless(run_fn, uci_env, tmp_path):
    out = _emit(run_fn, uci_env, tmp_path,
                gets={"mihomo.r1.enabled": "0", "mihomo.r1.domain": "off.com",
                      "mihomo.r1.action": "block",
                      "mihomo.r2.enabled": "1", "mihomo.r2.action": "block"},
                show=["mihomo.r1=mihomo_rule", "mihomo.r2=mihomo_rule"])
    assert out.strip() == ""   # r1 disabled, r2 has no domain


def test_get_access_rules(run, uci_env):
    env = uci_env(
        gets={
            "mihomo.r1.domain": "ad.com", "mihomo.r1.action": "block",
            "mihomo.r1.enabled": "1", "mihomo.r1.rule_type": "suffix",
            "mihomo.r1.src_ip": "1.2.3.4", "mihomo.r1.group": "", "mihomo.r1.comment": "",
        },
        show=["mihomo.r1=mihomo_rule"],
    )
    res = run("get_access_rules", env=env)
    data = json.loads(res.stdout)
    assert len(data) == 1
    assert data[0]["domain"] == "ad.com"
    assert data[0]["action"] == "block"
    assert data[0]["rule_type"] == "suffix"


def test_add_access_rule_records_uci_ops(run, uci_env):
    env = uci_env()
    res = run("add_access_rule", "192.168.1.5", "ad.com", "block", "", "suffix", env=env)
    assert res.returncode == 0
    assert res.stdout.strip() == "OK"
    ops = uci_env.ops()
    assert "mihomo.cfg0001.domain=ad.com" in ops
    assert "mihomo.cfg0001.action=block" in ops
    assert "mihomo.cfg0001.rule_type=suffix" in ops
    assert "mihomo.cfg0001.enabled=1" in ops
    assert "mihomo.cfg0001.src_ip=192.168.1.5" in ops


def test_add_access_rule_requires_domain(run, uci_env):
    res = run("add_access_rule", "", "", "block", "", "suffix", env=uci_env())
    assert res.returncode == 1
    assert "domain required" in res.stderr


def test_add_access_rule_rejects_yaml_delimiters(run, uci_env):
    res = run("add_access_rule", "", "bad,domain", "block", "", "suffix", env=uci_env())
    assert res.returncode == 1
    assert "invalid domain" in res.stderr


def test_del_access_rule(run, uci_env):
    env = uci_env()
    res = run("del_access_rule", "cfg0001", env=env)
    assert res.stdout.strip() == "OK"
    assert "delete mihomo.cfg0001" in uci_env.ops()


def test_import_rules_classifies_and_dedups(run, uci_env):
    env = uci_env()
    text = (
        "DOMAIN-SUFFIX,ad.com,REJECT\n"
        "DOMAIN,direct.com,DIRECT\n"
        "DOMAIN-KEYWORD,google,PROXY\n"
        "GEOIP,CN,DIRECT\n"            # unsupported type → skipped
        "DOMAIN-SUFFIX,ad.com,REJECT\n"  # duplicate
    )
    res = run("import_rules", text, "append", env=env)
    assert res.returncode == 0, res.stderr
    d = json.loads(res.stdout)
    assert d["imported"] == 3
    assert d["duplicates"] == 1
    assert d["skipped"] == 1
    ops = uci_env.ops()
    assert "mihomo.cfg0001.domain=ad.com" in ops
    assert "mihomo.cfg0003.domain=google" in ops
    assert "mihomo.cfg0003.group=PROXY" in ops   # PROXY policy → proxy + group


def test_import_rules_overwrite_clears_first(run, uci_env):
    env = uci_env(show=["mihomo.old=mihomo_rule"],
                  gets={"mihomo.old.domain": "old.com", "mihomo.old.action": "block"})
    res = run("import_rules", "DOMAIN-SUFFIX,new.com,REJECT", "overwrite", env=env)
    assert res.returncode == 0, res.stderr
    ops = uci_env.ops()
    # overwrite deletes existing sections first
    assert "delete mihomo.@mihomo_rule[0]" in ops
    assert json.loads(res.stdout)["imported"] == 1
