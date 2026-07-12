import os
import tarfile
import io
import time
import shutil

# Define configuration for the OpenClash replacement
PKG_NAME = "luci-app-mihomo"
PKG_VERSION = "1.0.0-27"
PKG_ARCH = "all"
IPK_FILENAME = f"{PKG_NAME}_{PKG_VERSION}_{PKG_ARCH}.ipk"

# File contents mapping
src_files = {
    # Package metadata
    "CONTROL/control": """Package: luci-app-mihomo
Version: 1.0.0-1
Depends: luci-base, ip-full, kmod-nft-tproxy, curl
Architecture: all
Maintainer: Antigravity
Section: luci
Priority: optional
Description: Lightweight Mihomo (Clash Meta) client for iStoreOS with Firewall4 (nftables) integration
""",
    
    # Post-installation script to clear LuCI index asynchronously
    "CONTROL/postinst": """#!/bin/sh
if [ -z "$IPKG_INSTROOT" ]; then
    rm -f /tmp/luci-indexcache
    rm -f /tmp/luci-modulecache
    (sleep 3; /etc/init.d/rpcd restart) &
fi
exit 0
""",

    # Post-removal script
    "CONTROL/postrm": """#!/bin/sh
if [ -z "$IPKG_INSTROOT" ]; then
    rm -f /tmp/luci-indexcache
    rm -f /tmp/luci-modulecache
    (sleep 3; /etc/init.d/rpcd restart) &
fi
exit 0
""",

    # UCI Configuration
    "root/etc/config/mihomo": """
config mihomo 'config'
	option enabled '0'
	option core_path '/usr/bin/mihomo'
	option config_path '/etc/mihomo/config.yaml'
	option work_dir '/etc/mihomo'
	option mix_port '7890'
	option tproxy_port '7893'
	option dns_port '1053'
	option dns_hijack '1'
	option tun_enabled '0'
	option subscription_url ''
""",
    # System Init Script managed by procd with TProxy/nftables/Dnsmasq redirection
    "root/etc/init.d/mihomo": """#!/bin/sh /etc/rc.common

START=95
USE_PROCD=1

enable_tproxy() {
\tlocal tproxy_port="$1"
\t
\t# 1. Add routing table 100
\tip rule add fwmark 1 table 100 2>/dev/null
\tip route add local default dev lo table 100 2>/dev/null
\t
\t# 2. Add nftables redirection rules
\tnft create table inet mihomo 2>/dev/null
\tnft add chain inet mihomo prerouting { type filter hook prerouting priority mangle \\; }
\tnft add rule inet mihomo prerouting ip daddr { 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 255.255.255.255/32 } return
\tnft add rule inet mihomo prerouting meta l4proto { tcp, udp } tproxy to :"$tproxy_port" meta mark set 1
\t
\tlogger -t mihomo "TProxy redirect rules enabled on port $tproxy_port"
}

disable_tproxy() {
\t# Remove nftables table and routing rules
\tnft delete table inet mihomo 2>/dev/null
\tip rule del fwmark 1 table 100 2>/dev/null
\tip route del local default dev lo table 100 2>/dev/null
\t
\tlogger -t mihomo "TProxy redirect rules disabled"
}

enable_dns_hijack() {
\tlocal dns_port="$1"
\t
\t# Configure Dnsmasq to forward external requests to Mihomo DNS
\tuci add_list dhcp.@dnsmasq[0].server="127.0.0.1#$dns_port"
\tuci set dhcp.@dnsmasq[0].noresolv="1"
\tuci commit dhcp
\t/etc/init.d/dnsmasq restart
\t
\tlogger -t mihomo "DNS hijack enabled: Dnsmasq forwarding to Mihomo DNS on port $dns_port"
}

disable_dns_hijack() {
\tlocal dns_port="$1"
\t
\t# Revert Dnsmasq changes
\tuci del_list dhcp.@dnsmasq[0].server="127.0.0.1#$dns_port" 2>/dev/null
\tuci del dhcp.@dnsmasq[0].noresolv 2>/dev/null
\tuci commit dhcp
\t/etc/init.d/dnsmasq restart
\t
\tlogger -t mihomo "DNS hijack disabled"
}

start_service() {
\tconfig_load mihomo
\t
\tlocal enabled core_path config_path work_dir dns_port dns_hijack tproxy_port tun_enabled
\t
\tconfig_get_bool enabled config enabled 0
\tconfig_get core_path config core_path "/usr/bin/mihomo"
\tconfig_get config_path config config_path "/etc/mihomo/config.yaml"
\tconfig_get work_dir config work_dir "/etc/mihomo"
\tconfig_get dns_port config dns_port "1053"
\tconfig_get_bool dns_hijack config dns_hijack 1
\tconfig_get tproxy_port config tproxy_port "7893"
\tconfig_get_bool tun_enabled config tun_enabled 0
\t
\t[ "$enabled" -eq 1 ] || return 0
\t
\tif [ ! -x "$core_path" ]; then
\t\tlogger -t mihomo "ERROR: Core binary not found or not executable at $core_path"
\t\treturn 1
\tfi
\t
\tmkdir -p "$work_dir"
\t
\tif [ ! -f "$config_path" ]; then
\t\tmkdir -p "$(dirname "$config_path")"
\t\tcat <<EOF > "$config_path"
port: 7890
socks-port: 7891
redir-port: 7892
tproxy-port: 7893
mixed-port: 7890
allow-lan: true
mode: rule
log-level: info
external-controller: 0.0.0.0:9090
secret: ""
dns:
  enable: true
  ipv6: false
  listen: 0.0.0.0:1053
  enhanced-mode: fake-ip
  nameserver:
    - 223.5.5.5
    - 114.114.114.114
EOF
\tfi
\t
\t# Prepare running configuration file in RAM
\t/usr/share/mihomo/helper.sh prepare_config
\tif [ $? -ne 0 ]; then
\t\tlogger -t mihomo "ERROR: Failed to prepare running configuration"
\t\treturn 1
\tfi
\t
\t# Start Daemon
\tprocd_open_instance
\tprocd_set_param command "$core_path" -d "$work_dir" -f "/tmp/mihomo_run.yaml"
\tprocd_set_param stdout 1
\tprocd_set_param stderr 1
\tprocd_set_param respawn
\tprocd_close_instance
\t
\t# Apply network redirections
\tif [ "$tun_enabled" -ne 1 ]; then
\t\tenable_tproxy "$tproxy_port"
\tfi
\t
\tif [ "$dns_hijack" -eq 1 ]; then
\t\tenable_dns_hijack "$dns_port"
\tfi
\t
\tlogger -t mihomo "Mihomo service started successfully"
}

stop_service() {
\tconfig_load mihomo
\t
\tlocal dns_port dns_hijack tun_enabled
\tconfig_get dns_port config dns_port "1053"
\tconfig_get_bool dns_hijack config dns_hijack 1
\tconfig_get_bool tun_enabled config tun_enabled 0
\t
\t# Clean up redirect rules
\tif [ "$tun_enabled" -ne 1 ]; then
\t\tdisable_tproxy
\tfi
\t
\tif [ "$dns_hijack" -eq 1 ]; then
\t\tdisable_dns_hijack "$dns_port"
\tfi
\t
\trm -f /tmp/mihomo_run.yaml
\tlogger -t mihomo "Mihomo service stopped"
}

service_triggers() {
\tprocd_add_reload_trigger "mihomo"
}
""",

    # Backend helper script to auto-detect architecture, download core, parse subscription, and merge config
    "root/usr/share/mihomo/helper.sh": """#!/bin/sh

get_arch() {
\tlocal raw_arch=$(uname -m)
\tcase "$raw_arch" in
\t\tx86_64)
\t\t\techo "amd64"
\t\t\t;;
\t\taarch64)
\t\t\techo "arm64"
\t\t\t;;
\t\tarmv7*)
\t\t\techo "armv7"
\t\t\t;;
\t\tmips)
\t\t\techo "mips-softfloat"
\t\t\t;;
\t\tmipsel)
\t\t\techo "mipsle-softfloat"
\t\t\t;;
\t\t*)
\t\t\tlocal opkg_arch=$(opkg print-architecture | awk '{print $2}' | grep -v 'all' | grep -v 'noarch' | head -n 1)
\t\t\tcase "$opkg_arch" in
\t\t\t\t*x86_64*) echo "amd64" ;;
\t\t\t\t*aarch64*|*arm64*) echo "arm64" ;;
\t\t\t\t*armv7*) echo "armv7" ;;
\t\t\t\t*mipsel*) echo "mipsle-softfloat" ;;
\t\t\t\t*mips*) echo "mips-softfloat" ;;
\t\t\t\t*) echo "unknown" ;;
\t\t\tesac
\t\t\t;;
\tesac
}

check_core() {
\tlocal core_path=$(uci -q get mihomo.config.core_path || echo "/usr/bin/mihomo")
\tif [ -x "$core_path" ]; then
\t\tlocal version=$("$core_path" -v 2>/dev/null | awk '{print $3}')
\t\techo "installed:$version"
\telse
\t\techo "not_installed"
\tfi
}

download_core() {
\tlocal arch=$(get_arch)
\tif [ "$arch" = "unknown" ]; then
\t\techo "ERROR: Unsupported architecture" >&2
\t\treturn 1
\tfi

\tlocal version="v1.18.9"
\tlocal mirror="https://github.com/MetaCubeX/mihomo/releases/download/${version}"
\tlocal filename="mihomo-linux-${arch}-${version}.gz"
\tlocal url="${mirror}/${filename}"

\tif [ -n "$1" ]; then
\t\turl="$1"
\t\tfilename=$(basename "$url")
\tfi

\tlocal core_path=$(uci -q get mihomo.config.core_path || echo "/usr/bin/mihomo")
\tlocal core_dir=$(dirname "$core_path")
\t
\tmkdir -p "$core_dir"
\tmkdir -p /tmp/mihomo_download

\techo "Downloading Mihomo core from $url..."
\tcurl -fsSL -k -o "/tmp/mihomo_download/$filename" "$url"
\tif [ $? -ne 0 ]; then
\t\techo "ERROR: Download failed" >&2
\t\trm -rf /tmp/mihomo_download
\t\treturn 1
\tfi

\techo "Extracting binary..."
\tif [ "${filename##*.}" = "gz" ] && [ "${filename#*.gz}" != "$filename" ]; then
\t\tgunzip -f "/tmp/mihomo_download/$filename"
\t\tlocal extracted_name=$(basename "$filename" .gz)
\t\tmv "/tmp/mihomo_download/$extracted_name" "$core_path"
\telif [ "${filename##*.}" = "tar.gz" ] || [ "${filename#*.tar.gz}" != "$filename" ]; then
\t\ttar -zxf "/tmp/mihomo_download/$filename" -C /tmp/mihomo_download
\t\tlocal bin_file=$(find /tmp/mihomo_download -type f -executable | head -n 1)
\t\tif [ -n "$bin_file" ]; then
\t\t\tmv "$bin_file" "$core_path"
\t\telse
\t\t\techo "ERROR: Could not find executable in tarball" >&2
\t\t\trm -rf /tmp/mihomo_download
\t\t\treturn 1
\tfi
\telse
\t\tmv "/tmp/mihomo_download/$filename" "$core_path"
\tfi

\tchmod +x "$core_path"
\trm -rf /tmp/mihomo_download
\techo "SUCCESS: Mihomo core installed to $core_path"
\treturn 0
}

update_subscription() {
\tlocal url="$1"
\tif [ -z "$url" ]; then
\t\techo "ERROR: No subscription URL specified" >&2
\t\treturn 1
\tfi
\tlocal work_dir=$(uci -q get mihomo.config.work_dir || echo "/etc/mihomo")
\tlocal config_path=$(uci -q get mihomo.config.config_path || echo "/etc/mihomo/config.yaml")

\tmkdir -p "$work_dir"
\tlogger -t mihomo "Updating subscription from $url"
\techo "Fetching subscription..."
\tcurl -fsSL -k -A "ClashMeta" -o "/tmp/mihomo_sub.yaml" "$url"
\tif [ $? -ne 0 ]; then
\t\techo "ERROR: Failed to download subscription" >&2
\t\tlogger -t mihomo 'Subscription update FAILED: download error'
\t\trm -f /tmp/mihomo_sub.yaml
\t\treturn 1
\tfi

\tmv "/tmp/mihomo_sub.yaml" "$config_path"
\techo "SUCCESS: Subscription updated at $config_path"
\tlogger -t mihomo "Subscription updated: $(wc -c < "$config_path") bytes at $config_path"
\treturn 0
}

prepare_config() {
\tlocal src_config=$(uci -q get mihomo.config.config_path || echo "/etc/mihomo/config.yaml")
\tlocal run_config="/tmp/mihomo_run.yaml"
\t
\tlocal dns_port=$(uci -q get mihomo.config.dns_port || echo "1053")
\tlocal tproxy_port=$(uci -q get mihomo.config.tproxy_port || echo "7893")
\tlocal mix_port=$(uci -q get mihomo.config.mix_port || echo "7890")
\tlocal tun_enabled=$(uci -q get mihomo.config.tun_enabled || echo "0")
\t
\tif [ ! -f "$src_config" ]; then
\t\techo "ERROR: Source configuration file $src_config not found" >&2
\t\treturn 1
\tfi
\t
\t# Copy source config to temp running config
\tcp "$src_config" "$run_config"
\t
\t# Strip existing dns and tun blocks to avoid duplicate key errors
\tawk -v in_block=0 '
\t/^dns:/ || /^tun:/ { in_block=1; next }
\tin_block && /^[a-zA-Z]/ { in_block=0 }
\t!in_block { print }
\t' "$run_config" > "${run_config}.tmp"
\tmv "${run_config}.tmp" "$run_config"
\t
\t# Strip top-level ports to avoid conflicts
\tsed -i '/^mixed-port:/d; /^tproxy-port:/d; /^port:/d; /^socks-port:/d; /^allow-lan:/d; /^external-controller:/d' "$run_config"
\t
\t# Prepend our controlled settings at the top
\tcat <<EOF > "${run_config}.tmp"
mixed-port: $mix_port
tproxy-port: $tproxy_port
allow-lan: true
external-controller: 0.0.0.0:9090
EOF
\tcat "$run_config" >> "${run_config}.tmp"
\tmv "${run_config}.tmp" "$run_config"
\t
\t# Append controlled DNS block
\tcat <<EOF >> "$run_config"
dns:
  enable: true
  ipv6: false
  listen: 0.0.0.0:$dns_port
  enhanced-mode: fake-ip
  nameserver:
    - 223.5.5.5
    - 119.29.29.29
EOF

\t# Append controlled TUN block
\tif [ "$tun_enabled" -eq 1 ]; then
\t\tcat <<EOF >> "$run_config"
tun:
  enable: true
  stack: system
  auto-route: true
  auto-detect-interface: true
EOF
\telse
\t\tcat <<EOF >> "$run_config"
tun:
  enable: false
EOF
\tfi
\t
\techo "SUCCESS: Prepared configuration at $run_config"
\treturn 0
}

get_proxy_groups() {
\tif ! curl -s -m 2 "http://127.0.0.1:9090/proxies"; then
\t\techo "{\\"proxies\\":{}}"
\tfi
}

select_node() {
\tlocal group="$1"
\tlocal node="$2"
\tif [ -z "$group" ] || [ -z "$node" ]; then
\t\techo "ERROR: Group and node name must be specified" >&2
\t\treturn 1
\tfi
\tcurl -s -X PUT \\
\t\t-H "Content-Type: application/json" \\
\t\t-d "{\\"name\\":\\"${node}\\"}" \\
\t\t"http://127.0.0.1:9090/proxies/${group}"
}

get_proxies() {
\tlocal config_path=$(uci -q get mihomo.config.config_path || echo "/etc/mihomo/config.yaml")
\tif [ ! -f "$config_path" ]; then
\t\tlogger -t mihomo "get_proxies: config file not found at $config_path"
\t\techo "{\\"error\\":\\"not_found\\", \\"msg\\":\\"本地尚未下载任何订阅配置文件，请点击下方按钮更新订阅。\\"}"
\t\treturn 0
\tfi
\t
\tlocal size=$(wc -c < "$config_path")
\tif [ "$size" -lt 10 ]; then
\t\tlogger -t mihomo "get_proxies: config file empty ($size bytes) at $config_path"
\t\techo "{\\"error\\":\\"empty\\", \\"msg\\":\\"配置文件内容为空，请重新更新订阅。\\"}"
\t\treturn 0
\tfi
\t
\tif grep -q -E "<html>|<!DOCTYPE html>" "$config_path"; then
\t\tlocal title=$(grep -o -E "<title>[^<]+</title>" "$config_path" | sed -e 's/<title>//g' -e 's/<\\/title>//g' | head -n 1)
\t\t[ -z "$title" ] && title="WAF 拦截或网络错误"
\t\tlogger -t mihomo "get_proxies: subscription returned an HTML page ($title)"
\t\techo "{\\"error\\":\\"html\\", \\"msg\\":\\"下载失败：服务器返回了网页内容 (${title})。请检查链接或网络环境。\\"}"
\t\treturn 0
\tfi
\t
\tlocal nodes=$(awk \'
\tfunction trim(s){ gsub(/^[ \t]+|[ \t]+$/, "", s); return s }
\tfunction getf(str, key,   rest, p, q, v){
\t\tif (match(str, key ":[ \t]*")) {
\t\t\trest = substr(str, RSTART+RLENGTH)
\t\t\tif (substr(rest,1,1) == "\\047") { rest=substr(rest,2); p=index(rest,"\\047"); if(p>0) v=substr(rest,1,p-1) }
\t\t\telse { p=index(rest,","); q=index(rest,"}"); if(p==0||(q>0&&q<p)) p=q; if(p>0) v=substr(rest,1,p-1) }
\t\t\treturn trim(v)
\t\t}
\t\treturn ""
\t}
\tBEGIN { print "["; first=1 }
\t/^proxies:/ { in_p=1; next }
\tin_p && /^[a-zA-Z]/ && $0 !~ /^[ \t]/ { in_p=0 }
\tin_p {
\t\tif ($0 ~ /^[ \t]*-/) {
\t\t\tif (name != "") { if(!first) printf ","; first=0; printf("  {\\042name\\042:\\042%s\\042,\\042type\\042:\\042%s\\042,\\042server\\042:\\042%s\\042}", name, type, server) }
\t\t\tname=""; type=""; server=""
\t\t\ts = $0; sub(/^[ \t]*-[ \t]*/, "", s)
\t\t\tif (s ~ /\\{/) {
\t\t\t\tname=getf(s,"name"); type=getf(s,"type"); server=getf(s,"server")
\t\t\t\tif (name != "") { if(!first) printf ","; first=0; printf("  {\\042name\\042:\\042%s\\042,\\042type\\042:\\042%s\\042,\\042server\\042:\\042%s\\042}", name, type, server) }
\t\t\t\tname=""; type=""; server=""
\t\t\t\tnext
\t\t\t}
\t\t\t$0 = s
\t\t\tif ($0 == "") next
\t\t}
\t\tif ($0 ~ /^name:/) { sub(/^name:[ \t]*/, "", $0); name=trim($0) }
\t\telse if ($0 ~ /^type:/) { sub(/^type:[ \t]*/, "", $0); type=trim($0) }
\t\telse if ($0 ~ /^server:/) { sub(/^server:[ \t]*/, "", $0); server=trim($0) }
\t}
\tEND { if (name != "") { if(!first) printf ","; printf("  {\\042name\\042:\\042%s\\042,\\042type\\042:\\042%s\\042,\\042server\\042:\\042%s\\042}", name, type, server) }; print "]" }
\t\' "$config_path")
\t
\tlocal count=$(printf '%s' "$nodes" | grep -c '"name"')
\tlogger -t mihomo "get_proxies: parsed $count node(s) from $config_path"
\t
\tif [ "$nodes" = "[]" ] || [ "$nodes" = "[
]" ]; then
\t\tif grep -q "proxies: \\[\\]" "$config_path"; then
\t\t\techo "{\"error\":\"no_nodes\", \"msg\":\"订阅更新成功，但服务器返回了空的节点列表（已过滤 Hysteria2 等不兼容节点，或订阅已过期）。\"}"
\t\telse
\t\t\techo "{\"error\":\"parse_failed\", \"msg\":\"未能解析出任何代理节点，请确认订阅内容是否为合法的 Clash/Mihomo 配置。\"}"
\t\tfi
\telse
\t\techo "$nodes"
\tfi
}

case "$1" in
\tget_arch)
\t\tget_arch
\t\t;;
\tcheck_core)
\t\tcheck_core
\t\t;;
\tdownload_core)
\t\tdownload_core "$2"
\t\t;;
\tupdate_subscription)
\t\tupdate_subscription "$2"
\t\t;;
\tprepare_config)
\t\tprepare_config
\t\t;;
\tget_proxies)
\t\tget_proxies
\t\t;;
\tget_proxy_groups)
\t\tget_proxy_groups
\t\t;;
\tselect_node)
\t\tselect_node "$2" "$3"
\t\t;;
\t*)
\t\techo "Usage: $0 {get_arch|check_core|download_core|update_subscription|prepare_config|get_proxies}"
\t\texit 1
\t\t;;
esac
""",

    # LuCI Menu definition (JSON)
    "root/usr/share/luci/menu.d/luci-app-mihomo.json": """{
    "admin/services/mihomo": {
        "title": "Mihomo 代理",
        "order": 50,
        "action": {
            "type": "firstchild"
        }
    },
    "admin/services/mihomo/dashboard": {
        "title": "运行状态",
        "order": 1,
        "action": {
            "type": "view",
            "path": "mihomo/dashboard"
        }
    },
    "admin/services/mihomo/settings": {
        "title": "服务设置",
        "order": 2,
        "action": {
            "type": "view",
            "path": "mihomo/settings"
        }
    }
}
""",

    # RPCD ACL Permissions for Web UI execution
    "root/usr/share/rpcd/acl.d/luci-app-mihomo.json": """{
	"luci-app-mihomo": {
		"description": "Grant access to Mihomo config, services and helpers",
		"read": {
			"uci": [ "mihomo" ],
			"ubus": {
				"service": [ "list" ]
			}
		},
		"write": {
			"uci": [ "mihomo" ],
			"ubus": {
				"service": [ "restart", "state", "list" ]
			},
			"file": {
				"/usr/share/mihomo/helper.sh": [ "exec" ],
				"/sbin/logread": [ "exec" ],
				"/etc/init.d/mihomo": [ "exec" ]
			}
		}
	}
}
""",

    # Frontend View - Status Dashboard (JavaScript)
    "root/www/luci-static/resources/view/mihomo/dashboard.js": """'use strict';
'require view';
'require ui';
'require fs';
'require rpc';
'require uci';

return view.extend({
\tload: function() {
\t\treturn uci.load('mihomo').then(function() {
\t\t\treturn Promise.all([
\t\t\t\tfs.exec('/usr/share/mihomo/helper.sh', ['check_core']).catch(function() { return { stdout: '' }; }),
\t\t\t\tfs.exec('/sbin/logread', ['-e', 'mihomo']).catch(function() { return { stdout: '' }; }),
\t\t\t\trpc.declare({
\t\t\t\t\tobject: 'service',
\t\t\t\t\tmethod: 'list',
\t\t\t\t\tparams: [ 'name' ]
\t\t\t\t})({ name: 'mihomo' }).catch(function() { return {}; }),
\t\t\t\tfs.exec('/usr/share/mihomo/helper.sh', ['get_proxies']).catch(function() { return { stdout: '[]' }; }),
\t\t\t\tfs.exec('/usr/share/mihomo/helper.sh', ['get_proxy_groups']).catch(function() { return { stdout: '{"proxies":{}}' }; })
\t\t\t]);
\t\t});
\t},

\trender: function(results) {
\t\tvar core_status = (results[0] && results[0].stdout) ? results[0].stdout.trim() : '';
\t\tvar logs = (results[1] && results[1].stdout) ? results[1].stdout.trim() : '';
\t\tlogs = logs || _('暂无日志记录。');
\t\tvar service_data = results[2];
\t\tvar proxy_data_raw = (results[3] && results[3].stdout) ? results[3].stdout.trim() : '[]';
\t\tvar proxy_groups_raw = (results[4] && results[4].stdout) ? results[4].stdout.trim() : '{"proxies":{}}';
\t\t
\t\tvar proxies = [];
\t\tvar parse_error = null;
\t\ttry {
\t\t\tvar parsed = JSON.parse(proxy_data_raw);
\t\t\tif (parsed && parsed.error) {
\t\t\t\tparse_error = parsed.msg;
\t\t\t} else {
\t\t\t\tproxies = parsed;
\t\t\t}
\t\t} catch(e) {
\t\t\tproxies = [];
\t\t\tparse_error = _('本地配置文件数据损坏或解析失败。');
\t\t}

\t\tvar proxy_groups = {};
\t\ttry {
\t\t\tproxy_groups = JSON.parse(proxy_groups_raw).proxies || {};
\t\t} catch(e) {
\t\t\tproxy_groups = {};
\t\t}

\t\tvar is_running = false;
\t\tif (service_data && service_data.mihomo && service_data.mihomo.instances) {
\t\t\tvar instances = service_data.mihomo.instances;
\t\t\tfor (var key in instances) {
\t\t\t\tif (instances[key].running) {
\t\t\t\t\tis_running = true;
\t\t\t\t\tbreak;
\t\t\t\t}
\t\t\t}
\t\t}

\t\tvar is_installed = core_status.indexOf('installed:') === 0;
\t\tvar core_ver = _('未安装');
\t\tif (is_installed) {
\t\t\tcore_ver = core_status.split(':')[1];
\t\t}

\t\tvar core_path = uci.get('mihomo', 'config', 'core_path') || '/usr/bin/mihomo';

\t\tvar status_badge = is_running 
\t\t\t? '<span class="label success" style="background-color: #2ed573; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold;">RUNNING</span>'
\t\t\t: '<span class="label danger" style="background-color: #ff4757; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold;">STOPPED</span>';

\t\tvar perform_download = function(ev) {
\t\t\tev.preventDefault();
\t\t\tvar url_input = document.getElementById('core_download_url');
\t\t\tvar url = url_input ? url_input.value.trim() : '';
\t\t\t
\t\t\tvar close_btn = E('button', {
\t\t\t\t'class': 'cbi-button cbi-button-neutral',
\t\t\t\t'style': 'display: none; margin-top: 15px;',
\t\t\t\t'click': function() {
\t\t\t\t\tui.hideModal();
\t\t\t\t\tlocation.reload();
\t\t\t\t}
\t\t\t}, _('关闭'));

\t\t\tui.showModal(_('正在下载核心'), [
\t\t\t\tE('p', {}, _('正在下载 Mihomo 核心二进制文件... 这可能需要一些时间。')),
\t\t\t\tE('pre', { 'id': 'download_log', 'style': 'max-height: 200px; overflow-y: auto; background: #222; color: #fff; padding: 10px; border-radius: 4px; font-family: monospace;' }, _('开始下载...\\n')),
\t\t\t\tE('div', { 'class': 'right' }, [close_btn])
\t\t\t]);

\t\t\tvar args = ['download_core'];
\t\t\tif (url) {
\t\t\t\targs.push(url);
\t\t\t}

\t\t\treturn fs.exec('/usr/share/mihomo/helper.sh', args).then(function(res) {
\t\t\t\tvar pre = document.getElementById('download_log');
\t\t\t\tif (pre) {
\t\t\t\t\tpre.textContent += (res.stdout || '') + (res.stderr ? '\\n' + res.stderr : '');
\t\t\t\t}
\t\t\t\tclose_btn.style.display = 'inline-block';
\t\t\t}).catch(function(err) {
\t\t\t\tvar pre = document.getElementById('download_log');
\t\t\t\tif (pre) {
\t\t\t\t\tpre.textContent += '\\nERROR: ' + err.message;
\t\t\t\t}
\t\t\t\tclose_btn.style.display = 'inline-block';
\t\t\t});
\t\t};

\t\tvar manager_fields = [
\t\t\tE('label', { 'class': 'cbi-value-title' }, _('自定义下载地址 (选填)')),
\t\t\tE('div', { 'class': 'cbi-value-field' }, [
\t\t\t\tE('input', {
\t\t\t\t\t'id': 'core_download_url',
\t\t\t\t\t'type': 'text',
\t\t\t\t\t'class': 'cbi-input-text',
\t\t\t\t\t'placeholder': '留空则默认使用 GitHub 官方源下载',
\t\t\t\t\t'style': 'width: 60%;'
\t\t\t\t})
\t\t\t])
\t\t];
\t\t
\t\tvar download_btn_field = E('div', { 'class': 'cbi-value' }, [
\t\t\tE('div', { 'class': 'cbi-value-field' }, [
\t\t\t\tE('button', {
\t\t\t\t\t'class': 'cbi-button cbi-button-action',
\t\t\t\t\t'click': perform_download
\t\t\t\t}, _('下载并安装核心'))
\t\t\t])
\t\t]);

\t\tvar core_manager_body;
\t\tif (is_installed) {
\t\t\tvar download_container = E('div', { 'style': 'display: none; margin-top: 15px; border-top: 1px dashed rgba(0,0,0,0.1); padding-top: 15px;' }, [
\t\t\t\tE('div', { 'class': 'cbi-value' }, manager_fields),
\t\t\t\tdownload_btn_field
\t\t\t]);

\t\t\tcore_manager_body = E('div', {}, [
\t\t\t\tE('table', { 'class': 'table' }, [
\t\t\t\t\tE('tr', {}, [
\t\t\t\t\t\tE('td', { 'width': '33%' }, _('安装状态')),
\t\t\t\t\t\tE('td', {}, '<span style="color: #2ed573; font-weight: bold;">✔ 已启用并部署</span>')
\t\t\t\t\t]),
\t\t\t\t\tE('tr', {}, [
\t\t\t\t\t\tE('td', {}, _('安装路径')),
\t\t\t\t\t\tE('td', {}, E('code', {}, core_path))
\t\t\t\t\t]),
\t\t\t\t\tE('tr', {}, [
\t\t\t\t\t\tE('td', {}, _('核心版本')),
\t\t\t\t\t\tE('td', {}, E('strong', {}, core_ver))
\t\t\t\t\t])
\t\t\t\t]),
\t\t\t\tE('div', { 'style': 'margin-top: 15px;' }, [
\t\t\t\t\tE('button', {
\t\t\t\t\t\t'class': 'cbi-button cbi-button-neutral',
\t\t\t\t\t\t'click': function(ev) {
\t\t\t\t\t\t\tev.preventDefault();
\t\t\t\t\t	\tif (download_container.style.display === 'none') {
\t\t\t\t\t		\t\tdownload_container.style.display = 'block';
\t\t\t\t\t		\t\tev.target.textContent = _('收起更新选项');
\t\t\t\t\t		\t} else {
\t\t\t\t\t		\t\tdownload_container.style.display = 'none';
\t\t\t\t\t		\t\tev.target.textContent = _('更新/重新安装核心');
\t\t\t\t\t		\t}
\t\t\t\t\t	}
\t\t\t\t\t}, _('更新/重新安装核心')),
\t\t\t\t\tdownload_container
\t\t\t\t])
\t\t\t]);
\t\t} else {
\t\t\tcore_manager_body = E('div', {}, [
\t\t\t\tE('div', { 'style': 'padding: 10px 15px; background: rgba(255, 71, 87, 0.1); border-left: 4px solid #ff4757; color: #ff4757; font-weight: bold; border-radius: 4px; margin-bottom: 15px;' }, 
\t\t\t\t\t_('⚠️ 未检测到 Mihomo 运行核心，请在下方点击下载安装。')
\t\t\t\t),
\t\t\t\tE('div', { 'class': 'cbi-value' }, manager_fields),
\t\t\t\tdownload_btn_field
\t\t\t]);
\t\t}

\t\tvar group_rows = [];
\t\tvar group_names = Object.keys(proxy_groups);
\t\tvar selector_groups_count = 0;

\t\tfor (var i = 0; i < group_names.length; i++) {
\t\t\tvar gname = group_names[i];
\t\t\tvar g = proxy_groups[gname];
\t\t\tif (g && g.type === 'Selector') {
\t\t\t\tselector_groups_count++;
\t\t\t\t
\t\t\t\tvar options = [];
\t\t\t\tfor (var j = 0; j < g.all.length; j++) {
\t\t\t\t\tvar nname = g.all[j];
\t\t\t\t\toptions.push(E('option', {
\t\t\t\t\t\t'value': nname,
\t\t\t\t\t\t'selected': (nname === g.now) ? 'selected' : null
\t\t\t\t\t}, nname));
\t\t\t\t}

\t\t\t\tvar select_el = E('select', {
\t\t\t\t\t'class': 'cbi-input-select',
\t\t\t\t\t'style': 'width: 100%; max-width: 280px; padding: 4px; border-radius: 4px; border: 1px solid rgba(0,0,0,0.15); background: white;',
\t\t\t\t\t'data-group': gname,
\t\t\t\t\t'change': function(ev) {
\t\t\t\t\t\tvar group = ev.target.getAttribute('data-group');
\t\t\t\t\t\tvar node = ev.target.value;
\t\t\t\t\t\t
\t\t\t\t\t\tui.addNotification(null, E('p', _('正在切换节点：') + group + ' ➡ ' + node), 'info');
\t\t\t\t\t\t
\t\t\t\t\t\treturn fs.exec('/usr/share/mihomo/helper.sh', ['select_node', group, node]).then(function(res) {
\t\t\t\t\t\t\tif (res.code === 0) {
\t\t\t\t\t\t\t\tui.addNotification(null, E('p', _('节点切换成功！')), 'info');
\t\t\t\t\t\t\t} else {
\t\t\t\t\t\t\t\tui.addNotification(null, E('p', _('节点切换失败：') + (res.stderr || res.stdout || '')), 'danger');
\t\t\t\t\t\t\t}
\t\t\t\t\t\t}).catch(function(err) {
\t\t\t\t\t\t\tui.addNotification(null, E('p', _('通信错误：') + err.message), 'danger');
\t\t\t\t\t\t});
\t\t\t\t\t}
\t\t\t\t}, options);

\t\t\t\tgroup_rows.push(E('tr', {}, [
\t\t\t\t\tE('td', { 'style': 'font-weight: bold; vertical-align: middle; padding: 8px;' }, gname),
\t\t\t\t\tE('td', { 'style': 'vertical-align: middle; padding: 8px;' }, E('span', { 'class': 'label info', 'style': 'background-color: #17a2b8; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px;' }, g.type.toUpperCase())),
\t\t\t\t\tE('td', { 'style': 'vertical-align: middle; padding: 8px;' }, select_el)
\t\t\t\t]));
\t\t\t}
\t\t}

\t\tvar proxy_groups_panel;
\t\tif (is_running && selector_groups_count > 0) {
\t\t\tproxy_groups_panel = E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 15px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [
\t\t\t\tE('h3', { 'style': 'margin-top: 0; margin-bottom: 15px; border-bottom: 1px solid rgba(0,0,0,0.06); padding-bottom: 8px;' }, _('分流策略组管理 (实时切换节点)')),
\t\t\t\tE('table', { 'class': 'table', 'style': 'margin: 0;' }, [
\t\t\t\t\tE('thead', {}, [
\t\t\t\t\t\tE('tr', {}, [
\t\t\t\t\t\t\tE('th', { 'width': '40%', 'style': 'background: rgba(0,0,0,0.02);' }, _('策略组名称')),
\t\t\t\t\t\t\tE('th', { 'width': '20%', 'style': 'background: rgba(0,0,0,0.02);' }, _('类型')),
\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('选择节点'))
\t\t\t\t\t\t])
\t\t\t\t\t]),
\t\t\t\t\tE('tbody', {}, group_rows)
\t\t\t\t])
\t\t\t]);
\t\t} else {
\t\t\tproxy_groups_panel = E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 15px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [
\t\t\t\tE('h3', { 'style': 'margin-top: 0; margin-bottom: 10px;' }, _('分流策略组管理')),
\t\t\t\tE('div', { 'style': 'padding: 10px; text-align: center; color: #ff4757; background: rgba(255, 71, 87, 0.05); border-radius: 4px; font-weight: bold;' }, 
\t\t\t\t\t_('提示：Mihomo 服务未运行，无法进行实时策略组切换。请先启动服务。')
\t\t\t\t)
\t\t\t]);
\t\t}

\t\tvar node_rows = [];
\t\tvar valid_node_count = 0;
\t\tfor (var i = 0; i < proxies.length; i++) {
\t\t\tvar p = proxies[i];
\t\t\tif (p && p.name && p.type) {
\t\t\t\tvalid_node_count++;
\t\t\t\tnode_rows.push(E('tr', {}, [
\t\t\t\t\tE('td', {}, E('strong', {}, p.name)),
\t\t\t\t\tE('td', {}, E('span', { 'class': 'label info', 'style': 'background-color: #17a2b8; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px;' }, p.type.toUpperCase())),
\t\t\t\t\tE('td', {}, E('code', {}, p.server))
\t\t\t\t]));
\t\t\t}
\t\t}

\t\tvar sub_url = uci.get('mihomo', 'config', 'subscription_url') || '';
\t\tvar node_list_body;
\t\tif (valid_node_count > 0) {
\t\t\tnode_list_body = E('div', { 'style': 'max-height: 300px; overflow-y: auto; border: 1px solid rgba(0,0,0,0.08); border-radius: 6px; margin-top: 10px;' }, [
\t\t\t\tE('table', { 'class': 'table', 'style': 'margin: 0;' }, [
\t\t\t\t\tE('thead', {}, [
\t\t\t\t\t\tE('tr', {}, [
\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0; z-index: 1;' }, _('节点名称')),
\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0; z-index: 1;' }, _('节点类型')),
\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0; z-index: 1;' }, _('服务器地址'))
\t\t\t\t\t\t])
\t\t\t\t\t]),
\t\t\t\t\tE('tbody', {}, node_rows)
\t\t\t\t])
\t\t\t]);
\t\t} else if (parse_error) {
\t\t\tvar retry_update_btn = E('button', {
\t\t\t\t'class': 'cbi-button cbi-button-action',
\t\t\t\t'style': 'margin-top: 10px;',
\t\t\t\t'click': function(ev) {
\t\t\t\t\tev.preventDefault();
\t\t\t\t\tui.showModal(_('正在下载订阅配置'), [
\t\t\t\t\t\tE('p', {}, _('正在从订阅链接下载节点和规则配置... 请稍候。'))
\t\t\t\t\t]);
\t\t\t\t\treturn fs.exec('/usr/share/mihomo/helper.sh', ['update_subscription', sub_url]).then(function(res) {
\t\t\t\t\t\tui.hideModal();
\t\t\t\t\t\tif (res.code === 0) {
\t\t\t\t\t\t\tui.addNotification(null, E('p', _('订阅配置已成功更新！')), 'info');
\t\t\t\t\t\t\tlocation.reload();
\t\t\t\t\t\t} else {
\t\t\t\t\t\t\tui.addNotification(null, E('p', _('更新配置失败：') + (res.stderr || res.stdout || '')), 'danger');
\t\t\t\t\t\t}
\t\t\t\t\t}).catch(function(err) {
\t\t\t\t\t\tui.hideModal();
\t\t\t\t\t\tui.addNotification(null, E('p', _('下载订阅失败：') + err.message), 'danger');
\t\t\t\t\t});
\t\t\t\t}
\t\t\t}, _('重新更新订阅'));
\t\t\tnode_list_body = E('div', { 'style': 'padding: 20px; text-align: center; color: #ff4757; background: rgba(255, 71, 87, 0.05); border-radius: 6px; border: 1px dashed #ff4757; line-height: 1.6;' }, [
\t\t\t\tE('p', { 'style': 'font-weight: bold; margin: 0;' }, parse_error),
\t\t\t\tretry_update_btn
\t\t\t]);
\t\t} else if (sub_url) {
\t\t\tvar quick_update_btn = E('button', {
\t\t\t\t'class': 'cbi-button cbi-button-action',
\t\t\t\t'style': 'margin-top: 10px;',
\t\t\t\t'click': function(ev) {
\t\t\t\t\tev.preventDefault();
\t\t\t\t\tui.showModal(_('正在下载订阅配置'), [
\t\t\t\t\t\tE('p', {}, _('正在从订阅链接下载节点和规则配置... 请稍候。'))
\t\t\t\t\t]);
\t\t\t\t\treturn fs.exec('/usr/share/mihomo/helper.sh', ['update_subscription', sub_url]).then(function(res) {
\t\t\t\t\t\tui.hideModal();
\t\t\t\t\t\tif (res.code === 0) {
\t\t\t\t\t\t\tui.addNotification(null, E('p', _('订阅配置已成功更新！')), 'info');
\t\t\t\t\t\t\tlocation.reload();
\t\t\t\t\t\t} else {
\t\t\t\t\t\t\tui.addNotification(null, E('p', _('更新配置失败：') + (res.stderr || res.stdout || '')), 'danger');
\t\t\t\t\t\t}
\t\t\t\t\t}).catch(function(err) {
\t\t\t\t\t\tui.hideModal();
\t\t\t\t\t\tui.addNotification(null, E('p', _('下载订阅失败：') + err.message), 'danger');
\t\t\t\t\t});
\t\t\t\t}
\t\t\t}, _('立即更新订阅'));
\t\t\tnode_list_body = E('div', { 'style': 'padding: 20px; text-align: center; color: #ff9f43; background: rgba(255, 159, 67, 0.05); border-radius: 6px; border: 1px dashed #ff9f43;' }, [
\t\t\t\tE('p', { 'style': 'font-weight: bold; margin: 0;' }, _('⚠️ 已配置订阅链接，但本地尚未下载节点数据。')),
\t\t\t\tquick_update_btn
\t\t\t]);
\t\t} else {
\t\t\tnode_list_body = E('div', { 'style': 'padding: 15px; text-align: center; color: #999;' }, _('暂无可用节点信息，请先输入订阅链接并点击立即更新订阅。'));
\t\t}

\t\tvar view_html = E('div', { 'class': 'cbi-map' }, [
\t\t\tE('h2', {}, _('Mihomo 代理仪表盘')),
\t\t\tE('p', {}, _('管理 Mihomo 核心守护进程，监控运行状态并选择代理节点。')),

\t\t\t// Status panel
\t\t\tE('div', { 'class': 'cbi-section' }, [
\t\t\t\tE('h3', {}, _('服务运行状态')),
\t\t\t\tE('table', { 'class': 'table' }, [
\t\t\t\t\tE('tr', {}, [
\t\t\t\t\t\tE('td', { 'width': '33%' }, _('守护进程状态')),
\t\t\t\t\t\tE('td', {}, status_badge)
\t\t\t\t\t]),
\t\t\t\t\tE('tr', {}, [
\t\t\t\t\t\tE('td', {}, _('已安装核心版本')),
\t\t\t\t\t\tE('td', {}, E('strong', {}, core_ver))
\t\t\t\t\t])
\t\t\t\t]),
\t\t\t\t
\t\t\t\tE('div', { 'class': 'cbi-section-node' }, [
\t\t\t\t\tE('button', {
\t\t\t\t\t	'class': 'cbi-button cbi-button-apply',
\t\t\t\t\t	'style': 'margin-right: 10px;',
\t\t\t\t\t	'click': function(ev) {
\t\t\t\t\t		ev.preventDefault();
\t\t\t\t\t		return fs.exec('/etc/init.d/mihomo', ['start']).then(function() {
\t\t\t\t\t			location.reload();
\t\t\t\t\t		});
\t\t\t\t		}
\t\t\t\t	}, _('启动')),
\t\t\t\t\tE('button', {
\t\t\t\t\t	'class': 'cbi-button cbi-button-reset',
\t\t\t\t		'style': 'margin-right: 10px;',
\t\t\t\t		'click': function(ev) {
\t\t\t\t\t		ev.preventDefault();
\t\t\t\t			return fs.exec('/etc/init.d/mihomo', ['stop']).then(function() {
\t\t\t\t				location.reload();
\t\t\t\t			});
\t\t\t\t		}
\t\t\t\t	}, _('停止')),
\t\t\t\t\tE('button', {
\t\t\t\t\t	'class': 'cbi-button cbi-button-action',
\t\t\t\t		'click': function(ev) {
\t\t\t\t\t		ev.preventDefault();
\t\t\t\t			return fs.exec('/etc/init.d/mihomo', ['restart']).then(function() {
\t\t\t\t				location.reload();
\t\t\t\t			});
\t\t\t\t		}
\t\t\t\t	}, _('重启'))
\t\t\t\t])
\t\t\t]),

\t\t\t// Proxy groups switching panel
\t\t\tproxy_groups_panel,

\t\t\t// Nodes list panel
\t\t\tE('div', { 'class': 'cbi-section' }, [
\t\t\t\tE('h3', {}, _('配置订阅节点列表')),
\t\t\t\tnode_list_body
\t\t\t]),

\t\t\t// Core Management panel
\t\t\tE('div', { 'class': 'cbi-section' }, [
\t\t\t\tE('h3', {}, _('核心程序管理')),
\t\t\t\tcore_manager_body
\t\t\t]),

\t\t\t// Logs panel
\t\t\tE('div', { 'class': 'cbi-section' }, [
\t\t\t\tE('h3', {}, _('系统代理日志')),
\t\t\t\tE('textarea', {
\t\t\t\t\t'style': 'width: 100%; height: 250px; font-family: monospace; padding: 12px; border-radius: 6px; border: 1px solid rgba(0,0,0,0.12); background: rgba(0,0,0,0.02); resize: vertical; margin-bottom: 12px; font-size: 13px; line-height: 1.5;',
\t\t\t\t\t'readonly': 'readonly'
\t\t\t\t}, logs)
\t\t\t])
\t\t]);

\t\treturn view_html;
\t}
});
""",
    "root/www/luci-static/resources/view/mihomo/settings.js": """'use strict';
'require view';
'require form';
'require ui';
'require fs';

return view.extend({
\trender: function() {
\t\tvar m, s, o;

\t\tm = new form.Map('mihomo', _('Mihomo 代理设置'),
\t\t\t_('配置代理服务参数、DNS 解析器和订阅节点信息。'));
\t\t
\t\tm.restart = 'mihomo';

\t\ts = m.section(form.TypedSection, 'mihomo', _('常规设置'));
\t\ts.anonymous = true;

\t\to = s.option(form.Flag, 'enabled', _('启用服务'), _('开启或关闭 Mihomo 路由代理服务。'));
\t\to.rmempty = false;

\t\to = s.option(form.Value, 'subscription_url', _('订阅链接'), _('用于下载节点配置的 Clash 兼容订阅链接。'));
\t\to.rmempty = true;

\t\t// 订阅管理按钮，直接放在订阅链接下方
\t\to = s.option(form.DummyValue, '_update_btn', _('订阅管理'));
\t\to.rawhtml = true;
\t\to.cfgvalue = function(section_id) {
\t\t\tvar update_btn = E('button', {
\t\t\t\t'class': 'cbi-button cbi-button-action',
\t\t\t\t'click': function(ev) {
\t\t\t\t\tev.preventDefault();
\t\t\t\t\tvar url_input = document.getElementById('cbid.mihomo.' + section_id + '.subscription_url');
\t\t\t\t\tvar url = url_input ? url_input.value.trim() : '';
					if (!url) {
						url = uci.get('mihomo', section_id, 'subscription_url') || '';
					}
\t\t\t\t\t
\t\t\t\t\tif (!url) {
\t\t\t\t\t\tui.addNotification(null, E('p', _('请先输入有效的订阅链接并点击保存！')), 'warning');
\t\t\t\t\t\treturn;
\t\t\t\t\t}

\t\t\t\t\t// 将订阅链接缓存到 UCI，避免刷新或跳转后丢失
\t\t\t\t\tuci.set('mihomo', section_id, 'subscription_url', url).then(function() {
\t\t\t\t\t\treturn uci.commit('mihomo');
\t\t\t\t\t}).catch(function() {});

\t\t\t\t\tui.showModal(_('正在下载订阅配置'), [
\t\t\t\t\t\tE('p', {}, _('正在从订阅链接下载节点和规则配置... 请稍候。'))
\t\t\t\t\t]);

\t\t\t\t\treturn fs.exec('/usr/share/mihomo/helper.sh', ['update_subscription', url]).then(function(res) {
\t\t\t\t\t\tui.hideModal();
\t\t\t\t\t\tif (res.code === 0) {
\t\t\t\t\t\t\tui.addNotification(null, E('p', _('订阅配置已成功更新！')), 'info');
\t\t\t\t\t\t} else {
\t\t\t\t\t\t\tui.addNotification(null, E('p', _('更新配置失败：') + (res.stderr || res.stdout || '')), 'danger');
\t\t\t\t\t\t}
\t\t\t\t\t}).catch(function(err) {
\t\t\t\t\t\tui.hideModal();
\t\t\t\t\t\tui.addNotification(null, E('p', _('下载订阅失败：') + err.message), 'danger');
\t\t\t\t\t});
\t\t\t\t}
\t\t\t}, _('立即更新订阅'));
\t\t\t
\t\t\treturn E('div', {}, [update_btn]);
\t\t};

\t\to = s.option(form.Flag, 'tun_enabled', _('启用 TUN 模式'), _('使用虚拟网卡 (TUN) 接口进行全局流量接管。接管更彻底但会消耗略高 CPU。'));
\t\to.rmempty = false;

\t\to = s.option(form.Flag, 'dns_hijack', _('劫持系统 DNS'), _('将本地所有 DNS 请求劫持并转发给 Mihomo 内置的 DNS 服务。'));
\t\to.rmempty = false;

\t\t// Advanced Section
\t\ts = m.section(form.TypedSection, 'mihomo', _('高级设置'));
\t\ts.anonymous = true;

\t\to = s.option(form.Value, 'core_path', _('核心程序路径'), _('Mihomo 核心程序的可执行文件绝对路径。'));
\t\to.placeholder = '/usr/bin/mihomo';
\t\to.rmempty = false;

\t\to = s.option(form.Value, 'config_path', _('订阅配置文件路径'), _('保存订阅节点和分流规则的 YAML 配置文件路径。'));
\t\to.placeholder = '/etc/mihomo/config.yaml';
\t\to.rmempty = false;

\t\to = s.option(form.Value, 'work_dir', _('工作目录'), _('Mihomo (Clash Meta) 工作数据库与配置根目录。'));
\t\to.placeholder = '/etc/mihomo';
\t\to.rmempty = false;

\t\to = s.option(form.Value, 'mix_port', _('Mixed 端口'), _('集成 HTTP(S) 和 SOCKS5 的混合代理端口。'));
\t\to.placeholder = '7890';
\t\to.rmempty = false;

\t\to = s.option(form.Value, 'tproxy_port', _('TProxy 端口'), _('TCP/UDP 透明代理使用的 TProxy 监听端口。'));
\t\to.placeholder = '7893';
\t\to.rmempty = false;

\t\to = s.option(form.Value, 'dns_port', _('DNS 端口'), _('Mihomo 本地 DNS 解析器监听端口。'));
\t\to.placeholder = '1053';
\t\to.rmempty = false;

\t\treturn m.render();
\t}
});
"""
}

