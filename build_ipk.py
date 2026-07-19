import datetime
import gzip
import io
import os
import re
import shutil
import subprocess
import tarfile

# Define configuration for the OpenClash replacement
PKG_NAME = "luci-app-mihomo"
PKG_VERSION = "1.0.0-159"
PKG_ARCH = "all"
IPK_FILENAME = f"{PKG_NAME}_{PKG_VERSION}_{PKG_ARCH}.ipk"

# File contents mapping
src_files = {
    # Package metadata
    "CONTROL/control": """Package: luci-app-mihomo
Version: 1.0.0-1
Depends: luci-base, ip-full, kmod-nft-tproxy, kmod-nft-nat, curl
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
	option config_mode 'subscription'
	option custom_config_path '/etc/mihomo/custom.yaml'
	option work_dir '/etc/mihomo'
	option mix_port '7890'
	option tproxy_port '7893'
	option dns_port '1053'
	option dns_hijack '1'
	option tun_enabled '0'
	option subscription_url ''
	option test_url ''
	option auto_update '0'
	option update_interval '24'
	option last_update ''
	option secret ''
	option geo_auto_update '1'
	option geo_update_interval '24'
	option geoip_mirror_url 'https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.dat'
	option geosite_mirror_url 'https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geosite.dat'
""",
    # System Init Script managed by procd with TProxy/nftables/Dnsmasq redirection
    "root/etc/init.d/mihomo": """#!/bin/sh /etc/rc.common

START=95
USE_PROCD=1

enable_tproxy() {
	local tproxy_port="$1" acl_mode="$2" acl_v4="$3" acl_v6="$4"
	local dns_hijack="$5" dns_port="$6" rip_v4="$7" rip_v6="$8"

	# 1. Policy routing for tproxy (fwmark 1 → table 100 → local route), IPv4 + IPv6.
	ip rule add fwmark 1 table 100 2>/dev/null
	ip route add local default dev lo table 100 2>/dev/null
	ip -6 rule add fwmark 1 table 100 2>/dev/null
	ip -6 route add local default dev lo table 100 2>/dev/null

	# 2. nftables redirection. Drop our tables first (tolerant), then apply the
	# additive ruleset from emit_tproxy_rules. In whitelist+dns_hijack mode that
	# ruleset also installs the source-scoped DNS DNAT (table inet mihomo_dns).
	nft delete table inet mihomo 2>/dev/null
	nft delete table inet mihomo_dns 2>/dev/null
	/usr/share/mihomo/helper.sh emit_tproxy_rules "$tproxy_port" "$acl_mode" "$acl_v4" "$acl_v6" "$dns_hijack" "$dns_port" "$rip_v4" "$rip_v6" | nft -f -

	logger -t mihomo "TProxy redirect rules enabled on port $tproxy_port (acl_mode: $acl_mode, dns_hijack: $dns_hijack)"
}

disable_tproxy() {
	# Remove nftables tables and routing rules (IPv4 + IPv6)
	nft delete table inet mihomo 2>/dev/null
	nft delete table inet mihomo_dns 2>/dev/null
	ip rule del fwmark 1 table 100 2>/dev/null
	ip route del local default dev lo table 100 2>/dev/null
	ip -6 rule del fwmark 1 table 100 2>/dev/null
	ip -6 route del local default dev lo table 100 2>/dev/null

	logger -t mihomo "TProxy redirect rules disabled"
}

enable_dns_hijack() {
	local dns_port="$1"
	
	# Configure Dnsmasq to forward external requests to Mihomo DNS
	uci add_list dhcp.@dnsmasq[0].server="127.0.0.1#$dns_port"
	uci set dhcp.@dnsmasq[0].noresolv="1"
	uci commit dhcp
	/etc/init.d/dnsmasq restart
	
	logger -t mihomo "DNS hijack enabled: Dnsmasq forwarding to Mihomo DNS on port $dns_port"
}

disable_dns_hijack() {
	local dns_port="$1"
	
	# Revert Dnsmasq changes
	uci del_list dhcp.@dnsmasq[0].server="127.0.0.1#$dns_port" 2>/dev/null
	uci del dhcp.@dnsmasq[0].noresolv 2>/dev/null
	uci commit dhcp
	/etc/init.d/dnsmasq restart
	
	logger -t mihomo "DNS hijack disabled"
}

start_service() {
	config_load mihomo
	echo "start $(date +%s)" > /tmp/mihomo_op.state
	/usr/share/mihomo/helper.sh restore_subscription_url
	
	local core_path config_path work_dir dns_port dns_hijack tproxy_port tun_enabled acl_mode config_mode custom_config_path
	
	config_get core_path config core_path "/usr/bin/mihomo"
	config_get config_path config config_path "/etc/mihomo/config.yaml"
	config_get config_mode config config_mode "subscription"
	config_get custom_config_path config custom_config_path "/etc/mihomo/custom.yaml"
	config_get work_dir config work_dir "/etc/mihomo"
	config_get dns_port config dns_port "1053"
	config_get_bool dns_hijack config dns_hijack 1
	config_get tproxy_port config tproxy_port "7893"
	config_get_bool tun_enabled config tun_enabled 0
	
	if [ ! -x "$core_path" ]; then
		logger -t mihomo "ERROR: Core binary not found or not executable at $core_path"
		return 1
	fi
	
	mkdir -p "$work_dir"
	
	# In custom-only mode the subscription file is irrelevant; never create a stub
	# subscription there (the source config is the user's custom_config_path).
	if [ "$config_mode" != "custom" ] && [ ! -f "$config_path" ]; then
		mkdir -p "$(dirname "$config_path")"
		cat <<EOF > "$config_path"
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
	fi
	
	# Prepare running configuration file in RAM
	/usr/share/mihomo/helper.sh prepare_config
	if [ $? -ne 0 ]; then
		logger -t mihomo "ERROR: Failed to prepare running configuration"
		return 1
	fi
	
	# Start Daemon — capture the core's real stdout/stderr (incl. FATAL errors)
	# to a dedicated file so the dashboard can surface startup failures.
	# Truncated per clean start; procd respawns append, so a crash loop stays visible.
	: > /tmp/mihomo_core.log
	procd_open_instance
	# Wrap in sh -c so we can redirect output to the log file (procd execs directly,
	# no shell, so '>' must live inside the sh -c script). $0=mihomo, $1=core, $2=workdir.
	procd_set_param command sh -c 'ulimit -Hn 65535; ulimit -n 65535; "$1" -d "$2" -f /tmp/mihomo_run.yaml >> /tmp/mihomo_core.log 2>&1' mihomo "$core_path" "$work_dir"
	procd_set_param respawn
	procd_close_instance
	
	# Apply network redirections
	if [ "$tun_enabled" -ne 1 ]; then
		local acl_mode acl_v4="" acl_v6=""
		config_get acl_mode config acl_mode "all"

		# Collect acl_ips and split by family: a ':' marks IPv6, anything else
		# (a single addr or an IPv4 CIDR like 192.168.1.0/24) is IPv4.
		append_acl_ip() {
			[ -n "$1" ] || return 0
			case "$1" in
				*:*) acl_v6="${acl_v6:+$acl_v6,}$1" ;;
				*) acl_v4="${acl_v4:+$acl_v4,}$1" ;;
			esac
		}
		config_list_foreach config acl_ips append_acl_ip

		# whitelist+dns_hijack: redirect only whitelisted clients' DNS to Mihomo
		# (source-scoped nft DNAT) instead of the global dnsmasq hijack. Detect the
		# router's LAN addresses as the DNAT target; fall back to the global hijack
		# if we can't find them or the acl is empty.
		local rip_v4="" rip_v6="" src_dns=0
		if [ "$acl_mode" = "whitelist" ] && [ "$dns_hijack" -eq 1 ] && { [ -n "$acl_v4" ] || [ -n "$acl_v6" ]; }; then
			rip_v4=$(/usr/share/mihomo/helper.sh get_lan_ip 2>/dev/null)
			rip_v6=$(/usr/share/mihomo/helper.sh get_lan_ip6 2>/dev/null)
			{ [ -n "$rip_v4" ] || [ -n "$rip_v6" ]; } && src_dns=1
			[ "$src_dns" = "0" ] && logger -t mihomo "WARN: LAN IP not detected; falling back to global DNS hijack"
		fi

		enable_tproxy "$tproxy_port" "$acl_mode" "$acl_v4" "$acl_v6" "$dns_hijack" "$dns_port" "$rip_v4" "$rip_v6"
	fi

	# Global dnsmasq hijack: 'all' mode, TUN mode, or whitelist mode without a
	# working source-scoped DNAT. Skipped when the source-scoped nft DNAT is
	# active so non-whitelisted clients keep the router's real DNS upstream.
	if [ "$dns_hijack" -eq 1 ] && [ "$src_dns" != "1" ]; then
		enable_dns_hijack "$dns_port"
	fi

	# Background collector: persist connections to /tmp/mihomo_access.log for the
	# access-log history view. No-ops when the core controller is unreachable.
	procd_open_instance
	procd_set_param command /usr/share/mihomo/helper.sh collect_loop
	procd_set_param stdout 1
	procd_set_param stderr 1
	procd_set_param respawn
	procd_close_instance

	# Traffic stats loop: every 5s accumulate proxy (chains[0]!=DIRECT) byte
	# deltas into a never-cleared grand total + clearable per-domain buckets.
	procd_open_instance
	procd_set_param command /usr/share/mihomo/helper.sh traffic_loop
	procd_set_param stdout 1
	procd_set_param stderr 1
	procd_set_param respawn
	procd_close_instance

	# Self-contained auto-update loop (no external cron dependency). It polls
	# every 10 minutes; the actual download frequency is gated by auto_update_now
	# according to the configured interval.
	procd_open_instance
	procd_set_param command /usr/share/mihomo/helper.sh auto_update_loop
	procd_set_param stdout 1
	procd_set_param stderr 1
	procd_set_param respawn
	procd_close_instance

	logger -t mihomo "Mihomo service started successfully"
}

stop_service() {
	config_load mihomo
	echo "stop $(date +%s)" > /tmp/mihomo_op.state
	
	local dns_port
	config_get dns_port config dns_port "1053"
	
	# Clean up redirect rules and DNS changes unconditionally to prevent stale configuration
	disable_tproxy
	disable_dns_hijack "$dns_port"
	
	rm -f /tmp/mihomo_run.yaml
	rm -f /tmp/mihomo_core.log
	logger -t mihomo "Mihomo service stopped"
}

service_triggers() {
	procd_add_reload_trigger "mihomo"
}
""",

    # Backend helper script to auto-detect architecture, download core, parse subscription, and merge config
    "root/usr/share/mihomo/helper.sh": """#!/bin/sh

API_PORT="9090"
API_SECRET=""
get_api_config() {
	local config_file="/tmp/mihomo_run.yaml"
	[ ! -f "$config_file" ] && config_file=$(uci -q get mihomo.config.config_path || echo "/etc/mihomo/config.yaml")
	
	API_PORT="9090"
	API_SECRET=""
	if [ -f "$config_file" ]; then
		local controller
		controller=$(grep '^external-controller:' "$config_file" | sed 's/external-controller://' | tr -d " '\\\"\\r" | head -n1)
		if [ -n "$controller" ]; then
			API_PORT=${controller##*:}
		fi
		[ -z "$API_PORT" ] && API_PORT="9090"
		API_SECRET=$(grep '^secret:' "$config_file" | sed 's/secret://' | tr -d " '\\\"\\r" | head -n1)
	fi
}
get_api_config

mihomo_curl() {
	local auth_header=""
	if [ -n "$API_SECRET" ]; then
		auth_header="Authorization: Bearer $API_SECRET"
	fi
	if [ -n "$auth_header" ]; then
		curl -H "$auth_header" "$@"
	else
		curl "$@"
	fi
}

cpu_amd64_v3() {
	# Go GOAMD64=v3 需要 AVX2 + BMI1 + BMI2 + FMA + F16C 等指令集；
	# 缺少任一关键标志即视为非 v3，退回 amd64-compatible 兼容构建。
	grep -qw -m1 avx2 /proc/cpuinfo 2>/dev/null || return 1
	grep -qw -m1 bmi2 /proc/cpuinfo 2>/dev/null || return 1
	grep -qw -m1 bmi1 /proc/cpuinfo 2>/dev/null || return 1
	grep -qw -m1 fma  /proc/cpuinfo 2>/dev/null || return 1
	grep -qw -m1 f16c /proc/cpuinfo 2>/dev/null || return 1
	return 0
}

get_arch() {
	local raw_arch=$(uname -m)
	case "$raw_arch" in
		x86_64)
			if cpu_amd64_v3; then
				echo "amd64"
			else
				echo "amd64-compatible"
			fi
			;;
		aarch64)
			echo "arm64"
			;;
		armv7*)
			echo "armv7"
			;;
		mips)
			echo "mips-softfloat"
			;;
		mipsel)
			echo "mipsle-softfloat"
			;;
		*)
			local opkg_arch=$(opkg print-architecture | awk '{print $2}' | grep -v 'all' | grep -v 'noarch' | head -n 1)
			case "$opkg_arch" in
				*x86_64*) echo "amd64" ;;
				*aarch64*|*arm64*) echo "arm64" ;;
				*armv7*) echo "armv7" ;;
				*mipsel*) echo "mipsle-softfloat" ;;
				*mips*) echo "mips-softfloat" ;;
				*) echo "unknown" ;;
			esac
			;;
	esac
}

check_core() {
	local core_path=$(uci -q get mihomo.config.core_path || echo "/usr/bin/mihomo")
	if [ -x "$core_path" ]; then
		local version=$("$core_path" -v 2>/dev/null | awk '{print $3}')
		echo "installed:$version"
	else
		echo "not_installed"
	fi
}

download_core() {
	local arch=$(get_arch)
	if [ "$arch" = "unknown" ]; then
		echo "ERROR: Unsupported architecture" >&2
		return 1
	fi

	local version="v1.19.28"
	local mirror="https://github.com/MetaCubeX/mihomo/releases/download/${version}"
	local filename="mihomo-linux-${arch}-${version}.gz"
	local url="${mirror}/${filename}"

	if [ -n "$1" ]; then
		url="$1"
		filename=$(basename "$url")
	fi

	local core_path=$(uci -q get mihomo.config.core_path || echo "/usr/bin/mihomo")
	local core_dir=$(dirname "$core_path")
	
	mkdir -p "$core_dir"
	mkdir -p /tmp/mihomo_download

	echo "Downloading Mihomo core from $url..."
	curl -fsSL -k -o "/tmp/mihomo_download/$filename" "$url"
	if [ $? -ne 0 ]; then
		echo "ERROR: Download failed" >&2
		rm -rf /tmp/mihomo_download
		return 1
	fi

	echo "Extracting binary..."
	if [ "${filename##*.}" = "gz" ] && [ "${filename#*.gz}" != "$filename" ]; then
		gunzip -f "/tmp/mihomo_download/$filename"
		local extracted_name=$(basename "$filename" .gz)
		mv "/tmp/mihomo_download/$extracted_name" "$core_path"
	elif [ "${filename##*.}" = "tar.gz" ] || [ "${filename#*.tar.gz}" != "$filename" ]; then
		tar -zxf "/tmp/mihomo_download/$filename" -C /tmp/mihomo_download
		local bin_file=$(find /tmp/mihomo_download -type f -executable | head -n 1)
		if [ -n "$bin_file" ]; then
			mv "$bin_file" "$core_path"
		else
			echo "ERROR: Could not find executable in tarball" >&2
			rm -rf /tmp/mihomo_download
			return 1
	fi
	else
		mv "/tmp/mihomo_download/$filename" "$core_path"
	fi

	chmod +x "$core_path"
	rm -rf /tmp/mihomo_download
	echo "SUCCESS: Mihomo core installed to $core_path"
	return 0
}

update_geox() {
	local work_dir=$(uci -q get mihomo.config.work_dir || echo "/etc/mihomo")
	local geoip_url=$(uci -q get mihomo.config.geoip_mirror_url)
	local geosite_url=$(uci -q get mihomo.config.geosite_mirror_url)
	# Optional overrides: $1 = geoip URL, $2 = geosite URL
	[ -n "$1" ] && geoip_url="$1"
	[ -n "$2" ] && geosite_url="$2"

	if [ -z "$geoip_url" ] && [ -z "$geosite_url" ]; then
		echo "ERROR: No GeoIP/GeoSite mirror URL configured" >&2
		return 1
	fi

	mkdir -p "$work_dir"
	local tmpd=$(mktemp -d)
	local ok=0 failed=""
	for pair in "geoip.dat:$geoip_url" "geosite.dat:$geosite_url"; do
		local fname="${pair%%:*}" url="${pair#*:}"
		[ -z "$url" ] && continue
		echo "Downloading $fname from $url..."
		if curl -fsSL -k -o "$tmpd/$fname" "$url"; then
			mv "$tmpd/$fname" "$work_dir/$fname"
			ok=$((ok + 1))
		else
			failed="$failed $fname"
		fi
	done
	rm -rf "$tmpd"

	if [ -n "$failed" ]; then
		echo "ERROR: Failed to download:$failed" >&2
		return 1
	fi

	# Ask the running core to reload geo databases via the controller API.
	if pidof mihomo >/dev/null 2>&1; then
		mihomo_curl -s -X PUT "http://127.0.0.1:${API_PORT}/configs?force=true" >/dev/null 2>&1 || true
		logger -t mihomo "geo databases reloaded via controller"
	fi
	logger -t mihomo "GeoIP/GeoSite updated in $work_dir ($ok files)"
	echo "SUCCESS: GeoIP/GeoSite updated in $work_dir ($ok files)"
	return 0
}

update_subscription() {
	local url="$1"
	# Custom-only mode has no subscription; refuse to overwrite the user's config.
	if [ "$(uci -q get mihomo.config.config_mode || echo subscription)" = "custom" ]; then
		echo "ERROR: 当前为「仅自定义配置」模式，无法下载订阅。请切换到订阅/混合模式后再更新订阅。" >&2
		return 1
	fi
	if [ -z "$url" ]; then
		url=$(uci -q get mihomo.config.subscription_url)
	fi
	if [ -z "$url" ]; then
		echo "ERROR: No subscription URL specified" >&2
		return 1
	fi
	local work_dir=$(uci -q get mihomo.config.work_dir || echo "/etc/mihomo")
	local config_path=$(uci -q get mihomo.config.config_path || echo "/etc/mihomo/config.yaml")

	mkdir -p "$work_dir"
	logger -t mihomo "Updating subscription from $url"
	echo "Fetching subscription..."
	# Bypass our own fake-ip DNS hijack: when dns_hijack is on, dnsmasq forwards to
	# mihomo which returns 198.18.x.x for everything, so a plain curl would connect
	# to a fake IP and fail. Resolve the host via a direct query to public resolvers
	# (router-originated UDP bypasses tproxy/dnsmasq) and force the real IP.
	local host realip resolve_arg=""
	host="${url#*://}"; host="${host%%/*}"; host="${host##*@}"; host="${host%%:*}"
	if [ -n "$host" ]; then
		for ns in 223.5.5.5 119.29.29.29 1.1.1.1; do
			realip=$(nslookup "$host" "$ns" 2>/dev/null | awk '/^Address:[[:space:]]/ {last=$NF} END {print last}')
			# accept only a pure IPv4; reject server-style "1.2.3.4:53", CNAMEs, empty
			case "$realip" in
				*[!0-9.]*|"") realip="" ;;
				*.*.*.*) break ;;
				*) realip="" ;;
			esac
			[ -n "$realip" ] && break
		done
		[ -n "$realip" ] && resolve_arg="--resolve ${host}:443:${realip}"
	fi
	curl -fsSL -k -A "ClashMeta" $resolve_arg -o "/tmp/mihomo_sub.yaml" "$url"
	if [ $? -ne 0 ] || ! grep -q "^proxies:" /tmp/mihomo_sub.yaml 2>/dev/null; then
		echo "ERROR: Failed to download subscription (resolved=$realip)" >&2
		logger -t mihomo "Subscription update FAILED: download error (resolved=$realip)"
		rm -f /tmp/mihomo_sub.yaml
		# Deadlock guard: if the live config has no proxies (stub/empty) but a backup
		# exists, restore it so we're never left without any proxy.
		if ! grep -q "^proxies:" "$config_path" 2>/dev/null && [ -s "${config_path}.bak" ]; then
			cp "${config_path}.bak" "$config_path"
			logger -t mihomo "Restored previous subscription from ${config_path}.bak"
			echo "WARNING: download failed; restored previous subscription from backup" >&2
			/etc/init.d/mihomo restart >/dev/null 2>&1
		fi
		return 1
	fi

	mv "/tmp/mihomo_sub.yaml" "$config_path"
	uci -q set mihomo.config.last_update="$(date +%s)"
	uci -q commit mihomo
	save_subscription_url "$url"
	# Restart the core so it loads the freshly downloaded config. Without this the
	# running core keeps serving the previous (empty/stale) proxy set while the
	# dashboard reads the new file, causing every node delay test to fail with
	# "Resource not found".
	if pidof mihomo >/dev/null 2>&1; then
		/etc/init.d/mihomo restart
	fi
	echo "SUCCESS: Subscription updated at $config_path"
	logger -t mihomo "Subscription updated: $(wc -c < "$config_path") bytes at $config_path"
	return 0
}

# Remove all locally downloaded subscription nodes. The subscription_url is
# preserved so the user can re-fetch later. If the core is running it is
# restarted so the deletion takes effect immediately.
clear_subscription() {
	# Custom-only mode has no subscription to clear.
	if [ "$(uci -q get mihomo.config.config_mode || echo subscription)" = "custom" ]; then
		echo '{"success":false,"msg":"当前为「仅自定义配置」模式，没有可清空的订阅节点"}'
		return 0
	fi
	local config_path=$(uci -q get mihomo.config.config_path || echo "/etc/mihomo/config.yaml")
	# Back up the current subscription before deleting, so it can be restored if a
	# later re-download fails (e.g. the subscription host is only reachable via the
	# proxy, which is now gone). update_subscription auto-restores this on failure.
	if [ -s "$config_path" ]; then
		cp "$config_path" "${config_path}.bak"
		logger -t mihomo "Backed up subscription to ${config_path}.bak before clearing"
	fi
	rm -f "$config_path"
	uci -q set mihomo.config.last_update=''
	uci -q commit mihomo
	logger -t mihomo "All subscription nodes cleared"
	if pidof mihomo >/dev/null 2>&1; then
		/etc/init.d/mihomo restart
	fi
	echo '{"success":true,"msg":"已清空所有订阅节点"}'
}

# Persist the subscription URL to a package-external store so it survives a full
# reinstall (opkg remove + install), which would otherwise delete the conffile
# /etc/config/mihomo. The file is not part of the package manifest, so opkg
# never removes it.
SUBSCRIPTION_URL_FILE="/etc/mihomo/.subscription_url"

save_subscription_url() {
	local url="$1"
	[ -z "$url" ] && url=$(uci -q get mihomo.config.subscription_url)
	[ -z "$url" ] && return 0
	mkdir -p "$(uci -q get mihomo.config.work_dir || echo /etc/mihomo)"
	printf '%s' "$url" > "$SUBSCRIPTION_URL_FILE"
	uci -q set mihomo.config.subscription_url="$url"
	uci -q commit mihomo
}

restore_subscription_url() {
	local url=$(uci -q get mihomo.config.subscription_url)
	if [ -z "$url" ] && [ -f "$SUBSCRIPTION_URL_FILE" ]; then
		url=$(cat "$SUBSCRIPTION_URL_FILE" 2>/dev/null)
		if [ -n "$url" ]; then
			uci -q set mihomo.config.subscription_url="$url"
			uci -q commit mihomo
			logger -t mihomo "Restored subscription_url from persistent store"
		fi
	fi
}

# Background loop driven by the procd service instance. Polls every 10 minutes;
# the real download cadence is enforced by auto_update_now based on the configured
# interval. Self-contained: does not rely on the system cron daemon.
auto_update_loop() {
	while true; do
		sleep 600
		auto_update_now
	done
}

# Called hourly by cron. Downloads a fresh subscription only when auto_update is
# enabled, a URL is configured, and the configured interval has elapsed.
auto_update_now() {
	local enabled=$(uci -q get mihomo.config.auto_update)
	[ "$enabled" = "1" ] || exit 0
	local url=$(uci -q get mihomo.config.subscription_url)
	[ -z "$url" ] && { logger -t mihomo "auto_update: no subscription_url configured"; exit 0; }
	local interval=$(uci -q get mihomo.config.update_interval || echo 24)
	case "$interval" in ''|*[!0-9]*) interval=24 ;; esac
	[ "$interval" -lt 1 ] && interval=1
	local last=$(uci -q get mihomo.config.last_update)
	local now=$(date +%s)
	if [ -n "$last" ] && [ "$last" -gt 0 ] 2>/dev/null; then
		local elapsed=$((now - last))
		if [ "$elapsed" -lt $((interval * 3600)) ]; then
			logger -t mihomo "auto_update: skipped, next run in $((interval * 3600 - elapsed))s"
			exit 0
		fi
	fi
	logger -t mihomo "auto_update: starting scheduled update"
	update_subscription "$url"
	if [ $? -eq 0 ] && pidof mihomo >/dev/null 2>&1; then
		/etc/init.d/mihomo restart
	fi
}

# Report auto-update schedule state for the UI.
get_schedule() {
	local enabled=$(uci -q get mihomo.config.auto_update)
	local interval=$(uci -q get mihomo.config.update_interval || echo 24)
	case "$interval" in ''|*[!0-9]*) interval=24 ;; esac
	[ "$interval" -lt 1 ] && interval=1
	local last=$(uci -q get mihomo.config.last_update)
	local url=$(uci -q get mihomo.config.subscription_url)
	local next=""
	if [ "$enabled" = "1" ] && [ -n "$url" ]; then
		if [ -n "$last" ] && [ "$last" -gt 0 ] 2>/dev/null; then
			next=$((last + interval * 3600))
		fi
	fi
	echo "{\\"auto_update\\":\\"$enabled\\",\\"interval\\":\\"$interval\\",\\"last_update\\":\\"$last\\",\\"next_update\\":\\"$next\\",\\"has_url\\":\\"$([ -n "$url" ] && echo 1 || echo 0)\\"}"
}

# Emit controlled access rules (from UCI mihomo_rule) as YAML rule lines.
# Note: Mihomo rules are GLOBAL and cannot be scoped per source IP. The recorded
# src_ip is kept only for management/traceability; the rule applies to all devices.
emit_access_rules_yaml() {
	# $1 = source config file, used to validate that a rule's target group actually
	# exists. A rule pointing at a non-existent group makes the core fatal-exit on
	# startup (taking down the whole proxy), so we skip such rules and warn instead.
	local config_file="${1:-$(uci -q get mihomo.config.config_path || echo /etc/mihomo/config.yaml)}"
	# Return 0 if $1 is a usable rule target: a built-in, or a name that appears as
	# some `name:` in the config. If the config is missing/unreadable, fail OPEN
	# (keep the rule) so we never silently drop a valid rule due to a parse glitch.
	rule_target_exists() {
		case "$1" in
			DIRECT|REJECT|REJECT-DROP|PASS|COMPATIBLE) return 0 ;;
		esac
		[ -n "$1" ] || return 0
		[ -f "$config_file" ] || return 0
		local esc
		esc=$(printf '%s' "$1" | sed 's/[][\.\\*^$(){}?+|]/\\&/g')
		grep -qE "name:[[:space:]]+[\\"']?${esc}[\\"']?[,}[:space:]]" "$config_file"
	}
	uci show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=mihomo_rule$/\\1/p' | while read -r sid; do
		local enabled domain action group rule_type rtype
		enabled=$(uci -q get mihomo.$sid.enabled)
		[ "$enabled" = "1" ] || continue
		domain=$(uci -q get mihomo.$sid.domain)
		[ -n "$domain" ] || continue
		rule_type=$(uci -q get mihomo.$sid.rule_type)
		case "$rule_type" in
			domain) rtype="DOMAIN" ;;
			keyword) rtype="DOMAIN-KEYWORD" ;;
			*) rtype="DOMAIN-SUFFIX" ;;
		esac
		action=$(uci -q get mihomo.$sid.action)
		case "$action" in
			block) echo "  - '$rtype,$domain,REJECT'" ;;
			direct) echo "  - '$rtype,$domain,DIRECT'" ;;
			proxy)
				local g
				g=$(uci -q get mihomo.$sid.group)
				if [ -z "$g" ]; then
					continue
				fi
				if rule_target_exists "$g"; then
					echo "  - '$rtype,$domain,$g'"
				else
					logger -t mihomo "access_rule skipped (invalid target): domain=$domain group=$g not found in current proxies/groups"
				fi
				;;
		esac
	done
}

# Built-in bypass rules: multicast / link-local discovery traffic cannot
# traverse a proxy (it is LAN-scope broadcast/multicast). Force DIRECT so it
# is not shoved through the final MATCH rule and wasted on doomed proxy dials
# (seen in logs as e.g. "[UDP] ... --> [ff02::fb]:5353 match Match using FTQ[...]"
# or "[ff02::1:3]:5355"). Emitted first, above UCI access rules.
emit_builtin_bypass_rules() {
	echo "  - 'DST-PORT,5353,DIRECT'"
	echo "  - 'DST-PORT,5355,DIRECT'"
	echo "  - 'IP-CIDR,224.0.0.0/4,DIRECT,no-resolve'"
	echo "  - 'IP-CIDR6,ff00::/8,DIRECT,no-resolve'"
}

# Merge a custom config on top of the prepared running config. Behaviour:
#   * Controlled (UCI-managed) keys (dns/tun/ports/secret/geo/...) are ignored.
#   * "Appendable" list blocks (rules/proxy-groups/proxies/providers) have their
#     items ADDED to the existing base block (custom supplements the subscription).
#   * Any other top-level key in custom REPLACES the base block (custom overrides).
# This implements the "mixed" mode: subscription as base, custom as a supplement.
apply_custom_overlay() {
	local base="$1" custom="$2"
	[ -f "$custom" ] || { logger -t mihomo "apply_custom_overlay: custom file $custom not found"; return 0; }
	local tmpd
	tmpd=$(mktemp -d)

	# Split the overlay into one file per top-level block (key + its indented body).
	# Controlled (UCI-managed) blocks are dropped whole in the loop below, which
	# keeps their bodies from leaking into the previous block.
	awk -v p="${tmpd}/blk" '
		function flush(){ if (f!="") { close(f); f="" } }
		/^[ \t]/ { if (f!="") print >> f; next }
		/^$/ { if (f!="") print >> f; next }
		{
			flush()
			key=$0; sub(/:.*/,"",key)
			gsub(/[^A-Za-z0-9_@.\-]/,"_",key)
			f = p "." key
			print > f
		}
		END { flush() }
	' "$custom"

	local APPENDABLE=" rules proxy-groups proxies proxy-providers rule-providers "

	for blk in "${tmpd}"/blk.*; do
		[ -f "$blk" ] || continue
		local first ckey
		first=$(head -n1 "$blk")
		ckey=$(printf '%s' "$first" | sed 's/:.*//')
		case "$ckey" in
			dns|tun|mixed-port|tproxy-port|port|socks-port|allow-lan|external-controller|secret|profile|geox-url|geo-auto-update|geo-update-interval)
				logger -t mihomo "custom overlay: ignored controlled key '$ckey' (managed via UCI)"
				continue ;;
		esac
		if [ "$ckey" = "rules" ]; then
			# rules 是顺序敏感列表：custom 项必须插在兜底规则 (MATCH/FINAL)
			# 之前，否则落在 MATCH 之后永不命中。其余列表型 key (proxies/groups
			# 等) 不顺序敏感，仍走 merge_append_list 末尾追加。
			merge_rules_before_catchall "$base" "$blk"
		elif echo " $APPENDABLE " | grep -q " $ckey "; then
			merge_append_list "$base" "$ckey" "$blk"
		else
			merge_replace_block "$base" "$ckey" "$blk"
		fi
	done

	rm -rf "$tmpd"
	return 0
}

# Append the list items of an overlay block onto the end of the matching base
# block (creating the block if the base doesn't have one yet).
merge_append_list() {
	local base="$1" ckey="$2" blk="$3"
	local itemsf; itemsf=$(mktemp)
	tail -n +2 "$blk" > "$itemsf"
	if grep -q "^${ckey}:" "$base"; then
		awk -v key="^${ckey}:" -v items="$itemsf" '
			$0 ~ key && !started { started=1; print; next }
			started && /^[A-Za-z0-9_@.\-]+:/ && $0 !~ key { system("cat " items); started=0; print; next }
			{ print }
			END { if (started) system("cat " items) }
		' "$base" > "${base}.tmp" && mv "${base}.tmp" "$base"
	else
		printf '\n%s\n' "$(cat "$blk")" >> "$base"
	fi
	rm -f "$itemsf"
}

# Insert custom rule items BEFORE the subscription's catch-all (MATCH/FINAL).
# rules is order-sensitive: items appended *after* MATCH never match, so we splice
# the overlay rules in front of the first MATCH/FINAL line; if there is no
# catch-all, fall back to appending at the end of the rules block.
merge_rules_before_catchall() {
	local base="$1" blk="$2"
	local itemsf; itemsf=$(mktemp)
	tail -n +2 "$blk" > "$itemsf"
	if grep -q '^rules:' "$base"; then
		awk -v items="$itemsf" '
			/^rules:[[:space:]]*$/ { inr=1; print; next }
			inr && /^[A-Za-z0-9_@.\-]+:/ { if (!done) { system("cat " items); done=1 } inr=0; print; next }
			inr && !done && /^[[:space:]]*-.*(MATCH|FINAL),/ { system("cat " items); done=1 }
			{ print }
			END { if (inr && !done) system("cat " items) }
		' "$base" > "${base}.tmp" && mv "${base}.tmp" "$base"
	else
		printf '\nrules:\n' >> "$base"
		cat "$itemsf" >> "$base"
	fi
	rm -f "$itemsf"
}

# Replace the base block for ckey with the overlay version (or add it if missing).
merge_replace_block() {
	local base="$1" ckey="$2" blk="$3"
	if grep -q "^${ckey}:" "$base"; then
		awk -v key="^${ckey}:" '
			$0 ~ key { skip=1; next }
			skip && /^[ \t]/ { next }
			skip && /^[A-Za-z0-9_@.\-]+:/ { skip=0 }
			{ print }
		' "$base" > "${base}.tmp" && mv "${base}.tmp" "$base"
	fi
	printf '\n%s\n' "$(cat "$blk")" >> "$base"
}

# Re-indent the rules block uniformly to 2 spaces so that list items from
# different sources (subscription / UCI rules / custom overlay) never mix indents
# into invalid YAML (which makes the core fatal-exit on startup).
normalize_rules() {
	local f="$1"
	if grep -q '^rules:' "$f"; then
		local normf="${f}.norm"
		awk '
			/^rules:[[:space:]]*$/ { in_rules = 1; print; next }
			in_rules && /^[A-Za-z]/ { in_rules = 0 }
			in_rules && /^[[:space:]]*-/ { sub(/^[[:space:]]*/, "  "); print; next }
			{ print }
		' "$f" > "$normf" && mv "$normf" "$f"
	fi
}

prepare_config() {
	local config_mode=$(uci -q get mihomo.config.config_mode || echo "subscription")
	local sub_config=$(uci -q get mihomo.config.config_path || echo "/etc/mihomo/config.yaml")
	local custom_config=$(uci -q get mihomo.config.custom_config_path || echo "/etc/mihomo/custom.yaml")
	local run_config="/tmp/mihomo_run.yaml"

	# Select the base source config according to the mode.
	local src_config
	if [ "$config_mode" = "custom" ]; then
		src_config="$custom_config"
	elif [ "$config_mode" = "mixed" ]; then
		src_config="$sub_config"
	else
		config_mode="subscription"
		src_config="$sub_config"
	fi

	local dns_port=$(uci -q get mihomo.config.dns_port || echo "1053")
	local tproxy_port=$(uci -q get mihomo.config.tproxy_port || echo "7893")
	local mix_port=$(uci -q get mihomo.config.mix_port || echo "7890")
	local tun_enabled=$(uci -q get mihomo.config.tun_enabled || echo "0")

	# Controller secret: auto-generate a random one on first run and persist it,
	# so the external-controller is never left unauthenticated (commercial-grade
	# default). get_api_config picks this up so mihomo_curl keeps working.
	local secret=$(uci -q get mihomo.config.secret)
	if [ -z "$secret" ]; then
		secret=$(head -c 24 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n' | head -c 32)
		[ -z "$secret" ] && secret="mihomo-$(date +%s)-$$"
		uci -q set mihomo.config.secret="$secret"
		uci -q commit mihomo
	fi
	local geo_auto_update=$(uci -q get mihomo.config.geo_auto_update || echo "1")
	local geo_update_interval=$(uci -q get mihomo.config.geo_update_interval || echo "24")
	[ "$geo_update_interval" -lt 1 ] 2>/dev/null && geo_update_interval=24
	local geoip_url=$(uci -q get mihomo.config.geoip_mirror_url)
	local geosite_url=$(uci -q get mihomo.config.geosite_mirror_url)

	if [ ! -f "$src_config" ]; then
		echo "ERROR: Source configuration file $src_config not found (mode=$config_mode)" >&2
		return 1
	fi

	# Copy source config to temp running config
	cp "$src_config" "$run_config"

	# Strip existing dns and tun blocks to avoid duplicate key errors
	awk -v in_block=0 '
	/^dns:/ || /^tun:/ { in_block=1; next }
	in_block && /^[a-zA-Z]/ { in_block=0 }
	!in_block { print }
	' "$run_config" > "${run_config}.tmp"
	mv "${run_config}.tmp" "$run_config"

	# Strip top-level ports to avoid conflicts
	sed -i '/^mixed-port:/d; /^tproxy-port:/d; /^port:/d; /^socks-port:/d; /^allow-lan:/d; /^external-controller:/d; /^secret:/d; /^profile:/d; /^geox-url:/d; /^geo-auto-update:/d; /^geo-update-interval:/d' "$run_config"

	# Prepend our controlled settings at the top
	cat <<EOF > "${run_config}.tmp"
mixed-port: $mix_port
tproxy-port: $tproxy_port
allow-lan: true
external-controller: 0.0.0.0:9090
secret: "$secret"
profile:
  store-selected: true
  store-fake-ip: true
EOF
	cat "$run_config" >> "${run_config}.tmp"
	mv "${run_config}.tmp" "$run_config"

	# GeoIP/GeoSite source: point the core at the (China-friendly) mirror and let
	# it auto-update on a schedule, so GEOIP/GEOSITE rules work even on a fresh
	# or offline first run instead of relying on the core's default GitHub fetch.
	if [ "$geo_auto_update" = "1" ] && [ -n "$geoip_url" ] && [ -n "$geosite_url" ]; then
		cat <<EOF >> "$run_config"
geox-url:
  geoip: $geoip_url
  geosite: $geosite_url
geo-auto-update: true
geo-update-interval: $geo_update_interval
EOF
	fi

	# Append controlled DNS block
	cat <<EOF >> "$run_config"
dns:
  enable: true
  ipv6: true
  listen: 0.0.0.0:$dns_port
  enhanced-mode: fake-ip
  nameserver:
    - 223.5.5.5
    - 119.29.29.29
EOF

	# Append controlled TUN block
	if [ "$tun_enabled" -eq 1 ]; then
		cat <<EOF >> "$run_config"
tun:
  enable: true
  stack: system
  auto-route: true
  auto-detect-interface: true
EOF
	else
		cat <<EOF >> "$run_config"
tun:
  enable: false
EOF
	fi

	# Inject built-in bypass rules (multicast/LLMNR → DIRECT) first, then UCI
	# access rules, at the top of the rules block (highest priority, first-match).
	local rules_file="${run_config}.rules"
	emit_builtin_bypass_rules > "$rules_file"
	emit_access_rules_yaml "$src_config" >> "$rules_file"
	if [ -s "$rules_file" ]; then
		if grep -q '^rules:' "$run_config"; then
			local tmpf="${run_config}.rules2"
			awk -v f="$rules_file" '
				BEGIN { while ((getline line < f) > 0) buf = buf line "\\n" }
				{ print }
				/^rules:/ && !done { printf "%s", buf; done=1 }
			' "$run_config" > "$tmpf" && mv "$tmpf" "$run_config"
		else
			printf 'rules:\n' >> "$run_config"
			cat "$rules_file" >> "$run_config"
		fi
		logger -t mihomo "Prepared config with bypass + UCI access rules"
	fi
	rm -f "$rules_file"

	# Normalize the rules block to a single 2-space indent. Subscription configs
	# and UCI-injected rules may use different indents (e.g. 4 vs 2 spaces); mixing
	# them is invalid YAML ("did not find expected '-' indicator") and the core
	# fatal-exits on startup. Re-indent every rule item uniformly to 2 spaces.
	normalize_rules "$run_config"

	# Mixed mode: overlay the user's custom config on top of the subscription base.
	if [ "$config_mode" = "mixed" ]; then
		apply_custom_overlay "$run_config" "$custom_config"
		# Re-normalize rules since the overlay may have added more rule items.
		normalize_rules "$run_config"
	fi

	echo "SUCCESS: Prepared configuration at $run_config (mode=$config_mode)"
	return 0
}

get_proxy_groups() {
	if ! mihomo_curl -s -m 2 "http://127.0.0.1:${API_PORT}/proxies"; then
		echo "{\\"proxies\\":{}}"
	fi
}

select_node() {
	local group="$1"
	local node="$2"
	if [ -z "$group" ] || [ -z "$node" ]; then
		echo "ERROR: Group and node name must be specified" >&2
		return 1
	fi
	local group_enc node_esc
	group_enc=$(urlencode "$group")
	node_esc=$(printf '%s' "$node" | sed 's/"/\\\\"/g')
	local resp
	resp=$(mihomo_curl -sS -X PUT \\
		-H "Content-Type: application/json" \\
		-d "{\\"name\\":\\"${node_esc}\\"}" \\
		"http://127.0.0.1:${API_PORT}/proxies/${group_enc}" 2>&1)
	local code=$?
	if [ $code -ne 0 ]; then
		echo "Network error: $resp" >&2
		return $code
	fi
	if [ -n "$resp" ]; then
		echo "$resp" >&2
		return 1
	fi
	return 0
}

get_proxies() {
	restore_subscription_url
	# Prefer the final merged run config (accurate for all 3 modes). Fall back to
	# the per-mode source file so the node list is still available before a start.
	local config_path="/tmp/mihomo_run.yaml"
	if [ ! -f "$config_path" ]; then
		local config_mode=$(uci -q get mihomo.config.config_mode || echo "subscription")
		if [ "$config_mode" = "custom" ]; then
			config_path=$(uci -q get mihomo.config.custom_config_path || echo "/etc/mihomo/custom.yaml")
		else
			config_path=$(uci -q get mihomo.config.config_path || echo "/etc/mihomo/config.yaml")
		fi
	fi
	if [ ! -f "$config_path" ]; then
		logger -t mihomo "get_proxies: config file not found at $config_path"
		echo "{\\"error\\":\\"not_found\\", \\"msg\\":\\"本地尚未下载任何订阅配置文件，请点击下方按钮更新订阅。\\"}"
		return 0
	fi
	
	local size=$(wc -c < "$config_path")
	if [ "$size" -lt 10 ]; then
		logger -t mihomo "get_proxies: config file empty ($size bytes) at $config_path"
		echo "{\\"error\\":\\"empty\\", \\"msg\\":\\"配置文件内容为空，请重新更新订阅。\\"}"
		return 0
	fi
	
	if grep -q -E "<html>|<!DOCTYPE html>" "$config_path"; then
		local title=$(grep -o -E "<title>[^<]+</title>" "$config_path" | sed -e 's/<title>//g' -e 's/<\\/title>//g' | head -n 1)
		[ -z "$title" ] && title="WAF 拦截或网络错误"
		logger -t mihomo "get_proxies: subscription returned an HTML page ($title)"
		echo "{\\"error\\":\\"html\\", \\"msg\\":\\"下载失败：服务器返回了网页内容 (${title})。请检查链接或网络环境。\\"}"
		return 0
	fi
	
	local nodes=$(tr -d '\r' < "$config_path" | awk \'
	function trim(s){ gsub(/^[ 	]+|[ 	]+$/, "", s); return s }
	function stripq(s){ if ((substr(s,1,1)=="\\042" && substr(s,length(s),1)=="\\042") || (substr(s,1,1)=="\\047" && substr(s,length(s),1)=="\\047")) s=substr(s,2,length(s)-2); return trim(s) }
	function getf(str, key,   rest, p, q, v){
		if (match(str, key ":[ 	]*")) {
			rest = substr(str, RSTART+RLENGTH)
			if (substr(rest,1,1) == "\\047") { rest=substr(rest,2); p=index(rest,"\\047"); if(p>0) v=substr(rest,1,p-1) }
			else { p=index(rest,","); q=index(rest,"}"); if(p==0||(q>0&&q<p)) p=q; if(p>0) v=substr(rest,1,p-1) }
			return stripq(v)
		}
		return ""
	}
	BEGIN { print "["; first=1 }
	/^proxies:/ { in_p=1; next }
	in_p && /^[a-zA-Z]/ && $0 !~ /^[ 	]/ { in_p=0 }
	in_p {
		if ($0 ~ /^[ 	]*-/) {
			if (name != "") { if(!first) printf ","; first=0; printf("  {\\042name\\042:\\042%s\\042,\\042type\\042:\\042%s\\042,\\042server\\042:\\042%s\\042}", name, type, server) }
			name=""; type=""; server=""
			s = $0; sub(/^[ 	]*-[ 	]*/, "", s)
			if (s ~ /\\{/) {
				name=getf(s,"name"); type=getf(s,"type"); server=getf(s,"server")
				if (name != "") { if(!first) printf ","; first=0; printf("  {\\042name\\042:\\042%s\\042,\\042type\\042:\\042%s\\042,\\042server\\042:\\042%s\\042}", name, type, server) }
				name=""; type=""; server=""
				next
			}
			$0 = s
			if ($0 == "") next
		}
		if ($0 ~ /^[ 	]*name:/) { sub(/^[ 	]*name:[ 	]*/, "", $0); name=stripq($0) }
		else if ($0 ~ /^[ 	]*type:/) { sub(/^[ 	]*type:[ 	]*/, "", $0); type=stripq($0) }
		else if ($0 ~ /^[ 	]*server:/) { sub(/^[ 	]*server:[ 	]*/, "", $0); server=stripq($0) }
	}
	END { if (name != "") { if(!first) printf ","; printf("  {\\042name\\042:\\042%s\\042,\\042type\\042:\\042%s\\042,\\042server\\042:\\042%s\\042}", name, type, server) }; print "]" }
	\')
	
	local count=$(printf '%s' "$nodes" | grep -c '"name"')
	logger -t mihomo "get_proxies: parsed $count node(s) from $config_path"
	
	if [ "$nodes" = "[]" ] || [ "$nodes" = "[
]" ]; then
		if grep -q "proxies: \\[\\]" "$config_path"; then
			echo "{\\"error\\":\\"no_nodes\\", \\"msg\\":\\"订阅更新成功，但服务器返回了空的节点列表（已过滤 Hysteria2 等不兼容节点，或订阅已过期）。\\"}"
		else
			echo "{\\"error\\":\\"parse_failed\\", \\"msg\\":\\"未能解析出任何代理节点，请确认订阅内容是否为合法的 Clash/Mihomo 配置。\\"}"
		fi
	else
		echo "$nodes"
	fi
}

# ---------- 访问日志：实时连接 + 历史采集 + 规则管理 ----------

resolve_host() {
	local ip="$1"
	local leases="/tmp/dhcp.leases"
	[ -z "$ip" ] && return 0
	[ -f "$leases" ] || return 0
	awk -v ip="$ip" '$3==ip { print $4; exit }' "$leases"
}

flatten_connections() {
	local raw="$1"
	[ -z "$raw" ] && return 0
	local ids ips hosts dst policy rule up down start
	ids=$(echo "$raw" | jsonfilter -e '$.connections[@].id' 2>/dev/null)
	ips=$(echo "$raw" | jsonfilter -e '$.connections[@].metadata.sourceIP' 2>/dev/null)
	hosts=$(echo "$raw" | jsonfilter -e '$.connections[@].metadata.host' 2>/dev/null)
	dst=$(echo "$raw" | jsonfilter -e '$.connections[@].metadata.destinationIP' 2>/dev/null)
	policy=$(echo "$raw" | jsonfilter -e '$.connections[@].policy' 2>/dev/null)
	rule=$(echo "$raw" | jsonfilter -e '$.connections[@].rule' 2>/dev/null)
	up=$(echo "$raw" | jsonfilter -e '$.connections[@].upload' 2>/dev/null)
	down=$(echo "$raw" | jsonfilter -e '$.connections[@].download' 2>/dev/null)
	start=$(echo "$raw" | jsonfilter -e '$.connections[@].start' 2>/dev/null)
	echo "$ids" | awk '{print NR, $0}' | while read -r n id; do
		[ -z "$id" ] && continue
		local ip host d pol r u dn st
		ip=$(echo "$ips" | sed -n "${n}p")
		host=$(echo "$hosts" | sed -n "${n}p")
		d=$(echo "$dst" | sed -n "${n}p")
		pol=$(echo "$policy" | sed -n "${n}p")
		r=$(echo "$rule" | sed -n "${n}p")
		u=$(echo "$up" | sed -n "${n}p")
		dn=$(echo "$down" | sed -n "${n}p")
		st=$(echo "$start" | sed -n "${n}p")
		local dev
		dev=$(resolve_host "$ip")
		[ -z "$host" ] && host="$d"
		echo "${id}|${ip}|${dev}|${host}|${d}|${pol}|${r}|${u}|${dn}|${st}"
	done
}

get_connections() {
	local raw
	raw=$(mihomo_curl -s --connect-timeout 2 "http://127.0.0.1:${API_PORT}/connections" 2>/dev/null)
	if [ -z "$raw" ]; then
		echo "{\\"error\\":\\"no_core\\", \\"msg\\":\\"无法连接 Mihomo 控制器 (${API_PORT})，请确认核心已启动。\\"}"
		return 0
	fi
	local err_msg
	err_msg=$(echo "$raw" | jsonfilter -e '$.message' 2>/dev/null)
	if [ -n "$err_msg" ]; then
		echo "{\\"error\\":\\"api_error\\", \\"msg\\":\\"Mihomo 控制器错误：${err_msg}\\"}"
		return 0
	fi
	echo "["
	first=1
	flatten_connections "$raw" | while IFS='|' read -r id ip dev host d pol r u dn st; do
		[ -z "$id" ] && continue
		if [ $first -eq 0 ]; then printf ','; fi
		first=0
		printf '{"id":"%s","ip":"%s","device":"%s","domain":"%s","dst":"%s","policy":"%s","rule":"%s","up":%s,"down":%s,"start":"%s"}' "$id" "$ip" "$dev" "$host" "$d" "$pol" "$r" "${u:-0}" "${dn:-0}" "$st"
	done
	echo "]"
}

collect_connections() {
	local raw logf seenf
	logf="/tmp/mihomo_access.log"
	seenf="/tmp/mihomo_access.seen"
	raw=$(mihomo_curl -s --connect-timeout 2 "http://127.0.0.1:${API_PORT}/connections" 2>/dev/null)
	[ -z "$raw" ] && return 0
	touch "$seenf"
	flatten_connections "$raw" | while IFS='|' read -r id ip dev host d pol r u dn st; do
		[ -z "$id" ] && continue
		grep -qxF "$id" "$seenf" && continue
		echo "$id" >> "$seenf"
		local ts
		ts=$(date +%s)
		printf '{"ts":%s,"id":"%s","ip":"%s","device":"%s","domain":"%s","dst":"%s","policy":"%s","rule":"%s","up":%s,"down":%s,"start":"%s"}' "$ts" "$id" "$ip" "$dev" "$host" "$d" "$pol" "$r" "${u:-0}" "${dn:-0}" "$st" >> "$logf"
	done
	tail -n 2000 "$seenf" > "$seenf.tmp" && mv "$seenf.tmp" "$seenf"
}

collect_loop() {
	sleep 5
	while true; do
		collect_connections
		sleep 15
	done
}

# Proxy traffic stats. Every poll: take /connections, for each connection whose
# chains[0] != DIRECT/REJECT, accumulate the byte delta (cur - last seen) into a
# grand total (/etc/mihomo/.traffic_total, never auto-cleared) and into a
# per-main-domain bucket (/etc/mihomo/.traffic_domains, clearable). Host-less
# proxy connections are bucketed as "其他". One awk pass does all the math.
collect_traffic() {
	local raw statef totalf domf tmpd dayf monf bkday bkmon
	statef="/tmp/mihomo_traffic_state"
	totalf="/etc/mihomo/.traffic_total"
	domf="/etc/mihomo/.traffic_domains"
	dayf="/etc/mihomo/.traffic_daily"
	monf="/etc/mihomo/.traffic_monthly"
	# Beijing time (UTC+8) keys for the never-cleared daily/monthly summary
	# buckets. POSIX TZ offset sign is inverted: "UTC-8" == 8h east of UTC.
	bkday=$(TZ=UTC-8 date +%Y-%m-%d)
	bkmon=$(TZ=UTC-8 date +%Y-%m)
	raw=$(mihomo_curl -s --connect-timeout 2 "http://127.0.0.1:${API_PORT}/connections" 2>/dev/null)
	[ -z "$raw" ] && return 0
	tmpd=$(mktemp -d)
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].id' 2>/dev/null > "$tmpd/id"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].chains[0]' 2>/dev/null > "$tmpd/ch"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].metadata.host' 2>/dev/null > "$tmpd/host"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].upload' 2>/dev/null > "$tmpd/up"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].download' 2>/dev/null > "$tmpd/dn"
	touch "$statef" "$totalf" "$domf" "$dayf" "$monf"
	awk -v statef="$statef" -v totalf="$totalf" -v domf="$domf" -v now="$(date +%s)" -v dayf="$dayf" -v monf="$monf" -v curday="$bkday" -v curmon="$bkmon" '
		BEGIN {
			comp = " com.cn net.cn org.cn gov.cn edu.cn ac.cn com.hk com.tw com.jp co.jp co.uk co.kr com.au com.sg com.br com.mx "
			total = 0; since = 0
			while ((getline ln < totalf) > 0) { split(ln, a, " "); total = a[1]+0; if (a[2] != "") since = a[2]+0 }
			close(totalf)
			if (since == 0) since = now
			while ((getline ln < domf) > 0) { if (split(ln, a, "\\t") >= 2 && a[1] != "") dom[a[1]] = a[2]+0 }
			close(domf)
			while ((getline ln < dayf) > 0) { if (split(ln, a, "\\t") >= 2 && a[1] != "") daily[a[1]] = a[2]+0 }
			close(dayf)
			while ((getline ln < monf) > 0) { if (split(ln, a, "\\t") >= 2 && a[1] != "") monthly[a[1]] = a[2]+0 }
			close(monf)
			while ((getline ln < statef) > 0) { if (split(ln, a, "\\t") >= 2 && a[1] != "") st[a[1]] = a[2]+0 }
			close(statef)
		}
		FILENAME ~ /\/id$/   { id[FNR] = $0; if (FNR > maxn) maxn = FNR }
		FILENAME ~ /\/ch$/   { ch[FNR] = $0 }
		FILENAME ~ /\/host$/ { host[FNR] = $0 }
		FILENAME ~ /\/up$/   { up[FNR] = $0 }
		FILENAME ~ /\/dn$/   { dn[FNR] = $0 }
		END {
			for (i = 1; i <= maxn; i++) {
				cid = id[i]; cch = ch[i]; chost = host[i]; cup = up[i]+0; cdn = dn[i]+0
				if (cid == "" || cch == "DIRECT" || cch == "REJECT" || cch == "REJECT-DROP") continue
				cur = cup + cdn
				delta = (cid in st) ? (cur - st[cid]) : cur
				if (delta < 0) delta = cur
				st[cid] = cur; seen[cid] = 1
				if (delta <= 0) continue
				total += delta
				md = "其他"
				if (chost != "") {
					gsub(/[:#].*/, "", chost); sub(/^\\./, "", chost)
					nc = split(chost, lbl, ".")
					if (nc >= 2) {
						l2 = tolower(lbl[nc-1] "." lbl[nc])
						md = (nc >= 3 && index(comp, " " l2 " ")) ? (lbl[nc-2] "." l2) : l2
					}
				}
				dom[md] += delta
				daily[curday] += delta
				monthly[curmon] += delta
			}
			for (k in seen) print k "\\t" st[k] > statef
			close(statef)
			print total " " since > totalf
			close(totalf)
			for (k in dom) if (dom[k] > 0) print k "\\t" dom[k] > domf
			close(domf)
			for (k in daily) if (daily[k] > 0) print k "\\t" daily[k] > dayf
			close(dayf)
			for (k in monthly) if (monthly[k] > 0) print k "\\t" monthly[k] > monf
			close(monf)
		}
	' "$tmpd/id" "$tmpd/ch" "$tmpd/host" "$tmpd/up" "$tmpd/dn"
	rm -rf "$tmpd"
}

traffic_loop() {
	sleep 5
	while true; do
		collect_traffic
		sleep 5
	done
}

# Emit traffic stats as JSON: {total, since, domains:[{domain,bytes}] (top 30 by
# bytes), daily:[{date,bytes}], monthly:[{month,bytes}]} -- daily/monthly are
# full, never-truncated Beijing-time (UTC+8) summaries. Front end formats bytes.
get_traffic() {
	local totalf="/etc/mihomo/.traffic_total" domf="/etc/mihomo/.traffic_domains"
	local dayf="/etc/mihomo/.traffic_daily" monf="/etc/mihomo/.traffic_monthly"
	local total=0 since=0
	read -r total since 2>/dev/null < "$totalf" || true
	total=${total:-0}; since=${since:-0}
	printf '{"total":%s,"since":%s,"domains":[' "$total" "$since"
	if [ -s "$domf" ]; then
		local _first=1 _d _b
		sort -k2 -nr "$domf" 2>/dev/null | head -n 30 | while IFS='	' read -r _d _b; do
			[ -z "$_d" ] && continue
			[ "$_first" -eq 0 ] && printf ','
			_first=0
			printf '{"domain":"%s","bytes":%d}' "$_d" "${_b:-0}"
		done
	fi
	printf '],"daily":['
	if [ -s "$dayf" ]; then
		local _first=1 _k _b
		sort -k1 -r "$dayf" 2>/dev/null | while IFS='	' read -r _k _b; do
			[ -z "$_k" ] && continue
			[ "$_first" -eq 0 ] && printf ','
			_first=0
			printf '{"date":"%s","bytes":%d}' "$_k" "${_b:-0}"
		done
	fi
	printf '],"monthly":['
	if [ -s "$monf" ]; then
		local _first=1 _k _b
		sort -k1 -r "$monf" 2>/dev/null | while IFS='	' read -r _k _b; do
			[ -z "$_k" ] && continue
			[ "$_first" -eq 0 ] && printf ','
			_first=0
			printf '{"month":"%s","bytes":%d}' "$_k" "${_b:-0}"
		done
	fi
	printf ']}'
}

reset_traffic_domains() {
	: > /etc/mihomo/.traffic_domains
	logger -t mihomo "Traffic per-domain stats cleared"
	echo '{"success":true}'
}

# URL-encode a string for use in an HTTP path (POSIX shell, no bashisms).
urlencode() {
	if command -v od >/dev/null 2>&1; then
		printf '%s' "$1" | tr -d '\r' | od -An -v -tx1 | tr -d ' \n' | sed 's/../%&/g'
		printf '\n'
		return
	fi
	local in c val hex out=""
	in=$(printf '%s' "$1" | tr -d '\r')
	out=""
	while [ -n "$in" ]; do
		c=${in%"${in#?}"}
		in=${in#?}
		case "$c" in
			[A-Za-z0-9._~-]) out="$out$c" ;;
			*)
				val=$(printf '%d' "'$c" 2>/dev/null)
				if [ -n "$val" ]; then
					hex=$(printf '%02X' "$val")
					out="$out%$hex"
				else
					out="$out$c"
				fi ;;
		esac
	done
	printf '%s' "$out"
	printf '\n'
}

# Resolve the exact proxy name Mihomo registered, by matching the requested name
# (after stripping quotes/CR/whitespace) against the controller's /proxies list.
# This tolerates YAML-parsing mismatches between the subscription file and the
# running config (e.g. quoted or CRLF-terminated names). Echoes the real name.
resolve_proxy_name() {
	local want="$1"
	local norm_want
	norm_want=$(printf '%s' "$want" | tr -d '\r' | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' | tr -d ' ')
	[ -z "$norm_want" ] && return 0
	mihomo_curl -s --connect-timeout 5 --max-time 8 "http://127.0.0.1:${API_PORT}/proxies" 2>/dev/null \
		| grep -o -E '"name"[[:space:]]*:[[:space:]]*"[^"]*"' \
		| sed -e 's/^"name"[[:space:]]*:[[:space:]]*//' -e 's/^"//' -e 's/"$//' \
		| while read -r p; do
			[ -z "$p" ] && continue
			if [ "$p" = "$want" ]; then echo "$p"; return 0; fi
			local n
			n=$(printf '%s' "$p" | tr -d '\r' | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' | tr -d ' ')
			[ "$n" = "$norm_want" ] && { echo "$p"; return 0; }
		done
	return 0
}

test_node_delay() {
	local name="$1"
	[ -z "$name" ] && { echo '{"delay":-1,"msg":"name required"}'; return 0; }
	# 测试目标 URL：优先 UCI 配置，其次环境变量，最后默认。
	# 某些网络环境下默认地址不可达会导致所有节点都显示失败，故开放为可配置项。
	local test_url
	test_url=$(uci -q get mihomo.config.test_url)
	[ -z "$test_url" ] && test_url="${MIHOMO_TEST_URL:-https://www.gstatic.com/generate_204}"
	local timeout=5000
	local enc url_enc body code
	# 去除可能混入的回车符（CRLF 订阅），避免名称不匹配
	name=$(printf '%s' "$name" | tr -d '\r')
	enc=$(urlencode "$name")
	url_enc=$(urlencode "$test_url")
	local core_running=no core_proxies=-1
	if pidof mihomo >/dev/null 2>&1; then core_running=yes; fi
	core_proxies=$(mihomo_curl -s --connect-timeout 3 --max-time 5 "http://127.0.0.1:${API_PORT}/proxies" 2>/dev/null | grep -o -E '"name"[[:space:]]*:' | wc -l | tr -d ' ')
	logger -t mihomo "test_node_delay: name='$name' enc='$enc' test_url='$test_url' core_running=$core_running core_proxy_count=$core_proxies"
	body=$(mktemp)
	code=$(mihomo_curl -s -o "$body" -w '%{http_code}' --connect-timeout 5 --max-time $((timeout / 1000 + 5)) "http://127.0.0.1:${API_PORT}/proxies/${enc}/delay?url=${url_enc}&timeout=${timeout}" 2>/dev/null)
	if [ -z "$code" ] || [ "$code" = "000" ]; then
		echo '{"delay":-1,"msg":"controller_unreachable"}'
		rm -f "$body"
		return 0
	fi
	local msg=""
	msg=$(grep -o -E '"message"[[:space:]]*:[[:space:]]*"[^"]*"' "$body" | head -n1 | sed -e 's/^"message"[[:space:]]*:[[:space:]]*//' -e 's/^"//' -e 's/"$//')
	if [ "$code" != "200" ] && { [ "$msg" = "Resource not found" ] || [ "$msg" = "proxy not found" ]; }; then
		local real=""
		real=$(resolve_proxy_name "$name")
		local avail=""
		avail=$(mihomo_curl -s --connect-timeout 3 --max-time 5 "http://127.0.0.1:${API_PORT}/proxies" 2>/dev/null | grep -o -E '"name"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -e 's/^"name"[[:space:]]*:[[:space:]]*//' -e 's/^"//' -e 's/"$//' | head -n 20 | tr '\n' '|')
		logger -t mihomo "test_node_delay: '$name' -> $msg; resolved='$real'; core_has_proxies='$avail'"
		if [ -n "$real" ] && [ "$real" != "$name" ]; then
			name="$real"
			enc=$(urlencode "$name")
			code=$(mihomo_curl -s -o "$body" -w '%{http_code}' --connect-timeout 5 --max-time $((timeout / 1000 + 5)) "http://127.0.0.1:${API_PORT}/proxies/${enc}/delay?url=${url_enc}&timeout=${timeout}" 2>/dev/null)
		fi
	fi
	cat "$body"
	rm -f "$body"
}

# Test the delay of every node in one shot. The dashboard used to fire one
# fs.exec per node (30 concurrent calls), which rpcd/file-exec cannot serve
# reliably and every call timed out. This runs all tests in the backend with
# bounded parallelism and returns a single JSON array aligned by index with
# the node order reported by get_proxies.
test_all_nodes() {
	local test_url="$1"
	[ -z "$test_url" ] && test_url=$(uci -q get mihomo.config.test_url)
	[ -z "$test_url" ] && test_url="${MIHOMO_TEST_URL:-https://www.gstatic.com/generate_204}"
	local timeout=5000
	local nodes_json name enc url_enc resp delay msg tmpd i
	nodes_json=$(get_proxies 2>/dev/null)
	[ -z "$nodes_json" ] && { echo "[]"; return 0; }
	tmpd=$(mktemp -d)
	printf '%s' "$nodes_json" | grep -o '"name":"[^"]*"' | sed 's/"name":"//; s/"$//' > "$tmpd/names"
	i=0
	while IFS= read -r name; do
		[ -z "$name" ] && continue
		i=$((i + 1))
		(
			enc=$(urlencode "$name")
			url_enc=$(urlencode "$test_url")
			resp=$(mihomo_curl -s -m $((timeout / 1000 + 5)) "http://127.0.0.1:${API_PORT}/proxies/${enc}/delay?url=${url_enc}&timeout=${timeout}" 2>/dev/null)
			delay=$(printf '%s' "$resp" | grep -o '"delay"[[:space:]]*:[[:space:]]*[0-9]*' | grep -o '[0-9]*$')
			msg=$(printf '%s' "$resp" | grep -o '"message"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:"//; s/"$//')
			if [ -n "$delay" ] && [ "$delay" -ge 0 ] 2>/dev/null; then
				printf '{"delay":%s}' "$delay" > "$tmpd/$i"
			else
				printf '{"delay":-1,"msg":"%s"}' "${msg:-timeout}" > "$tmpd/$i"
			fi
		) &
	done < "$tmpd/names"
	wait
	echo "["
	first=1
	for f in $(ls "$tmpd" | grep -v '^names$' | sort -n); do
		[ "$first" -eq 0 ] && printf ','
		first=0
		cat "$tmpd/$f"
	done
	echo "]"
	rm -rf "$tmpd"
}

# One-click connectivity test: HEAD each site THROUGH the proxy mixed-port so
# it exercises the real rule + selected-node path (same backend clients use).
# delay = total request time in ms; code != "000" means reachable. Powers the
# dashboard "网站连通性测试" panel — answers "why can't devices open foreign sites".
test_connectivity() {
	local mix_port=$(uci -q get mihomo.config.mix_port || echo "7890")
	local proxy="http://127.0.0.1:${mix_port}"
	local sites="百度|https://www.baidu.com
Google|https://www.google.com
YouTube|https://www.youtube.com
Facebook|https://www.facebook.com
TikTok|https://www.tiktok.com"
	echo "["
	first=1
	printf '%s\n' "$sites" | while IFS='|' read -r name url; do
		[ -z "$name" ] && continue
		[ "$first" -eq 0 ] && echo ","
		first=0
		out=$(curl -x "$proxy" -I -m 6 -o /dev/null -s -w '%{http_code} %{time_total}' "$url" 2>/dev/null)
		code=$(printf '%s' "$out" | awk '{print $1}')
		delay=$(printf '%s' "$out" | awk '{print $2}')
		if [ -n "$code" ] && [ "$code" != "000" ] && [ -n "$delay" ]; then
			ms=$(awk -v t="$delay" 'BEGIN{ printf "%d", (t+0)*1000 }')
			printf '{"name":"%s","delay":%s,"code":"%s","ok":true}' "$name" "$ms" "$code"
		else
			printf '{"name":"%s","delay":0,"code":"","ok":false,"msg":"timeout"}' "$name"
		fi
	done
	echo "]"
}

get_history() {
	local logf="/tmp/mihomo_access.log"
	local limit="${1:-200}"
	[ -f "$logf" ] || { echo "[]"; return 0; }
	echo "["
	first=1
	tail -n "$limit" "$logf" | awk '{a[i++]=$0} END {for (j=i-1; j>=0; j--) print a[j]}' | while read -r line; do
		[ -z "$line" ] && continue
		if [ $first -eq 0 ]; then printf ','; fi
		first=0
		printf '%s' "$line"
	done
	echo "]"
}

get_access_rules() {
	echo "["
	first=1
	local sids
	sids=$(uci show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=mihomo_rule$/\\1/p')
	for sid in $sids; do
		local ip domain action group enabled comment rule_type
		ip=$(uci -q get mihomo.$sid.src_ip)
		domain=$(uci -q get mihomo.$sid.domain)
		action=$(uci -q get mihomo.$sid.action)
		group=$(uci -q get mihomo.$sid.group)
		enabled=$(uci -q get mihomo.$sid.enabled)
		comment=$(uci -q get mihomo.$sid.comment)
		rule_type=$(uci -q get mihomo.$sid.rule_type)
		[ -z "$rule_type" ] && rule_type="suffix"
		[ -z "$domain" ] && continue
		if [ $first -eq 0 ]; then printf ','; fi
		first=0
		printf '{"sid":"%s","ip":"%s","domain":"%s","action":"%s","group":"%s","enabled":"%s","comment":"%s","rule_type":"%s"}' "$sid" "$ip" "$domain" "$action" "$group" "$enabled" "$comment" "$rule_type"
	done
	echo "]"
}

add_access_rule() {
	local ip="$1" domain="$2" action="$3" group="$4" rule_type="$5"
	[ -z "$domain" ] && { echo "ERROR: domain required" >&2; return 1; }
	[ -z "$action" ] && action="block"
	[ -z "$rule_type" ] && rule_type="suffix"
	local sid
	sid=$(uci add mihomo mihomo_rule)
	uci -q set mihomo.$sid.src_ip="$ip"
	uci -q set mihomo.$sid.domain="$domain"
	uci -q set mihomo.$sid.action="$action"
	[ -n "$group" ] && uci -q set mihomo.$sid.group="$group"
	uci -q set mihomo.$sid.rule_type="$rule_type"
	uci -q set mihomo.$sid.enabled="1"
	uci commit mihomo
	logger -t mihomo "access_rule added: ip=$ip domain=$domain action=$action rule_type=$rule_type"
	echo "OK"
}

del_access_rule() {
	local sid="$1"
	[ -z "$sid" ] && { echo "ERROR: sid required" >&2; return 1; }
	uci -q delete mihomo.$sid
	uci commit mihomo
	logger -t mihomo "access_rule deleted: $sid"
	echo "OK"
}

import_rules() {
	local text="$1" mode="$2"
	local imported=0 skipped=0 duplicates=0 skipped_samples=""
	local tmpin tmpex tmpclean
	tmpin=$(mktemp); tmpex=$(mktemp); tmpclean=$(mktemp)
	if [ "$mode" = "overwrite" ]; then
		local _n=0
		while [ $_n -lt 1000 ]; do uci -q delete mihomo.@mihomo_rule[0] || break; _n=$((_n+1)); done
	fi
	echo "$text" > "$tmpin"
	sed -e 's/[[:space:]]*$//' -e 's/^[[:space:]]*//' -e "s/^-[[:space:]]*//" -e "s/^'//" -e "s/'$//" "$tmpin" | grep -v -E '^(#|$|rules:)' > "$tmpclean"
	uci show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=mihomo_rule$/\\1/p' | while read -r sid; do
		local d a g rt
		d=$(uci -q get mihomo.$sid.domain); a=$(uci -q get mihomo.$sid.action)
		g=$(uci -q get mihomo.$sid.group); rt=$(uci -q get mihomo.$sid.rule_type)
		[ -z "$rt" ] && rt="suffix"
		[ -n "$d" ] && echo "$rt|$d|$a|$g"
	done > "$tmpex"
	while IFS= read -r line; do
		[ -z "$line" ] && continue
		local rest type value policy
		type=${line%%,*}; rest=${line#*,}
		[ "$rest" = "$line" ] && { skipped=$((skipped+1)); continue; }
		value=${rest%%,*}; rest2=${rest#*,}; policy=${rest2%%,*}
		[ -z "$type" ] || [ -z "$value" ] || [ -z "$policy" ] && { skipped=$((skipped+1)); continue; }
		type=$(printf '%s' "$type" | tr '[:lower:]' '[:upper:]')
		local rt action group=""
		case "$type" in
			DOMAIN) rt="domain" ;;
			DOMAIN-SUFFIX) rt="suffix" ;;
			DOMAIN-KEYWORD) rt="keyword" ;;
			*) skipped=$((skipped+1)); case " $skipped_samples " in *" $type "*) ;; *) [ $(printf '%s' "$skipped_samples" | wc -c) -lt 200 ] && skipped_samples="$skipped_samples $type" ;; esac; continue ;;
		esac
		case "$policy" in
			DIRECT) action="direct" ;;
			REJECT) action="block" ;;
			*) action="proxy"; group="$policy" ;;
		esac
		if grep -qxF "$rt|$value|$action|$group" "$tmpex"; then duplicates=$((duplicates+1)); continue; fi
		echo "$rt|$value|$action|$group" >> "$tmpex"
		local sid; sid=$(uci add mihomo mihomo_rule)
		uci -q set mihomo.$sid.domain="$value"
		uci -q set mihomo.$sid.action="$action"
		[ -n "$group" ] && uci -q set mihomo.$sid.group="$group"
		uci -q set mihomo.$sid.rule_type="$rt"
		uci -q set mihomo.$sid.enabled="1"
		imported=$((imported+1))
	done < "$tmpclean"
	uci commit mihomo
	rm -f "$tmpin" "$tmpex" "$tmpclean"
	local arr="" first=1 t
	for t in $skipped_samples; do [ $first -eq 0 ] && arr="$arr,"; first=0; arr="$arr\\"$t\\""; done
	echo "{\\"imported\\":$imported,\\"skipped\\":$skipped,\\"duplicates\\":$duplicates,\\"skipped_samples\\":[$arr]}"
	return 0
}

get_op_state() {
	local statef="/tmp/mihomo_op.state"
	local op="" since=0 now elapsed ctrl=0 nftexists=0 state result=""
	now=$(date +%s)
	if [ -f "$statef" ]; then
		op=$(awk '{print $1}' "$statef" 2>/dev/null)
		since=$(awk '{print $2}' "$statef" 2>/dev/null)
	fi
	case "$since" in ''|*[!0-9]*) since=0 ;; esac
	mihomo_curl -s -m 1 "http://127.0.0.1:${API_PORT}/version" >/dev/null 2>&1 && ctrl=1
	nft list table inet mihomo >/dev/null 2>&1 && nftexists=1
	elapsed=$((now - since))
	if [ -z "$op" ] || [ "$since" = "0" ]; then
		state="idle"; [ "$ctrl" = "1" ] && result="running" || result="stopped"
	elif [ "$op" = "start" ] || [ "$op" = "restart" ]; then
		if [ "$ctrl" = "1" ]; then state="done"; result="running";
			elif [ "$elapsed" -lt 45 ]; then state="in_progress";
			else state="timeout"; fi
	elif [ "$op" = "stop" ]; then
		if [ "$ctrl" = "0" ] && [ "$nftexists" = "0" ]; then state="done"; result="stopped";
			elif [ "$elapsed" -lt 30 ]; then state="in_progress";
			else state="timeout"; fi
	else
		state="idle"; [ "$ctrl" = "1" ] && result="running" || result="stopped"
	fi
	echo "{\\"state\\":\\"$state\\",\\"op\\":\\"$op\\",\\"elapsed\\":$elapsed,\\"running\\":$ctrl,\\"result\\":\\"$result\\"}"
}
get_core_log() {
	# Tail the core's real stdout/stderr (captured by init.d). Falls back to syslog
	# (init.d's own logger -t mihomo lines) when the file is missing or empty.
	local logfile="/tmp/mihomo_core.log"
	local lines="${1:-300}"
	if [ -s "$logfile" ]; then
		tail -n "$lines" "$logfile" 2>/dev/null
	else
		logread -e mihomo 2>/dev/null | tail -n "$lines"
	fi
}
clear_access_rules() {
	local _n=0
	while [ $_n -lt 1000 ]; do uci -q delete mihomo.@mihomo_rule[0] || break; _n=$((_n+1)); done
	uci commit mihomo
	logger -t mihomo "access_rules cleared ($_n)"
	echo "OK:$_n"
}

# Detect this router's LAN IPv4 address — used as the DNAT target for the
# source-scoped DNS redirect in whitelist+dns_hijack mode. Priority: UCI →
# br-lan interface → any global-scope address → default-route source.
get_lan_ip() {
	local ip
	ip=$(uci -q get network.lan.ipaddr 2>/dev/null | awk '{print $1}')
	[ -n "$ip" ] && { echo "$ip"; return 0; }
	ip=$(ip -4 -o addr show br-lan 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
	[ -n "$ip" ] && { echo "$ip"; return 0; }
	ip=$(ip -4 -o addr show scope global 2>/dev/null | awk 'NR==1{print $4}' | cut -d/ -f1)
	[ -n "$ip" ] && { echo "$ip"; return 0; }
	ip=$(ip -4 -o route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -n1)
	[ -n "$ip" ] && { echo "$ip"; return 0; }
	return 1
}

# Detect a LAN IPv6 address for the v6 source-scoped DNS DNAT. Prefer br-lan
# link-local fe80:: (on-link, stable within a boot), fall back to a global v6.
get_lan_ip6() {
	local ip
	ip=$(ip -6 -o addr show br-lan scope link 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
	[ -n "$ip" ] && { echo "$ip"; return 0; }
	ip=$(ip -6 -o addr show br-lan scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
	[ -n "$ip" ] && { echo "$ip"; return 0; }
	ip=$(ip -6 -o addr show scope global 2>/dev/null | awk 'NR==1{print $4}' | cut -d/ -f1)
	[ -n "$ip" ] && { echo "$ip"; return 0; }
	return 1
}

# Emit the nft ruleset for TProxy redirection plus (in whitelist+dns_hijack mode)
# a source-scoped DNS DNAT so only whitelisted clients reach Mihomo's fake-ip DNS.
# Pure: prints additive rules to stdout, no side effects. The caller pre-deletes
# any existing tables, then pipes this into `nft -f -`.
# Args: tproxy_port acl_mode acl_ips_v4 acl_ips_v6 dns_hijack dns_port router_ip_v4 router_ip_v6
emit_tproxy_rules() {
	local tproxy_port="$1" acl_mode="$2" acl_v4="$3" acl_v6="$4"
	local dns_hijack="$5" dns_port="$6" rip_v4="$7" rip_v6="$8"

	local dns_scope=0
	if [ "$acl_mode" = "whitelist" ] && [ "$dns_hijack" = "1" ] && [ -n "$dns_port" ] && { [ -n "$acl_v4" ] || [ -n "$acl_v6" ]; } && { [ -n "$rip_v4" ] || [ -n "$rip_v6" ]; }; then
		dns_scope=1
	fi

	echo "add table inet mihomo"
	echo "add chain inet mihomo prerouting { type filter hook prerouting priority mangle; }"
	echo "add rule inet mihomo prerouting ip daddr { 127.0.0.0/8, 10.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 255.255.255.255/32 } return"
	echo "add rule inet mihomo prerouting ip6 daddr { fc00::/7, fe80::/10, ff00::/8 } return"

	# Let whitelisted clients' DNS fall through to the nat DNAT (must precede the
	# bypass rule below, otherwise their DNS gets tproxy'd to the tproxy port).
	if [ "$dns_scope" = "1" ]; then
		if [ -n "$acl_v4" ]; then
			echo "add rule inet mihomo prerouting ip saddr { $acl_v4 } tcp dport 53 return"
			echo "add rule inet mihomo prerouting ip saddr { $acl_v4 } udp dport 53 return"
		fi
		if [ -n "$acl_v6" ]; then
			echo "add rule inet mihomo prerouting ip6 saddr { $acl_v6 } tcp dport 53 return"
			echo "add rule inet mihomo prerouting ip6 saddr { $acl_v6 } udp dport 53 return"
		fi
	fi

	# Whitelist bypass: non-whitelisted sources skip tproxy entirely (direct route).
	if [ "$acl_mode" = "whitelist" ]; then
		if [ -n "$acl_v4" ]; then
			echo "add rule inet mihomo prerouting ip saddr != { $acl_v4 } return"
		fi
		if [ -n "$acl_v6" ]; then
			echo "add rule inet mihomo prerouting ip6 saddr != { $acl_v6 } return"
		fi
	fi

	echo "add rule inet mihomo prerouting meta l4proto { tcp, udp } tproxy to :$tproxy_port meta mark set 1"

	# Source-scoped DNS DNAT: only whitelisted clients' port-53 → Mihomo DNS.
	# dnat (not redirect) so hardcoded-DNS clients are caught regardless of dst.
	if [ "$dns_scope" = "1" ]; then
		echo "add table inet mihomo_dns"
		echo "add chain inet mihomo_dns prerouting { type nat hook prerouting priority dstnat; }"
		if [ -n "$acl_v4" ] && [ -n "$rip_v4" ]; then
			echo "add rule inet mihomo_dns prerouting ip saddr { $acl_v4 } udp dport 53 dnat ip to $rip_v4:$dns_port"
			echo "add rule inet mihomo_dns prerouting ip saddr { $acl_v4 } tcp dport 53 dnat ip to $rip_v4:$dns_port"
		fi
		if [ -n "$acl_v6" ] && [ -n "$rip_v6" ]; then
			echo "add rule inet mihomo_dns prerouting ip6 saddr { $acl_v6 } udp dport 53 dnat ip6 to [$rip_v6]:$dns_port"
			echo "add rule inet mihomo_dns prerouting ip6 saddr { $acl_v6 } tcp dport 53 dnat ip6 to [$rip_v6]:$dns_port"
		fi
	fi
}

case "$1" in
	get_arch)
		get_arch
		;;
	get_lan_ip)
		get_lan_ip
		;;
	get_lan_ip6)
		get_lan_ip6
		;;
	emit_tproxy_rules)
		emit_tproxy_rules "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9"
		;;
	check_core)
		check_core
		;;
	download_core)
		download_core "$2"
		;;
	update_geox)
		update_geox "$2" "$3"
		;;
	update_subscription)
		update_subscription "$2"
		;;
	clear_subscription)
		clear_subscription
		;;
	save_subscription_url)
		save_subscription_url "$2"
		;;
	restore_subscription_url)
		restore_subscription_url
		;;
	auto_update_now)
		auto_update_now
		;;
	auto_update_loop)
		auto_update_loop
		;;
	get_schedule)
		get_schedule
		;;
	prepare_config)
		prepare_config
		;;
	get_proxies)
		get_proxies
		;;
	test_node_delay)
		test_node_delay "$2"
		;;
	test_all_nodes)
		test_all_nodes "$2"
		;;
	test_connectivity)
		test_connectivity
		;;
	get_proxy_groups)
		get_proxy_groups
		;;
	select_node)
		select_node "$2" "$3"
		;;
	get_connections)
		get_connections
		;;
	collect_connections)
		collect_connections
		;;
	collect_loop)
		collect_loop
		;;
	traffic_loop)
		traffic_loop
		;;
	get_traffic)
		get_traffic
		;;
	reset_traffic_domains)
		reset_traffic_domains
		;;
	get_history)
		get_history "$2"
		;;
	get_op_state)
		get_op_state
		;;
	get_core_log)
		get_core_log "$2"
		;;
	get_access_rules)
		get_access_rules
		;;
	add_access_rule)
		add_access_rule "$2" "$3" "$4" "$5" "$6"
		;;
	del_access_rule)
		del_access_rule "$2"
		;;
	clear_access_rules)
		clear_access_rules
		;;
	import_rules)
		import_rules "$2" "$3"
		;;
	*)
		echo "Usage: $0 {get_arch|get_lan_ip|get_lan_ip6|emit_tproxy_rules|check_core|download_core|update_geox|update_subscription|clear_subscription|save_subscription_url|restore_subscription_url|auto_update_now|auto_update_loop|get_schedule|prepare_config|get_proxies|get_proxy_groups|select_node|get_connections|collect_connections|collect_loop|get_history|get_access_rules|get_op_state|get_core_log|add_access_rule|del_access_rule|clear_access_rules|import_rules|test_node_delay|test_all_nodes|test_connectivity|traffic_loop|get_traffic|reset_traffic_domains}"
		exit 1
		;;
esac
""",

    # LuCI Menu definition (JSON)
    "root/usr/share/luci/menu.d/luci-app-mihomo.json": """{
    "admin/services/mihomo": {
        "title": "豆豉代理",
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
    },
    "admin/services/mihomo/rules": {
        "title": "规则管理",
        "order": 4,
        "action": {
            "type": "view",
            "path": "mihomo/rules"
        }
    },
    "admin/services/mihomo/traffic": {
        "title": "流量统计",
        "order": 5,
        "action": {
            "type": "view",
            "path": "mihomo/traffic"
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
	return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
		return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
	});
}

function fmt_time(ts) {
	if (!ts) return '';
	try {
		var d = new Date(ts * 1000);
		return d.toLocaleString();
	} catch (e) { return String(ts); }
}

function fmt_bytes(n) {
	n = Number(n) || 0;
	if (n < 1024) return n + ' B';
	if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
	if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
	return (n / 1073741824).toFixed(2) + ' GB';
}

return view.extend({
	load: function() {
		return uci.load('mihomo').then(function() {
			return Promise.all([
				fs.exec('/usr/share/mihomo/helper.sh', ['get_connections']).catch(function() { return { stdout: '[]' }; }),
				fs.exec('/usr/share/mihomo/helper.sh', ['get_history', '300']).catch(function() { return { stdout: '[]' }; }),
				fs.exec('/usr/share/mihomo/helper.sh', ['get_proxy_groups']).catch(function() { return { stdout: '{"proxies":{}}' }; })
			]);
		});
	},

	render: function(results) {
		var self = this;
		if (self._timer) { clearInterval(self._timer); self._timer = null; }

		var conn_raw = (results[0] && results[0].stdout) ? results[0].stdout.trim() : '[]';
		var hist_raw = (results[1] && results[1].stdout) ? results[1].stdout.trim() : '[]';
		var groups_raw = (results[2] && results[2].stdout) ? results[2].stdout.trim() : '{"proxies":{}}';

		var connections = [];
		var conn_error = null;
		try {
			var cj = JSON.parse(conn_raw);
			if (cj && cj.error) conn_error = cj.msg;
			else connections = cj;
		} catch (e) { conn_error = _('无法解析实时连接数据。'); }

		var history = [];
		try { history = JSON.parse(hist_raw); } catch (e) { history = []; }

		var proxy_groups = {};
		try { proxy_groups = JSON.parse(groups_raw).proxies || {}; } catch (e) { proxy_groups = {}; }

		var group_names = [];
		for (var gk in proxy_groups) {
			if (proxy_groups[gk] && proxy_groups[gk].type === 'Selector') group_names.push(gk);
		}

		function add_rule(ip, domain, action, group) {
			var args = ['add_access_rule', ip || '', domain, action];
			if (group) args.push(group);
			ui.addNotification(null, E('p', _('正在添加规则：') + esc(domain) + ' -> ' + action), 'info');
			return fs.exec('/usr/share/mihomo/helper.sh', args).then(function(res) {
				if (res.code === 0) {
					ui.addNotification(null, E('p', _('规则已保存（请前往「规则管理」页面应用并重启核心后生效）。')), 'info');
				} else {
					ui.addNotification(null, E('p', _('添加失败：') + (res.stderr || res.stdout || '')), 'danger');
				}
			}).catch(function(err) {
				ui.addNotification(null, E('p', _('通信错误：') + err.message), 'danger');
			});
		}

		function btn(label, cls, fn) {
			return E('button', { 'class': 'cbi-button ' + cls, 'style': 'margin: 1px 2px; padding: 2px 8px;', 'click': function(ev) {
				ev.preventDefault(); fn();
			} }, label);
		}

		function updateTrackedDevices(conns, hist) {
			var state = {};
			try {
				state = JSON.parse(localStorage.getItem('mihomo_tracked_devices') || '{}');
			} catch (e) { state = {}; }

			var directPolicies = { 'direct': true, 'reject': true, 'block': true, '-': true, '': true };

			function processItem(item) {
				var ip = item.ip;
				if (!ip) return;
				if (ip === '127.0.0.1' || ip === '::1') return;
				
				var devName = item.device || '';
				var isProxied = false;
				var policy = (item.policy || '').toLowerCase();
				if (policy && !directPolicies[policy]) {
					isProxied = true;
				}

				if (!state[ip]) {
					state[ip] = {
						ip: ip,
						name: devName,
						proxied: isProxied
					};
				} else {
					if (devName && !state[ip].name) {
						state[ip].name = devName;
					}
					if (isProxied) {
						state[ip].proxied = true;
					}
				}
			}

			if (conns && conns.length) {
				for (var i = 0; i < conns.length; i++) {
					processItem(conns[i]);
				}
			}

			if (hist && hist.length) {
				for (var j = 0; j < hist.length; j++) {
					processItem(hist[j]);
				}
			}

			localStorage.setItem('mihomo_tracked_devices', JSON.stringify(state));

			var whitelistBox = document.getElementById('whitelist-box');
			var deviceBox = document.getElementById('device-box');
			
			var whitelistCountEl = document.getElementById('whitelist-count');
			var deviceCountEl = document.getElementById('device-count');

			var whitelistHTML = '';
			var deviceHTML = '';
			
			var whitelistCount = 0;
			var deviceCount = 0;

			var keys = Object.keys(state).sort(function(a, b) {
				var pa = a.split('.').map(Number);
				var pb = b.split('.').map(Number);
				if (pa.length === 4 && pb.length === 4 && !pa.some(isNaN) && !pb.some(isNaN)) {
					for (var i = 0; i < 4; i++) {
						if (pa[i] !== pb[i]) return pa[i] - pb[i];
					}
					return 0;
				}
				return a.localeCompare(b);
			});

			for (var k = 0; k < keys.length; k++) {
				var dev = state[keys[k]];
				var label = dev.name ? dev.name + ' (' + dev.ip + ')' : dev.ip;
				var row = E('div', { 'style': 'padding: 6px 12px; border-bottom: 1px dashed rgba(0,0,0,0.04); display: flex; justify-content: space-between; align-items: center;' }, [
					E('span', { 'style': 'font-family: monospace; font-weight: 500;' }, label),
					E('span', { 'style': 'color: #999; font-size: 11px;' }, _('已活跃'))
				]);

				if (dev.proxied) {
					whitelistHTML += row.outerHTML;
					whitelistCount++;
				} else {
					deviceHTML += row.outerHTML;
					deviceCount++;
				}
			}

			if (whitelistBox) {
				whitelistBox.innerHTML = whitelistHTML || '<div style="text-align: center; color: #999; padding: 15px; font-size: 13px;">' + _('暂无红杏设备') + '</div>';
			}
			if (deviceBox) {
				deviceBox.innerHTML = deviceHTML || '<div style="text-align: center; color: #999; padding: 15px; font-size: 13px;">' + _('暂无活跃设备') + '</div>';
			}

			if (whitelistCountEl) whitelistCountEl.innerText = whitelistCount;
			if (deviceCountEl) deviceCountEl.innerText = deviceCount;
		}

		function render_connections() {
			var box = document.getElementById('conn-body');
			if (!box) return;
			if (conn_error) {
				box.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#ff4757;padding:15px;">' + esc(conn_error) + '</td></tr>';
				return;
			}
			
			box.innerHTML = '';
			
			var filter_el = document.getElementById('conn-filter');
			var filter_val = filter_el ? filter_el.value.trim().toLowerCase() : '';
			var filtered = connections;
			if (filter_val) {
				filtered = connections.filter(function(c) {
					var dev = String(c.device || '').toLowerCase();
					var ip = String(c.ip || '').toLowerCase();
					var domain = String(c.domain || '').toLowerCase();
					var dst = String(c.dst || '').toLowerCase();
					var policy = String(c.policy || '').toLowerCase();
					var rule = String(c.rule || '').toLowerCase();
					return dev.indexOf(filter_val) !== -1 ||
					       ip.indexOf(filter_val) !== -1 ||
					       domain.indexOf(filter_val) !== -1 ||
					       dst.indexOf(filter_val) !== -1 ||
					       policy.indexOf(filter_val) !== -1 ||
					       rule.indexOf(filter_val) !== -1;
				});
			}
			
			if (!filtered || !filtered.length) {
				box.appendChild(E('tr', {}, [
					E('td', { 'colspan': 5, 'style': 'text-align:center;color:#999;padding:15px;' }, _('暂无数据'))
				]));
				return;
			}
			
			for (var i = 0; i < filtered.length; i++) {
				var c = filtered[i];
				var domain = c.domain || c.dst || '-';
				var ip = c.ip || '';
				var dev = c.device || '';
				var policy = c.policy || (c.rule ? c.rule : '-');
				var traffic = fmt_bytes(c.up) + ' / ' + fmt_bytes(c.down);
				
				var tr = E('tr', {}, [
					E('td', {}, dev || ip || '-'),
					E('td', {}, domain),
					E('td', {}, policy),
					E('td', {}, traffic),
					E('td', {}, [
						btn(_('代理'), 'cbi-button-action', (function(ip, d) {
							return function() { add_rule(ip, d, 'proxy', group_names[0]); };
						})(ip, c.domain || c.dst)),
						btn(_('直连'), 'cbi-button-neutral', (function(ip, d) {
							return function() { add_rule(ip, d, 'direct'); };
						})(ip, c.domain || c.dst)),
						btn(_('拦截'), 'cbi-button-reset', (function(ip, d) {
							return function() { add_rule(ip, d, 'block'); };
						})(ip, c.domain || c.dst))
					])
				]);
				box.appendChild(tr);
			}
		}

		function render_history() {
			var box = document.getElementById('hist-body');
			if (!box) return;
			box.innerHTML = '';
			if (!history.length) {
				box.appendChild(E('tr', {}, [
					E('td', { 'colspan': 5, 'style': 'text-align:center;color:#999;padding:15px;' }, _('暂无历史记录（核心运行时每 15 秒采集一次）'))
				]));
				return;
			}
			for (var i = 0; i < history.length; i++) {
				var h = history[i];
				var domain = h.domain || h.dst || '-';
				var dev = h.device || '';
				var ip = h.ip || '';
				var time = fmt_time(h.ts);
				var policy = h.policy || (h.rule ? h.rule : '-');
				
				var tr = E('tr', {}, [
					E('td', {}, time),
					E('td', {}, dev || ip || '-'),
					E('td', {}, domain),
					E('td', {}, policy),
					E('td', {}, [
						btn(_('拦截'), 'cbi-button-reset', (function(ip, d) {
							return function() { add_rule(ip, d, 'block'); };
						})(ip, h.domain || h.dst)),
						btn(_('直连'), 'cbi-button-neutral', (function(ip, d) {
							return function() { add_rule(ip, d, 'direct'); };
						})(ip, h.domain || h.dst))
					])
				]);
				box.appendChild(tr);
			}
		}

		var view_html = E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, _('豆豉代理访问日志')),
			E('p', {}, _('监控局域网设备实时连接与历史访问。您可以点击操作按钮快速针对特定域名创建规则。')),

			// IP列表板块（红杏 vs 设备）
			E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 15px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [
				E('h3', { 'style': 'margin-top: 0; margin-bottom: 15px; border-bottom: 1px solid rgba(0,0,0,0.06); padding-bottom: 8px;' }, _('IP列表')),
				E('div', { 'style': 'display: flex; gap: 20px; margin-bottom: 15px; align-items: stretch;' }, [
					// 左边：红杏 (走代理)
					E('div', { 'style': 'flex: 1; border: 1px solid rgba(0,0,0,0.08); border-radius: 6px; padding: 12px; background: rgba(46, 213, 115, 0.02); display: flex; flex-direction: column; min-height: 180px;' }, [
						E('h4', { 'style': 'margin-top: 0; margin-bottom: 8px; border-bottom: 1px solid rgba(0,0,0,0.06); padding-bottom: 6px; color: #2ed573; display: flex; justify-content: space-between; align-items: center;' }, [
							E('span', { 'style': 'font-weight: bold;' }, _('红杏 (走代理)')),
							E('span', { 'id': 'whitelist-count', 'style': 'background: #2ed573; color: white; padding: 2px 6px; border-radius: 10px; font-size: 11px; font-weight: bold;' }, '0')
						]),
						E('div', { 'id': 'whitelist-box', 'style': 'flex: 1; max-height: 200px; overflow-y: auto;' }, [
							E('div', { 'style': 'text-align: center; color: #999; padding: 15px; font-size: 13px;' }, _('暂无红杏设备'))
						])
					]),
					// 右边：设备 (直通/直连)
					E('div', { 'style': 'flex: 1; border: 1px solid rgba(0,0,0,0.08); border-radius: 6px; padding: 12px; background: rgba(30, 144, 255, 0.02); display: flex; flex-direction: column; min-height: 180px;' }, [
						E('h4', { 'style': 'margin-top: 0; margin-bottom: 8px; border-bottom: 1px solid rgba(0,0,0,0.06); padding-bottom: 6px; color: #1e90ff; display: flex; justify-content: space-between; align-items: center;' }, [
							E('span', { 'style': 'font-weight: bold;' }, _('设备 (直通/直连)')),
							E('span', { 'id': 'device-count', 'style': 'background: #1e90ff; color: white; padding: 2px 6px; border-radius: 10px; font-size: 11px; font-weight: bold;' }, '0')
						]),
						E('div', { 'id': 'device-box', 'style': 'flex: 1; max-height: 200px; overflow-y: auto;' }, [
							E('div', { 'style': 'text-align: center; color: #999; padding: 15px; font-size: 13px;' }, _('暂无活跃设备'))
						])
					])
				]),
				E('div', { 'style': 'text-align: right;' }, [
					btn(_('清空所有记录'), 'cbi-button-reset', function() {
						if (confirm(_('确定要清空所有检测到的设备历史记录并重新开始吗？'))) {
							localStorage.removeItem('mihomo_tracked_devices');
							updateTrackedDevices([], []);
						}
					})
				])
			]),

			// Real-time connections
			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, _('实时连接（每 5 秒刷新）')),
				E('div', { 'style': 'margin-bottom: 12px; display: flex; align-items: center;' }, [
					E('span', { 'style': 'margin-right: 8px; font-weight: bold; font-size: 13px; color: #444;' }, _('搜索过滤：')),
					E('input', {
						'id': 'conn-filter',
						'type': 'text',
						'class': 'cbi-input-text',
						'placeholder': _('输入设备名称、IP、域名或策略进行搜索过滤...'),
						'style': 'width: 380px; padding: 4px 8px; border-radius: 4px; border: 1px solid #ccc;',
						'keyup': function() { render_connections(); }
					})
				]),
				E('div', { 'id': 'conn-wrap', 'style': 'max-height: 380px; overflow-y: auto; border: 1px solid rgba(0,0,0,0.08); border-radius: 6px;' }, [
					E('table', { 'class': 'table', 'style': 'margin: 0;' }, [
						E('thead', {}, [
							E('tr', {}, [
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('设备')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('域名 / 目标')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('策略')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, [ _('流量 (↑/↓)') ]),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('操作'))
							])
						]),
						E('tbody', { 'id': 'conn-body' })
					])
				])
			]),

			// History
			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, _('历史访问记录')),
				E('div', { 'style': 'max-height: 380px; overflow-y: auto; border: 1px solid rgba(0,0,0,0.08); border-radius: 6px;' }, [
					E('table', { 'class': 'table', 'style': 'margin: 0;' }, [
						E('thead', {}, [
							E('tr', {}, [
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('时间')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('设备')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('域名 / 目标')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('策略')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('操作'))
							])
						]),
						E('tbody', { 'id': 'hist-body' })
					])
				])
			])
		]);

		setTimeout(function() {
			render_connections();
			render_history();
			updateTrackedDevices(connections, history);
		}, 0);

		self._timer = setInterval(function() {
			fs.exec('/usr/share/mihomo/helper.sh', ['get_connections']).then(function(res) {
				try {
					var j = JSON.parse((res.stdout || '[]').trim());
					if (j && !j.error) { 
						connections = j; 
						conn_error = null; 
						render_connections(); 
						updateTrackedDevices(connections, history);
					}
				} catch (e) {}
			});
			fs.exec('/usr/share/mihomo/helper.sh', ['get_history', '300']).then(function(res) {
				try {
					history = JSON.parse((res.stdout || '[]').trim());
					render_history();
					updateTrackedDevices(connections, history);
				} catch (e) {}
			});
		}, 5000);

		return view_html;
	},

	unload: function() {
		if (this._timer) { clearInterval(this._timer); this._timer = null; }
	}
});
""",

    "root/www/luci-static/resources/view/mihomo/rules.js": """'use strict';
'require view';
'require ui';
'require fs';
'require uci';

function esc(s) {
	return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
		return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
	});
}

return view.extend({
	load: function() {
		return uci.load('mihomo').then(function() {
			return Promise.all([
				fs.exec('/usr/share/mihomo/helper.sh', ['get_proxy_groups']).catch(function() { return { stdout: '{"proxies":{}}' }; })
			]);
		});
	},

	render: function(results) {
		var self = this;
		var groups_raw = (results[0] && results[0].stdout) ? results[0].stdout.trim() : '{"proxies":{}}';

		var rules = [];

		var proxy_groups = {};
		try { proxy_groups = JSON.parse(groups_raw).proxies || {}; } catch (e) { proxy_groups = {}; }

		var group_names = [];
		for (var gk in proxy_groups) {
			if (proxy_groups[gk] && proxy_groups[gk].type === 'Selector') group_names.push(gk);
		}

		function del_rule(sid) {
			return fs.exec('/usr/share/mihomo/helper.sh', ['del_access_rule', sid]).then(function(res) {
				if (res.code === 0) {
					ui.addNotification(null, E('p', _('规则已删除（需重启核心后生效）。')), 'info');
					loadRules();
				} else {
					ui.addNotification(null, E('p', _('删除失败：') + (res.stderr || res.stdout || '')), 'danger');
				}
			}).catch(function(err) {
				ui.addNotification(null, E('p', _('通信错误：') + err.message), 'danger');
			});
		}

		function btn(label, cls, fn) {
			return E('button', { 'class': 'cbi-button ' + cls, 'style': 'margin: 1px 2px; padding: 2px 8px;', 'click': function(ev) {
				ev.preventDefault(); fn();
			} }, label);
		}

		function render_rules() {
			var box = document.getElementById('rule-body');
			if (!box) return;
			box.innerHTML = '';
			if (!rules.length) {
				box.appendChild(E('tr', {}, [
					E('td', { 'colspan': 6, 'style': 'text-align:center;color:#999;padding:15px;' }, _('暂无自定义规则'))
				]));
				return;
			}
			for (var i = 0; i < rules.length; i++) {
				var r = rules[i];
				var action_label = r.action === 'block' ? _('拦截') : (r.action === 'direct' ? _('直连') : _('代理'));
				var action_color = r.action === 'block' ? '#ff4757' : (r.action === 'direct' ? '#1e90ff' : '#2ed573');
				var rt = r.rule_type || 'suffix';
				var rt_label = rt === 'domain' ? _('精确') : (rt === 'keyword' ? _('关键字') : _('后缀'));
				var rt_color = rt === 'domain' ? '#8e44ad' : (rt === 'keyword' ? '#e67e22' : '#7f8c8d');
				
				var action_node = E('span', { 'style': 'color:' + action_color + ';font-weight:bold;' }, action_label);
				var action_td = E('td', {}, [
					action_node,
					r.group ? ' (' + r.group + ')' : ''
				]);

				var tr = E('tr', {}, [
					E('td', {}, r.ip || '*'),
					E('td', {}, [
						r.domain,
						E('span', { 'style': 'margin-left:6px;font-size:11px;padding:1px 6px;border-radius:8px;background:' + rt_color + ';color:#fff;' }, rt_label)
					]),
					action_td,
					E('td', {}, r.comment || ''),
					E('td', {}, r.enabled === '0' ? _('已禁用') : _('启用')),
					E('td', {}, [
						r.sid ? btn(_('删除'), 'cbi-button-reset', (function(sid) {
							return function() { del_rule(sid); };
						})(r.sid)) : ''
					])
				]);
				box.appendChild(tr);
			}
		}

		var group_options = '';
		for (var gi = 0; gi < group_names.length; gi++) {
			group_options += '<option value="' + esc(group_names[gi]) + '">' + esc(group_names[gi]) + '</option>';
		}

		var rule_form = E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 15px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [
			E('h3', { 'style': 'margin-top: 0; margin-bottom: 15px; border-bottom: 1px solid rgba(0,0,0,0.06); padding-bottom: 8px;' }, _('新增访问规则')),
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, _('域名')),
				E('div', { 'class': 'cbi-value-field' }, [
					E('input', { 'id': 'rule_domain', 'type': 'text', 'class': 'cbi-input-text', 'placeholder': '例如 example.com（按后缀匹配）', 'style': 'width: 60%;' }),
					E('select', { 'id': 'rule_type', 'class': 'cbi-input-select', 'style': 'margin-left:8px;width:120px;' }, [
						E('option', { 'value': 'suffix' }, _('后缀')),
						E('option', { 'value': 'domain' }, _('精确')),
						E('option', { 'value': 'keyword' }, _('关键字'))
					])
				])
			]),
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, _('来源 IP（选填）')),
				E('div', { 'class': 'cbi-value-field' }, [
					E('input', { 'id': 'rule_ip', 'type': 'text', 'class': 'cbi-input-text', 'placeholder': '留空表示所有设备', 'style': 'width: 60%;' })
				])
			]),
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, _('动作')),
				E('div', { 'class': 'cbi-value-field' }, [
					E('select', { 'id': 'rule_action', 'class': 'cbi-input-select', 'style': 'width: 200px;' }, [
						E('option', { 'value': 'block' }, _('拦截 (REJECT)')),
						E('option', { 'value': 'direct' }, _('直连 (DIRECT)')),
						E('option', { 'value': 'proxy' }, _('走代理'))
					]),
					E('select', { 'id': 'rule_group', 'class': 'cbi-input-select', 'style': 'width: 200px; margin-left:8px;', 'innerHTML': group_options })
				])
			]),
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, _('备注（选填）')),
				E('div', { 'class': 'cbi-value-field' }, [
					E('input', { 'id': 'rule_comment', 'type': 'text', 'class': 'cbi-input-text', 'style': 'width: 60%;' })
				])
			]),
			E('div', { 'class': 'cbi-value' }, [
				E('div', { 'class': 'cbi-value-field' }, [
					btn(_('添加规则'), 'cbi-button-add', function() {
						var d = document.getElementById('rule_domain').value.trim();
						var ip = document.getElementById('rule_ip').value.trim();
						var ac = document.getElementById('rule_action').value;
						var gp = document.getElementById('rule_group').value;
						var cm = document.getElementById('rule_comment').value.trim();
						if (!d) { ui.addNotification(null, E('p', _('请填写域名。')), 'danger'); return; }
						var rt = document.getElementById('rule_type').value;
						var args = ['add_access_rule', ip, d, ac, (ac === 'proxy' ? gp : ''), rt];
						return fs.exec('/usr/share/mihomo/helper.sh', args).then(function(res) {
							if (res.code === 0) {
								ui.addNotification(null, E('p', _('规则已保存（需重启核心后生效）。')), 'info');
								document.getElementById('rule_domain').value = '';
								document.getElementById('rule_ip').value = '';
								document.getElementById('rule_comment').value = '';
								loadRules();
							} else {
								ui.addNotification(null, E('p', _('添加失败：') + (res.stderr || res.stdout || '')), 'danger');
							}
						}).catch(function(err) {
							ui.addNotification(null, E('p', _('通信错误：') + err.message), 'danger');
						});
					}),
					btn(_('应用并重启核心'), 'cbi-button-apply', function(ev) {
						ev.preventDefault();
						return fs.exec('/etc/init.d/mihomo', ['restart']).then(function() {
							ui.addNotification(null, E('p', _('核心已重启，规则已生效。')), 'info');
							setTimeout(function() { location.reload(); }, 1500);
						}).catch(function(err) {
							ui.addNotification(null, E('p', _('重启失败：') + err.message), 'danger');
						});
					})
				])
			])
		]);



		function doImport(mode) {			var txt = document.getElementById('rule_import').value.trim();			if (!txt) { ui.addNotification(null, E('p', _('请粘贴规则内容。')), 'danger'); return; }			return fs.exec('/usr/share/mihomo/helper.sh', ['import_rules', txt, mode]).then(function(res) {				var msg;				try {					var j = JSON.parse((res.stdout || '').trim());					msg = _('已导入 ') + j.imported + _(' 条；跳过 ') + j.skipped + _(' 条（非域名规则）') + (j.duplicates ? _('；重复 ') + j.duplicates + _(' 条') : '') + (j.skipped_samples && j.skipped_samples.length ? '：' + j.skipped_samples.join(', ') : '');				} catch (e) { msg = _('导入完成：') + (res.stdout || res.stderr || ''); }				if (res.code === 0) {					ui.addNotification(null, E('p', msg), 'info');					document.getElementById('rule_import').value = '';					loadRules();				} else {					ui.addNotification(null, E('p', _('导入失败：') + (res.stderr || res.stdout || '')), 'danger');				}			}).catch(function(err) {				ui.addNotification(null, E('p', _('通信错误：') + err.message), 'danger');			});		}

		var import_form = E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 15px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [			E('h3', { 'style': 'margin-top: 0; margin-bottom: 10px; border-bottom: 1px solid rgba(0,0,0,0.06); padding-bottom: 8px;' }, _('批量导入规则')),			E('p', { 'style': 'color:#666;font-size:13px;margin-bottom:10px;' }, _('粘贴 Mihomo 规则，每行一条（如 DOMAIN-SUFFIX,example.com,DIRECT）。仅导入 DOMAIN / DOMAIN-SUFFIX / DOMAIN-KEYWORD，IP-CIDR / GEOIP / MATCH 等会跳过。覆盖导入=先清空全部现有 UCI 规则再导入；追加导入=保留现有规则。')),			E('textarea', { 'id': 'rule_import', 'class': 'cbi-input-textarea', 'style': 'width:100%;height:160px;font-family:monospace;font-size:12px;margin-bottom:10px;', 'placeholder': 'DOMAIN-SUFFIX,apple.com,DIRECT' }),			btn(_('覆盖导入'), 'cbi-button-reset', function() { doImport('overwrite'); }),			btn(_('追加导入'), 'cbi-button-add', function() { doImport('append'); })		]);

function loadRules() {
	return fs.exec('/usr/share/mihomo/helper.sh', ['get_access_rules']).then(function(res) {
		var rr = (res && res.stdout) ? res.stdout.trim() : '[]';
		try { rules = JSON.parse(rr); } catch (e) { rules = []; }
		render_rules();
	}).catch(function () {});
}

var view_html = E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, _('Mihomo 访问规则管理')),
			E('p', {}, _('配置并管理局域网设备的特定域名代理规则。用户规则会被注入到核心配置文件 rules 列表的最顶部以优先匹配。规则保存在 UCI，需重启核心后才能生效。')),
			E('div', { 'style': 'background: #fff8e1; border: 1px solid #ffe082; border-radius: 6px; padding: 10px 14px; margin-bottom: 15px;' }, [
				E('p', { 'style': 'margin: 0; font-size: 13px; color: #6d5b00; line-height: 1.6;' }, _('<b>此处只管理你手动添加的「自定义规则」</b>（保存于 UCI，注入运行配置 rules 段最前、优先匹配）。订阅自带的规则（含末尾兜底 MATCH）<b>不在此处管理</b>，随订阅更新而变化。')),
				E('p', { 'style': 'margin: 8px 0 0; font-size: 13px; color: #6d5b00; line-height: 1.6;' }, _('如需大批量追加节点 / 规则组，或完全使用自己的配置，请改用「设置 → 配置模式」中的自定义配置文件（custom.yaml）。'))
			]),

			E('button', { 'id': 'btn_uci_toggle', 'class': 'cbi-button cbi-button-action', 'style': 'margin-bottom: 15px;', 'click': function(ev) {
				ev.preventDefault();
				var sec = document.getElementById('uci_rules_section');
				var tg = document.getElementById('btn_uci_toggle');
				if (!sec) return;
				var open = sec.style.display !== 'none';
				sec.style.display = open ? 'none' : 'block';
				if (tg) tg.textContent = open ? _('UCI模式编辑') : _('收起 UCI 列表');
				if (!open) loadRules();
			} }, _('UCI模式编辑')),
			E('button', { 'id': 'btn_clear_rules', 'class': 'cbi-button cbi-button-reset', 'style': 'margin-bottom: 15px; margin-left: 8px;', 'click': function(ev) {
				ev.preventDefault();
				if (!confirm(_('确定清空所有自定义规则？此操作不可撤销，需重启核心后生效。'))) return;
				return fs.exec('/usr/share/mihomo/helper.sh', ['clear_access_rules']).then(function(res) {
					if (res.code === 0) {
						ui.addNotification(null, E('p', _('已清空所有规则（需重启核心后生效）。')), 'info');
						rules = [];
						render_rules();
					} else {
						ui.addNotification(null, E('p', _('清空失败：') + (res.stderr || res.stdout || '')), 'danger');
					}
				}).catch(function(err) { ui.addNotification(null, E('p', _('通信错误：') + err.message), 'danger'); });
			} }, _('清空所有规则')),
			E('div', { 'id': 'uci_rules_section', 'class': 'cbi-section', 'style': 'display: none;' }, [
				E('h3', {}, _('自定义规则列表')),
				E('div', { 'style': 'max-height: 400px; overflow-y: auto; border: 1px solid rgba(0,0,0,0.08); border-radius: 6px; margin-bottom: 15px;' }, [
					E('table', { 'class': 'table', 'style': 'margin: 0;' }, [
						E('thead', {}, [
							E('tr', {}, [
								E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('来源 IP')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('域名')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('动作')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('备注')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('状态')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('操作'))
							])
						]),
						E('tbody', { 'id': 'rule-body' })
					])
				])
			]),

			rule_form,

			import_form
		]);

		setTimeout(function() {
			render_rules();
		}, 0);

		return view_html;
	}
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
	load: function() {
		return uci.load('mihomo').then(function() {
			return Promise.all([
				fs.exec('/usr/share/mihomo/helper.sh', ['check_core']).catch(function() { return { stdout: '' }; }),
				fs.exec('/usr/share/mihomo/helper.sh', ['get_core_log']).catch(function() { return { stdout: '' }; }),
				rpc.declare({
					object: 'service',
					method: 'list',
					params: [ 'name' ]
				})({ name: 'mihomo' }).catch(function() { return {}; }),
				fs.exec('/usr/share/mihomo/helper.sh', ['get_proxies']).catch(function() { return { stdout: '[]' }; }),
				fs.exec('/usr/share/mihomo/helper.sh', ['get_proxy_groups']).catch(function() { return { stdout: '{"proxies":{}}' }; }),
				fs.exec('/usr/share/mihomo/helper.sh', ['get_schedule']).catch(function() { return { stdout: '{"auto_update":"0","interval":"24","last_update":"","next_update":"","has_url":"0"}' }; }),
				fs.exec('/usr/share/mihomo/helper.sh', ['get_op_state']).catch(function() { return { stdout: '{"state":"idle","op":"","elapsed":0,"running":0,"result":"stopped"}' }; })
			]);
		});
	},

	render: function(results) {
		var core_status = (results[0] && results[0].stdout) ? results[0].stdout.trim() : '';
		var logs = (results[1] && results[1].stdout) ? results[1].stdout.trim() : '';
		logs = logs || _('暂无日志记录。');
		var service_data = results[2];
		var proxy_data_raw = (results[3] && results[3].stdout) ? results[3].stdout.trim() : '[]';
		var proxy_groups_raw = (results[4] && results[4].stdout) ? results[4].stdout.trim() : '{"proxies":{}}';

		var schedule_raw = (results[5] && results[5].stdout) ? results[5].stdout.trim() : '{"auto_update":"0","interval":"24","last_update":"","next_update":"","has_url":"0"}';
		var schedule = {};
		try { schedule = JSON.parse(schedule_raw); } catch(e) { schedule = {}; }
		var op_state_raw = (results[6] && results[6].stdout) ? results[6].stdout.trim() : '{"state":"idle","op":"","elapsed":0,"running":0,"result":"stopped"}';
		var op_state = {};
		try { op_state = JSON.parse(op_state_raw); } catch(e) { op_state = { state: 'idle' }; }
		
		var proxies = [];
		var parse_error = null;
		try {
			var parsed = JSON.parse(proxy_data_raw);
			if (parsed && parsed.error) {
				parse_error = parsed.msg;
			} else {
				proxies = parsed;
			}
		} catch(e) {
			proxies = [];
			parse_error = _('本地配置文件数据损坏或解析失败。');
		}

		var proxy_groups = {};
		try {
			proxy_groups = JSON.parse(proxy_groups_raw).proxies || {};
		} catch(e) {
			proxy_groups = {};
		}

		// 控制器是否可达：get_proxy_groups 在核心未启动/控制器不可达时返回 {"proxies":{}}
		var controller_up = (proxy_groups_raw.indexOf('"proxies":{}') === -1);

		var is_running = false;
		if (service_data && service_data.mihomo && service_data.mihomo.instances) {
			var instances = service_data.mihomo.instances;
			for (var key in instances) {
				if (instances[key].running) {
					is_running = true;
					break;
				}
			}
		}

		var is_installed = core_status.indexOf('installed:') === 0;
		var core_ver = _('未安装');
		if (is_installed) {
			core_ver = core_status.split(':')[1];
		}

		var core_path = uci.get('mihomo', 'config', 'core_path') || '/usr/bin/mihomo';

		var status_badge;
		if (controller_up) {
			status_badge = E('span', { 'class': 'label success', 'style': 'background-color: #2ed573; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold;' }, 'RUNNING');
		} else if (is_running) {
			status_badge = E('span', { 'class': 'label', 'style': 'background-color: #ffa502; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold;' }, '运行异常');
		} else {
			status_badge = E('span', { 'class': 'label danger', 'style': 'background-color: #ff4757; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold;' }, 'STOPPED');
		}
		// 运行异常：procd 在拉起进程但控制器未就绪——提示用户去看下方红色错误日志
		var status_cell = E('td', {}, status_badge);
		if (!controller_up && is_running) {
			status_cell.appendChild(E('span', { 'style': 'margin-left: 10px; color: #e03131; font-size: 12px;' }, _('核心进程在运行但控制器未就绪，请查看下方「系统代理日志」中的红色错误行排查配置。')));
		}

		var perform_download = function(ev) {
			ev.preventDefault();
			var url_input = document.getElementById('core_download_url');
			var url = url_input ? url_input.value.trim() : '';
			
			var close_btn = E('button', {
				'class': 'cbi-button cbi-button-neutral',
				'style': 'display: none; margin-top: 15px;',
				'click': function() {
					ui.hideModal();
					location.reload();
				}
			}, _('关闭'));

			ui.showModal(_('正在下载核心'), [
				E('p', {}, _('正在下载 Mihomo 核心二进制文件... 这可能需要一些时间。')),
				E('pre', { 'id': 'download_log', 'style': 'max-height: 200px; overflow-y: auto; background: #222; color: #fff; padding: 10px; border-radius: 4px; font-family: monospace;' }, _('开始下载...\\n')),
				E('div', { 'class': 'right' }, [close_btn])
			]);

			var args = ['download_core'];
			if (url) {
				args.push(url);
			}

			return fs.exec('/usr/share/mihomo/helper.sh', args).then(function(res) {
				var pre = document.getElementById('download_log');
				if (pre) {
					pre.textContent += (res.stdout || '') + (res.stderr ? '\\n' + res.stderr : '');
				}
				close_btn.style.display = 'inline-block';
			}).catch(function(err) {
				var pre = document.getElementById('download_log');
				if (pre) {
					pre.textContent += '\\nERROR: ' + err.message;
				}
				close_btn.style.display = 'inline-block';
			});
		};

		var manager_fields = [
			E('label', { 'class': 'cbi-value-title' }, _('自定义下载地址 (选填)')),
			E('div', { 'class': 'cbi-value-field' }, [
				E('input', {
					'id': 'core_download_url',
					'type': 'text',
					'class': 'cbi-input-text',
					'placeholder': '留空则默认使用 GitHub 官方源下载',
					'style': 'width: 60%;'
				})
			])
		];
		
		var download_btn_field = E('div', { 'class': 'cbi-value' }, [
			E('div', { 'class': 'cbi-value-field' }, [
				E('button', {
					'class': 'cbi-button cbi-button-action',
					'click': perform_download
				}, _('下载并安装核心'))
			])
		]);

		var core_manager_body;
		if (is_installed) {
			var download_container = E('div', { 'style': 'display: none; margin-top: 15px; border-top: 1px dashed rgba(0,0,0,0.1); padding-top: 15px;' }, [
				E('div', { 'class': 'cbi-value' }, manager_fields),
				download_btn_field
			]);

			core_manager_body = E('div', {}, [
				E('table', { 'class': 'table' }, [
					E('tr', {}, [
						E('td', { 'width': '33%' }, _('安装状态')),
						E('td', {}, '<span style="color: #2ed573; font-weight: bold;">✔ 已启用并部署</span>')
					]),
					E('tr', {}, [
						E('td', {}, _('安装路径')),
						E('td', {}, E('code', {}, core_path))
					]),
					E('tr', {}, [
						E('td', {}, _('核心版本')),
						E('td', {}, E('strong', {}, core_ver))
					])
				]),
				E('div', { 'style': 'margin-top: 15px;' }, [
					E('button', {
						'class': 'cbi-button cbi-button-neutral',
						'click': function(ev) {
							ev.preventDefault();
							if (download_container.style.display === 'none') {
									download_container.style.display = 'block';
									ev.target.textContent = _('收起更新选项');
								} else {
									download_container.style.display = 'none';
									ev.target.textContent = _('更新/重新安装核心');
								}
						}
					}, _('更新/重新安装核心')),
					download_container
				])
			]);
		} else {
			core_manager_body = E('div', {}, [
				E('div', { 'style': 'padding: 10px 15px; background: rgba(255, 71, 87, 0.1); border-left: 4px solid #ff4757; color: #ff4757; font-weight: bold; border-radius: 4px; margin-bottom: 15px;' }, 
					_('⚠️ 未检测到 Mihomo 运行核心，请在下方点击下载安装。')
				),
				E('div', { 'class': 'cbi-value' }, manager_fields),
				download_btn_field
			]);
		}

		var group_rows = [];
		var group_names = Object.keys(proxy_groups);
		var selector_groups_count = 0;

		for (var i = 0; i < group_names.length; i++) {
			var gname = group_names[i];
			var g = proxy_groups[gname];
			if (g && g.type === 'Selector') {
				selector_groups_count++;
				
				var options = [];
				for (var j = 0; j < g.all.length; j++) {
					var nname = g.all[j];
					options.push(E('option', {
						'value': nname,
						'selected': (nname === g.now) ? 'selected' : null
					}, nname));
				}

				var select_el = E('select', {
					'class': 'cbi-input-select',
					'style': 'width: 100%; max-width: 280px; padding: 4px; border-radius: 4px; border: 1px solid rgba(0,0,0,0.15); background: white;',
					'data-group': gname,
					'change': function(ev) {
						var group = ev.target.getAttribute('data-group');
						var node = ev.target.value;
						
						ui.addNotification(null, E('p', _('正在切换节点：') + group + ' ➡ ' + node), 'info');
						
						return fs.exec('/usr/share/mihomo/helper.sh', ['select_node', group, node]).then(function(res) {
							if (res.code === 0) {
								ui.addNotification(null, E('p', _('节点切换成功！')), 'info');
							} else {
								ui.addNotification(null, E('p', _('节点切换失败：') + (res.stderr || res.stdout || '')), 'danger');
							}
						}).catch(function(err) {
							ui.addNotification(null, E('p', _('通信错误：') + err.message), 'danger');
						});
					}
				}, options);

				group_rows.push(E('tr', {}, [
					E('td', { 'style': 'font-weight: bold; vertical-align: middle; padding: 8px;' }, gname),
					E('td', { 'style': 'vertical-align: middle; padding: 8px;' }, E('span', { 'class': 'label info', 'style': 'background-color: #17a2b8; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px;' }, g.type.toUpperCase())),
					E('td', { 'style': 'vertical-align: middle; padding: 8px;' }, select_el)
				]));
			}
		}

		var auto_setup_btn = E('button', {
			'class': 'cbi-button cbi-button-action',
			'style': 'margin: 0;',
			'click': function(ev) {
				ev.preventDefault();
				var selGroups = [];
				var _gn = Object.keys(proxy_groups);
				for (var i = 0; i < _gn.length; i++) {
					var _g = proxy_groups[_gn[i]];
					if (_g && _g.type === 'Selector' && _g.all && _g.all.length) {
						selGroups.push({ name: _gn[i], now: _g.now, all: _g.all });
					}
				}
				if (selGroups.length === 0) {
					ui.addNotification(null, E('p', _('没有可自动切换的手动策略组。')), 'info');
					return;
				}
				var close_btn = E('button', {
					'class': 'cbi-button',
					'style': 'display: none; margin-top: 10px;',
					'click': function() { ui.hideModal(); }
				}, _('关闭'));
				var log_pre = E('pre', { 'id': 'auto_setup_log', 'style': 'max-height: 240px; overflow-y: auto; background: #222; color: #fff; padding: 10px; border-radius: 4px; font-family: monospace; font-size: 12px; white-space: pre-wrap;' }, _('① 正在测试全部节点速度，请稍候…') + '\\n');
				ui.showModal(_('自动设置代理'), [
					E('p', {}, _('一键测速并自动把各手动策略组切换到最快健康节点。')),
					log_pre,
					E('div', { 'class': 'right' }, [close_btn])
				]);
				var log = function(line) {
					var pre = document.getElementById('auto_setup_log');
					if (pre) pre.textContent += line + '\\n';
				};
				fs.exec('/usr/share/mihomo/helper.sh', ['test_all_nodes']).then(function(res) {
					var arr;
					try { arr = JSON.parse((res.stdout || '[]').trim()); } catch(e) { arr = []; }
					var delayMap = {};
					var healthy = 0;
					for (var i = 0; i < proxies.length; i++) {
						var d = arr[i];
						if (d && typeof d.delay === 'number' && d.delay >= 0) {
							delayMap[proxies[i].name] = d.delay;
							healthy++;
						}
					}
					if (healthy === 0) {
						log(_('✗ 未能测得任何健康节点。请确认核心服务已启动且节点服务器可达。'));
						close_btn.style.display = 'inline-block';
						return;
					}
					log(_('✓ 测得 ') + healthy + _(' 个健康节点，开始为各策略组选择最快节点…'));
					var chain = Promise.resolve();
					selGroups.forEach(function(sg) {
						chain = chain.then(function() {
							var best = null;
							for (var k = 0; k < sg.all.length; k++) {
								var nm = sg.all[k];
								if (nm in delayMap && (!best || delayMap[nm] < best.delay)) {
									best = { name: nm, delay: delayMap[nm] };
								}
							}
							if (!best) {
								log('• ' + sg.name + '：' + _('无健康节点，跳过'));
								return;
							}
							if (best.name === sg.now) {
								log('• ' + sg.name + ' → ' + best.name + ' (' + best.delay + 'ms) ' + _('已是最优，无需切换'));
								return;
							}
							return fs.exec('/usr/share/mihomo/helper.sh', ['select_node', sg.name, best.name]).then(function(r) {
								if (r.code === 0) {
									log('✓ ' + sg.name + ' → ' + best.name + ' (' + best.delay + 'ms) ' + _('切换成功'));
									var sel = document.querySelector('select[data-group="' + sg.name + '"]');
									if (sel) sel.value = best.name;
								} else {
									log('✗ ' + sg.name + ' ' + _('切换失败：') + (r.stderr || r.stdout || ''));
								}
							}).catch(function(err) {
								log('✗ ' + sg.name + ' ' + _('通信错误：') + err.message);
							});
						});
					});
					return chain.then(function() {
						log(_('完成。'));
						close_btn.style.display = 'inline-block';
					});
				}).catch(function(err) {
					log(_('✗ 测试失败：') + err.message);
					close_btn.style.display = 'inline-block';
				});
			}
		}, _('自动设置代理'));
		var proxy_groups_panel;
		if (controller_up && selector_groups_count > 0) {
			proxy_groups_panel = E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 15px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [
				E('div', { 'style': 'display: flex; align-items: center; justify-content: space-between; margin-top: 0; margin-bottom: 15px; border-bottom: 1px solid rgba(0,0,0,0.06); padding-bottom: 8px;' }, [
					E('h3', { 'style': 'margin: 0;' }, _('分流策略组管理 (实时切换节点)')),
					auto_setup_btn
				]),
				E('table', { 'class': 'table', 'style': 'margin: 0;' }, [
					E('thead', {}, [
						E('tr', {}, [
							E('th', { 'width': '40%', 'style': 'background: rgba(0,0,0,0.02);' }, _('策略组名称')),
							E('th', { 'width': '20%', 'style': 'background: rgba(0,0,0,0.02);' }, _('类型')),
							E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('选择节点'))
						])
					]),
					E('tbody', {}, group_rows)
				])
			]);
		} else {
			var pg_hint = controller_up
				? _('该订阅配置中暂无可选的策略组（selector）。')
				: _('Mihomo 核心未运行或控制器不可达，无法进行实时策略组切换。请先在上方「运行状态」点击「启动」，刷新本页面后再试。');
			proxy_groups_panel = E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 15px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [
				E('h3', { 'style': 'margin-top: 0; margin-bottom: 10px;' }, _('分流策略组管理')),
				E('div', { 'style': 'padding: 10px; text-align: center; color: #ff4757; background: rgba(255, 71, 87, 0.05); border-radius: 4px; font-weight: bold;' }, 
					pg_hint
				)
			]);
		}

		var node_cards = [];
		var delay_els = {};
		var valid_node_count = 0;
		for (var i = 0; i < proxies.length; i++) {
			var p = proxies[i];
			if (p && p.name && p.type) {
				valid_node_count++;
				var card_tip = _('节点类型') + '：' + p.type + '\\n' + _('服务器地址') + '：' + (p.server || '-');
				var delay_el = E('div', { 'style': 'font-size: 12px; color: #888; margin-top: 6px;' }, _('延时 —'));
				delay_els[p.name] = delay_el;
				var tip = E('div', { 'style': 'display:none; position:absolute; left:0; top:100%; z-index:50; margin-top:4px; background:#333; color:#fff; padding:6px 8px; border-radius:4px; font-size:11px; white-space:pre-line; max-width:240px; word-break:break-all;' }, card_tip);
				var card = E('div', {
					'style': 'position:relative; border:1px solid rgba(0,0,0,0.08); border-radius:8px; padding:10px; background:#fff; cursor:default; min-height:60px; display:flex; flex-direction:column; justify-content:space-between;',
					'onmouseover': (function(t) { return function() { t.style.display = 'block'; }; })(tip),
					'onmouseout': (function(t) { return function() { t.style.display = 'none'; }; })(tip)
				}, [
					E('div', { 'style': 'font-weight:bold; font-size:13px; line-height:1.3; word-break:break-all;' }, p.name),
					delay_el,
					tip
				]);
				node_cards.push(card);
			}
		}

		var sub_url = uci.get('mihomo', 'config', 'subscription_url') || '';

		var run_delay_test = function() {
			if (!valid_node_count) return;
			ui.addNotification(null, E('p', _('正在测试节点延时...')), 'info');
			for (var i = 0; i < proxies.length; i++) {
				var p = proxies[i];
				if (p && p.name && delay_els[p.name]) delay_els[p.name].textContent = _('测试中...');
			}
			fs.exec('/usr/share/mihomo/helper.sh', ['test_all_nodes']).then(function(res) {
				try {
					var arr = JSON.parse((res.stdout || '[]').trim());
					for (var i = 0; i < proxies.length; i++) {
						var p = proxies[i];
						var el = delay_els[p.name];
						if (!el) continue;
						var d = arr[i];
						if (d && typeof d.delay === 'number' && d.delay >= 0) {
							el.textContent = d.delay + ' ms';
						} else {
							var reason = (d && d.msg) || '';
							if (reason === 'controller_unreachable') el.textContent = _('控制器未连接');
							else if (reason) el.textContent = _('失败') + ':' + String(reason).slice(0, 16);
							else el.textContent = _('超时/失败');
						}
					}
				} catch (e) {
					for (var k in delay_els) delay_els[k].textContent = _('超时/失败');
				}
			}).catch(function() {
				for (var k in delay_els) delay_els[k].textContent = _('超时/失败');
			});
		};

		var node_test_btn = E('button', {
			'class': 'cbi-button cbi-button-action',
			'style': 'float: right; margin-top: -2px;',
			'click': function(ev) {
				ev.preventDefault();
				run_delay_test();
			}
		}, _('测试'));

		var node_clear_btn = E('button', {
			'class': 'cbi-button cbi-button-reset',
			'style': 'float: right; margin-top: -2px; margin-right: 8px;',
			'click': function(ev) {
				ev.preventDefault();
				if (!confirm(_('确定要删除所有已订阅的节点吗？此操作不可恢复，删除后需重新更新订阅。'))) return;
				ui.showModal(_('正在清空节点'), [ E('p', {}, _('正在删除所有已订阅的节点...')) ]);
				return fs.exec('/usr/share/mihomo/helper.sh', ['clear_subscription']).then(function(res) {
					ui.hideModal();
					ui.addNotification(null, E('p', _('已清空所有订阅节点。')), 'info');
					location.reload();
				}).catch(function(err) {
					ui.hideModal();
					ui.addNotification(null, E('p', _('清空节点失败：') + err.message), 'danger');
				});
			}
		}, _('清空节点'));

		var node_header_right = E('div', { 'style': 'display: flex; gap: 8px; align-items: center;' }, []);
		if (valid_node_count > 0) {
			node_header_right.appendChild(node_clear_btn);
			if (controller_up) node_header_right.appendChild(node_test_btn);
		}
		var node_list_header_children = [ E('h3', { 'style': 'margin-top: 0; margin-bottom: 0;' }, _('配置订阅节点列表')), node_header_right ];
		var node_list_header = E('div', { 'style': 'display: flex; align-items: center; justify-content: space-between;' }, node_list_header_children);

		var node_list_schedule = null;
		if (schedule.auto_update === '1') {
			var sched_txt = _('自动更新：每 ') + schedule.interval + _(' 小时');
			if (schedule.last_update && schedule.last_update !== '') {
				sched_txt += _('　|　上次更新：') + new Date(parseInt(schedule.last_update, 10) * 1000).toLocaleString();
			}
			if (schedule.next_update && schedule.next_update !== '') {
				sched_txt += _('　|　下次更新：') + new Date(parseInt(schedule.next_update, 10) * 1000).toLocaleString();
			} else if (schedule.has_url !== '1') {
				sched_txt += _('　|　未配置订阅链接');
			}
			node_list_schedule = E('div', {
				'style': 'margin-top: 8px; font-size: 12px; color: #888;'
			}, sched_txt);
		}

		var node_list_hint = null;
		if (valid_node_count > 0 && !controller_up) {
			node_list_hint = E('div', {
				'style': 'margin-top: 12px; padding: 10px 12px; border-radius: 6px; background: rgba(255, 159, 67, 0.08); border: 1px solid #ff9f43; color: #e67e22; font-size: 13px; line-height: 1.5;'
			}, _('Mihomo 核心未运行或控制器不可达，无法测试节点延时。请先在上方「运行状态」点击「启动」，刷新本页面后再测试。'));
		}

		var node_list_body;
		if (valid_node_count > 0) {
			node_list_body = E('div', { 'style': 'display: grid; grid-template-columns: repeat(auto-fill, minmax(100px, 200px)); gap: 10px; margin-top: 12px;' }, node_cards);
		} else if (parse_error) {
			var retry_update_btn = E('button', {
				'class': 'cbi-button cbi-button-action',
				'style': 'margin-top: 10px;',
				'click': function(ev) {
					ev.preventDefault();
					ui.showModal(_('正在下载订阅配置'), [
						E('p', {}, _('正在从订阅链接下载节点和规则配置... 请稍候。'))
					]);
					return fs.exec('/usr/share/mihomo/helper.sh', ['update_subscription', sub_url]).then(function(res) {
						ui.hideModal();
						if (res.code === 0) {
							ui.addNotification(null, E('p', _('订阅配置已成功更新！')), 'info');
							location.reload();
						} else {
							ui.addNotification(null, E('p', _('更新配置失败：') + (res.stderr || res.stdout || '')), 'danger');
						}
					}).catch(function(err) {
						ui.hideModal();
						ui.addNotification(null, E('p', _('下载订阅失败：') + err.message), 'danger');
					});
				}
			}, _('重新更新订阅'));
			node_list_body = E('div', { 'style': 'padding: 20px; text-align: center; color: #ff4757; background: rgba(255, 71, 87, 0.05); border-radius: 6px; border: 1px dashed #ff4757; line-height: 1.6;' }, [
				E('p', { 'style': 'font-weight: bold; margin: 0;' }, parse_error),
				retry_update_btn
			]);
		} else if (sub_url) {
			var quick_update_btn = E('button', {
				'class': 'cbi-button cbi-button-action',
				'style': 'margin-top: 10px;',
				'click': function(ev) {
					ev.preventDefault();
					ui.showModal(_('正在下载订阅配置'), [
						E('p', {}, _('正在从订阅链接下载节点和规则配置... 请稍候。'))
					]);
					return fs.exec('/usr/share/mihomo/helper.sh', ['update_subscription', sub_url]).then(function(res) {
						ui.hideModal();
						if (res.code === 0) {
							ui.addNotification(null, E('p', _('订阅配置已成功更新！')), 'info');
							location.reload();
						} else {
							ui.addNotification(null, E('p', _('更新配置失败：') + (res.stderr || res.stdout || '')), 'danger');
						}
					}).catch(function(err) {
						ui.hideModal();
						ui.addNotification(null, E('p', _('下载订阅失败：') + err.message), 'danger');
					});
				}
			}, _('立即更新订阅'));
			node_list_body = E('div', { 'style': 'padding: 20px; text-align: center; color: #ff9f43; background: rgba(255, 159, 67, 0.05); border-radius: 6px; border: 1px dashed #ff9f43;' }, [
				E('p', { 'style': 'font-weight: bold; margin: 0;' }, _('⚠️ 已配置订阅链接，但本地尚未下载节点数据。')),
				quick_update_btn
			]);
		} else {
			node_list_body = E('div', { 'style': 'padding: 15px; text-align: center; color: #999;' }, _('暂无可用节点信息，请先输入订阅链接并点击立即更新订阅。'));
		}

		// 系统代理日志：逐行渲染，错误/致命行红色加粗，告警行橙色
		var logs_pre = document.createElement('pre');
		logs_pre.setAttribute('style', 'width: 100%; height: 250px; overflow-y: auto; font-family: monospace; padding: 12px; border-radius: 6px; border: 1px solid rgba(0,0,0,0.12); background: rgba(0,0,0,0.02); resize: vertical; margin-bottom: 12px; font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;');
		function renderLogs(text) {
			while (logs_pre.firstChild) { logs_pre.removeChild(logs_pre.firstChild); }
			var lines = String(text || '').split('\\n');
			if (lines.length === 0 || (lines.length === 1 && lines[0] === '')) {
				var empty = document.createElement('div');
				empty.textContent = _('暂无日志记录。');
				empty.style.color = '#999';
				logs_pre.appendChild(empty);
				return;
			}
			for (var i = 0; i < lines.length; i++) {
				var line = lines[i];
				var row = document.createElement('div');
				row.textContent = line;
				if (/\\b(FATAL|FAT|ERROR|ERR|PANIC)\\b|\\blevel=(fatal|error)\\b/i.test(line)) {
					row.style.color = '#e03131';
					row.style.fontWeight = 'bold';
				} else if (/\\b(WARN|WARNING)\\b|\\blevel=warning\\b/i.test(line)) {
					row.style.color = '#e8590c';
				}
				logs_pre.appendChild(row);
			}
		}
		renderLogs(logs);
		var clear_logs_btn = E('button', {
			'class': 'cbi-button cbi-button-neutral',
			'style': 'margin: 0;',
			'click': function(ev) {
				ev.preventDefault();
				while (logs_pre.firstChild) { logs_pre.removeChild(logs_pre.firstChild); }
			}
		}, _('清空'));
		var download_logs_btn = E('button', {
			'class': 'cbi-button cbi-button-neutral',
			'style': 'margin: 0;',
			'click': function(ev) {
				ev.preventDefault();
				var orig = download_logs_btn.textContent;
				download_logs_btn.disabled = true;
				download_logs_btn.textContent = _('下载中…');
				fs.exec('/usr/share/mihomo/helper.sh', ['get_core_log']).then(function(res) {
					var text = (res && res.stdout) ? res.stdout : '';
					if (!text || !text.trim()) {
						ui.addNotification(null, E('p', _('当前无日志可下载。')), 'info');
						return;
					}
					var d = new Date();
					var p = function(n) { return (n < 10 ? '0' : '') + n; };
					var ts = d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
					var blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
					var url = URL.createObjectURL(blob);
					var a = document.createElement('a');
					a.href = url;
					a.download = 'mihomo-core-' + ts + '.log';
					document.body.appendChild(a);
					a.click();
					document.body.removeChild(a);
					setTimeout(function() { URL.revokeObjectURL(url); }, 1500);
				}).catch(function() {
					ui.addNotification(null, E('p', _('下载日志失败，请稍后重试。')), 'danger');
				}).then(function() {
					download_logs_btn.disabled = false;
					download_logs_btn.textContent = orig;
				});
			}
		}, _('下载日志'));

		var opBusy = (op_state.state === 'in_progress');
		var opPollCount = 0;
		function setOpBusy(busy, label) {
			opBusy = busy;
			var ids = ['btn_start', 'btn_stop', 'btn_restart'];
			for (var i = 0; i < ids.length; i++) {
				var bn = document.getElementById(ids[i]);
				if (bn) { bn.disabled = busy; bn.style.opacity = busy ? '0.5' : '1'; }
			}
			var st = document.getElementById('op_status');
			if (st) { st.textContent = label || ''; }
		}
		function pollOpState() {
			opPollCount++;
			if (opPollCount > 50) { ui.addNotification(null, E('p', _('操作超时，可能失败，可重试。')), 'warning'); setOpBusy(false, ''); return; }
			return fs.exec('/usr/share/mihomo/helper.sh', ['get_op_state']).then(function(res) {
				var o = {};
				try { o = JSON.parse((res.stdout || '').trim()); } catch(e) { o = { state: 'idle' }; }
				if (o.state === 'done') { location.reload(); return; }
				if (o.state === 'in_progress') {
					setOpBusy(true, _('操作进行中…已 ') + (o.elapsed || 0) + 's');
					setTimeout(pollOpState, 1500);
				} else {
					if (o.state === 'timeout') ui.addNotification(null, E('p', _('操作超时，可能失败，可重试。')), 'warning');
					setOpBusy(false, '');
				}
			}).catch(function() { setTimeout(pollOpState, 2000); });
		}
		function doServiceOp(op) {
			if (opBusy) return;
			var label = op === 'start' ? _('正在启动…') : (op === 'stop' ? _('正在停止…') : _('正在重启…'));
			setOpBusy(true, label);
			return fs.exec('/etc/init.d/mihomo', [op]).then(function() {
				setTimeout(pollOpState, 1000);
			}).catch(function(err) {
				ui.addNotification(null, E('p', _('操作失败：') + err.message), 'danger');
				setOpBusy(false, '');
			});
		}
		if (opBusy) { setOpBusy(true, _('操作进行中…')); setTimeout(pollOpState, 1000); }
		// Connectivity test panel: one-click reachability of key sites through the proxy.
		var conn_sites = ['百度', 'Google', 'YouTube', 'Facebook', 'TikTok'];
		var conn_cells = {};
		var conn_tbody = E('tbody', {}, []);
		for (var ci = 0; ci < conn_sites.length; ci++) {
			var cn = conn_sites[ci];
			var cell = E('td', { 'style': 'vertical-align: middle; padding: 8px; color: #888;' }, _('—'));
			conn_cells[cn] = cell;
			conn_tbody.appendChild(E('tr', {}, [
				E('td', { 'style': 'font-weight: bold; vertical-align: middle; padding: 8px;' }, cn),
				cell
			]));
		}
		var conn_btn = E('button', {
			'class': 'cbi-button cbi-button-action',
			'style': 'margin: 0;',
			'click': function(ev) {
				ev.preventDefault();
				for (var ck in conn_cells) { conn_cells[ck].textContent = _('测试中…'); conn_cells[ck].style.color = '#888'; }
				fs.exec('/usr/share/mihomo/helper.sh', ['test_connectivity']).then(function(res) {
					var arr;
					try { arr = JSON.parse((res.stdout || '[]').trim()); } catch(e) { arr = []; }
					var byName = {};
					for (var ai = 0; ai < arr.length; ai++) if (arr[ai] && arr[ai].name) byName[arr[ai].name] = arr[ai];
					for (var ck2 in conn_cells) {
						var d = byName[ck2];
						var el = conn_cells[ck2];
						if (d && d.ok && typeof d.delay === 'number') {
							el.textContent = d.delay + ' ms';
							el.style.color = (d.delay < 500) ? '#2f9e44' : '#e8590c';
						} else {
							el.textContent = _('不通');
							el.style.color = '#e03131';
						}
					}
				}).catch(function() {
					for (var ck3 in conn_cells) { conn_cells[ck3].textContent = _('失败'); conn_cells[ck3].style.color = '#e03131'; }
				});
			}
		}, _('一键测试'));
		var connectivity_panel = E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 15px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [
			E('div', { 'style': 'display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;' }, [
				E('h3', { 'style': 'margin: 0;' }, _('网站连通性测试')),
				conn_btn
			]),
			E('table', { 'class': 'table', 'style': 'margin: 0;' }, [
				E('thead', {}, [ E('tr', {}, [
					E('th', { 'width': '50%', 'style': 'background: rgba(0,0,0,0.02);' }, _('网站')),
					E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('延时'))
				]) ]),
				conn_tbody
			])
		]);

		var view_html = E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, _('豆豉代理仪表盘')),
			E('p', {}, _('管理 Mihomo 核心守护进程，监控运行状态并选择代理节点。')),

			// Status panel
			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, _('服务运行状态')),
				E('table', { 'class': 'table' }, [
					E('tr', {}, [
						E('td', { 'width': '33%' }, _('守护进程状态')),
						status_cell
					]),
					E('tr', {}, [
						E('td', {}, _('已安装核心版本')),
						E('td', {}, E('strong', {}, core_ver))
					]),
					E('tr', {}, [
						E('td', {}, _('插件版本')),
						E('td', {}, E('strong', {}, '__PKG_VERSION__'))
					])
				]),
				
				E('div', { 'class': 'cbi-section-node' }, [
					E('button', { 'id': 'btn_start', 'class': 'cbi-button cbi-button-apply', 'style': 'margin-right: 10px;', 'click': function(ev) { ev.preventDefault(); doServiceOp('start'); } }, _('启动')),
					E('button', { 'id': 'btn_stop', 'class': 'cbi-button cbi-button-reset', 'style': 'margin-right: 10px;', 'click': function(ev) { ev.preventDefault(); doServiceOp('stop'); } }, _('停止')),
					E('button', { 'id': 'btn_restart', 'class': 'cbi-button cbi-button-action', 'style': 'margin-right: 10px;', 'click': function(ev) { ev.preventDefault(); doServiceOp('restart'); } }, _('重启')),
					E('span', { 'id': 'op_status', 'style': 'margin-left: 10px; color: #666; font-size: 13px;' }, '')
				])
			]),
			// Proxy groups switching panel
			proxy_groups_panel,

			// Connectivity test panel
			connectivity_panel,

			// Nodes list panel
			E('div', { 'class': 'cbi-section' }, [
				node_list_header,
				node_list_schedule,
				node_list_body,
				node_list_hint
			].filter(function(x) { return x !== null; })),

			// Core Management panel
			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, _('核心程序管理')),
				core_manager_body
			]),

			// Logs panel
			E('div', { 'class': 'cbi-section' }, [
				E('div', { 'style': 'display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;' }, [
					E('h3', { 'style': 'margin: 0;' }, _('系统代理日志')),
					E('div', { 'style': 'display: flex; gap: 8px;' }, [ download_logs_btn, clear_logs_btn ])
				]),
				logs_pre
			])
		]);

		// 日志每 5s 自动刷新，便于实时观察核心启动报错
		this._logTimer = setInterval(function() {
			fs.exec('/usr/share/mihomo/helper.sh', ['get_core_log']).then(function(res) {
				renderLogs((res.stdout || '').trim());
			}).catch(function() {});
		}, 5000);

		// Traffic stats: initial load + refresh every 15s

		return view_html;
	},

	unload: function() {
		if (this._logTimer) { clearInterval(this._logTimer); this._logTimer = null; }
	}
});
""",
    "root/www/luci-static/resources/view/mihomo/traffic.js": """'use strict';
'require view';
'require ui';
'require fs';

return view.extend({
	_sortKey: 'bytes',
	_sortDir: 'desc',

	render: function() {
		var self = this;
		var fmtBytes = function(b) {
			b = Number(b) || 0;
			if (b < 1024) return b + ' B';
			var u = ['KB', 'MB', 'GB', 'TB']; var i = -1;
			do { b /= 1024; i++; } while (b >= 1024 && i < u.length - 1);
			return b.toFixed(b >= 100 ? 0 : (b >= 10 ? 1 : 2)) + ' ' + u[i];
		};
		var totalEl = E('div', { 'style': 'font-size: 26px; font-weight: bold; color: #2f9e44; margin: 4px 0 2px;' }, _('—'));
		var sinceEl = E('div', { 'style': 'color: #888; font-size: 12px; margin-bottom: 14px;' }, '');
		var countEl = E('span', { 'style': 'color: #888; font-size: 12px;' }, '');
		var tbody = E('tbody', {}, []);
		var domainTh = E('th', { 'style': 'cursor: pointer; background: rgba(0,0,0,0.02);', 'click': function() { setSort('domain'); } }, _('域名'));
		var bytesTh = E('th', { 'style': 'cursor: pointer; background: rgba(0,0,0,0.02);', 'click': function() { setSort('bytes'); } }, _('流量 ▼'));
		var dailyTbody = E('tbody', {}, []);
		var monthlyTbody = E('tbody', {}, []);
		var fillSummary = function(body, items, keyField) {
			while (body.firstChild) body.removeChild(body.firstChild);
			var arr = (items || []).slice();
			if (!arr.length) {
				body.appendChild(E('tr', {}, [ E('td', { 'colspan': 2, 'style': 'padding: 16px; color: #999; text-align: center;' }, _('暂无数据（稍候刷新）')) ]));
				return;
			}
			for (var i = 0; i < arr.length; i++) {
				body.appendChild(E('tr', {}, [
					E('td', { 'style': 'font-weight: bold; vertical-align: middle; padding: 8px;' }, arr[i][keyField]),
					E('td', { 'style': 'vertical-align: middle; padding: 8px;' }, fmtBytes(arr[i].bytes))
				]));
			}
		};
		var renderRows = function() {
			var data = self._lastData || { domains: [] };
			fillSummary(dailyTbody, data.daily, 'date');
			fillSummary(monthlyTbody, data.monthly, 'month');
			totalEl.textContent = fmtBytes(data.total || 0);
			if (data.since) {
				var dt = new Date(data.since * 1000);
				sinceEl.textContent = _('自') + ' ' + dt.getFullYear() + '-' + ('0' + (dt.getMonth() + 1)).slice(-2) + '-' + ('0' + dt.getDate()).slice(-2) + _(' 起，仅统计走代理节点的流量（5s 轮询差分，极短连接可能漏计）');
			}
			var ds = (data.domains || []).slice();
			var dir = (self._sortDir === 'asc') ? 1 : -1;
			if (self._sortKey === 'bytes') ds.sort(function(a, b) { return ((a.bytes || 0) - (b.bytes || 0)) * dir; });
			else ds.sort(function(a, b) { return String(a.domain).localeCompare(String(b.domain)) * dir; });
			while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
			countEl.textContent = _('共 ') + ds.length + _(' 个域名');
			if (!ds.length) {
				tbody.appendChild(E('tr', {}, [ E('td', { 'colspan': 2, 'style': 'padding: 16px; color: #999; text-align: center;' }, _('暂无数据（稍候刷新）')) ]));
				return;
			}
			for (var i = 0; i < ds.length; i++) {
				tbody.appendChild(E('tr', {}, [
					E('td', { 'style': 'font-weight: bold; vertical-align: middle; padding: 8px;' }, ds[i].domain),
					E('td', { 'style': 'vertical-align: middle; padding: 8px;' }, fmtBytes(ds[i].bytes))
				]));
			}
		};
		function setSort(key) {
			if (self._sortKey === key) self._sortDir = (self._sortDir === 'asc' ? 'desc' : 'asc');
			else { self._sortKey = key; self._sortDir = (key === 'bytes' ? 'desc' : 'asc'); }
			domainTh.textContent = _('域名') + (self._sortKey === 'domain' ? (self._sortDir === 'asc' ? ' ▲' : ' ▼') : '');
			bytesTh.textContent = _('流量') + (self._sortKey === 'bytes' ? (self._sortDir === 'asc' ? ' ▲' : ' ▼') : '');
			renderRows();
		}
		var loadTraffic = function() {
			fs.exec('/usr/share/mihomo/helper.sh', ['get_traffic']).then(function(res) {
				try { self._lastData = JSON.parse((res.stdout || '{}').trim()); } catch(e) {}
				renderRows();
			}).catch(function() {});
		};
		var refreshBtn = E('button', { 'class': 'cbi-button cbi-button-neutral', 'click': function(ev) { ev.preventDefault(); loadTraffic(); } }, _('刷新'));
		var resetBtn = E('button', { 'class': 'cbi-button cbi-button-reset', 'click': function(ev) {
			ev.preventDefault();
			if (!confirm(_('确定清零"按域名"统计？累计总量保留不变。'))) return;
			fs.exec('/usr/share/mihomo/helper.sh', ['reset_traffic_domains']).then(function() { loadTraffic(); });
		} }, _('清零域名统计'));
		loadTraffic();
		this._timer = setInterval(loadTraffic, 15000);
		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, _('代理流量统计')),
			E('p', {}, _('仅统计经过代理节点（非直连）的流量，按主域名归并（如 map.google.com → google.com）。点击表头可排序。')),
			E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 18px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [
				E('div', { 'style': 'display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;' }, [
					E('div', {}, [ E('div', { 'style': 'color: #666; font-size: 13px;' }, _('累计代理流量')), totalEl, sinceEl ]),
					E('div', { 'style': 'display: flex; gap: 8px; align-items: center;' }, [ countEl, refreshBtn, resetBtn ])
				]),
				E('table', { 'class': 'table', 'style': 'margin: 0;' }, [
					E('thead', {}, [ E('tr', {}, [ domainTh, bytesTh ]) ]),
					tbody
				])
			]),
			E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 18px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [
				E('h3', { 'style': 'margin: 0 0 4px; font-size: 16px;' }, _('按天汇总')),
				E('p', { 'style': 'color: #888; font-size: 12px; margin: 0 0 12px;' }, _('按北京时间（UTC+8）汇总，永久存储，不会清零。')),
				E('div', { 'style': 'max-height: 400px; overflow-y: auto;' }, [
					E('table', { 'class': 'table', 'style': 'margin: 0;' }, [
						E('thead', {}, [ E('tr', {}, [
							E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('日期')),
							E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('流量'))
						]) ]),
						dailyTbody
					])
				])
			]),
			E('div', { 'class': 'cbi-section', 'style': 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 18px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);' }, [
				E('h3', { 'style': 'margin: 0 0 4px; font-size: 16px;' }, _('按月汇总')),
				E('p', { 'style': 'color: #888; font-size: 12px; margin: 0 0 12px;' }, _('按北京时间（UTC+8）汇总，永久存储，不会清零。')),
				E('div', { 'style': 'max-height: 400px; overflow-y: auto;' }, [
					E('table', { 'class': 'table', 'style': 'margin: 0;' }, [
						E('thead', {}, [ E('tr', {}, [
							E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('月份')),
							E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('流量'))
						]) ]),
						monthlyTbody
					])
				])
			])
		]);
	},

	unload: function() {
		if (this._timer) { clearInterval(this._timer); this._timer = null; }
	}
});
""",

    "root/www/luci-static/resources/view/mihomo/settings.js": """'use strict';
'require view';
'require form';
'require ui';
'require fs';

return view.extend({
	render: function() {
		var m, s, o;

		m = new form.Map('mihomo', _('豆豉代理设置'),
			_('配置代理服务参数、DNS 解析器和订阅节点信息。'));
		
		m.restart = 'mihomo';

		s = m.section(form.TypedSection, 'mihomo', _('常规设置'));
		s.anonymous = true;

		o = s.option(form.ListValue, 'config_mode', _('配置模式'), _('选择配置来源：使用订阅配置 / 仅使用自定义配置 / 订阅与自定义混合（自定义作为订阅的补充，可追加节点组、规则等）。'));
		o.value('subscription', _('仅使用订阅配置'));
		o.value('custom', _('仅使用自定义配置'));
		o.value('mixed', _('混合：订阅 + 自定义（自定义补充订阅）'));
		o.default = 'subscription';

		o = s.option(form.Value, 'custom_config_path', _('自定义配置文件路径'), _('你的自定义 YAML 配置文件绝对路径（如 /etc/mihomo/custom.yaml）。仅在「仅自定义」或「混合」模式下生效，受 UCI 管理的端口/DNS/TUN 设置不会被此文件覆盖。'));
		o.placeholder = '/etc/mihomo/custom.yaml';
		o.rmempty = true;
		o.depends('config_mode', 'custom');
		o.depends('config_mode', 'mixed');

		o = s.option(form.Value, 'subscription_url', _('订阅链接'), _('用于下载节点配置的 Clash 兼容订阅链接。'));
		o.rmempty = true;
		o.depends('config_mode', 'subscription');
		o.depends('config_mode', 'mixed');

		// 订阅管理按钮，直接放在订阅链接下方
		o = s.option(form.DummyValue, '_update_btn', _('订阅管理'));
		o.rawhtml = true;
		o.depends('config_mode', 'subscription');
		o.depends('config_mode', 'mixed');
		o.cfgvalue = function(section_id) {
			var update_btn = E('button', {
				'class': 'cbi-button cbi-button-action',
				'click': function(ev) {
					ev.preventDefault();
					var url_input = document.getElementById('cbid.mihomo.' + section_id + '.subscription_url');
					var url = url_input ? url_input.value.trim() : '';
					if (!url) {
						url = uci.get('mihomo', section_id, 'subscription_url') || '';
					}
					
					if (!url) {
						ui.addNotification(null, E('p', _('请先输入有效的订阅链接并点击保存！')), 'warning');
						return;
					}

					// 将订阅链接缓存到 UCI，避免刷新或跳转后丢失
					uci.set('mihomo', section_id, 'subscription_url', url).then(function() {
						return uci.commit('mihomo');
					}).catch(function() {});

					ui.showModal(_('正在下载订阅配置'), [
						E('p', {}, _('正在从订阅链接下载节点和规则配置... 请稍候。'))
					]);

					return fs.exec('/usr/share/mihomo/helper.sh', ['update_subscription', url]).then(function(res) {
						ui.hideModal();
						if (res.code === 0) {
							ui.addNotification(null, E('p', _('订阅配置已成功更新！')), 'info');
						} else {
							ui.addNotification(null, E('p', _('更新配置失败：') + (res.stderr || res.stdout || '')), 'danger');
						}
					}).catch(function(err) {
						ui.hideModal();
						ui.addNotification(null, E('p', _('下载订阅失败：') + err.message), 'danger');
					});
				}
			}, _('立即更新订阅'));
			
			return E('div', {}, [update_btn]);
		};

		o = s.option(form.Flag, 'auto_update', _('定时更新订阅'), _('开启后，系统会每小时检查一次，并按下方设置的时间间隔自动重新下载订阅节点（需已配置订阅链接）。'));
	o.rmempty = false;
	o.depends('config_mode', 'subscription');
	o.depends('config_mode', 'mixed');

	o = s.option(form.Value, 'update_interval', _('更新间隔（小时）'), _('自动更新订阅的时间间隔，单位：小时。例如填 24 表示每天更新一次，填 6 表示每 6 小时更新一次。'));
	o.rmempty = true;
	o.datatype = 'uinteger';
	o.value('6', _('每 6 小时'));
	o.value('12', _('每 12 小时'));
	o.value('24', _('每天'));
	o.value('48', _('每 2 天'));
	o.placeholder = '24';

	o = s.option(form.DummyValue, '_clear_btn', _('节点管理'));
	o.rawhtml = true;
	o.depends('config_mode', 'subscription');
	o.depends('config_mode', 'mixed');
	o.cfgvalue = function(section_id) {
		var clear_btn = E('button', {
			'class': 'cbi-button cbi-button-reset',
			'click': function(ev) {
				ev.preventDefault();
				if (!confirm(_('确定要删除所有已订阅的节点吗？此操作不可恢复，删除后需重新更新订阅。'))) return;
				ui.showModal(_('正在清空节点'), [ E('p', {}, _('正在删除所有已订阅的节点...')) ]);
				return fs.exec('/usr/share/mihomo/helper.sh', ['clear_subscription']).then(function(res) {
					ui.hideModal();
					ui.addNotification(null, E('p', _('已清空所有订阅节点。')), 'info');
					location.reload();
				}).catch(function(err) {
					ui.hideModal();
					ui.addNotification(null, E('p', _('清空节点失败：') + err.message), 'danger');
				});
			}
		}, _('清空已订阅节点'));
		return E('div', {}, [clear_btn]);
	};

	o = s.option(form.Value, 'test_url', _('延时测试地址'), _('节点「测试」按钮用来探测延时的目标 URL。某些网络环境下默认地址不可达会导致所有节点都显示失败，可改为你网络中可正常访问的地址（如 https://www.google.com/generate_204）。留空使用默认。'));
		o.rmempty = true;
		o.placeholder = 'https://www.gstatic.com/generate_204';

		o = s.option(form.Flag, 'tun_enabled', _('启用 TUN 模式'), _('使用虚拟网卡 (TUN) 接口进行全局流量接管。接管更彻底但会消耗略高 CPU。'));
		o.rmempty = false;

		o = s.option(form.Flag, 'dns_hijack', _('劫持系统 DNS'), _('将 DNS 请求转发给 Mihomo 内置 DNS。配合「仅允许列表中的设备」时，仅列表内设备的 DNS 会被按源地址重定向到 Mihomo（fake-ip），其余设备继续使用路由器真实 DNS 直连。'));
		o.rmempty = false;

		o = s.option(form.ListValue, 'acl_mode', _('IP 转发控制模式'), _('选择走 Mihomo 代理转发的局域网设备范围。开启「劫持系统 DNS」时可与「仅允许列表」共存：仅列表内设备走代理，其余设备直连。'));
		o.value('all', _('所有设备'));
		o.value('whitelist', _('仅允许列表中的设备'));
		o.default = 'all';

		o = s.option(form.DynamicList, 'acl_ips', _('受控 IP 列表'), _('填入需要走代理的设备 IPv4/IPv6 地址或 CIDR 网段（如 192.168.1.100、192.168.1.0/24 或 fd00::1）。非列表中的设备流量将直接旁路，不走代理；其 DNS 也不会被劫持。'));
		o.depends('acl_mode', 'whitelist');

		// Advanced Section
		s = m.section(form.TypedSection, 'mihomo', _('高级设置'));
		s.anonymous = true;

		o = s.option(form.Value, 'core_path', _('核心程序路径'), _('Mihomo 核心程序的可执行文件绝对路径。'));
		o.placeholder = '/usr/bin/mihomo';
		o.rmempty = false;

		o = s.option(form.Value, 'config_path', _('订阅配置文件路径'), _('保存订阅节点和分流规则的 YAML 配置文件路径。'));
		o.placeholder = '/etc/mihomo/config.yaml';
		o.rmempty = false;
		o.depends('config_mode', 'subscription');
		o.depends('config_mode', 'mixed');

		o = s.option(form.Value, 'work_dir', _('工作目录'), _('Mihomo (Clash Meta) 工作数据库与配置根目录。'));
		o.placeholder = '/etc/mihomo';
		o.rmempty = false;

		o = s.option(form.Value, 'mix_port', _('Mixed 端口'), _('集成 HTTP(S) 和 SOCKS5 的混合代理端口。'));
		o.placeholder = '7890';
		o.rmempty = false;

		o = s.option(form.Value, 'tproxy_port', _('TProxy 端口'), _('TCP/UDP 透明代理使用的 TProxy 监听端口。'));
		o.placeholder = '7893';
		o.rmempty = false;

		o = s.option(form.Value, 'dns_port', _('DNS 端口'), _('Mihomo 本地 DNS 解析器监听端口。'));
		o.placeholder = '1053';
		o.rmempty = false;

		// --- 安全与 Geo（商业化加固）---
		o = s.option(form.Value, 'secret', _('控制器密钥 (Secret)'), _('外部控制器的访问密钥。留空将在下次启动核心时自动生成随机密钥（推荐）。使用第三方面板（metacubexd 等）时需填入此密钥。'));
		o.rmempty = true;
		o.password = true;

		o = s.option(form.Flag, 'geo_auto_update', _('自动更新 Geo 数据库'), _('让核心从下方镜像地址获取 GeoIP/GeoSite 并按周期自动更新，保证 GEOIP/GEOSITE 分流规则在离线/首次启动时也可用。'));
		o.rmempty = false;

		o = s.option(form.Value, 'geo_update_interval', _('Geo 更新间隔 (小时)'), _('GeoIP/GeoSite 自动更新的周期。'));
		o.placeholder = '24';
		o.rmempty = false;
		o.depends('geo_auto_update', '1');

		o = s.option(form.Value, 'geoip_mirror_url', _('GeoIP 镜像地址'), _('GeoIP 数据库下载地址，国内可改为加速镜像（如 jsDelivr/ghproxy）。'));
		o.placeholder = 'https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.dat';
		o.rmempty = true;
		o.depends('geo_auto_update', '1');

		o = s.option(form.Value, 'geosite_mirror_url', _('GeoSite 镜像地址'), _('GeoSite 数据库下载地址，国内可改为加速镜像。'));
		o.placeholder = 'https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geosite.dat';
		o.rmempty = true;
		o.depends('geo_auto_update', '1');

		// 立即更新 Geo 数据库：独立按钮，直接调 helper.sh，不触发表单保存/核心重启
		o = s.option(form.DummyValue, '_geo_btn', _('立即更新 Geo 数据库'), _('从上方镜像地址立即下载最新的 GeoIP/GeoSite 数据库。'));
		o.rawhtml = true;
		o.cfgvalue = function(section_id) {
			return E('button', {
				'class': 'cbi-button cbi-button-action',
				'click': function(ev) {
					ev.preventDefault();
					var btn = ev.target;
					btn.disabled = true;
					fs.exec('/usr/share/mihomo/helper.sh', ['update_geox']).then(function(res) {
						btn.disabled = false;
						if (res.code === 0) {
							ui.addNotification(null, E('p', {}, _('Geo 数据库更新成功：') + ' ' + (res.stdout || '')));
						} else {
							ui.addNotification(null, E('p', {}, _('更新失败：') + ' ' + (res.stderr || res.stdout || '')), 'error');
						}
					}).catch(function(err) {
						btn.disabled = false;
						ui.addNotification(null, E('p', {}, _('通信错误：') + ' ' + err.message), 'error');
					});
				}
			}, _('立即更新 Geo'));
		};

		return m.render().then(function(node) {
			setTimeout(function() {
				function updateWhitelistStatus() {
					var dns_hijack_el = document.getElementById('cbid.mihomo.config.dns_hijack');
					var tun_enabled_el = document.getElementById('cbid.mihomo.config.tun_enabled');
					var acl_mode_el = document.getElementById('cbid.mihomo.config.acl_mode');
					var acl_ips_container = document.getElementById('cbid.mihomo.config.acl_ips');
					
					var is_dns_hijack = dns_hijack_el && dns_hijack_el.checked;
					var is_tun_enabled = tun_enabled_el && tun_enabled_el.checked;
					var disable_whitelist = is_tun_enabled;
					
					if (acl_mode_el) {
						acl_mode_el.disabled = disable_whitelist;
						if (disable_whitelist) {
							acl_mode_el.value = 'all';
							var event = document.createEvent('HTMLEvents');
							event.initEvent('change', true, true);
							acl_mode_el.dispatchEvent(event);
						}
					}
					
					if (acl_ips_container) {
						var inputs = acl_ips_container.querySelectorAll('input, button');
						for (var i = 0; i < inputs.length; i++) {
							inputs[i].disabled = disable_whitelist;
						}
					}
				}
				
				var dns_hijack_el = document.getElementById('cbid.mihomo.config.dns_hijack');
				var tun_enabled_el = document.getElementById('cbid.mihomo.config.tun_enabled');
				if (dns_hijack_el) {
					dns_hijack_el.addEventListener('change', updateWhitelistStatus);
				}
				if (tun_enabled_el) {
					tun_enabled_el.addEventListener('change', updateWhitelistStatus);
				}
				updateWhitelistStatus();
			}, 100);
			return node;
		});
	}
});
"""
}

