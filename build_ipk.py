import os
import tarfile
import io
import time
import shutil

# Define configuration for the OpenClash replacement
PKG_NAME = "luci-app-mihomo"
PKG_VERSION = "1.0.0-48"
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

    # Mark UCI config as a conffile so user settings (notably subscription_url)
    # survive package upgrades instead of being overwritten by package defaults.
    "CONTROL/conffiles": """/etc/config/mihomo
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
	option test_url ''
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
\tlocal core_path config_path work_dir dns_port dns_hijack tproxy_port tun_enabled
	
\tconfig_get core_path config core_path "/usr/bin/mihomo"
\tconfig_get config_path config config_path "/etc/mihomo/config.yaml"
\tconfig_get work_dir config work_dir "/etc/mihomo"
\tconfig_get dns_port config dns_port "1053"
\tconfig_get_bool dns_hijack config dns_hijack 1
\tconfig_get tproxy_port config tproxy_port "7893"
\tconfig_get_bool tun_enabled config tun_enabled 0
	
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

\t# Background collector: persist connections to /tmp/mihomo_access.log for the
\t# access-log history view. No-ops when the core controller is unreachable.
\tprocd_open_instance
\tprocd_set_param command /bin/sh -c "/usr/share/mihomo/helper.sh collect_connections; while true; do /usr/share/mihomo/helper.sh collect_connections; sleep 15; done"
\tprocd_set_param stdout 1
\tprocd_set_param stderr 1
\tprocd_set_param respawn
\tprocd_close_instance

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

cpu_amd64_v3() {
\t# Go GOAMD64=v3 需要 AVX2 + BMI1 + BMI2 + FMA + F16C 等指令集；
\t# 缺少任一关键标志即视为非 v3，退回 amd64-compatible 兼容构建。
\tgrep -qw -m1 avx2 /proc/cpuinfo 2>/dev/null || return 1
\tgrep -qw -m1 bmi2 /proc/cpuinfo 2>/dev/null || return 1
\tgrep -qw -m1 bmi1 /proc/cpuinfo 2>/dev/null || return 1
\tgrep -qw -m1 fma  /proc/cpuinfo 2>/dev/null || return 1
\tgrep -qw -m1 f16c /proc/cpuinfo 2>/dev/null || return 1
\treturn 0
}

get_arch() {
\tlocal raw_arch=$(uname -m)
\tcase "$raw_arch" in
\t\tx86_64)
\t\t\tif cpu_amd64_v3; then
\t\t\t\techo "amd64"
\t\t\telse
\t\t\t\techo "amd64-compatible"
\t\t\tfi
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

# Emit controlled access rules (from UCI mihomo_rule) as YAML rule lines.
# Note: Mihomo rules are GLOBAL and cannot be scoped per source IP. The recorded
# src_ip is kept only for management/traceability; the rule applies to all devices.
emit_access_rules_yaml() {
\tuci show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=mihomo_rule$/\\1/p' | while read -r sid; do
\t\tlocal enabled domain action group
\t\tenabled=$(uci -q get mihomo.$sid.enabled)
\t\t[ "$enabled" = "1" ] || continue
\t\tdomain=$(uci -q get mihomo.$sid.domain)
\t\t[ -n "$domain" ] || continue
\t\taction=$(uci -q get mihomo.$sid.action)
\t\tcase "$action" in
\t\t\tblock) echo "  - 'DOMAIN-SUFFIX,$domain,REJECT'" ;;
\t\t\tdirect) echo "  - 'DOMAIN-SUFFIX,$domain,DIRECT'" ;;
\t\t\tproxy)
\t\t\t\tlocal g
\t\t\t\tg=$(uci -q get mihomo.$sid.group)
\t\t\t\t[ -n "$g" ] && echo "  - 'DOMAIN-SUFFIX,$domain,$g'"
\t\t\t\t;;
\t\tesac
\tdone
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

\t# Inject controlled access rules from UCI (highest priority, first-match).
\tlocal rules_file="${run_config}.rules"
\temit_access_rules_yaml > "$rules_file"
\tif [ -s "$rules_file" ]; then
\t\tif grep -q '^rules:' "$run_config"; then
\t\t\tlocal tmpf="${run_config}.rules2"
\t\t\tawk -v f="$rules_file" '
\t\t\t\tBEGIN { while ((getline line < f) > 0) buf = buf line "\\n" }
\t\t\t\t{ print }
\t\t\t\t/^rules:/ && !done { printf "%s", buf; done=1 }
\t\t\t' "$run_config" > "$tmpf" && mv "$tmpf" "$run_config"
\t\telse
\t\t\tprintf 'rules:\n' >> "$run_config"
\t\t\tcat "$rules_file" >> "$run_config"
\t\tfi
\t\tlogger -t mihomo "Prepared config with UCI access rules"
\tfi
\trm -f "$rules_file"

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

# ---------- 访问日志：实时连接 + 历史采集 + 规则管理 ----------

resolve_host() {
\tlocal ip="$1"
\tlocal leases="/tmp/dhcp.leases"
\t[ -z "$ip" ] && return 0
\t[ -f "$leases" ] || return 0
\task -v ip="$ip" '$3==ip { print $4; exit }' "$leases"
}

flatten_connections() {
\tlocal raw="$1"
\t[ -z "$raw" ] && return 0
\tlocal ids ips hosts dst policy rule up down start
\tids=$(echo "$raw" | jsonfilter -e '$.connections[@].id' 2>/dev/null)
\tips=$(echo "$raw" | jsonfilter -e '$.connections[@].metadata.sourceIP' 2>/dev/null)
\thosts=$(echo "$raw" | jsonfilter -e '$.connections[@].metadata.host' 2>/dev/null)
\tdst=$(echo "$raw" | jsonfilter -e '$.connections[@].metadata.destinationIP' 2>/dev/null)
\tpolicy=$(echo "$raw" | jsonfilter -e '$.connections[@].policy' 2>/dev/null)
\trule=$(echo "$raw" | jsonfilter -e '$.connections[@].rule' 2>/dev/null)
\tup=$(echo "$raw" | jsonfilter -e '$.connections[@].upload' 2>/dev/null)
\tdown=$(echo "$raw" | jsonfilter -e '$.connections[@].download' 2>/dev/null)
\tstart=$(echo "$raw" | jsonfilter -e '$.connections[@].start' 2>/dev/null)
\techo "$ids" | nl -ba | while read -r n id; do
\t\t[ -z "$id" ] && continue
\t\tlocal ip host d pol r u dn st
\t\tip=$(echo "$ips" | sed -n "${n}p")
\t\thost=$(echo "$hosts" | sed -n "${n}p")
\t\td=$(echo "$dst" | sed -n "${n}p")
\t\tpol=$(echo "$policy" | sed -n "${n}p")
\t\tr=$(echo "$rule" | sed -n "${n}p")
\t\tu=$(echo "$up" | sed -n "${n}p")
\t\tdn=$(echo "$down" | sed -n "${n}p")
\t\tst=$(echo "$start" | sed -n "${n}p")
\t\tlocal dev
\t\tdev=$(resolve_host "$ip")
\t\t[ -z "$host" ] && host="$d"
\t\techo "${id}|${ip}|${dev}|${host}|${d}|${pol}|${r}|${u}|${dn}|${st}"
\tdone
}

get_connections() {
\tlocal raw
\traw=$(curl -s --connect-timeout 2 http://127.0.0.1:9090/connections 2>/dev/null)
\tif [ -z "$raw" ]; then
\t\techo "{\\"error\\":\\"no_core\\", \\"msg\\":\\"无法连接 Mihomo 控制器 (9090)，请确认核心已启动。\\"}"
\t\treturn 0
\tfi
\techo "["
\tfirst=1
\tflatten_connections "$raw" | while IFS='|' read -r id ip dev host d pol r u dn st; do
\t\t[ -z "$id" ] && continue
\t\tif [ $first -eq 0 ]; then printf ','; fi
\t\tfirst=0
\t\tprintf '{"id":"%s","ip":"%s","device":"%s","domain":"%s","dst":"%s","policy":"%s","rule":"%s","up":%s,"down":%s,"start":"%s"}' "$id" "$ip" "$dev" "$host" "$d" "$pol" "$r" "${u:-0}" "${dn:-0}" "$st"
\tdone
\techo "]"
}

collect_connections() {
\tlocal raw logf seenf
\tlogf="/tmp/mihomo_access.log"
\tseenf="/tmp/mihomo_access.seen"
\traw=$(curl -s --connect-timeout 2 http://127.0.0.1:9090/connections 2>/dev/null)
\t[ -z "$raw" ] && return 0
\ttouch "$seenf"
\tflatten_connections "$raw" | while IFS='|' read -r id ip dev host d pol r u dn st; do
\t\t[ -z "$id" ] && continue
\t\tgrep -qxF "$id" "$seenf" && continue
\t\techo "$id" >> "$seenf"
\t\tlocal ts
\t\tts=$(date +%s)
\t\tprintf '{"ts":%s,"id":"%s","ip":"%s","device":"%s","domain":"%s","dst":"%s","policy":"%s","rule":"%s","up":%s,"down":%s,"start":"%s"}' "$ts" "$id" "$ip" "$dev" "$host" "$d" "$pol" "$r" "${u:-0}" "${dn:-0}" "$st" >> "$logf"
\tdone
\ttail -n 2000 "$seenf" > "$seenf.tmp" && mv "$seenf.tmp" "$seenf"
}

# URL-encode a string for use in an HTTP path (POSIX shell, no bashisms).
urlencode() {
\tlocal s="$1" out=""
\twhile [ -n "$s" ]; do
\t\tlocal c="${s%"${s#?}"}"
\t\tcase "$c" in
\t\t\t[a-zA-Z0-9.~_-]) out="${out}${c}" ;;
\t\t\t' ') out="${out}%20" ;;
\t\t\t*) out="${out}$(printf '%%%02X' "'$c")" ;;
\t\tesac
\t\ts="${s#?}"
\tdone
\techo "$out"
}

test_node_delay() {
\tlocal name="$1"
\t[ -z "$name" ] && { echo '{"delay":-1,"msg":"name required"}'; return 0; }
\t# 测试目标 URL：优先 UCI 配置，其次环境变量，最后默认。
\t# 某些网络环境下默认地址不可达会导致所有节点都显示失败，故开放为可配置项。
\tlocal test_url
\ttest_url=$(uci -q get mihomo.config.test_url)
\t[ -z "$test_url" ] && test_url="${MIHOMO_TEST_URL:-https://www.gstatic.com/generate_204}"
\tlocal timeout=5000
\tlocal enc url_enc body code
\tenc=$(urlencode "$name")
\turl_enc=$(urlencode "$test_url")
\tbody=$(mktemp)
\tcode=$(curl -s -o "$body" -w '%{http_code}' --connect-timeout 5 --max-time $((timeout / 1000 + 5)) "http://127.0.0.1:9090/proxies/${enc}/delay?url=${url_enc}&timeout=${timeout}" 2>/dev/null)
\tif [ -z "$code" ] || [ "$code" = "000" ]; then
\t\techo '{"delay":-1,"msg":"controller_unreachable"}'
\t\trm -f "$body"
\t\treturn 0
\tfi
\tcat "$body"
\trm -f "$body"
}

get_history() {
\tlocal logf="/tmp/mihomo_access.log"
\tlocal limit="${1:-200}"
\t[ -f "$logf" ] || { echo "[]"; return 0; }
\techo "["
\tfirst=1
\ttail -n "$limit" "$logf" | tac | while read -r line; do
\t\t[ -z "$line" ] && continue
\t\tif [ $first -eq 0 ]; then printf ','; fi
\t\tfirst=0
\t\tprintf '%s' "$line"
\tdone
\techo "]"
}

get_access_rules() {
\techo "["
\tfirst=1
\tlocal sids
\tsids=$(uci show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=mihomo_rule$/\\1/p')
\tfor sid in $sids; do
\t\tlocal ip domain action group enabled comment
\t\tip=$(uci -q get mihomo.$sid.src_ip)
\t\tdomain=$(uci -q get mihomo.$sid.domain)
\t\taction=$(uci -q get mihomo.$sid.action)
\t\tgroup=$(uci -q get mihomo.$sid.group)
\t\tenabled=$(uci -q get mihomo.$sid.enabled)
\t\tcomment=$(uci -q get mihomo.$sid.comment)
\t\t[ -z "$domain" ] && continue
\t\tif [ $first -eq 0 ]; then printf ','; fi
\t\tfirst=0
\t\tprintf '{"sid":"%s","ip":"%s","domain":"%s","action":"%s","group":"%s","enabled":"%s","comment":"%s"}' "$sid" "$ip" "$domain" "$action" "$group" "$enabled" "$comment"
\tdone
\techo "]"
}

add_access_rule() {
\tlocal ip="$1" domain="$2" action="$3" group="$4"
\t[ -z "$domain" ] && { echo "ERROR: domain required" >&2; return 1; }
\t[ -z "$action" ] && action="block"
\tlocal sid
\tsid=$(uci add mihomo mihomo_rule)
\tuci -q set mihomo.$sid.src_ip="$ip"
\tuci -q set mihomo.$sid.domain="$domain"
\tuci -q set mihomo.$sid.action="$action"
\t[ -n "$group" ] && uci -q set mihomo.$sid.group="$group"
\tuci -q set mihomo.$sid.enabled="1"
\tuci commit mihomo
\tlogger -t mihomo "access_rule added: ip=$ip domain=$domain action=$action"
\techo "OK"
}

del_access_rule() {
\tlocal sid="$1"
\t[ -z "$sid" ] && { echo "ERROR: sid required" >&2; return 1; }
\tuci -q delete mihomo.$sid
\tuci commit mihomo
\tlogger -t mihomo "access_rule deleted: $sid"
\techo "OK"
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
\ttest_node_delay)
\t\ttest_node_delay "$2"
\t\t;;
\tget_proxy_groups)
\t\tget_proxy_groups
\t\t;;
\tselect_node)
\t\tselect_node "$2" "$3"
\t\t;;
\tget_connections)
\t\tget_connections
\t\t;;
\tcollect_connections)
\t\tcollect_connections
\t\t;;
\tget_history)
\t\tget_history "$2"
\t\t;;
\tget_access_rules)
\t\tget_access_rules
\t\t;;
\tadd_access_rule)
\t\tadd_access_rule "$2" "$3" "$4" "$5"
\t\t;;
\tdel_access_rule)
\t\tdel_access_rule "$2"
\t\t;;
\t*)
\t\techo "Usage: $0 {get_arch|check_core|download_core|update_subscription|prepare_config|get_proxies|get_proxy_groups|select_node|get_connections|collect_connections|get_history|get_access_rules|add_access_rule|del_access_rule|test_node_delay}"
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
    },
    "admin/services/mihomo/accesslog": {
        "title": "访问日志",
        "order": 3,
        "action": {
            "type": "view",
            "path": "mihomo/accesslog"
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

    # Frontend View - Access Log (JavaScript)
    "root/www/luci-static/resources/view/mihomo/accesslog.js": """'use strict';