def create_source_tree(src_dir):
    """Writes all source files into the specified directory to allow user editing."""
    print(f"Creating source tree in '{src_dir}'...")
    if os.path.exists(src_dir):
        shutil.rmtree(src_dir)
        
    for rel_path, content in src_files.items():
        full_path = os.path.join(src_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        
        # Override the version in CONTROL/control dynamically
        if rel_path == "CONTROL/control":
            import re
            content = re.sub(r'Version:\s*.*', f'Version: {PKG_VERSION}', content)
            
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        # Ensure scripts are executable locally
        if ("CONTROL/" in rel_path and rel_path != "CONTROL/control") or "etc/init.d/" in rel_path or "usr/share/mihomo/helper.sh" in rel_path:
            os.chmod(full_path, 0o755)
    print("Source tree created successfully.")

def make_tar_gz(source_dir, output_filename, is_control=False):
    """Generates a reproducible tar.gz archive with root:root ownership and correct modes, including directories and using './' prefix."""
    print(f"Archiving '{source_dir}' -> '{output_filename}'...")
    with tarfile.open(output_filename, "w:gz") as tar:
        all_entries = []
        for root, dirs, files in os.walk(source_dir):
            for d in dirs:
                full_path = os.path.join(root, d)
                rel_path = os.path.relpath(full_path, source_dir)
                all_entries.append((rel_path, full_path, True))
            for f in files:
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, source_dir)
                all_entries.append((rel_path, full_path, False))
                
        all_entries.sort(key=lambda x: x[0])
        
        root_tarinfo = tarfile.TarInfo(name=".")
        root_tarinfo.type = tarfile.DIRTYPE
        root_tarinfo.mode = 0o755
        root_tarinfo.uid = 0
        root_tarinfo.gid = 0
        root_tarinfo.uname = "root"
        root_tarinfo.gname = "root"
        root_tarinfo.mtime = 1700000000
        tar.addfile(root_tarinfo)
        
        for rel_path, full_path, is_dir in all_entries:
            arcname = "./" + rel_path
            tarinfo = tar.gettarinfo(full_path, arcname=arcname)
            tarinfo.uid = 0
            tarinfo.gid = 0
            tarinfo.uname = "root"
            tarinfo.gname = "root"
            tarinfo.mtime = 1700000000
            
            if is_dir:
                tarinfo.type = tarfile.DIRTYPE
                tarinfo.mode = 0o755
                tar.addfile(tarinfo)
            else:
                tarinfo.type = tarfile.REGTYPE
                if is_control:
                    if os.path.basename(full_path) in ["postinst", "postrm", "preinst", "prerm"]:
                        tarinfo.mode = 0o755
                    else:
                        tarinfo.mode = 0o644
                else:
                    if "etc/init.d/" in rel_path or "usr/share/mihomo/helper.sh" in rel_path:
                        tarinfo.mode = 0o755
                    else:
                        tarinfo.mode = 0o644
                        
                with open(full_path, "rb") as f:
                    tar.addfile(tarinfo, f)