def _bump_version_string(current_ver):
    """Return the next version string after ``current_ver``.

    Revision form ``MAJOR.MINOR.PATCH-N`` bumps the revision (``N -> N+1``);
    plain dotted form bumps the last numeric segment. Non-numeric tails fall
    back to appending ``.1`` (revision form) or ``-1`` (dotted form).
    """
    if '-' in current_ver:
        ver_part, rev_part = current_ver.rsplit('-', 1)
        try:
            new_rev = int(rev_part) + 1
            return f"{ver_part}-{new_rev}"
        except ValueError:
            return current_ver + ".1"
    else:
        parts = current_ver.split('.')
        try:
            parts[-1] = str(int(parts[-1]) + 1)
            return '.'.join(parts)
        except ValueError:
            return current_ver + "-1"


def _compute_file_mode(rel_path, basename, is_control):
    """Return the tar entry mode for a regular file.

    In a control tarball, only the maintainer scripts
    (postinst/postrm/preinst/prerm) are executable. In the data tarball, the
    init.d scripts and ``helper.sh`` are executable. Everything else is 0o644.
    """
    if is_control:
        if basename in ("postinst", "postrm", "preinst", "prerm"):
            return 0o755
        return 0o644
    if "etc/init.d/" in rel_path or "usr/share/mihomo/helper.sh" in rel_path:
        return 0o755
    return 0o644


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
            content = re.sub(r'Version:\s*.*', f'Version: {PKG_VERSION}', content)
        # Bake the current package version into views that use the __PKG_VERSION__ placeholder
        content = content.replace('__PKG_VERSION__', PKG_VERSION)
            
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        # Ensure scripts are executable locally
        if ("CONTROL/" in rel_path and rel_path != "CONTROL/control") or "etc/init.d/" in rel_path or "usr/share/mihomo/helper.sh" in rel_path:
            os.chmod(full_path, 0o755)
    print("Source tree created successfully.")