'require view';
'require ui';
'require fs';
'require rpc';
'require uci';

function esc(s) {
\treturn String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
\t\treturn { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
\t});
}

function fmt_time(ts) {
\tif (!ts) return '';
\ttry {
\t\tvar d = new Date(ts * 1000);
\t\treturn d.toLocaleString();
\t} catch (e) { return String(ts); }
}

function fmt_bytes(n) {
\tn = Number(n) || 0;
\tif (n < 1024) return n + ' B';
\tif (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
\tif (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
\treturn (n / 1073741824).toFixed(2) + ' GB';
}

return view.extend({
\tload: function() {
\t\treturn uci.load('mihomo').then(function() {
\t\t\treturn Promise.all([
\t\t\t\tfs.exec('/usr/share/mihomo/helper.sh', ['get_connections']).catch(function() { return { stdout: '[]' }; }),
\t\t\t\tfs.exec('/usr/share/mihomo/helper.sh', ['get_history', '300']).catch(function() { return { stdout: '[]' }; }),
\t\t\t\tfs.exec('/usr/share/mihomo/helper.sh', ['get_access_rules']).catch(function() { return { stdout: '[]' }; }),
\t\t\t\tfs.exec('/usr/share/mihomo/helper.sh', ['get_proxy_groups']).catch(function() { return { stdout: '{"proxies":{}}' }; })
\t\t\t]);
\t\t});
\t},

\trender: function(results) {
\t\tvar self = this;
\t\tif (self._timer) { clearInterval(self._timer); self._timer = null; }

\t\tvar conn_raw = (results[0] && results[0].stdout) ? results[0].stdout.trim() : '[]';
\t\tvar hist_raw = (results[1] && results[1].stdout) ? results[1].stdout.trim() : '[]';
\t\tvar rules_raw = (results[2] && results[2].stdout) ? results[2].stdout.trim() : '[]';
\t\tvar groups_raw = (results[3] && results[3].stdout) ? results[3].stdout.trim() : '{"proxies":{}}';

\t\tvar connections = [];
\t\tvar conn_error = null;
\t\ttry {
\t\t\tvar cj = JSON.parse(conn_raw);
\t\t\tif (cj && cj.error) conn_error = cj.msg;
\t\t\telse connections = cj;
\t\t} catch (e) { conn_error = _('无法解析实时连接数据。'); }

\t\tvar history = [];
\t\ttry { history = JSON.parse(hist_raw); } catch (e) { history = []; }

\t\tvar rules = [];
\t\ttry { rules = JSON.parse(rules_raw); } catch (e) { rules = []; }

\t\tvar proxy_groups = {};
\t\ttry { proxy_groups = JSON.parse(groups_raw).proxies || {}; } catch (e) { proxy_groups = {}; }

\t\tvar group_names = [];
\t\tfor (var gk in proxy_groups) {
\t\t\tif (proxy_groups[gk] && proxy_groups[gk].type === 'Selector') group_names.push(gk);
\t\t}

\t\tfunction add_rule(ip, domain, action, group) {
\t\t\tvar args = ['add_access_rule', ip || '', domain, action];
\t\t\tif (group) args.push(group);
\t\t\tui.addNotification(null, E('p', _('正在添加规则：') + esc(domain) + ' -> ' + action), 'info');
\t\t\treturn fs.exec('/usr/share/mihomo/helper.sh', args).then(function(res) {
\t\t\t\tif (res.code === 0) {
\t\t\t\t\tui.addNotification(null, E('p', _('规则已保存（需重启核心后生效）。')), 'info');
\t\t\t\t\trender_rules();
\t\t\t\t} else {
\t\t\t\t\tui.addNotification(null, E('p', _('添加失败：') + (res.stderr || res.stdout || '')), 'danger');
\t\t\t\t}
\t\t\t}).catch(function(err) {
\t\t\t\tui.addNotification(null, E('p', _('通信错误：') + err.message), 'danger');
\t\t\t});
\t\t}

\t\tfunction del_rule(sid) {
\t\t\treturn fs.exec('/usr/share/mihomo/helper.sh', ['del_access_rule', sid]).then(function(res) {
\t\t\t\tif (res.code === 0) {
\t\t\t\t\tui.addNotification(null, E('p', _('规则已删除（需重启核心后生效）。')), 'info');
\t\t\t\t\trender_rules();
\t\t\t\t} else {
\t\t\t\t\tui.addNotification(null, E('p', _('删除失败：') + (res.stderr || res.stdout || '')), 'danger');
\t\t\t\t}
\t\t\t}).catch(function(err) {
\t\t\t\tui.addNotification(null, E('p', _('通信错误：') + err.message), 'danger');
\t\t\t});
\t\t}

\t\tfunction btn(label, cls, fn) {
\t\t\treturn E('button', { 'class': 'cbi-button ' + cls, 'style': 'margin: 1px 2px; padding: 2px 8px;', 'click': function(ev) {
\t\t\t\tev.preventDefault(); fn();
\t\t\t} }, label);
\t\t}

\t\tfunction conn_rows_html(list, with_ip) {
\t\t\tif (!list || !list.length) return '<tr><td colspan="5" style="text-align:center;color:#999;padding:15px;">' + _('暂无数据') + '</td></tr>';
\t\t\tvar rows = '';
\t\t\tfor (var i = 0; i < list.length; i++) {
\t\t\t\tvar c = list[i];
\t\t\t\tvar domain = esc(c.domain || c.dst || '-');
\t\t\t\tvar ip = esc(c.ip || '');
\t\t\t\tvar dev = esc(c.device || '');
\t\t\t\tvar policy = esc(c.policy || (c.rule ? c.rule : '-'));
\t\t\t\tvar traffic = fmt_bytes(c.up) + ' / ' + fmt_bytes(c.down);
\t\t\t\trows += '<tr>';
\t\t\t\tif (with_ip) rows += '<td>' + (dev || ip || '-') + '</td>';
\t\t\t\trows += '<td>' + domain + '</td>';
\t\t\t\trows += '<td>' + policy + '</td>';
\t\t\t\trows += '<td>' + traffic + '</td>';
\t\t\t\trows += '<td>' + btn(_('代理'), 'cbi-button-action', function() { add_rule(ip, c.domain || c.dst, 'proxy', group_names[0]); }) + btn(_('直连'), 'cbi-button-neutral', function() { add_rule(ip, c.domain || c.dst, 'direct'); }) + btn(_('拦截'), 'cbi-button-reset', function() { add_rule(ip, c.domain || c.dst, 'block'); }) + '</td>';
\t\t\t\trows += '</tr>';
\t\t\t}
\t\t\treturn rows;
\t\t}

\t\tfunction render_connections() {
\t\t\tvar box = document.getElementById('conn-body');
\t\t\tif (!box) return;
\t\t\tif (conn_error) {
\t\t\t\tbox.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#ff4757;padding:15px;">' + esc(conn_error) + '</td></tr>';
\t\t\t\treturn;
\t\t\t}
\t\t\tbox.innerHTML = conn_rows_html(connections, true);
\t\t}

\t\tfunction render_history() {
\t\t\tvar box = document.getElementById('hist-body');
\t\t\tif (!box) return;
\t\t\tif (!history.length) {
\t\t\t\tbox.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#999;padding:15px;">' + _('暂无历史记录（核心运行时每 15 秒采集一次）') + '</td></tr>';
\t\t\t\treturn;
\t\t\t}
\t\t\tvar rows = '';
\t\t\tfor (var i = 0; i < history.length; i++) {
\t\t\t\tvar h = history[i];
\t\t\t\tvar domain = esc(h.domain || h.dst || '-');
\t\t\t\tvar dev = esc(h.device || '');
\t\t\t\tvar ip = esc(h.ip || '');
\t\t\t\tvar time = fmt_time(h.ts);
\t\t\t\tvar policy = esc(h.policy || (h.rule ? h.rule : '-'));
\t\t\t\trows += '<tr>';
\t\t\t\trows += '<td>' + time + '</td>';
\t\t\t\trows += '<td>' + (dev || ip || '-') + '</td>';
\t\t\t\trows += '<td>' + domain + '</td>';
\t\t\t\trows += '<td>' + policy + '</td>';
\t\t\t\trows += '<td>' + btn(_('拦截'), 'cbi-button-reset', function() { add_rule(ip, h.domain || h.dst, 'block'); }) + btn(_('直连'), 'cbi-button-neutral', function() { add_rule(ip, h.domain || h.dst, 'direct'); }) + '</td>';
\t\t\t\trows += '</tr>';
\t\t\t}
\t\t\tbox.innerHTML = rows;
\t\t}

\t\tfunction render_rules() {
\t\t\tvar box = document.getElementById('rule-body');
\t\t\tif (!box) return;
\t\t\tif (!rules.length) {
\t\t\t\tbox.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#999;padding:15px;">' + _('暂无自定义规则') + '</td></tr>';
\t\t\t\treturn;
\t\t\t}
\t\t\tvar rows = '';
\t\t\tfor (var i = 0; i < rules.length; i++) {
\t\t\t\tvar r = rules[i];
\t\t\t\tvar action_label = r.action === 'block' ? _('拦截') : (r.action === 'direct' ? _('直连') : _('代理'));
\t\t\t\tvar action_color = r.action === 'block' ? '#ff4757' : (r.action === 'direct' ? '#1e90ff' : '#2ed573');
\t\t\t\trows += '<tr>';
\t\t\t\trows += '<td>' + esc(r.ip || '*') + '</td>';
\t\t\t\trows += '<td>' + esc(r.domain) + '</td>';
\t\t\t\trows += '<td><span style="color:' + action_color + ';font-weight:bold;">' + action_label + '</span>' + (r.group ? ' (' + esc(r.group) + ')' : '') + '</td>';
\t\t\t\trows += '<td>' + esc(r.comment || '') + '</td>';
\t\t\t\trows += '<td>' + (r.enabled === '0' ? _('已禁用') : _('启用')) + '</td>';
\t\t\t\trows += '<td>' + (r.sid ? btn(_('删除'), 'cbi-button-reset', (function(sid) { return function() { del_rule(sid); }; })(r.sid)) : '') + '</td>';
\t\t\t\trows += '</tr>';
\t\t\t}
\t\t\tbox.innerHTML = rows;
\t\t}

\t\tvar group_options = '';
\t\tfor (var gi = 0; gi < group_names.length; gi++) {
\t\t\tgroup_options += '<option value="' + esc(group_names[gi]) + '">' + esc(group_names[gi]) + '</option>';
\t\t}

\t\tvar rule_form = E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 15px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [
\t\t\tE('h3', { 'style': 'margin-top: 0; margin-bottom: 15px; border-bottom: 1px solid rgba(0,0,0,0.06); padding-bottom: 8px;' }, _('新增访问规则')),
\t\t\tE('div', { 'class': 'cbi-value' }, [
\t\t\t\tE('label', { 'class': 'cbi-value-title' }, _('域名 / 后缀')),
\t\t\t\tE('div', { 'class': 'cbi-value-field' }, [
\t\t\t\t\tE('input', { 'id': 'rule_domain', 'type': 'text', 'class': 'cbi-input-text', 'placeholder': '例如 example.com（按后缀匹配）', 'style': 'width: 60%;' })
\t\t\t\t])
\t\t\t]),
\t\t\tE('div', { 'class': 'cbi-value' }, [
\t\t\t\tE('label', { 'class': 'cbi-value-title' }, _('来源 IP（选填）')),
\t\t\t\tE('div', { 'class': 'cbi-value-field' }, [
\t\t\t\t\tE('input', { 'id': 'rule_ip', 'type': 'text', 'class': 'cbi-input-text', 'placeholder': '留空表示所有设备', 'style': 'width: 60%;' })
\t\t\t\t])
\t\t\t]),
\t\t\tE('div', { 'class': 'cbi-value' }, [
\t\t\t\tE('label', { 'class': 'cbi-value-title' }, _('动作')),
\t\t\t\tE('div', { 'class': 'cbi-value-field' }, [
\t\t\t\t\tE('select', { 'id': 'rule_action', 'class': 'cbi-input-select', 'style': 'width: 200px;' }, [
\t\t\t\t\t\tE('option', { 'value': 'block' }, _('拦截 (REJECT)')),
\t\t\t\t\t\tE('option', { 'value': 'direct' }, _('直连 (DIRECT)')),
\t\t\t\t\t\tE('option', { 'value': 'proxy' }, _('走代理'))
\t\t\t\t\t]),
\t\t\t\t\tE('select', { 'id': 'rule_group', 'class': 'cbi-input-select', 'style': 'width: 200px; margin-left:8px;', 'innerHTML': group_options })
\t\t\t\t])
\t\t\t]),
\t\t\tE('div', { 'class': 'cbi-value' }, [
\t\t\t\tE('label', { 'class': 'cbi-value-title' }, _('备注（选填）')),
\t\t\t\tE('div', { 'class': 'cbi-value-field' }, [
\t\t\t\t\tE('input', { 'id': 'rule_comment', 'type': 'text', 'class': 'cbi-input-text', 'style': 'width: 60%;' })
\t\t\t\t])
\t\t\t]),
\t\t\tE('div', { 'class': 'cbi-value' }, [
\t\t\t\tE('div', { 'class': 'cbi-value-field' }, [
\t\t\t\t\tbtn(_('添加规则'), 'cbi-button-add', function() {
\t\t\t\t\t\tvar d = document.getElementById('rule_domain').value.trim();
\t\t\t\t\t\tvar ip = document.getElementById('rule_ip').value.trim();
\t\t\t\t\t\tvar ac = document.getElementById('rule_action').value;
\t\t\t\t\t\tvar gp = document.getElementById('rule_group').value;
\t\t\t\t\t\tvar cm = document.getElementById('rule_comment').value.trim();
\t\t\t\t\t\tif (!d) { ui.addNotification(null, E('p', _('请填写域名。')), 'danger'); return; }
\t\t\t\t\t\tvar args = ['add_access_rule', ip, d, ac];
\t\t\t\t\t\tif (ac === 'proxy' && gp) args.push(gp);
\t\t\t\t\t\treturn fs.exec('/usr/share/mihomo/helper.sh', args).then(function(res) {
\t\t\t\t\t\t\tif (res.code === 0) {
\t\t\t\t\t\t\t\tui.addNotification(null, E('p', _('规则已保存（需重启核心后生效）。')), 'info');
\t\t\t\t\t\t\t\trender_rules();
\t\t\t\t\t\t\t} else {
\t\t\t\t\t\t\t\tui.addNotification(null, E('p', _('添加失败：') + (res.stderr || res.stdout || '')), 'danger');
\t\t\t\t\t\t\t}
\t\t\t\t\t\t}).catch(function(err) {
\t\t\t\t\t\t\tui.addNotification(null, E('p', _('通信错误：') + err.message), 'danger');
\t\t\t\t\t\t});
\t\t\t\t\t}),
\t\t\t\t\tbtn(_('应用并重启核心'), 'cbi-button-apply', function(ev) {
\t\t\t\t\t\tev.preventDefault();
\t\t\t\t\t\treturn fs.exec('/etc/init.d/mihomo', ['restart']).then(function() {
\t\t\t\t\t\t\tui.addNotification(null, E('p', _('核心已重启，规则已生效。')), 'info');
\t\t\t\t\t\t\tsetTimeout(function() { location.reload(); }, 1500);
\t\t\t\t\t\t}).catch(function(err) {
\t\t\t\t\t\t\tui.addNotification(null, E('p', _('重启失败：') + err.message), 'danger');
\t\t\t\t\t\t});
\t\t\t\t\t})
\t\t\t\t])
\t\t\t])
\t\t]);

\t\tvar view_html = E('div', { 'class': 'cbi-map' }, [
\t\t\tE('h2', {}, _('Mihomo 访问日志')),
\t\t\tE('p', {}, _('监控局域网设备实时连接与历史访问，并按域名配置拦截 / 直连 / 代理规则。规则保存在 UCI，重启核心后生效。')),

\t\t\t// Real-time connections
\t\t\tE('div', { 'class': 'cbi-section' }, [
\t\t\t\tE('h3', {}, _('实时连接（每 5 秒刷新）')),
\t\t\t\tE('div', { 'id': 'conn-wrap', 'style': 'max-height: 380px; overflow-y: auto; border: 1px solid rgba(0,0,0,0.08); border-radius: 6px;' }, [
\t\t\t\t\tE('table', { 'class': 'table', 'style': 'margin: 0;' }, [
\t\t\t\t\t\tE('thead', {}, [
\t\t\t\t\t\t\tE('tr', {}, [
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('设备')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('域名 / 目标')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('策略')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('流量 (↑/↓)')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('操作'))
\t\t\t\t\t\t\t])
\t\t\t\t\t\t]),
\t\t\t\t\t\tE('tbody', { 'id': 'conn-body' })
\t\t\t\t\t])
\t\t\t\t])
\t\t\t]),

\t\t\t// History
\t\t\tE('div', { 'class': 'cbi-section' }, [
\t\t\t\tE('h3', {}, _('历史访问记录')),
\t\t\t\tE('div', { 'style': 'max-height: 380px; overflow-y: auto; border: 1px solid rgba(0,0,0,0.08); border-radius: 6px;' }, [
\t\t\t\t\tE('table', { 'class': 'table', 'style': 'margin: 0;' }, [
\t\t\t\t\t\tE('thead', {}, [
\t\t\t\t\t\t\tE('tr', {}, [
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('时间')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('设备')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('域名 / 目标')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('策略')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('操作'))
\t\t\t\t\t\t\t])
\t\t\t\t\t\t]),
\t\t\t\t\t\tE('tbody', { 'id': 'hist-body' })
\t\t\t\t\t])
\t\t\t\t])
\t\t\t]),

\t\t\t// Rules
\t\t\tE('div', { 'class': 'cbi-section' }, [
\t\t\t\tE('h3', {}, _('访问规则管理')),
\t\t\t\tE('div', { 'style': 'max-height: 320px; overflow-y: auto; border: 1px solid rgba(0,0,0,0.08); border-radius: 6px; margin-bottom: 15px;' }, [
\t\t\t\t\tE('table', { 'class': 'table', 'style': 'margin: 0;' }, [
\t\t\t\t\t\tE('thead', {}, [
\t\t\t\t\t\t\tE('tr', {}, [
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('来源 IP')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('域名')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('动作')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('备注')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('状态')),
\t\t\t\t\t\t\t\tE('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('操作'))
\t\t\t\t\t\t\t])
\t\t\t\t\t\t]),
\t\t\t\t\t\tE('tbody', { 'id': 'rule-body' })
\t\t\t\t\t])
\t\t\t\t])
\t\t\t]),

\t\t\trule_form
\t\t]);

\t\tsetTimeout(function() {
\t\t\trender_connections();
\t\t\trender_history();
\t\t\trender_rules();
\t\t}, 0);

\t\tself._timer = setInterval(function() {
\t\t\tfs.exec('/usr/share/mihomo/helper.sh', ['get_connections']).then(function(res) {
\t\t\t\ttry {
\t\t\t\t\tvar j = JSON.parse((res.stdout || '[]').trim());
\t\t\t\t\tif (j && !j.error) { connections = j; conn_error = null; render_connections(); }
\t\t\t\t} catch (e) {}
\t\t\t});
\t\t\tfs.exec('/usr/share/mihomo/helper.sh', ['get_history', '300']).then(function(res) {
\t\t\t\ttry {
\t\t\t\t\thistory = JSON.parse((res.stdout || '[]').trim());
\t\t\t\t\trender_history();
\t\t\t\t} catch (e) {}
\t\t\t});
\t\t}, 5000);

\t\treturn view_html;
\t},

\tunload: function() {
\t\tif (this._timer) { clearInterval(this._timer); this._timer = null; }
\t}
});
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

