"""Tests for ``emit_tproxy_rules`` (nft ruleset generation) and the LAN-IP
detection helpers ``get_lan_ip`` / ``get_lan_ip6``.

``emit_tproxy_rules`` is a pure function in the dispatcher-stripped helper
library, so it is exercised via the ``run_fn`` fixture (sources the lib, then
invokes the function with the same 8 positional args init.d passes). The
assertions pin the exact nft rule lines for each of the four operating modes
plus the IPv4/IPv6 family split and the empty-acl / missing-LAN-IP fallbacks.
"""
import shlex


def _emit(run_fn, *args):
    """Invoke emit_tproxy_rules with positional args; return full stdout text."""
    body = "emit_tproxy_rules " + " ".join(shlex.quote(str(a)) for a in args)
    res = run_fn(body)
    assert res.returncode == 0, res.stderr
    return res.stdout


# -- emit_tproxy_rules: the four operating modes -----------------------------


def test_all_mode_is_bare_minimum(run_fn):
    # all + dns_hijack=1: only the private-dst bypass + tproxy catch-all.
    # No source rules, no mihomo_dns table (global dnsmasq hijack handles DNS).
    out = _emit(run_fn, 7893, "all", "", "", 1, 1053, "192.168.66.1", "fe80::1")
    assert "add table inet mihomo" in out
    assert "ip daddr {" in out and "return" in out
    assert "ip6 daddr {" in out
    assert "tproxy to :7893" in out
    assert "saddr" not in out          # no whitelist source rules
    assert "mihomo_dns" not in out     # no DNS DNAT table
    assert "dnat" not in out


def test_whitelist_dns_off_has_bypass_only(run_fn):
    # whitelist + dns_hijack=0: source bypass for non-whitelisted, no DNS table.
    out = _emit(run_fn, 7893, "whitelist", "192.168.1.0/24", "", 0, 1053, "", "")
    assert "ip saddr != { 192.168.1.0/24 } return" in out
    assert "dport 53" not in out       # no DNS early-return
    assert "mihomo_dns" not in out


def test_whitelist_dns_on_full_coexistence(run_fn):
    # The headline case: whitelist + dns_hijack=1, both families.
    out = _emit(run_fn, 7893, "whitelist", "192.168.66.151", "fd00::1",
                1, 1053, "192.168.66.1", "fe80::1")

    # DNS early-return (whitelisted clients' 53 → falls through to nat DNAT)
    # must be emitted BEFORE the source bypass rule, otherwise their DNS would
    # be tproxy'd to the tproxy port instead of reaching Mihomo DNS.
    v4_dns = "ip saddr { 192.168.66.151 } udp dport 53 return"
    v4_bypass = "ip saddr != { 192.168.66.151 } return"
    assert v4_dns in out and v4_bypass in out
    assert out.index(v4_dns) < out.index(v4_bypass)
    assert "ip6 saddr { fd00::1 } udp dport 53 return" in out
    assert "ip6 saddr != { fd00::1 } return" in out

    # Source-scoped DNS DNAT table: dnat (not redirect) catches hardcoded DNS.
    assert "add table inet mihomo_dns" in out
    assert "add chain inet mihomo_dns prerouting { type nat hook prerouting priority dstnat; }" in out
    assert "ip saddr { 192.168.66.151 } udp dport 53 dnat ip to 192.168.66.1:1053" in out
    assert "ip saddr { 192.168.66.151 } tcp dport 53 dnat ip to 192.168.66.1:1053" in out
    # IPv6 DNAT target uses brackets around the address.
    assert "ip6 saddr { fd00::1 } udp dport 53 dnat ip6 to [fe80::1]:1053" in out
    assert "ip6 saddr { fd00::1 } tcp dport 53 dnat ip6 to [fe80::1]:1053" in out


def test_whitelist_dns_on_v4_only(run_fn):
    # Only IPv4 acl + router IP: v4 DNAT emitted, plus an explicit IPv6
    # family bypass so IPv6 traffic cannot accidentally fall through to the
    # catch-all TProxy rule.
    out = _emit(run_fn, 7893, "whitelist", "10.0.0.5", "", 1, 1053, "10.0.0.1", "")
    assert "ip saddr { 10.0.0.5 } udp dport 53 dnat ip to 10.0.0.1:1053" in out
    assert "ip6 saddr ::/0 return" in out