def make_tar_gz(source_dir, output_filename, is_control=False):
    """Generates a reproducible tar.gz archive with root:root ownership and correct modes, including directories and using './' prefix."""
    print(f"Archiving '{source_dir}' -> '{output_filename}'...")
    # Pin the gzip header (fixed mtime, no embedded filename) so two builds of
    # the same inputs produce byte-identical archives.
    _raw = open(output_filename, "wb")
    _gz = gzip.GzipFile(filename="", fileobj=_raw, mode="wb", mtime=1700000000)
    with tarfile.open(fileobj=_gz, mode="w") as tar:
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
                tarinfo.mode = _compute_file_mode(rel_path, os.path.basename(full_path), is_control)
                        
                with open(full_path, "rb") as f:
                    tar.addfile(tarinfo, f)
    _gz.close()
    _raw.close()

def write_tar_gz_outer_archive(archive_path, file_list):
    """Writes the final .ipk as a gzipped tarball containing the three components."""
    print(f"Creating IPK archive (tar.gz format) '{archive_path}'...")
    _raw = open(archive_path, "wb")
    _gz = gzip.GzipFile(filename="", fileobj=_raw, mode="wb", mtime=1700000000)
    with tarfile.open(fileobj=_gz, mode="w") as tar:
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
    _gz.close()
    _raw.close()