\t\t// 控制器是否可达：get_proxy_groups 在核心未启动/控制器不可达时返回 {"proxies":{}}
\t\tvar controller_up = (proxy_groups_raw.indexOf('"proxies":{}') === -1);

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

\t\tvar node_cards = [];
\t\tvar delay_els = {};
\t\tvar valid_node_count = 0;
\t\tfor (var i = 0; i < proxies.length; i++) {
\t\t\tvar p = proxies[i];
\t\t\tif (p && p.name && p.type) {
\t\t\t\tvalid_node_count++;
\t\t\t\tvar card_tip = _('节点类型') + '：' + p.type + '\\n' + _('服务器地址') + '：' + (p.server || '-');
\t\t\t\tvar delay_el = E('div', { 'style': 'font-size: 12px; color: #888; margin-top: 6px;' }, _('延时 —'));
\t\t\t\tdelay_els[p.name] = delay_el;
\t\t\t\tvar tip = E('div', { 'style': 'display:none; position:absolute; left:0; top:100%; z-index:50; margin-top:4px; background:#333; color:#fff; padding:6px 8px; border-radius:4px; font-size:11px; white-space:pre-line; max-width:240px; word-break:break-all;' }, card_tip);
\t\t\t\tvar card = E('div', {
\t\t\t\t\t'style': 'position:relative; border:1px solid rgba(0,0,0,0.08); border-radius:8px; padding:10px; background:#fff; cursor:default; min-height:60px; display:flex; flex-direction:column; justify-content:space-between;',
\t\t\t\t\t'onmouseover': (function(t) { return function() { t.style.display = 'block'; }; })(tip),
\t\t\t\t\t'onmouseout': (function(t) { return function() { t.style.display = 'none'; }; })(tip)
\t\t\t\t}, [
\t\t\t\t\tE('div', { 'style': 'font-weight:bold; font-size:13px; line-height:1.3; word-break:break-all;' }, p.name),
\t\t\t\t\tdelay_el,
\t\t\t\t\ttip
\t\t\t\t]);
\t\t\t\tnode_cards.push(card);
\t\t\t}
\t\t}

