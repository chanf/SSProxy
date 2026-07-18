"""Tests for commercial-grade hardening: profile persistence, controller secret,
and Geo database management (prepare_config injection + update_geox subcommand).
"""
import re

RUN_CONFIG = "/tmp/mihomo_run.yaml"

SRC = (
    "proxies:\n"
    "  - name: testnode\n"
    "    type: ss\n"
    "    server: 1.2.3.4\n"
)

SRC_WITH_KEYS = (
    'secret: "original-secret"\n'
    "profile:\n"
    "  store-selected: false\n"
    "external-controller: 1.2.3.4:9999\n"
    "geox-url:\n"
    "  geoip: https://old/geoip.dat\n"
    "proxies:\n"
    "  - name: testnode\n"
)


def _base_gets(src_path):
    return {
        "mihomo.config.config_path": str(src_path),
        "mihomo.config.dns_port": "1053",
        "mihomo.config.tproxy_port": "7893",
        "mihomo.config.mix_port": "7890",
        "mihomo.config.tun_enabled": "0",
    }


def _run(run, uci_env, src_path, **extra):
    gets = _base_gets(src_path)
    gets.update({f"mihomo.config.{k}": v for k, v in extra.items()})
    return run("prepare_config", env=uci_env(gets=gets))


# --- profile persistence ---

def test_profile_block_injected(run, uci_env, tmp_path):
    src = tmp_path / "src.yaml"
    src.write_text(SRC)
    assert _run(run, uci_env, src).returncode == 0
    out = open(RUN_CONFIG, encoding="utf-8").read()
    assert "profile:" in out
    assert "store-selected: true" in out
    assert "store-fake-ip: true" in out


# --- controller secret ---

def test_secret_from_uci_injected(run, uci_env, tmp_path):
    src = tmp_path / "src.yaml"
    src.write_text(SRC)
    _run(run, uci_env, src, secret="my-secret-123")
    out = open(RUN_CONFIG, encoding="utf-8").read()
    assert 'secret: "my-secret-123"' in out


def test_secret_auto_generated_when_empty(run, uci_env, tmp_path):
    src = tmp_path / "src.yaml"
    src.write_text(SRC)
    res = _run(run, uci_env, src)   # no secret → auto-gen
    assert res.returncode == 0, res.stderr
    out = open(RUN_CONFIG, encoding="utf-8").read()
    m = re.search(r'^secret: "([^"]+)"', out, re.M)
    assert m and len(m.group(1)) >= 8
    # persisted to UCI so it stays stable across restarts
    assert any("set mihomo.config.secret=" in ln for ln in uci_env.ops().splitlines())


# --- geox-url injection ---

def test_geox_injected_when_enabled(run, uci_env, tmp_path):
    src = tmp_path / "src.yaml"
    src.write_text(SRC)
    _run(run, uci_env, src,
         geo_auto_update="1",
         geoip_mirror_url="https://example.com/geoip.dat",
         geosite_mirror_url="https://example.com/geosite.dat",
         geo_update_interval="12")
    out = open(RUN_CONFIG, encoding="utf-8").read()
    assert "geox-url:" in out
    assert "https://example.com/geoip.dat" in out
    assert "geo-auto-update: true" in out
    assert "geo-update-interval: 12" in out


def test_geox_not_injected_when_disabled(run, uci_env, tmp_path):
    src = tmp_path / "src.yaml"
    src.write_text(SRC)
    _run(run, uci_env, src, geo_auto_update="0",
         geoip_mirror_url="https://example.com/geoip.dat")
    out = open(RUN_CONFIG, encoding="utf-8").read()
    assert "geox-url:" not in out


# --- strip source-supplied keys (idempotency / no duplicates) ---

def test_source_keys_are_replaced_not_duplicated(run, uci_env, tmp_path):
    src = tmp_path / "src.yaml"
    src.write_text(SRC_WITH_KEYS)
    _run(run, uci_env, src, secret="controlled")
    out = open(RUN_CONFIG, encoding="utf-8").read()
    assert out.count("secret:") == 1
    assert out.count("profile:") == 1
    assert out.count("external-controller:") == 1
    # the source's values are gone, the controlled ones are in
    assert "original-secret" not in out
    assert "1.2.3.4:9999" not in out
    assert 'secret: "controlled"' in out
    assert "store-selected: true" in out


# --- update_geox subcommand ---

def test_update_geox_downloads_to_workdir(run, uci_env, tmp_path):
    work = tmp_path / "work"
    env = uci_env(gets={
        "mihomo.config.work_dir": str(work),
        "mihomo.config.geoip_mirror_url": "https://example.com/geoip.dat",
        "mihomo.config.geosite_mirror_url": "https://example.com/geosite.dat",
    })
    res = run("update_geox", env=env)
    assert res.returncode == 0, res.stderr
    assert "SUCCESS" in res.stdout
    assert (work / "geoip.dat").is_file()
    assert (work / "geosite.dat").is_file()


def test_update_geox_no_urls_errors(run, uci_env, tmp_path):
    env = uci_env(gets={"mihomo.config.work_dir": str(tmp_path / "work")})
    res = run("update_geox", env=env)
    assert res.returncode == 1
    assert "ERROR" in res.stderr
