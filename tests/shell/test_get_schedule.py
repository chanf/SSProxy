"""Tests for the ``get_schedule`` subcommand (auto-update schedule JSON)."""
import json


def _sched(run, uci_env, gets=None):
    res = run("get_schedule", env=uci_env(gets=gets or {}))
    assert res.returncode == 0, res.stderr
    # get_schedule emits one JSON object on stdout (plus uci noise on stderr).
    line = [ln for ln in res.stdout.splitlines() if ln.strip().startswith("{")][0]
    return json.loads(line)


def test_disabled_no_url(run, uci_env):
    s = _sched(run, uci_env)
    assert s["auto_update"] == ""
    assert s["interval"] == "24"           # default when unset
    assert s["last_update"] == ""
    assert s["next_update"] == ""
    assert s["has_url"] == "0"


def test_enabled_with_last_update_computes_next(run, uci_env):
    s = _sched(run, uci_env, gets={
        "mihomo.config.auto_update": "1",
        "mihomo.config.update_interval": "12",
        "mihomo.config.last_update": "1000",
        "mihomo.config.subscription_url": "https://example.com/sub",
    })
    assert s["auto_update"] == "1"
    assert s["interval"] == "12"
    assert s["last_update"] == "1000"
    assert s["next_update"] == str(1000 + 12 * 3600)   # 44200
    assert s["has_url"] == "1"


def test_enabled_but_no_last_update(run, uci_env):
    s = _sched(run, uci_env, gets={
        "mihomo.config.auto_update": "1",
        "mihomo.config.subscription_url": "https://example.com/sub",
    })
    assert s["next_update"] == ""          # last_update empty → no next


def test_invalid_interval_defaults_to_24(run, uci_env):
    s = _sched(run, uci_env, gets={"mihomo.config.update_interval": "abc"})
    assert s["interval"] == "24"


def test_interval_below_one_clamped_to_one(run, uci_env):
    s = _sched(run, uci_env, gets={"mihomo.config.update_interval": "0"})
    assert s["interval"] == "1"