\t\tvar sub_url = uci.get('mihomo', 'config', 'subscription_url') || '';

\t\tvar run_delay_test = function() {
\t\t\tif (!valid_node_count) return;
\t\t\tui.addNotification(null, E('p', _('正在测试节点延时...')), 'info');
\t\t\tfor (var i = 0; i < proxies.length; i++) {
\t\t\t\t(function(p) {
\t\t\t\t\tif (!p || !p.name || !p.type) return;
\t\t\t\t\tvar el = delay_els[p.name];
\t\t\t\t\tif (el) el.textContent = _('测试中...');
					fs.exec('/usr/share/mihomo/helper.sh', ['test_node_delay', p.name]).then(function(res) {
						try {
							var d = JSON.parse((res.stdout || '{}').trim());
							if (typeof d.delay === 'number' && d.delay >= 0) {
								el.textContent = d.delay + ' ms';
							} else {
								var reason = d.msg || d.message || '';
								if (reason === 'controller_unreachable') el.textContent = _('控制器未连接');
								else if (reason === 'name required') el.textContent = _('名称缺失');
								else if (reason) el.textContent = _('失败') + ':' + String(reason).slice(0, 16);
								else el.textContent = _('超时/失败');
							}
						} catch (e) {
							el.textContent = _('超时/失败');
						}
					}).catch(function() {
						if (el) el.textContent = _('超时/失败');
					});
\t\t\t\t})(proxies[i]);
\t\t\t}
\t\t};