def write_tar_gz_outer_archive(archive_path, file_list):
    """Writes the final .ipk as a gzipped tarball containing the three components."""
    print(f"Creating IPK archive (tar.gz format) '{archive_path}'...")
    with tarfile.open(archive_path, "w:gz") as tar:
        for name, data in file_list:
            arcname = "./" + name
            tarinfo = tarfile.TarInfo(name=arcname)
            tarinfo.size = len(data)
            tarinfo.uid = 0
            tarinfo.gid = 0
            tarinfo.uname = "root"
            tarinfo.gname = "root"
            tarinfo.mtime = 1700000000
            tarinfo.mode = 0o644
            tarinfo.type = tarfile.REGTYPE
            tar.addfile(tarinfo, io.BytesIO(data))

def increment_version():
    """Increments the PKG_VERSION in the script file dynamically and updates memory variables."""
    global PKG_VERSION, IPK_FILENAME
    
    script_path = __file__
    with open(script_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    import re
    match = re.search(r'PKG_VERSION\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        print("Warning: PKG_VERSION variable not found in script.")
        return
        
    current_ver = match.group(1)
    if '-' in current_ver:
        ver_part, rev_part = current_ver.rsplit('-', 1)
        try:
            new_rev = int(rev_part) + 1
            new_ver = f"{ver_part}-{new_rev}"
        except ValueError:
            new_ver = current_ver + ".1"
    else:
        parts = current_ver.split('.')
        try:
            parts[-1] = str(int(parts[-1]) + 1)
            new_ver = '.'.join(parts)
        except ValueError:
            new_ver = current_ver + "-1"
            
    # Replace in file content
    new_line = f'PKG_VERSION = "{new_ver}"'
    content = re.sub(r'PKG_VERSION\s*=\s*["\']([^"\']+)["\']', new_line, content, count=1)
    
    # Save back to script
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(content)
        
    print(f"Incremented version: {current_ver} -> {new_ver}")
    PKG_VERSION = new_ver
    IPK_FILENAME = f"{PKG_NAME}_{PKG_VERSION}_{PKG_ARCH}.ipk"

def main():
    # 1. Automatically increment the package version number
    increment_version()
    
    workspace = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(workspace, "src")
    build_dir = os.path.join(workspace, "build")
    dist_dir = os.path.join(workspace, "dist")
    
    # 2. Force recreate source tree on build update
    print("Initializing source tree for luci-app-mihomo...")
    create_source_tree(src_dir)
        
    # 3. Recreate build and dist directories
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir, exist_ok=True)
    os.makedirs(dist_dir, exist_ok=True)
    
    # 4. Create tarballs
    control_tar = os.path.join(build_dir, "control.tar.gz")
    data_tar = os.path.join(build_dir, "data.tar.gz")
    
    make_tar_gz(os.path.join(src_dir, "CONTROL"), control_tar, is_control=True)
    make_tar_gz(os.path.join(src_dir, "root"), data_tar, is_control=False)
    
    # 5. Read tarballs and write the final .ipk file
    with open(control_tar, "rb") as f:
        control_bytes = f.read()
    with open(data_tar, "rb") as f:
        data_bytes = f.read()
        
    debian_binary = b"2.0\n"
    
    file_list = [
        ("debian-binary", debian_binary),
        ("control.tar.gz", control_bytes),
        ("data.tar.gz", data_bytes)
    ]
    
    ipk_tar_gz_path = os.path.join(dist_dir, IPK_FILENAME)
    write_tar_gz_outer_archive(ipk_tar_gz_path, file_list)
    
    print("\nSUCCESS!")
    print(f"Packaged IPK file created at: {ipk_tar_gz_path}")

if __name__ == "__main__":
    main()