def increment_version(script_path=None):
    """Increments the PKG_VERSION in the script file dynamically and updates memory variables.

    When ``script_path`` is omitted it defaults to this script (``__file__``);
    tests pass an explicit path to avoid mutating the real builder.
    """
    global PKG_VERSION, IPK_FILENAME
    
    script_path = script_path or __file__
    with open(script_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    match = re.search(r'PKG_VERSION\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        print("Warning: PKG_VERSION variable not found in script.")
        return
        
    current_ver = match.group(1)
    new_ver = _bump_version_string(current_ver)
            
    # Replace in file content
    new_line = f'PKG_VERSION = "{new_ver}"'
    content = re.sub(r'PKG_VERSION\s*=\s*["\']([^"\']+)["\']', new_line, content, count=1)
    
    # Save back to script
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(content)
        
    print(f"Incremented version: {current_ver} -> {new_ver}")
    PKG_VERSION = new_ver
    IPK_FILENAME = f"{PKG_NAME}_{PKG_VERSION}_{PKG_ARCH}.ipk"


def _git(args):
    """Run a git command in the repo root; return stripped stdout, or '' on failure.

    Returns empty string for any error (not a repo, git missing, non-zero exit,
    timeout) so callers can treat git as best-effort.
    """
    workspace = os.path.dirname(os.path.abspath(__file__))
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def generate_release_note(dist_dir):
    """Write ``dist/releaseNote.md`` describing this build's changes.

    Lists git commits since the last build (tracked via ``.release_baseline``
    in the repo root) and flags uncommitted working-tree changes -- important
    because this project's loop often builds *before* committing. Fully
    automatic: if git is unavailable or this isn't a repo, the note still
    records version / date / package name. Not part of the reproducible .ipk
    artifact, so a real build date is fine here.
    """
    workspace = os.path.dirname(os.path.abspath(__file__))
    note_path = os.path.join(dist_dir, "releaseNote.md")
    baseline_path = os.path.join(workspace, ".release_baseline")

    head = _git(["rev-parse", "HEAD"])
    baseline = ""
    if os.path.exists(baseline_path):
        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline = f.read().strip()

    log_lines = []
    note = ""
    if not head:
        note = "（当前目录非 git 仓库，无法提取提交记录）"
    elif baseline and baseline != head:
        raw = _git(["log", "--pretty=format:%h %s", f"{baseline}..HEAD"])
        if raw:
            log_lines = raw.splitlines()
            note = "自上次打包以来的提交"
        else:
            # baseline commit unreachable; fall back to recent history
            raw = _git(["log", "--pretty=format:%h %s", "-15"])
            log_lines = raw.splitlines() if raw else []
            note = "（上次基准不可达，列出最近提交）"
    elif baseline == head:
        note = "（自上次打包以来无新提交）"
    else:
        raw = _git(["log", "--pretty=format:%h %s", "-15"])
        log_lines = raw.splitlines() if raw else []
        note = "（首次打包基准，列出最近提交）"

    dirty = _git(["status", "--porcelain"])
    today = datetime.date.today().isoformat()

    out = []
    out.append(f"# Release Note — {PKG_NAME}")
    out.append("")
    out.append(f"**版本：** v{PKG_VERSION}")
    out.append(f"**发布日期：** {today}")
    out.append(f"**安装包：** `{IPK_FILENAME}`")
    out.append("")
    out.append("## 变更记录")
    out.append("")
    out.append(f"_{note}_")
    out.append("")
    if log_lines:
        for ln in log_lines:
            out.append(f"- {ln}")
    else:
        out.append("- （无）")
    out.append("")
    if dirty:
        out.append("## ⚠️ 未提交改动")
        out.append("")
        out.append("本次打包时工作树含未提交改动（未进入 git 提交，但已打进 ipk）：")
        out.append("")
        out.append("```")
        for ln in dirty.splitlines():
            out.append(ln)
        out.append("```")
        out.append("")
    out.append("## 安装")
    out.append("")
    out.append("```bash")
    out.append("python3 build_ipk.py && ./deploy.sh")
    out.append("```")
    out.append("")

    with open(note_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))

    # Record this build's HEAD as the baseline for the next build.
    if head:
        with open(baseline_path, "w", encoding="utf-8") as f:
            f.write(head)

    print(f"Release note generated at: {note_path}")


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

    # 6. Generate dist/releaseNote.md (auto-extracted from git commits)
    generate_release_note(dist_dir)

if __name__ == "__main__":
    main()