\t\tvar node_test_btn = E('button', {
\t\t\t'class': 'cbi-button cbi-button-action',
\t\t\t'style': 'float: right; margin-top: -2px;',
\t\t\t'click': function(ev) {
\t\t\t\tev.preventDefault();
\t\t\t\trun_delay_test();
\t\t\t}
\t\t}, _('测试'));

\t\tvar node_list_header_children = [ E('h3', { 'style': 'margin-top: 0; margin-bottom: 0;' }, _('配置订阅节点列表')) ];
\t\tif (valid_node_count > 0 && controller_up) {
\t\t\tnode_list_header_children.push(node_test_btn);
\t\t}
\t\tvar node_list_header = E('div', { 'style': 'display: flex; align-items: center; justify-content: space-between;' }, node_list_header_children);

\t\tvar node_list_hint = null;
\t\tif (valid_node_count > 0 && !controller_up) {
\t\t\tnode_list_hint = E('div', {
\t\t\t\t'style': 'margin-top: 12px; padding: 10px 12px; border-radius: 6px; background: rgba(255, 159, 67, 0.08); border: 1px solid #ff9f43; color: #e67e22; font-size: 13px; line-height: 1.5;'
\t\t\t}, _('Mihomo 核心未运行或控制器不可达，无法测试节点延时。请先在上方「运行状态」点击「启动」，刷新本页面后再测试。'));
\t\t}

\t\tvar node_list_body;
\t\tif (valid_node_count > 0) {
\t\t\tnode_list_body = E('div', { 'style': 'display: grid; grid-template-columns: repeat(auto-fill, minmax(100px, 200px)); gap: 10px; margin-top: 12px;' }, node_cards);
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
\t\t\t\tnode_list_header,
\t\t\t\tnode_list_body,
\t\t\t\tnode_list_hint
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

\t\to = s.option(form.Value, 'test_url', _('延时测试地址'), _('节点「测试」按钮用来探测延时的目标 URL。某些网络环境下默认地址不可达会导致所有节点都显示失败，可改为你网络中可正常访问的地址（如 https://www.google.com/generate_204）。留空使用默认。'));
\t\to.rmempty = true;
\t\to.placeholder = 'https://www.gstatic.com/generate_204';

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