def test_whitelist_dns_on_v6_only(run_fn):
    # Only IPv6 acl + router IP: v6 DNAT emitted, plus an explicit IPv4
    # family bypass so IPv4 traffic cannot accidentally fall through to the
    # catch-all TProxy rule.
    out = _emit(run_fn, 7893, "whitelist", "", "fd00::5", 1, 1053, "", "fe80::1")
    assert "ip6 saddr { fd00::5 } udp dport 53 dnat ip6 to [fe80::1]:1053" in out
    assert "ip6 saddr != { fd00::5 } return" in out
    assert "ip saddr 0.0.0.0/0 return" in out


def test_empty_acl_emits_no_source_rules(run_fn):
    # whitelist + dns_hijack but empty acl: bypass both address families and
    # omit the DNS table. init.d falls back to the global dnsmasq hijack here.
    out = _emit(run_fn, 7893, "whitelist", "", "", 1, 1053, "192.168.66.1", "fe80::1")
    assert "ip saddr 0.0.0.0/0 return" in out
    assert "ip6 saddr ::/0 return" in out
    assert "mihomo_dns" not in out


def test_missing_router_ip_disables_dns_scope(run_fn):
    # whitelist + dns_hijack + acl present but NO detected LAN IP: must NOT
    # install the source-scoped DNS DNAT (would divert DNS to nothing). The
    # source bypass still applies; init.d falls back to the global dnsmasq hijack.
    out = _emit(run_fn, 7893, "whitelist", "192.168.66.151", "", 1, 1053, "", "")
    assert "ip saddr != { 192.168.66.151 } return" in out
    assert "dport 53" not in out
    assert "mihomo_dns" not in out


def test_custom_dns_port_is_interpolated(run_fn):
    out = _emit(run_fn, 7893, "whitelist", "192.168.66.151", "", 1, 5353,
                "192.168.66.1", "fe80::1")
    assert "add table inet mihomo_dns" in out
    assert "dnat ip to 192.168.66.1:5353" in out


# -- get_lan_ip / get_lan_ip6: priority + fallback ---------------------------


def test_get_lan_ip_prefers_uci(run_fn, uci_env):
    env = uci_env(gets={"network.lan.ipaddr": "192.168.1.1"})
    res = run_fn("get_lan_ip", env={**env, "IPV4_BR_LAN": "10.0.0.1/24"})
    assert res.stdout.strip() == "192.168.1.1"


def test_get_lan_ip_falls_back_to_interface(run_fn, uci_env):
    # No UCI value → falls through to the `ip -o addr show br-lan` stub.
    env = uci_env(gets={})
    res = run_fn("get_lan_ip", env={**env, "IPV4_BR_LAN": "10.0.0.7/24"})
    assert res.stdout.strip() == "10.0.0.7"


def test_get_lan_ip_returns_nonzero_when_unknown(run_fn, uci_env):
    env = uci_env(gets={})
    res = run_fn("get_lan_ip", env=env)  # no UCI, no ip stub data
    assert res.returncode != 0
    assert res.stdout.strip() == ""


def test_get_lan_ip6_prefers_link_local(run_fn, uci_env):
    env = uci_env(gets={})
    res = run_fn("get_lan_ip6", env={**env, "IPV6_LL": "fe80::abcd/64", "IPV6_GUA": "2001:db8::1/64"})
    assert res.stdout.strip() == "fe80::abcd"


def test_get_lan_ip6_falls_back_to_global(run_fn, uci_env):
    env = uci_env(gets={})
    res = run_fn("get_lan_ip6", env={**env, "IPV6_GUA": "2001:db8::9/64"})
    assert res.stdout.strip() == "2001:db8::9"


def test_get_lan_ip6_returns_nonzero_when_unknown(run_fn, uci_env):
    env = uci_env(gets={})
    res = run_fn("get_lan_ip6", env=env)
    assert res.returncode != 0
    assert res.stdout.strip() == ""
