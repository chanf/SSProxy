import datetime
import contextlib
import gzip
import io
import os
import re
import shutil
import subprocess
import tarfile

# Define configuration for the OpenClash replacement
PKG_NAME = "luci-app-ssproxy"
PKG_VERSION = "1.0.0-200"
PKG_ARCH = "all"
IPK_FILENAME = f"{PKG_NAME}_{PKG_VERSION}_{PKG_ARCH}.ipk"

# File contents mapping
src_files = {
    # Package metadata
    "CONTROL/control": """Package: luci-app-ssproxy
Version: 1.0.0-1
Depends: luci-base, ip-full, kmod-nft-tproxy, kmod-nft-nat, curl, ca-bundle
Architecture: all
Maintainer: Antigravity
Section: luci
Priority: optional
Description: Lightweight Mihomo (Clash Meta) client for iStoreOS with Firewall4 (nftables) integration
""",

    # Back up the complete UCI file before upgrade. The backup lives outside
    # the package manifest, so both chain records and other user settings can
    # survive remove/install cycles as well as ordinary upgrades.
    "CONTROL/preinst": """#!/bin/sh
if [ -z "$IPKG_INSTROOT" ]; then
    config_file="${MIHOMO_UCI_CONFIG:-/etc/config/mihomo}"
    backup_file="${MIHOMO_UCI_BACKUP:-/etc/mihomo/.uci_config_backup}"
    if [ -s "$config_file" ]; then
        mkdir -p "$(dirname "$backup_file")"
        cp -f "$config_file" "${backup_file}.tmp" && mv -f "${backup_file}.tmp" "$backup_file"
        chmod 600 "$backup_file"
    fi
fi
exit 0
""",
    
    # Post-installation script to clear LuCI index asynchronously
    "CONTROL/postinst": """#!/bin/sh
if [ -z "$IPKG_INSTROOT" ]; then
    config_file="${MIHOMO_UCI_CONFIG:-/etc/config/mihomo}"
    backup_file="${MIHOMO_UCI_BACKUP:-/etc/mihomo/.uci_config_backup}"
    if [ -s "$backup_file" ]; then
        mkdir -p "$(dirname "$config_file")"
        cp -f "$backup_file" "${config_file}.tmp" && mv -f "${config_file}.tmp" "$config_file"
        chmod 600 "$config_file"
    fi
    if [ "$SSPROXY_SKIP_RUNTIME_HOOKS" != "1" ]; then
        rm -f /tmp/luci-indexcache
        rm -f /tmp/luci-modulecache
        (sleep 3; /etc/init.d/rpcd restart) &
    fi
fi
exit 0
""",

    # Capture the latest UCI state before a real package removal. This is what
    # makes a later fresh install restore landing-node and data-link sections.
    "CONTROL/prerm": """#!/bin/sh
if [ -z "$IPKG_INSTROOT" ]; then
    config_file="${MIHOMO_UCI_CONFIG:-/etc/config/mihomo}"
    backup_file="${MIHOMO_UCI_BACKUP:-/etc/mihomo/.uci_config_backup}"
    if [ -s "$config_file" ]; then
        mkdir -p "$(dirname "$backup_file")"
        cp -f "$config_file" "${backup_file}.tmp" && mv -f "${backup_file}.tmp" "$backup_file"
        chmod 600 "$backup_file"
    fi
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
	option chain_front_node 'individual'
	option auto_update '0'
	option update_interval '24'
	option last_update ''
	option secret ''
	option geo_auto_update '1'
	option geo_update_interval '24'
	option geoip_mirror_url 'https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.dat'
	option geosite_mirror_url 'https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geosite.dat'
	option adblock_enabled '0'
""",
    # System Init Script managed by procd with TProxy/nftables/Dnsmasq redirection
    "root/etc/init.d/mihomo": """#!/bin/sh /etc/rc.common

START=95
USE_PROCD=1
EXTRA_COMMANDS="apply_network"
EXTRA_HELP="        apply_network  Wait for Mihomo and apply TProxy/DNS interception"

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
	local nft_status=$?
	if [ "$nft_status" -ne 0 ]; then
		logger -t mihomo "ERROR: Failed to apply nftables TProxy rules"
		return 1
	fi

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
	local state_file="/etc/mihomo/.dnsmasq_state"
	mkdir -p /etc/mihomo
	if [ ! -f "$state_file" ]; then
		local original_noresolv
		original_noresolv=$(uci -q get dhcp.@dnsmasq[0].noresolv)
		if [ -n "$original_noresolv" ]; then
			printf 'noresolv=%s\n' "$original_noresolv" > "$state_file"
		else
			printf 'noresolv=__unset__\n' > "$state_file"
		fi
		printf 'port=%s\n' "$dns_port" >> "$state_file"
		chmod 600 "$state_file"
	fi
	
	# Configure Dnsmasq to forward external requests to Mihomo DNS
	if ! uci -q show dhcp.@dnsmasq[0].server 2>/dev/null | grep -q "127.0.0.1#${dns_port}"; then
		uci add_list dhcp.@dnsmasq[0].server="127.0.0.1#$dns_port" || return 1
	fi
	uci set dhcp.@dnsmasq[0].noresolv="1" || return 1
	uci commit dhcp || return 1
	/etc/init.d/dnsmasq restart || return 1
	
	logger -t mihomo "DNS hijack enabled: Dnsmasq forwarding to Mihomo DNS on port $dns_port"
}

disable_dns_hijack() {
	local dns_port="$1" state_file="/etc/mihomo/.dnsmasq_state"
	if [ -f "$state_file" ]; then
		local state_port original_noresolv
		state_port=$(sed -n 's/^port=//p' "$state_file" | head -n 1)
		[ -n "$state_port" ] && dns_port="$state_port"
		original_noresolv=$(sed -n 's/^noresolv=//p' "$state_file" | head -n 1)
	else
		original_noresolv="__unset__"
	fi
	
	# Revert Dnsmasq changes
	uci del_list dhcp.@dnsmasq[0].server="127.0.0.1#$dns_port" 2>/dev/null
	if [ "$original_noresolv" = "__unset__" ]; then
		uci del dhcp.@dnsmasq[0].noresolv 2>/dev/null
	elif [ -n "$original_noresolv" ]; then
		uci set dhcp.@dnsmasq[0].noresolv="$original_noresolv"
	fi
	uci commit dhcp
	/etc/init.d/dnsmasq restart
	rm -f "$state_file"
	
	logger -t mihomo "DNS hijack disabled"
}

apply_network() {
	config_load mihomo
	local dns_port dns_hijack tproxy_port tun_enabled acl_mode
	local acl_v4="" acl_v6="" rip_v4="" rip_v6="" src_dns=0
	config_get dns_port config dns_port "1053"
	config_get_bool dns_hijack config dns_hijack 1
	config_get tproxy_port config tproxy_port "7893"
	config_get_bool tun_enabled config tun_enabled 0
	config_get acl_mode config acl_mode "all"

	# procd starts registered instances only after start_service returns. This
	# command runs as a separate one-shot instance, so the core can become ready
	# while we wait without deadlocking the service start.
	if ! /usr/share/mihomo/helper.sh wait_controller 30; then
		logger -t mihomo "ERROR: Mihomo controller did not become ready; network interception not applied"
		disable_tproxy
		return 1
	fi

	if [ "$tun_enabled" -ne 1 ]; then
		append_acl_ip() {
			[ -n "$1" ] || return 0
			if ! /usr/share/mihomo/helper.sh validate_acl_ip "$1"; then
				logger -t mihomo "WARN: ignoring invalid ACL IP/CIDR '$1'"
				return 0
			fi
			case "$1" in
				*:*) acl_v6="${acl_v6:+$acl_v6,}$1" ;;
				*) acl_v4="${acl_v4:+$acl_v4,}$1" ;;
			esac
		}
		config_list_foreach config acl_ips append_acl_ip

		if [ "$acl_mode" = "whitelist" ] && [ "$dns_hijack" -eq 1 ] && { [ -n "$acl_v4" ] || [ -n "$acl_v6" ]; }; then
			rip_v4=$(/usr/share/mihomo/helper.sh get_lan_ip 2>/dev/null)
			rip_v6=$(/usr/share/mihomo/helper.sh get_lan_ip6 2>/dev/null)
			{ [ -n "$rip_v4" ] || [ -n "$rip_v6" ]; } && src_dns=1
			[ "$src_dns" = "0" ] && logger -t mihomo "WARN: LAN IP not detected; falling back to global DNS hijack"
		fi

		if ! enable_tproxy "$tproxy_port" "$acl_mode" "$acl_v4" "$acl_v6" "$dns_hijack" "$dns_port" "$rip_v4" "$rip_v6"; then
			disable_tproxy
			return 1
		fi
	fi

	if [ "$dns_hijack" -eq 1 ] && [ "$src_dns" != "1" ]; then
		if ! enable_dns_hijack "$dns_port"; then
			logger -t mihomo "ERROR: Failed to configure dnsmasq DNS hijack"
			disable_tproxy
			return 1
		fi
	elif [ -f /etc/mihomo/.dnsmasq_state ]; then
		# whitelist 按源 DNAT（src_dns=1）或未开 DNS 劫持时，绝不能保留全局 dnsmasq 劫持：
		# 否则非白名单设备会从 dnsmasq 拿到 mihomo fake-ip，又被 whitelist 旁路直连 → 断网。
		disable_dns_hijack "$dns_port"
	fi
	logger -t mihomo "Mihomo network interception applied after controller became ready"
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
	
	# Validate before handing the config to procd. This catches fatal YAML/provider
	# errors without installing any traffic interception.
	: > /tmp/mihomo_core.log
	if ! "$core_path" -t -d "$work_dir" -f /tmp/mihomo_run.yaml >> /tmp/mihomo_core.log 2>&1; then
		logger -t mihomo "ERROR: Mihomo configuration validation failed"
		return 1
	fi

	# Start Daemon — capture the core's real stdout/stderr (incl. FATAL errors)
	# to a dedicated file so the dashboard can surface startup failures.
	# procd respawns append, so a crash loop stays visible.
	procd_open_instance
	# Wrap in sh -c so we can redirect output to the log file (procd execs directly,
	# no shell, so '>' must live inside the sh -c script). $0=mihomo, $1=core, $2=workdir.
	procd_set_param command sh -c 'ulimit -Hn 65535; ulimit -n 65535; "$1" -d "$2" -f /tmp/mihomo_run.yaml >> /tmp/mihomo_core.log 2>&1' mihomo "$core_path" "$work_dir"
	procd_set_param respawn
	procd_close_instance

	# One-shot procd instance: wait for the actual core process, then install
	# interception. It deliberately has no respawn policy.
	procd_open_instance network_setup
	procd_set_param command /etc/init.d/mihomo apply_network
	procd_set_param stdout 1
	procd_set_param stderr 1
	procd_close_instance

	# Unified telemetry collector: fetch /connections once every 5 seconds, then
	# reuse the snapshot for traffic, chain metrics and 15-second access history.
	procd_open_instance
	procd_set_param command /usr/share/mihomo/helper.sh collect_loop
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

	logger -t mihomo "Mihomo service instances registered successfully"
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
	if [ -z "$API_SECRET" ]; then
		curl "$@"
		return $?
	fi
	local auth_file rc
	auth_file=$(mktemp) || return 1
	chmod 600 "$auth_file"
	printf 'Authorization: Bearer %s\n' "$API_SECRET" > "$auth_file"
	curl -H "@$auth_file" "$@"
	rc=$?
	rm -f "$auth_file"
	return "$rc"
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

check_controller() {
	mihomo_curl -s -m 1 "http://127.0.0.1:${API_PORT}/version" >/dev/null 2>&1
}

wait_controller() {
	local timeout="$1" i=0
	case "$timeout" in ''|*[!0-9]*) timeout=30 ;; esac
	while [ "$i" -lt "$timeout" ]; do
		check_controller && return 0
		i=$((i + 1))
		sleep 1
	done
	return 1
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
	local expected_sha256="$2" custom_url=0

	if [ -n "$1" ]; then
		url="$1"
		custom_url=1
		local url_path="${url%%[?]*}"
		filename=$(basename "$url_path")
	fi
	[ -n "$filename" ] && [ "$filename" != "." ] && [ "$filename" != "/" ] || {
		echo "ERROR: Invalid core download filename" >&2
		return 1
	}
	case "$url" in
		https://*) ;;
		*)
			echo "ERROR: Core downloads require HTTPS" >&2
			return 1
		;;
	esac
	if [ "$custom_url" = "0" ]; then
		case "$filename" in
			mihomo-linux-amd64-compatible-v1.19.28.gz) expected_sha256="70d01cfb8cb7bf7a92fd1af16cb4b9553d90bb4eecde3b5c4849103e27c80ddb" ;;
			mihomo-linux-amd64-v1.19.28.gz) expected_sha256="d5967e079d9f793515a5a8193aabda455f7e012427eccd567dbc4f2f15498204" ;;
			mihomo-linux-arm64-v1.19.28.gz) expected_sha256="2474450cd1c41dfa53036a54a4e85579f493d3af524d86c3d4b8e2b240b56cd2" ;;
			mihomo-linux-armv7-v1.19.28.gz) expected_sha256="661a64466f79ab9c39cd3a1c1ece5371a4d93f87cb2d6610ff8c0dacaaa9f180" ;;
			mihomo-linux-mips-softfloat-v1.19.28.gz) expected_sha256="cfe16b8422198831b6e8d002a93786b0c39fe58a1e240ee4c38d1692d71865b0" ;;
			mihomo-linux-mipsle-softfloat-v1.19.28.gz) expected_sha256="cb181a3464310055a0c39c3fe8453c7ad9ad657cb24fbf1cadc2218899d0ec13" ;;
			*) expected_sha256="" ;;
		esac
	fi
	expected_sha256=$(printf '%s' "$expected_sha256" | tr 'A-F' 'a-f')
	printf '%s' "$expected_sha256" | grep -Eq '^[0-9a-f]{64}$' || {
		echo "ERROR: A SHA256 digest is required for this core asset" >&2
		return 1
	}
	command -v sha256sum >/dev/null 2>&1 || {
		echo "ERROR: sha256sum is required to verify the core" >&2
		return 1
	}

	local core_path=$(uci -q get mihomo.config.core_path || echo "/usr/bin/mihomo")
	local core_dir=$(dirname "$core_path")
	local download_dir
	
	mkdir -p "$core_dir"
	download_dir=$(mktemp -d /tmp/mihomo_download.XXXXXX) || {
		echo "ERROR: Cannot create download directory" >&2
		return 1
	}

	echo "Downloading Mihomo core from $url..."
	curl -fsSL --proto '=https' --tlsv1.2 -o "$download_dir/$filename" "$url"
	if [ $? -ne 0 ]; then
		echo "ERROR: Download failed" >&2
		rm -rf "$download_dir"
		return 1
	fi
	local actual_sha256
	actual_sha256=$(sha256sum "$download_dir/$filename" | awk '{print $1}')
	if [ "$actual_sha256" != "$expected_sha256" ]; then
		echo "ERROR: Core SHA256 verification failed" >&2
		rm -rf "$download_dir"
		return 1
	fi

	echo "Extracting binary..."
	local candidate="$download_dir/mihomo_candidate"
	case "$filename" in
	*.tar.gz|*.tgz)
		tar -zxf "$download_dir/$filename" -C "$download_dir"
		local bin_file=$(find "$download_dir" -type f -name 'mihomo*' | head -n 1)
		if [ -n "$bin_file" ]; then
			mv "$bin_file" "$candidate"
		else
			echo "ERROR: Could not find executable in tarball" >&2
			rm -rf "$download_dir"
			return 1
		fi
		;;
	*.gz)
		gunzip -c "$download_dir/$filename" > "$candidate" || {
			echo "ERROR: Could not extract core gzip" >&2
			rm -rf "$download_dir"
			return 1
		}
		;;
	*)
		mv "$download_dir/$filename" "$candidate"
		;;
	esac

	[ -s "$candidate" ] || {
		echo "ERROR: Extracted core is empty" >&2
		rm -rf "$download_dir"
		return 1
	}
	chmod +x "$candidate"
	mv -f "$candidate" "$core_path"
	rm -rf "$download_dir"
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
		if ! valid_http_url "$url"; then
			failed="$failed $fname(invalid-url)"
		elif curl -fsSL -o "$tmpd/$fname" "$url"; then
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

SUBSCRIPTION_URL_FILE="/etc/mihomo/.subscription_url"

subscription_sections() {
	uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=subscription$/\\1/p'
}

subscription_cache_dir() {
	local work_dir
	work_dir=$(uci -q get mihomo.config.work_dir || echo /etc/mihomo)
	printf '%s/subscriptions\n' "$work_dir"
}

enabled_subscription_count() {
	local count
	count=$(subscription_sections | while read -r sid; do [ "$(uci -q get mihomo.$sid.enabled)" != "0" ] && echo 1; done | wc -l | tr -d ' ')
	if [ "${count:-0}" -eq 0 ] && [ -n "$(uci -q get mihomo.config.subscription_url)" ]; then count=1; fi
	printf '%s\n' "${count:-0}"
}

migrate_legacy_subscription() {
	[ -n "$(subscription_sections | head -n 1)" ] && return 0
	local url sid config_path cache_dir
	url=$(uci -q get mihomo.config.subscription_url)
	[ -z "$url" ] && [ -f "$SUBSCRIPTION_URL_FILE" ] && url=$(cat "$SUBSCRIPTION_URL_FILE" 2>/dev/null)
	[ -n "$url" ] || return 0
	valid_http_url "$url" || return 1
	sid=$(uci add mihomo subscription) || return 1
	uci -q set mihomo.$sid.name="订阅 1"
	uci -q set mihomo.$sid.url="$url"
	uci -q set mihomo.$sid.enabled="1"
	uci -q commit mihomo
	config_path=$(uci -q get mihomo.config.config_path || echo /etc/mihomo/config.yaml)
	cache_dir=$(subscription_cache_dir)
	mkdir -p "$cache_dir"
	[ -s "$config_path" ] && grep -q '^proxies:' "$config_path" 2>/dev/null && cp -f "$config_path" "$cache_dir/$sid.yaml"
	logger -t mihomo "Migrated legacy subscription_url to section $sid"
}

save_subscription_url() {
	local url="$1"
	[ -z "$url" ] && url=$(uci -q get mihomo.config.subscription_url)
	[ -n "$url" ] || return 0
	mkdir -p "$(uci -q get mihomo.config.work_dir || echo /etc/mihomo)"
	printf '%s' "$url" > "$SUBSCRIPTION_URL_FILE"
	uci -q set mihomo.config.subscription_url="$url"
	uci -q commit mihomo
	migrate_legacy_subscription
}

restore_subscription_url() {
	local url
	url=$(uci -q get mihomo.config.subscription_url)
	if [ -z "$url" ] && [ -f "$SUBSCRIPTION_URL_FILE" ]; then
		url=$(cat "$SUBSCRIPTION_URL_FILE" 2>/dev/null)
		[ -n "$url" ] && uci -q set mihomo.config.subscription_url="$url"
		[ -n "$url" ] && uci -q commit mihomo
	fi
	migrate_legacy_subscription
}

download_subscription_file() {
	local url="$1" output="$2" scheme authority host realip resolve_arg="" resolve_port=443
	valid_http_url "$url" || return 1
	scheme="${url%%://*}"
	authority="${url#*://}"; authority="${authority%%/*}"; authority="${authority##*@}"
	case "$scheme" in http) resolve_port=80 ;; https) resolve_port=443 ;; esac
	case "$authority" in
		\\[*\\]:[0-9]*) host="${authority#\\[}"; host="${host%%\\]}"; resolve_port="${authority##*]:}" ;;
		*:[0-9]*) host="${authority%:*}"; resolve_port="${authority##*:}" ;;
		*) host="$authority" ;;
	esac
	case "$resolve_port" in ''|*[!0-9]*) resolve_port=443 ;; esac
	if [ -n "$host" ]; then
		for ns in 223.5.5.5 119.29.29.29 1.1.1.1; do
			realip=$(nslookup "$host" "$ns" 2>/dev/null | awk '/^Address:[[:space:]]/ {last=$NF} END {print last}')
			case "$realip" in *[!0-9.]*|"") realip="" ;; *.*.*.*) break ;; *) realip="" ;; esac
			[ -n "$realip" ] && break
		done
		[ -n "$realip" ] && resolve_arg="--resolve ${host}:${resolve_port}:${realip}"
	fi
	curl -fsSL -A "ClashMeta" $resolve_arg -o "$output" "$url" || return 1
	grep -q '^proxies:' "$output" 2>/dev/null || return 1
	grep -q -E '<html>|<!DOCTYPE html>' "$output" 2>/dev/null && return 1
	return 0
}

collect_proxy_node_names() {
	local cfg="$1"
	awk '
		function clean(s) { gsub(/^[[:space:]]+|[[:space:]]+$/, "", s); gsub(/^["'\''']|["'\''']$/, "", s); return s }
		/^proxies:[[:space:]]*(\\[\\])?[[:space:]]*$/ { inb=1; next }
		inb && /^[^[:space:]]/ { exit }
		inb && /^[[:space:]]*-[[:space:]]*name:[[:space:]]*/ {
			s=$0; sub(/^[[:space:]]*-[[:space:]]*name:[[:space:]]*/, "", s); print clean(s); next
		}
		inb && /^[[:space:]]*-[[:space:]]*\\{[[:space:]]*name:[[:space:]]*/ {
			s=$0; sub(/^[[:space:]]*-[[:space:]]*\\{[[:space:]]*name:[[:space:]]*/, "", s); sub(/,.*/, "", s); print clean(s)
		}
	' "$cfg"
}

extract_unique_proxy_entries() {
	local cfg="$1" names="$2" output="$3"
	awk -v names="$names" '
		function clean(s) { gsub(/^[[:space:]]+|[[:space:]]+$/, "", s); gsub(/^["'\''']|["'\''']$/, "", s); return s }
		function entry_name(line, s) {
			s=line
			sub(/^[[:space:]]*-[[:space:]]*/, "", s)
			sub(/^\\{[[:space:]]*/, "", s)
			if (s !~ /^name:[[:space:]]*/) return ""
			sub(/^name:[[:space:]]*/, "", s); sub(/,.*/, "", s)
			return clean(s)
		}
		function flush() {
			if (buf != "" && name != "" && !(name in seen)) { printf "%s", buf; print name >> names; seen[name]=1 }
			buf=""; name=""
		}
		BEGIN { while ((getline n < names) > 0) seen[n]=1; close(names) }
		/^proxies:[[:space:]]*(\\[\\])?[[:space:]]*$/ { inb=1; next }
		inb && /^[^[:space:]]/ { flush(); exit }
		inb && /^[[:space:]]*-[[:space:]]*/ {
			match($0, /^[[:space:]]*/); current_indent=RLENGTH
			if (entry_indent == 0) entry_indent=current_indent
			if (current_indent == entry_indent) { flush(); buf=$0 ORS; name=entry_name($0); next }
		}
		inb && buf != "" { buf=buf $0 ORS }
		END { flush() }
	' "$cfg" > "$output"
}

append_yaml_list_body() {
	local cfg="$1" key="$2" body="$3"
	[ -s "$body" ] || return 0
	align_list_body_indent "$cfg" "$key" "$body"
	if grep -q "^${key}:[[:space:]]*\\[\\][[:space:]]*$" "$cfg"; then
		awk -v key="$key" -v body="$body" '
			$0 ~ "^" key ":[[:space:]]*\\[\\][[:space:]]*$" { print key ":"; while ((getline l < body) > 0) print l; next }
			{ print }
		' "$cfg" > "${cfg}.tmp" && mv "${cfg}.tmp" "$cfg"
	elif grep -q "^${key}:[[:space:]]*$" "$cfg"; then
		awk -v key="$key" -v body="$body" '
			$0 ~ "^" key ":[[:space:]]*$" { print; while ((getline l < body) > 0) print l; next }
			{ print }
		' "$cfg" > "${cfg}.tmp" && mv "${cfg}.tmp" "$cfg"
	else
		{ echo "$key:"; cat "$body"; } >> "$cfg"
	fi
}

inject_aggregate_into_selectors() {
	local cfg="$1" group="$2"
	awk -v group="$group" '
		function add_to_inline_proxies(line, quoted) {
			quoted = "\\"" group "\\""
			if (line ~ /proxies:[[:space:]]*\\[[[:space:]]*\\]/) {
				sub(/proxies:[[:space:]]*\\[[[:space:]]*\\]/, "proxies: [" quoted "]", line)
			} else {
				sub(/proxies:[[:space:]]*\\[[[:space:]]*/, "&" quoted ", ", line)
			}
			return line
		}
		/^proxy-groups:[[:space:]]*$/ { in_groups=1; print; next }
		in_groups && /^[^[:space:]]/ { in_groups=0 }
		in_groups && /^[[:space:]]*-[[:space:]]*\\{/ && /type:[[:space:]]*select([[:space:],}]|$)/ && /proxies:[[:space:]]*\\[/ {
			print add_to_inline_proxies($0); next
		}
		in_groups && /^[[:space:]]*-[[:space:]]*name:/ { is_select=0; added=0 }
		in_groups && /^[[:space:]]+type:[[:space:]]*select[[:space:]]*(#.*)?$/ { is_select=1 }
		in_groups && is_select && !added && /^[[:space:]]+proxies:[[:space:]]*\\[/ {
			print add_to_inline_proxies($0); added=1; next
		}
		in_groups && is_select && !added && /^[[:space:]]+proxies:[[:space:]]*$/ {
			print; match($0, /^[[:space:]]*/); indent=substr($0, 1, RLENGTH) "  "; print indent "- \\"" group "\\""; added=1; next
		}
		{ print }
	' "$cfg" > "${cfg}.tmp" && mv "${cfg}.tmp" "$cfg"
}

merge_subscription_configs() {
	local output="$1"; shift
	[ "$#" -gt 0 ] || return 1
	local input_count="$#" tmpd first cfg body names group="SSProxy - 全部订阅"
	tmpd=$(mktemp -d) || return 1
	first="$1"; shift
	cp "$first" "$output" || { rm -rf "$tmpd"; return 1; }
	if [ "$input_count" -eq 1 ]; then rm -rf "$tmpd"; return 0; fi
	names="$tmpd/names"
	collect_proxy_node_names "$output" > "$names"
	for cfg in "$@"; do
		body="$tmpd/extra"
		extract_unique_proxy_entries "$cfg" "$names" "$body"
		append_yaml_list_body "$output" "proxies" "$body"
	done
	inject_aggregate_into_selectors "$output" "$group"
	body="$tmpd/group"
	{
		echo "  - name: \\"$(yaml_quote "$group")\\""
		echo "    type: select"
		echo "    proxies:"
		while IFS= read -r name; do [ -n "$name" ] && echo "      - \\"$(yaml_quote "$name")\\""; done < "$names"
	} > "$body"
	append_yaml_list_body "$output" "proxy-groups" "$body"
	rm -rf "$tmpd"
	grep -q '^proxies:' "$output"
}

update_subscriptions() {
	[ "$(uci -q get mihomo.config.config_mode || echo subscription)" != "custom" ] || {
		echo "ERROR: 当前为仅自定义配置模式" >&2; return 1;
	}
	migrate_legacy_subscription
	local cache_dir config_path tmpd sid enabled url name fresh=0 stale=0 failed=0 available=0
	cache_dir=$(subscription_cache_dir)
	config_path=$(uci -q get mihomo.config.config_path || echo /etc/mihomo/config.yaml)
	mkdir -p "$cache_dir" "$(dirname "$config_path")"
	tmpd=$(mktemp -d) || return 1
	set --
	local known_sections=" $(subscription_sections | tr '\n' ' ') " cached_file cached_sid
	for cached_file in "$cache_dir"/*.yaml; do
		[ -e "$cached_file" ] || continue
		cached_sid=$(basename "$cached_file" .yaml)
		case "$known_sections" in
			*" $cached_sid "*) ;;
			*) rm -f "$cached_file" ;;
		esac
	done
	for sid in $(subscription_sections); do
		enabled=$(uci -q get mihomo.$sid.enabled); [ "$enabled" = "0" ] && continue
		url=$(uci -q get mihomo.$sid.url); name=$(uci -q get mihomo.$sid.name); [ -n "$name" ] || name="$sid"
		if [ -n "$url" ] && download_subscription_file "$url" "$tmpd/$sid.yaml"; then
			mv "$tmpd/$sid.yaml" "$cache_dir/$sid.yaml"; fresh=$((fresh + 1))
			logger -t mihomo "Subscription '$name' updated"
		elif [ -s "$cache_dir/$sid.yaml" ]; then
			stale=$((stale + 1)); logger -t mihomo "Subscription '$name' update failed; using cache"
		else
			failed=$((failed + 1)); logger -t mihomo "Subscription '$name' unavailable"
			continue
		fi
		set -- "$@" "$cache_dir/$sid.yaml"; available=$((available + 1))
	done
	if [ "$available" -eq 0 ]; then
		rm -rf "$tmpd"; echo "ERROR: No enabled subscription cache is available" >&2; return 1
	fi
	merge_subscription_configs "$tmpd/merged.yaml" "$@" || {
		rm -rf "$tmpd"; echo "ERROR: Failed to merge subscriptions" >&2; return 1;
	}
	[ -s "$config_path" ] && cp -f "$config_path" "${config_path}.bak"
	mv "$tmpd/merged.yaml" "$config_path"
	rm -rf "$tmpd"
	[ "$fresh" -gt 0 ] && uci -q set mihomo.config.last_update="$(date +%s)"
	uci -q commit mihomo
	local nodes
	nodes=$(collect_proxy_node_names "$config_path" | wc -l | tr -d ' ')
	if pidof mihomo >/dev/null 2>&1; then /etc/init.d/mihomo restart; fi
	printf '{"updated":%s,"cached":%s,"failed":%s,"available":%s,"nodes":%s}\n' "$fresh" "$stale" "$failed" "$available" "${nodes:-0}"
}

update_subscription() {
	local url="$1" sid
	if [ -n "$url" ]; then
		valid_http_url "$url" || { echo "ERROR: Invalid subscription URL" >&2; return 1; }
		uci -q set mihomo.config.subscription_url="$url"
		migrate_legacy_subscription
		sid=$(subscription_sections | head -n 1)
		[ -n "$sid" ] && uci -q set mihomo.$sid.url="$url"
		uci -q commit mihomo
	fi
	update_subscriptions
}

clear_subscription() {
	local config_path cache_dir
	config_path=$(uci -q get mihomo.config.config_path || echo /etc/mihomo/config.yaml)
	cache_dir=$(subscription_cache_dir)
	[ -s "$config_path" ] && cp -f "$config_path" "${config_path}.bak"
	rm -f "$config_path" "$cache_dir"/*.yaml
	uci -q set mihomo.config.last_update=''
	uci -q commit mihomo
	logger -t mihomo "All subscription caches cleared"
	if pidof mihomo >/dev/null 2>&1; then /etc/init.d/mihomo restart; fi
	echo '{"success":true,"msg":"已清空所有订阅节点缓存"}'
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

# Downloads a fresh subscription only when auto_update is enabled, a URL is
# configured, and the configured interval has elapsed.
auto_update_now() {
	local enabled=$(uci -q get mihomo.config.auto_update)
	[ "$enabled" = "1" ] || return 0
	migrate_legacy_subscription
	local subscription_count
	subscription_count=$(enabled_subscription_count)
	[ "${subscription_count:-0}" -gt 0 ] || { logger -t mihomo "auto_update: no enabled subscription configured"; return 0; }
	local interval=$(uci -q get mihomo.config.update_interval || echo 24)
	case "$interval" in ''|*[!0-9]*) interval=24 ;; esac
	[ "$interval" -lt 1 ] && interval=1
	local last=$(uci -q get mihomo.config.last_update)
	local now=$(date +%s)
	if [ -n "$last" ] && [ "$last" -gt 0 ] 2>/dev/null; then
		local elapsed=$((now - last))
		if [ "$elapsed" -lt $((interval * 3600)) ]; then
			logger -t mihomo "auto_update: skipped, next run in $((interval * 3600 - elapsed))s"
			return 0
		fi
	fi
	logger -t mihomo "auto_update: starting scheduled batch update"
	update_subscriptions
}

# Report auto-update schedule state for the UI.
get_schedule() {
	local enabled=$(uci -q get mihomo.config.auto_update)
	local interval=$(uci -q get mihomo.config.update_interval || echo 24)
	case "$interval" in ''|*[!0-9]*) interval=24 ;; esac
	[ "$interval" -lt 1 ] && interval=1
	local last=$(uci -q get mihomo.config.last_update)
	migrate_legacy_subscription
	local subscription_count next=""
	subscription_count=$(enabled_subscription_count)
	if [ "$enabled" = "1" ] && [ "${subscription_count:-0}" -gt 0 ]; then
		if [ -n "$last" ] && [ "$last" -gt 0 ] 2>/dev/null; then
			next=$((last + interval * 3600))
		fi
	fi
	printf '{"auto_update":%s,"interval":%s,"last_update":%s,"next_update":%s,"has_url":%s,"subscription_count":%s}\\n' \
		"$(json_quote "$enabled")" "$(json_quote "$interval")" "$(json_quote "$last")" \
		"$(json_quote "$next")" "$(json_quote "$([ "${subscription_count:-0}" -gt 0 ] && echo 1 || echo 0)")" "${subscription_count:-0}"
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
		valid_rule_domain "$domain" || { logger -t mihomo "access_rule skipped: invalid domain"; continue; }
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

# --- Chain proxy: landing-node assets + device data links ---

chain_log() {
	logger -t mihomo-chain "$*"
}

yaml_quote() {
	printf '%s' "$1" | tr '\r\n' '  ' | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g'
}

valid_rule_domain() {
	local value="$1"
	[ -n "$value" ] && [ "${#value}" -le 253 ] 2>/dev/null || return 1
	printf '%s' "$value" | awk 'BEGIN { q=sprintf("%c",34); a=sprintf("%c",39); bad=0 } /[[:space:],]/ { bad=1 } { if (index($0,q) || index($0,a)) bad=1 } END { exit bad }'
}

valid_http_url() {
	local value="$1"
	case "$value" in
		http://*|https://*) ;;
		*) return 1 ;;
	esac
	printf '%s' "$value" | awk 'BEGIN { bad=0 } /[[:space:]\\047\\042\\r\\n]/ { bad=1 } END { exit bad }'
}

validate_acl_ip() {
	local value="$1" base prefix
	[ -n "$value" ] || return 1
	case "$value" in *[!0-9A-Fa-f:./]*) return 1 ;; esac
	base="$value"; prefix=""
	if [ "${value#*/}" != "$value" ]; then
		base="${value%%/*}"; prefix="${value##*/}"
		case "$prefix" in ''|*[!0-9]*) return 1 ;; esac
	fi
	case "$base" in
		*:*)
			[ -z "$prefix" ] || [ "$prefix" -le 128 ] 2>/dev/null || return 1
			printf '%s' "$base" | grep -q ':' || return 1
			;;
		*)
			[ -z "$prefix" ] || [ "$prefix" -le 32 ] 2>/dev/null || return 1
			printf '%s' "$base" | awk -F. 'NF == 4 { ok=1; for (i=1; i<=4; i++) if ($i !~ /^[0-9]+$/ || $i > 255) ok=0 } END { exit !ok }'
			;;
	esac
}

json_quote() {
	local value
	value=$(printf '%s' "$1" | tr '\r\n' '  ' | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g')
	printf '"%s"' "$value"
}

# Match generated list items to the indentation already used by a subscription.
# Both 2-space and 4-space list indentation are valid YAML, but mixing them in
# one block makes later items children of the generated item.
align_list_body_indent() {
	local cfg="$1" key="$2" body="$3"
	[ -f "$cfg" ] && [ -s "$body" ] || return 0
	local indent
	indent=$(awk -v key="$key:" '
		$0 == key { in_block=1; next }
		in_block && /^[^[:space:]]/ { exit }
		in_block && /^[[:space:]]*-/ { s=$0; sub(/-.*/, "", s); print length(s); exit }
	' "$cfg")
	case "$indent" in ''|*[!0-9]*) return 0 ;; esac
	[ "$indent" -gt 2 ] || return 0
	local extra=$((indent - 2))
	awk -v count="$extra" 'BEGIN { pad=""; for (i=0; i<count; i++) pad=pad " " } { print pad $0 }' "$body" > "${body}.tmp" && mv "${body}.tmp" "$body"
}

safe_section_id() {
	printf '%s' "$1" | tr -c 'A-Za-z0-9_' '_'
}

landing_proxy_name() {
	echo "ssproxy-landing-$(safe_section_id "$1")"
}

data_link_group_name() {
	echo "ssproxy-chain-$(safe_section_id "$1")"
}

chain_front_node() {
	local node
	node=$(uci -q get mihomo.config.chain_front_node)
	[ -n "$node" ] || node="individual"
	echo "$node"
}

effective_data_link_node() {
	local sid="$1" cfg="$2" quiet="$3" node
	node=$(chain_front_node)
	if [ "$node" = "individual" ]; then
		node=$(uci -q get mihomo.$sid.subscription_node)
		[ -n "$node" ] || { [ "$quiet" = "1" ] || chain_log "data_link $sid skipped: subscription node required in individual mode"; return 1; }
	fi
	if [ -n "$cfg" ] && [ -f "$cfg" ]; then
		collect_proxy_names "$cfg" | grep -qxF "$node" || { [ "$quiet" = "1" ] || chain_log "data_link $sid skipped: effective front node '$node' not found"; return 1; }
	fi
	echo "$node"
}

landing_node_exists() {
	local wanted="$1"
	uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=landing_node$/\\1/p' | grep -qxF "$wanted"
}

landing_node_valid() {
	local sid="$1" quiet="$2"
	local enabled type server port password cipher uuid
	landing_node_exists "$sid" || { [ "$quiet" = "1" ] || chain_log "landing_node $sid skipped: section not found"; return 1; }
	enabled=$(uci -q get mihomo.$sid.enabled); [ "$enabled" = "1" ] || return 1
	type=$(uci -q get mihomo.$sid.type)
	server=$(uci -q get mihomo.$sid.server)
	port=$(uci -q get mihomo.$sid.port)
	case "$port" in ''|*[!0-9]*) [ "$quiet" = "1" ] || chain_log "landing_node $sid skipped: invalid port"; return 1 ;; esac
	[ "$port" -ge 1 ] 2>/dev/null && [ "$port" -le 65535 ] 2>/dev/null || { [ "$quiet" = "1" ] || chain_log "landing_node $sid skipped: port out of range"; return 1; }
	[ -n "$server" ] || { [ "$quiet" = "1" ] || chain_log "landing_node $sid skipped: server required"; return 1; }
	password=$(uci -q get mihomo.$sid.password)
	cipher=$(uci -q get mihomo.$sid.cipher)
	uuid=$(uci -q get mihomo.$sid.uuid)
	case "$type" in
		socks5|http) ;;
		ss) [ -n "$cipher" ] && [ -n "$password" ] || { [ "$quiet" = "1" ] || chain_log "landing_node $sid skipped: SS cipher/password required"; return 1; } ;;
		trojan) [ -n "$password" ] || { [ "$quiet" = "1" ] || chain_log "landing_node $sid skipped: Trojan password required"; return 1; } ;;
		vmess|vless) [ -n "$uuid" ] || { [ "$quiet" = "1" ] || chain_log "landing_node $sid skipped: UUID required for $type"; return 1; } ;;
		*) [ "$quiet" = "1" ] || chain_log "landing_node $sid skipped: unsupported type '$type'"; return 1 ;;
	esac
	if [ "$type" = "vmess" ] || [ "$type" = "vless" ]; then
		local network
		network=$(uci -q get mihomo.$sid.network); [ -n "$network" ] || network=tcp
		case "$network" in
			tcp|ws|grpc|h2|http) ;;
			*) [ "$quiet" = "1" ] || chain_log "landing_node $sid skipped: unsupported network '$network'"; return 1 ;;
		esac
	fi
	return 0
}

# Output list entries for the top-level `proxies:` block.
emit_landing_proxies_yaml() {
	local only_sid="$1" override_name="$2" dialer_proxy="$3"
	uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=landing_node$/\\1/p' | while read -r sid; do
		[ -z "$only_sid" ] || [ "$sid" = "$only_sid" ] || continue
		landing_node_valid "$sid" || continue
		local name type server port username password cipher uuid alter_id tls sni flow network scv
		name=$(landing_proxy_name "$sid")
		[ -z "$override_name" ] || name="$override_name"
		type=$(uci -q get mihomo.$sid.type)
		server=$(uci -q get mihomo.$sid.server)
		port=$(uci -q get mihomo.$sid.port)
		username=$(uci -q get mihomo.$sid.username)
		password=$(uci -q get mihomo.$sid.password)
		cipher=$(uci -q get mihomo.$sid.cipher)
		uuid=$(uci -q get mihomo.$sid.uuid)
		alter_id=$(uci -q get mihomo.$sid.alter_id); [ -n "$alter_id" ] || alter_id=0
		tls=$(uci -q get mihomo.$sid.tls)
		sni=$(uci -q get mihomo.$sid.sni)
		flow=$(uci -q get mihomo.$sid.flow)
		network=$(uci -q get mihomo.$sid.network); [ -n "$network" ] || network=tcp
		scv=$(uci -q get mihomo.$sid.skip_cert_verify)
		echo "  - name: \\"$(yaml_quote "$name")\\""
		echo "    type: $type"
		echo "    server: \\"$(yaml_quote "$server")\\""
		echo "    port: $port"
		[ -n "$dialer_proxy" ] && echo "    dialer-proxy: \\"$(yaml_quote "$dialer_proxy")\\""
		case "$type" in
			socks5|http)
				[ -n "$username" ] && echo "    username: \\"$(yaml_quote "$username")\\""
				[ -n "$password" ] && echo "    password: \\"$(yaml_quote "$password")\\""
				[ "$tls" = "1" ] && echo "    tls: true"
				[ "$scv" = "1" ] && echo "    skip-cert-verify: true"
				[ "$type" = "socks5" ] && echo "    udp: true"
				;;
			ss)
				echo "    cipher: \\"$(yaml_quote "$cipher")\\""
				echo "    password: \\"$(yaml_quote "$password")\\""
				echo "    udp: true"
				;;
			trojan)
				echo "    password: \\"$(yaml_quote "$password")\\""
				[ -n "$sni" ] && echo "    sni: \\"$(yaml_quote "$sni")\\""
				[ "$scv" = "1" ] && echo "    skip-cert-verify: true"
				echo "    udp: true"
				;;
			vmess)
				echo "    uuid: \\"$(yaml_quote "$uuid")\\""
				echo "    alterId: $alter_id"
				echo "    cipher: \\"$(yaml_quote "${cipher:-auto}")\\""
				echo "    network: $network"
				[ "$tls" = "1" ] && echo "    tls: true"
				[ -n "$sni" ] && echo "    servername: \\"$(yaml_quote "$sni")\\""
				[ "$scv" = "1" ] && echo "    skip-cert-verify: true"
				echo "    udp: true"
				;;
			vless)
				echo "    uuid: \\"$(yaml_quote "$uuid")\\""
				echo "    network: $network"
				[ -n "$flow" ] && echo "    flow: \\"$(yaml_quote "$flow")\\""
				[ "$tls" = "1" ] && echo "    tls: true"
				[ -n "$sni" ] && echo "    servername: \\"$(yaml_quote "$sni")\\""
				[ "$scv" = "1" ] && echo "    skip-cert-verify: true"
				echo "    udp: true"
				;;
		esac
	done
}

emit_referenced_landing_proxies_yaml() {
	local cfg="$1" seen=""
	uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=data_link$/\\1/p' | while read -r sid; do
		data_link_valid "$sid" "$cfg" 1 || continue
		local landing
		landing=$(uci -q get mihomo.$sid.landing_node)
		case " $seen " in
			*" $landing "*) continue ;;
		esac
		seen="$seen $landing"
		emit_landing_proxies_yaml "$landing"
	done
}

data_link_valid() {
	local sid="$1" cfg="$2" quiet="$3"
	local enabled device sub landing
	enabled=$(uci -q get mihomo.$sid.enabled); [ "$enabled" = "1" ] || return 1
	device=$(uci -q get mihomo.$sid.device_ip)
	landing=$(uci -q get mihomo.$sid.landing_node)
	[ -n "$device" ] && [ -n "$landing" ] || { [ "$quiet" = "1" ] || chain_log "data_link $sid skipped: device/landing required"; return 1; }
	case "$device" in *[!0-9A-Fa-f:./]*) [ "$quiet" = "1" ] || chain_log "data_link $sid skipped: invalid device IP/CIDR"; return 1 ;; esac
	if [ "${device#*/}" != "$device" ]; then
		local prefix="${device##*/}" max_prefix=32
		case "$device" in *:*) max_prefix=128 ;; esac
		case "$prefix" in ''|*[!0-9]*) [ "$quiet" = "1" ] || chain_log "data_link $sid skipped: invalid CIDR prefix"; return 1 ;; esac
		[ "$prefix" -le "$max_prefix" ] 2>/dev/null || { [ "$quiet" = "1" ] || chain_log "data_link $sid skipped: CIDR prefix out of range"; return 1; }
	fi
	landing_node_valid "$landing" "$quiet" || { [ "$quiet" = "1" ] || chain_log "data_link $sid skipped: landing node '$landing' invalid"; return 1; }
	sub=$(effective_data_link_node "$sid" "$cfg" "$quiet") || return 1
	return 0
}

# Output one landing-proxy clone per data link. `dialer-proxy` makes Mihomo
# establish the landing connection through the selected subscription node;
# this replaces the relay group type removed in Mihomo v1.19.x.
emit_data_link_proxies_yaml() {
	local cfg="$1"
	local seen_devices=""
	uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=data_link$/\\1/p' | while read -r sid; do
		data_link_valid "$sid" "$cfg" || continue
		local device sub landing chain_name
		device=$(uci -q get mihomo.$sid.device_ip)
		case " $seen_devices " in *" $device "*) chain_log "data_link $sid skipped: duplicate device '$device'"; continue ;; esac
		seen_devices="$seen_devices $device"
		sub=$(effective_data_link_node "$sid" "$cfg" 1) || continue
		landing=$(uci -q get mihomo.$sid.landing_node)
		chain_name=$(data_link_group_name "$sid")
		emit_landing_proxies_yaml "$landing" "$chain_name" "$sub"
	done
}

# Output source-address rules targeting generated dialer-proxy entries.
emit_data_link_rules_yaml() {
	local cfg="$1"
	local seen_devices=""
	uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=data_link$/\\1/p' | while read -r sid; do
		data_link_valid "$sid" "$cfg" || continue
		local device gname rule_type suffix
		device=$(uci -q get mihomo.$sid.device_ip)
		case " $seen_devices " in *" $device "*) continue ;; esac
		seen_devices="$seen_devices $device"
		gname=$(data_link_group_name "$sid")
		case "$device" in
			*:*) rule_type="SRC-IP-CIDR6"; case "$device" in */*) suffix="" ;; *) suffix="/128" ;; esac ;;
			*) rule_type="SRC-IP-CIDR"; case "$device" in */*) suffix="" ;; *) suffix="/32" ;; esac ;;
		esac
		echo "  - '$rule_type,$device$suffix,$gname'"
	done
}

# Emit adblock rule-providers block BODY (no top-level "rule-providers:" header;
# prepare_config emits the header only when this output is non-empty). One entry
# per enabled UCI adblock_source section. path MUST stay inside core -d work dir
# (/etc/mihomo), so we use ./ruleset/<name>.yaml (relative). interval is fixed to
# daily; the core auto-fetches on this schedule (no helper update loop needed).
emit_adblock_rule_providers_yaml() {
	uci show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=adblock_source$/\\1/p' | while read -r sid; do
		local e name url behavior fmt
		e=$(uci -q get mihomo.$sid.enabled); [ "$e" = "1" ] || continue
		name=$(uci -q get mihomo.$sid.name); url=$(uci -q get mihomo.$sid.url)
		[ -n "$name" ] && [ -n "$url" ] || continue
		case "$name" in *[!A-Za-z0-9_-]*) logger -t mihomo "adblock source skipped: invalid name"; continue ;; esac
		behavior=$(uci -q get mihomo.$sid.behavior || echo domain)
		fmt=$(uci -q get mihomo.$sid.format || echo yaml)
		case "$behavior" in domain|classical|ipcidr) ;; *) logger -t mihomo "adblock source skipped: invalid behavior"; continue ;; esac
		case "$fmt" in yaml|text) ;; *) logger -t mihomo "adblock source skipped: invalid format"; continue ;; esac
		valid_http_url "$url" || { logger -t mihomo "adblock source skipped: invalid URL"; continue; }
		echo "  $name:"
		echo "    type: http"
		echo "    behavior: $behavior"
		echo "    format: $fmt"
		echo "    url: \\"$(yaml_quote "$url")\\""
		echo "    path: ./ruleset/$name.yaml"
		echo "    interval: 86400"
	done
}

# Emit RULE-SET,<name>,REJECT lines for every enabled adblock_source.
emit_adblock_rules_yaml() {
	uci show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=adblock_source$/\\1/p' | while read -r sid; do
		local e name
		e=$(uci -q get mihomo.$sid.enabled); [ "$e" = "1" ] || continue
		name=$(uci -q get mihomo.$sid.name); [ -n "$name" ] || continue
		echo "  - 'RULE-SET,$name,REJECT'"
	done
}

# Return 0 if at least one enabled adblock_source uses domain behavior (needed to
# decide whether DNS-layer nameserver-policy can reference rule-set).
has_domain_adblock_source() {
	uci show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=adblock_source$/\\1/p' | while read -r sid; do
		local e b
		e=$(uci -q get mihomo.$sid.enabled); [ "$e" = "1" ] || continue
		b=$(uci -q get mihomo.$sid.behavior || echo domain)
		[ "$b" = "domain" ] && echo yes
	done | grep -q yes
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
			/^rules:[[:space:]]*(\[\])?[[:space:]]*$/ { inr=1; print; next }
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
			/^rules:[[:space:]]*(\[\])?[[:space:]]*$/ { in_rules = 1; if ($0 ~ /\[\]/) sub(/\[\][[:space:]]*$/, ""); print; next }
			in_rules && /^[A-Za-z]/ { in_rules = 0 }
			in_rules && /^[[:space:]]*-/ { sub(/^[[:space:]]*/, "  "); print; next }
			{ print }
		' "$f" > "$normf" && mv "$normf" "$f"
	fi
}

prepare_config() {
	# Seed built-in adblock sources on first run (idempotent uci add).
	init_adblock_sources
	local ad_enabled=$(uci -q get mihomo.config.adblock_enabled || echo 0)
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
	local port_name port_value
	for port_name in dns_port tproxy_port mix_port; do
		port_value=$(eval "printf '%s' \"\$$port_name\"")
		case "$port_value" in ''|*[!0-9]*) echo "ERROR: Invalid $port_name" >&2; return 1 ;; esac
		[ "$port_value" -ge 1 ] 2>/dev/null && [ "$port_value" -le 65535 ] 2>/dev/null || { echo "ERROR: $port_name out of range" >&2; return 1; }
	done

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

	# Mihomo warns that HTTP health checks are unreliable because providers may
	# hijack them or mishandle repeated HEAD requests. Preserve the configured
	# host/path while upgrading generate_204 probes to HTTPS. This covers both
	# block-style health-check.url and inline proxy-group url fields without
	# touching unrelated HTTP rule-provider downloads.
	sed -i 's#http://\\([^ \",}]*generate_204\\)#https://\\1#g' "$run_config"

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
secret: "$(yaml_quote "$secret")"
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
	# Adblock DNS-layer interception: send ad domains a null answer at resolve
	# time (before fake-ip is even handed out). Only domain-behavior providers
	# can be referenced by nameserver-policy rule-set.
	if [ "$ad_enabled" = "1" ] && has_domain_adblock_source; then
		echo "  nameserver-policy:" >> "$run_config"
		uci show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=adblock_source$/\\1/p' | while read -r sid; do
			local e n b
			e=$(uci -q get mihomo.$sid.enabled); [ "$e" = "1" ] || continue
			b=$(uci -q get mihomo.$sid.behavior || echo domain)
			[ "$b" = "domain" ] || continue
			n=$(uci -q get mihomo.$sid.name); [ -n "$n" ] || continue
			echo "    \\"rule-set:$n\\": rcode://success" >> "$run_config"
		done
	fi

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

	# Adblock: merge a controlled rule-providers block, deduped against any
	# same-named provider the subscription already defines (duplicate keys make
	# the core fatal-exit on startup). The ssproxy-ad- prefix avoids collisions.
	if [ "$ad_enabled" = "1" ]; then
		local ad_provs="${run_config}.adprovs"
		emit_adblock_rule_providers_yaml > "$ad_provs"
		if [ -s "$ad_provs" ]; then
			local ad_dedup="${run_config}.adprovs.dd"
			: > "$ad_dedup"
			local cur_name="" skip=0
			while IFS= read -r line; do
				case "$line" in
					"  "*:)
						cur_name=${line#  }; cur_name=${cur_name%:}
						if grep -qE "^  ${cur_name}:" "$run_config"; then skip=1; else skip=0; fi
						;;
				esac
				[ "$skip" = "0" ] && echo "$line" >> "$ad_dedup"
			done < "$ad_provs"
			if [ -s "$ad_dedup" ]; then
				if grep -q '^rule-providers:' "$run_config"; then
					awk -v body="$ad_dedup" '
						/^rule-providers:[[:space:]]*$/ { print; while ((getline l < body) > 0) print l; next }
						{ print }
					' "$run_config" > "${run_config}.tmp" && mv "${run_config}.tmp" "$run_config"
				else
					{ echo "rule-providers:"; cat "$ad_dedup"; } >> "$run_config"
				fi
			fi
			rm -f "$ad_dedup"
		fi
		rm -f "$ad_provs"
	fi

	# Inject built-in bypass rules (multicast/LLMNR → DIRECT) first, then UCI
	# access rules, at the top of the rules block (highest priority, first-match).
	local rules_file="${run_config}.rules"
	emit_builtin_bypass_rules > "$rules_file"
	emit_data_link_rules_yaml "$run_config" >> "$rules_file"
	emit_access_rules_yaml "$src_config" >> "$rules_file"
	# Adblock REJECT rules go AFTER access rules so user whitelist (direct)
	# entries always win over blanket ad blocking (prevents false positives).
	emit_adblock_rules_yaml >> "$rules_file"
	if [ -s "$rules_file" ]; then
		if grep -q '^rules:' "$run_config"; then
			local tmpf="${run_config}.rules2"
			awk -v f="$rules_file" '
				BEGIN { while ((getline line < f) > 0) buf = buf line "\\n" }
				{ print }
				/^rules:[[:space:]]*(\\[\\])?[[:space:]]*$/ && !done { if ($0 ~ /\\[\\]/) sub(/\\[\\][[:space:]]*$/, ""); printf "%s", buf; done=1 }
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

	# Inject user-managed landing proxies before generating fixed two-hop links.
	local landing_yaml="${run_config}.landing"
	emit_referenced_landing_proxies_yaml "$run_config" > "$landing_yaml"
	if [ -s "$landing_yaml" ]; then
		align_list_body_indent "$run_config" "proxies" "$landing_yaml"
		if grep -q '^proxies:[[:space:]]*\[\][[:space:]]*$' "$run_config"; then
			awk -v body="$landing_yaml" '
				/^proxies:[[:space:]]*\[\][[:space:]]*$/ { print "proxies:"; while ((getline l < body) > 0) print l; next }
				{ print }
			' "$run_config" > "${run_config}.tmp" && mv "${run_config}.tmp" "$run_config"
		elif grep -q '^proxies:[[:space:]]*$' "$run_config"; then
			awk -v body="$landing_yaml" '
				/^proxies:[[:space:]]*$/ { print; while ((getline l < body) > 0) print l; next }
				{ print }
			' "$run_config" > "${run_config}.tmp" && mv "${run_config}.tmp" "$run_config"
		else
			{ echo "proxies:"; cat "$landing_yaml"; } >> "$run_config"
		fi
		chain_log "landing proxies injected"
	fi
	rm -f "$landing_yaml"

	local chain_yaml="${run_config}.data-links"
	emit_data_link_proxies_yaml "$run_config" > "$chain_yaml"
	if [ -s "$chain_yaml" ]; then
		align_list_body_indent "$run_config" "proxies" "$chain_yaml"
		if grep -q '^proxies:[[:space:]]*$' "$run_config"; then
			awk -v body="$chain_yaml" '
				/^proxies:[[:space:]]*$/ { print; while ((getline l < body) > 0) print l; next }
				{ print }
			' "$run_config" > "${run_config}.tmp" && mv "${run_config}.tmp" "$run_config"
		else
			{ echo "proxies:"; cat "$chain_yaml"; } >> "$run_config"
		fi
		chain_log "data-link dialer proxies injected"
	fi
	rm -f "$chain_yaml"

	echo "SUCCESS: Prepared configuration at $run_config (mode=$config_mode)"
	return 0
}

# Collect proxy and proxy-group names from a block-style Mihomo config.
collect_proxy_names() {
	local cfg="$1"
	[ -z "$cfg" ] || [ ! -f "$cfg" ] && return 0
	tr -d '\\r' < "$cfg" | awk '
		function strip(s){ gsub(/^[[:space:]]+|[[:space:]]+$/,"",s); gsub(/^["'\''']|["'\''']$/,"",s); return s }
		/^proxies:/     { inb=1; next }
		/^proxy-groups:/ { inb=1; next }
		/^[a-zA-Z]/ && $0 !~ /^[[:space:]]/ { inb=0 }
		inb && /^[[:space:]]*-[[:space:]]*\{/ {
			s=$0; sub(/^[[:space:]]*-[[:space:]]*\{[[:space:]]*/,"",s)
			if (s ~ /^name:[[:space:]]*/) { sub(/^name:[[:space:]]*/,"",s); sub(/,.*/,"",s); print strip(s) }
			next
		}
		inb && /^[[:space:]]*-[[:space:]]*name:/ { s=$0; sub(/^[[:space:]]*-[[:space:]]*name:[[:space:]]*/,"",s); print strip(s) }
	'
}

chain_source_config() {
	local mode
	mode=$(uci -q get mihomo.config.config_mode); [ -n "$mode" ] || mode="subscription"
	if [ "$mode" = "custom" ]; then
		uci -q get mihomo.config.custom_config_path || echo "/etc/mihomo/custom.yaml"
	else
		uci -q get mihomo.config.config_path || echo "/etc/mihomo/config.yaml"
	fi
}

set_chain_front_node() {
	local node="$1" cfg
	[ -n "$node" ] || node="individual"
	if [ "$node" != "individual" ]; then
		cfg=$(chain_source_config)
		[ -f "$cfg" ] || { echo "ERROR: source config not found" >&2; return 1; }
		collect_proxy_names "$cfg" | grep -qxF "$node" || { echo "ERROR: front node not found in current config" >&2; return 1; }
	fi
	uci -q set mihomo.config.chain_front_node="$node"
	uci -q commit mihomo
	printf '{"front_node":%s}\n' "$(json_quote "$node")"
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
	function jsonv(s,   out, i, c, slash, quote, cr, lf){
		slash=sprintf("%c",92); quote=sprintf("%c",34)
		cr=sprintf("%c",13); lf=sprintf("%c",10); out=""
		for (i=1; i<=length(s); i++) {
			c=substr(s,i,1)
			if (c == slash) out=out slash slash
			else if (c == quote) out=out slash quote
			else if (c == cr || c == lf) out=out " "
			else out=out c
		}
		return out
	}
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
			if (name != "") { if(!first) printf ","; first=0; printf("  {\\042name\\042:\\042%s\\042,\\042type\\042:\\042%s\\042,\\042server\\042:\\042%s\\042}", jsonv(name), jsonv(type), jsonv(server)) }
			name=""; type=""; server=""
			s = $0; sub(/^[ 	]*-[ 	]*/, "", s)
			if (s ~ /\\{/) {
				name=getf(s,"name"); type=getf(s,"type"); server=getf(s,"server")
				if (name != "") { if(!first) printf ","; first=0; printf("  {\\042name\\042:\\042%s\\042,\\042type\\042:\\042%s\\042,\\042server\\042:\\042%s\\042}", jsonv(name), jsonv(type), jsonv(server)) }
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
	END { if (name != "") { if(!first) printf ","; printf("  {\\042name\\042:\\042%s\\042,\\042type\\042:\\042%s\\042,\\042server\\042:\\042%s\\042}", jsonv(name), jsonv(type), jsonv(server)) }; print "]" }
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

# ---------- 访问日志：结构化网络访问记录 ----------

access_log_file="${MIHOMO_ACCESS_LOG_FILE:-/tmp/mihomo_access.log}"
access_seen_file="${MIHOMO_ACCESS_SEEN_FILE:-/tmp/mihomo_access.seen}"

normalize_access_log() {
	[ -s "$access_log_file" ] || return 0
	local lines
	lines=$(wc -l < "$access_log_file" 2>/dev/null)
	# Versions before 1.0.0-180 omitted record newlines and produced one giant,
	# invalid JSON stream. Discard it once instead of returning megabytes to LuCI.
	[ "${lines:-0}" -eq 0 ] && : > "$access_log_file"
}

resolve_host() {
	local ip="$1"
	local leases="/tmp/dhcp.leases"
	[ -z "$ip" ] && return 0
	[ -f "$leases" ] || return 0
	awk -v ip="$ip" '$3==ip && $4!="*" { print $4; exit }' "$leases"
}

flatten_connections() {
	local raw="$1"
	[ -z "$raw" ] && return 0
	local tmpd leasef
	tmpd=$(mktemp -d) || return 1
	leasef="$tmpd/leases"
	[ -f /tmp/dhcp.leases ] && cp /tmp/dhcp.leases "$leasef" || : > "$leasef"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].id' 2>/dev/null > "$tmpd/id"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].metadata.sourceIP' 2>/dev/null > "$tmpd/ip"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].metadata.host' 2>/dev/null > "$tmpd/host"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].metadata.destinationIP' 2>/dev/null > "$tmpd/dst"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].chains[0]' 2>/dev/null > "$tmpd/policy"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].rule' 2>/dev/null > "$tmpd/rule"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].upload' 2>/dev/null > "$tmpd/up"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].download' 2>/dev/null > "$tmpd/down"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].start' 2>/dev/null > "$tmpd/start"
	awk -v leasef="$leasef" -v idf="$tmpd/id" -v ipf="$tmpd/ip" -v hostf="$tmpd/host" \
		-v dstf="$tmpd/dst" -v policyf="$tmpd/policy" -v rulef="$tmpd/rule" \
		-v upf="$tmpd/up" -v downf="$tmpd/down" -v startf="$tmpd/start" '
		FILENAME == leasef { if ($3 != "" && $4 != "*") device[$3]=$4; next }
		FILENAME == idf { id[FNR]=$0; if (FNR > maxn) maxn=FNR; next }
		FILENAME == ipf { ip[FNR]=$0; next }
		FILENAME == hostf { host[FNR]=$0; next }
		FILENAME == dstf { dst[FNR]=$0; next }
		FILENAME == policyf { policy[FNR]=$0; next }
		FILENAME == rulef { rule[FNR]=$0; next }
		FILENAME == upf { up[FNR]=$0; next }
		FILENAME == downf { down[FNR]=$0; next }
		FILENAME == startf { start[FNR]=$0; next }
		END {
			for (i=1; i<=maxn; i++) {
				if (id[i] == "") continue
				hostname=host[i]; if (hostname == "") hostname=dst[i]
				print id[i] "|" ip[i] "|" device[ip[i]] "|" hostname "|" dst[i] "|" policy[i] "|" rule[i] "|" up[i] "|" down[i] "|" start[i]
			}
		}' "$leasef" "$tmpd/id" "$tmpd/ip" "$tmpd/host" "$tmpd/dst" "$tmpd/policy" "$tmpd/rule" "$tmpd/up" "$tmpd/down" "$tmpd/start"
	rm -rf "$tmpd"
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
		printf '{"error":"api_error","msg":%s}\\n' "$(json_quote "Mihomo 控制器错误：${err_msg}")"
		return 0
	fi
	echo "["
	first=1
	flatten_connections "$raw" | while IFS='|' read -r id ip dev host d pol r u dn st; do
		[ -z "$id" ] && continue
		if [ $first -eq 0 ]; then printf ','; fi
		first=0
		printf '{"id":%s,"ip":%s,"device":%s,"domain":%s,"dst":%s,"policy":%s,"rule":%s,"up":%s,"down":%s,"start":%s}' \
			"$(json_quote "$id")" "$(json_quote "$ip")" "$(json_quote "$dev")" "$(json_quote "$host")" "$(json_quote "$d")" "$(json_quote "$pol")" "$(json_quote "$r")" "${u:-0}" "${dn:-0}" "$(json_quote "$st")"
	done
	echo "]"
}

collect_connections() {
	local raw logf seenf
	logf="$access_log_file"
	seenf="$access_seen_file"
	raw="$1"
	[ -n "$raw" ] || raw=$(mihomo_curl -s --connect-timeout 2 "http://127.0.0.1:${API_PORT}/connections" 2>/dev/null)
	[ -z "$raw" ] && return 0
	normalize_access_log
	touch "$seenf"
	local tmpd
	tmpd=$(mktemp -d) || return 1
	flatten_connections "$raw" > "$tmpd/all"
	awk -F'|' 'NR==FNR { seen[$1]=1; next } !($1 in seen)' "$seenf" "$tmpd/all" > "$tmpd/new"
	while IFS='|' read -r id ip dev host d pol r u dn st; do
		[ -z "$id" ] && continue
		echo "$id" >> "$seenf"
		local ts
		ts=$(date +%s)
		printf '{"ts":%s,"id":%s,"ip":%s,"device":%s,"domain":%s,"dst":%s,"policy":%s,"rule":%s,"up":%s,"down":%s,"start":%s}\n' \
			"$ts" "$(json_quote "$id")" "$(json_quote "$ip")" "$(json_quote "$dev")" "$(json_quote "$host")" "$(json_quote "$d")" "$(json_quote "$pol")" "$(json_quote "$r")" "${u:-0}" "${dn:-0}" "$(json_quote "$st")" >> "$logf"
	done < "$tmpd/new"
	rm -rf "$tmpd"
	tail -n 2000 "$seenf" > "$seenf.tmp" && mv "$seenf.tmp" "$seenf"
	if [ -f "$logf" ] && [ "$(wc -l < "$logf")" -gt 2000 ]; then
		tail -n 2000 "$logf" > "$logf.tmp" && mv "$logf.tmp" "$logf"
	fi
}

collect_loop() {
	sleep 5
	local tick=0 raw
	while true; do
		raw=$(mihomo_curl -s --connect-timeout 2 "http://127.0.0.1:${API_PORT}/connections" 2>/dev/null)
		if [ -n "$raw" ]; then
			collect_traffic "$raw"
			if [ "$tick" -eq 0 ]; then collect_connections "$raw"; fi
			tick=$(( (tick + 1) % 3 ))
		fi
		sleep 5
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
	raw="$1"
	[ -n "$raw" ] || raw=$(mihomo_curl -s --connect-timeout 2 "http://127.0.0.1:${API_PORT}/connections" 2>/dev/null)
	[ -z "$raw" ] && return 0
	collect_data_link_traffic "$raw"
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
	# Compatibility alias for older procd definitions during package upgrades.
	collect_loop
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
			printf '{"domain":%s,"bytes":%d}' "$(json_quote "$_d")" "${_b:-0}"
		done
	fi
	printf '],"daily":['
	if [ -s "$dayf" ]; then
		local _first=1 _k _b
		sort -k1 -r "$dayf" 2>/dev/null | while IFS='	' read -r _k _b; do
			[ -z "$_k" ] && continue
			[ "$_first" -eq 0 ] && printf ','
			_first=0
			printf '{"date":%s,"bytes":%d}' "$(json_quote "$_k")" "${_b:-0}"
		done
	fi
	printf '],"monthly":['
	if [ -s "$monf" ]; then
		local _first=1 _k _b
		sort -k1 -r "$monf" 2>/dev/null | while IFS='	' read -r _k _b; do
			[ -z "$_k" ] && continue
			[ "$_first" -eq 0 ] && printf ','
			_first=0
			printf '{"month":%s,"bytes":%d}' "$(json_quote "$_k")" "${_b:-0}"
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
	printf '%s' "$nodes_json" | awk '
		BEGIN {
			q=sprintf("%c",34); slash=sprintf("%c",92)
			needle=q "name" q ":" q
			data=""
		}
		{ data=data $0 }
		END {
			pos=1
			while ((off=index(substr(data,pos), needle)) > 0) {
				start=pos+off-1+length(needle); value=""
				for (i=start; i<=length(data); i++) {
					c=substr(data,i,1)
					if (c == q) { print value; pos=i+1; break }
					if (c == slash && i < length(data)) {
						i++; c=substr(data,i,1)
						if (c == "n") value=value sprintf("%c",10)
						else if (c == "r") value=value sprintf("%c",13)
						else if (c == "t") value=value sprintf("%c",9)
						else value=value c
					} else value=value c
				}
			}
		}
	' > "$tmpd/names"
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
				printf '{"delay":-1,"msg":%s}' "$(json_quote "${msg:-timeout}")" > "$tmpd/$i"
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
			printf '{"name":%s,"delay":%s,"code":%s,"ok":true}' "$(json_quote "$name")" "$ms" "$(json_quote "$code")"
		else
			printf '{"name":%s,"delay":0,"code":"","ok":false,"msg":"timeout"}' "$(json_quote "$name")"
		fi
	done
	echo "]"
}

get_history() {
	local logf="$access_log_file"
	local limit="${1:-200}"
	normalize_access_log
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

clear_access_log() {
	# Mark every connection that is already active as seen before truncating the
	# file. Otherwise a connection established just before the click but not yet
	# sampled would immediately reappear and make the clear action look broken.
	local raw id
	raw=$(mihomo_curl -s --connect-timeout 2 "http://127.0.0.1:${API_PORT}/connections" 2>/dev/null)
	if [ -n "$raw" ]; then
		touch "$access_seen_file"
		printf '%s' "$raw" | jsonfilter -e '$.connections[@].id' 2>/dev/null | while IFS= read -r id; do
			[ -n "$id" ] || continue
			grep -qxF "$id" "$access_seen_file" 2>/dev/null || echo "$id" >> "$access_seen_file"
		done
		tail -n 2000 "$access_seen_file" > "${access_seen_file}.tmp" && mv "${access_seen_file}.tmp" "$access_seen_file"
	fi
	: > "$access_log_file"
	echo "OK"
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
		printf '{"sid":%s,"ip":%s,"domain":%s,"action":%s,"group":%s,"enabled":%s,"comment":%s,"rule_type":%s}' \
			"$(json_quote "$sid")" "$(json_quote "$ip")" "$(json_quote "$domain")" "$(json_quote "$action")" "$(json_quote "$group")" "$(json_quote "$enabled")" "$(json_quote "$comment")" "$(json_quote "$rule_type")"
	done
	echo "]"
}

add_access_rule() {
	local ip="$1" domain="$2" action="$3" group="$4" rule_type="$5" comment="$6"
	[ -z "$domain" ] && { echo "ERROR: domain required" >&2; return 1; }
	valid_rule_domain "$domain" || { echo "ERROR: invalid domain" >&2; return 1; }
	[ -z "$action" ] && action="block"
	[ -z "$rule_type" ] && rule_type="suffix"
	case "$action" in block|direct|proxy) ;; *) echo "ERROR: invalid action" >&2; return 1 ;; esac
	case "$rule_type" in suffix|domain|keyword) ;; *) echo "ERROR: invalid rule_type" >&2; return 1 ;; esac
	local sid
	sid=$(uci add mihomo mihomo_rule)
	uci -q set mihomo.$sid.src_ip="$ip"
	uci -q set mihomo.$sid.domain="$domain"
	uci -q set mihomo.$sid.action="$action"
	[ -n "$group" ] && uci -q set mihomo.$sid.group="$group"
	uci -q set mihomo.$sid.rule_type="$rule_type"
	[ -n "$comment" ] && uci -q set mihomo.$sid.comment="$comment"
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

# --- Adblock rule-source management (UCI adblock_source sections) ---

get_adblock_sources() {
	echo "["
	local first=1
	uci show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=adblock_source$/\\1/p' | while read -r sid; do
		local e name url behavior fmt
		e=$(uci -q get mihomo.$sid.enabled)
		name=$(uci -q get mihomo.$sid.name)
		url=$(uci -q get mihomo.$sid.url)
		behavior=$(uci -q get mihomo.$sid.behavior); [ -z "$behavior" ] && behavior="domain"
		fmt=$(uci -q get mihomo.$sid.format); [ -z "$fmt" ] && fmt="yaml"
		[ $first -eq 0 ] && printf ','
		first=0
		printf '{"sid":%s,"name":%s,"url":%s,"behavior":%s,"format":%s,"enabled":%s}' \
			"$(json_quote "$sid")" "$(json_quote "$name")" "$(json_quote "$url")" "$(json_quote "$behavior")" "$(json_quote "$fmt")" "$(json_quote "$e")"
	done
	echo "]"
}

add_adblock_source() {
	local name="$1" url="$2" behavior="$3" fmt="$4"
	[ -z "$url" ] && { echo "ERROR: url required" >&2; return 1; }
	[ -z "$name" ] && { echo "ERROR: name required" >&2; return 1; }
	case "$name" in
		ssproxy-ad-*) ;;
		*) name="ssproxy-ad-$name" ;;
	esac
	case "$name" in *[!A-Za-z0-9_-]*) echo "ERROR: invalid name" >&2; return 1 ;; esac
	[ -z "$behavior" ] && behavior="domain"
	[ -z "$fmt" ] && fmt="yaml"
	case "$behavior" in domain|classical|ipcidr) ;; *) echo "ERROR: invalid behavior" >&2; return 1 ;; esac
	case "$fmt" in yaml|text) ;; *) echo "ERROR: invalid format" >&2; return 1 ;; esac
	valid_http_url "$url" || { echo "ERROR: URL must use http:// or https://" >&2; return 1; }
	local sid
	sid=$(uci add mihomo adblock_source)
	uci -q set mihomo.$sid.name="$name"
	uci -q set mihomo.$sid.url="$url"
	uci -q set mihomo.$sid.behavior="$behavior"
	uci -q set mihomo.$sid.format="$fmt"
	uci -q set mihomo.$sid.enabled="1"
	uci commit mihomo
	logger -t mihomo "adblock_source added: name=$name url=$url behavior=$behavior"
	echo "OK"
}

del_adblock_source() {
	local sid="$1"
	[ -z "$sid" ] && { echo "ERROR: sid required" >&2; return 1; }
	uci -q delete mihomo.$sid
	uci commit mihomo
	logger -t mihomo "adblock_source deleted: $sid"
	echo "OK"
}

toggle_adblock_source() {
	local sid="$1" val="$2"
	[ -z "$sid" ] && { echo "ERROR: sid required" >&2; return 1; }
	uci -q set mihomo.$sid.enabled="${val:-0}"
	uci commit mihomo
	logger -t mihomo "adblock_source toggled: $sid enabled=${val:-0}"
	echo "OK"
}

set_adblock_enabled() {
	local val="$1"
	[ -z "$val" ] && val="0"
	uci -q set mihomo.config.adblock_enabled="$val"
	uci commit mihomo
	logger -t mihomo "adblock enabled=$val"
	echo "OK"
}

# Idempotently seed built-in preset adblock sources on first run (only when no
# adblock_source section exists yet). Presets default to disabled; the user opts
# in via the UI. All presets use domain behavior so they feed both rule-layer
# REJECT and DNS-layer nameserver-policy.
init_adblock_sources() {
	uci -q show mihomo.@adblock_source[0] >/dev/null 2>&1 && return 0
	local sid
	sid=$(uci add mihomo adblock_source)
	uci -q set mihomo.$sid.name="ssproxy-ad-antiad"
	uci -q set mihomo.$sid.url="https://anti-ad.net/clash.yaml"
	uci -q set mihomo.$sid.behavior="domain"
	uci -q set mihomo.$sid.format="yaml"
	uci -q set mihomo.$sid.enabled="0"
	uci commit mihomo
	logger -t mihomo "adblock: preset sources initialized"
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

normalize_acl_identity() {
	local value="$1"
	case "$value" in
		*:*\/128) printf '%s\n' "${value%/128}" ;;
		*.*.*.*\/32) printf '%s\n' "${value%/32}" ;;
		*) printf '%s\n' "$value" ;;
	esac
}

data_link_is_intercepted() {
	local sid="$1" device mode tun_enabled wanted acl
	tun_enabled=$(uci -q get mihomo.config.tun_enabled)
	[ "$tun_enabled" = "1" ] && return 0
	mode=$(uci -q get mihomo.config.acl_mode); [ -n "$mode" ] || mode=all
	[ "$mode" != "whitelist" ] && return 0
	device=$(uci -q get mihomo.$sid.device_ip)
	wanted=$(normalize_acl_identity "$device")
	for acl in $(uci -q get mihomo.config.acl_ips); do
		[ "$(normalize_acl_identity "$acl")" = "$wanted" ] && return 0
	done
	return 1
}

add_data_link_to_acl() {
	local sid="$1" device wanted acl
	uci -X show mihomo 2>/dev/null | grep -q "^mihomo\.$sid=data_link$" || {
		echo "ERROR: data link not found" >&2
		return 1
	}
	device=$(uci -q get mihomo.$sid.device_ip)
	validate_acl_ip "$device" || {
		echo "ERROR: data link has invalid device IP/CIDR" >&2
		return 1
	}
	wanted=$(normalize_acl_identity "$device")
	for acl in $(uci -q get mihomo.config.acl_ips); do
		if [ "$(normalize_acl_identity "$acl")" = "$wanted" ]; then
			echo '{"changed":0}'
			return 0
		fi
	done
	uci -q add_list mihomo.config.acl_ips="$device" || return 1
	uci -q commit mihomo || return 1
	printf '{"changed":1,"device":%s}\n' "$(json_quote "$device")"
}

get_chain_status() {
	local run_config="/tmp/mihomo_run.yaml"
	local controller=0 landing_total landing_enabled landing_valid link_total link_enabled link_valid runtime_landing=0 runtime_links=0
	local acl_mode tun_enabled links
	mihomo_curl -s -m 2 "http://127.0.0.1:${API_PORT}/version" >/dev/null 2>&1 && controller=1
	acl_mode=$(uci -q get mihomo.config.acl_mode); [ -n "$acl_mode" ] || acl_mode=all
	tun_enabled=$(uci -q get mihomo.config.tun_enabled); [ "$tun_enabled" = "1" ] || tun_enabled=0
	landing_total=$(uci -X show mihomo 2>/dev/null | grep -c '=landing_node$')
	landing_enabled=$(uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=landing_node$/\\1/p' | while read -r sid; do [ "$(uci -q get mihomo.$sid.enabled)" = "1" ] && echo 1; done | wc -l | tr -d ' ')
	landing_valid=$(uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=landing_node$/\\1/p' | while read -r sid; do landing_node_valid "$sid" 1 && echo 1; done | wc -l | tr -d ' ')
	link_total=$(uci -X show mihomo 2>/dev/null | grep -c '=data_link$')
	link_enabled=$(uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=data_link$/\\1/p' | while read -r sid; do [ "$(uci -q get mihomo.$sid.enabled)" = "1" ] && echo 1; done | wc -l | tr -d ' ')
	link_valid=$(uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=data_link$/\\1/p' | while read -r sid; do data_link_valid "$sid" "$run_config" 1 && echo 1; done | wc -l | tr -d ' ')
	if [ -f "$run_config" ]; then
		runtime_landing=$(grep -c 'name: "ssproxy-landing-' "$run_config" 2>/dev/null)
		runtime_links=$(grep -c 'name: "ssproxy-chain-' "$run_config" 2>/dev/null)
	fi
	links=$(uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=data_link$/\\1/p' | {
		local first=1 seen_devices="" sid
		while read -r sid; do
			[ -n "$sid" ] || continue
			local enabled device identity duplicate=0 runtime=0 intercepted=0 valid=0 code reason group
			enabled=$(uci -q get mihomo.$sid.enabled); [ "$enabled" = "1" ] || enabled=0
			device=$(uci -q get mihomo.$sid.device_ip)
			identity=$(normalize_acl_identity "$device")
			printf '%s\n' " $seen_devices " | grep -qF " $identity " && duplicate=1
			group=$(data_link_group_name "$sid")
			[ -f "$run_config" ] && grep -qF "name: \\"$group\\"" "$run_config" && runtime=1
			data_link_is_intercepted "$sid" && intercepted=1
			if [ "$enabled" = "0" ]; then
				code="disabled"; reason="链路已停用"
			elif ! landing_node_valid "$(uci -q get mihomo.$sid.landing_node)" 1; then
				code="invalid_landing"; reason="落地节点无效或字段不完整"
			elif ! effective_data_link_node "$sid" "$run_config" 1 >/dev/null; then
				code="missing_front"; reason="前置节点不存在于当前运行配置"
			elif [ "$duplicate" = "1" ]; then
				code="duplicate"; reason="设备与另一条启用链路冲突"
			elif ! data_link_valid "$sid" "$run_config" 1; then
				code="invalid"; reason="链路配置校验失败"
			else
				valid=1
				seen_devices="$seen_devices $identity"
				if [ "$runtime" = "0" ]; then
					code="not_applied"; reason="配置有效，但尚未应用到运行配置"
				elif [ "$intercepted" = "0" ]; then
					code="bypassed"; reason="设备不在受控 IP 列表，流量会绕过 Mihomo"
				elif [ "$controller" = "0" ]; then
					code="controller_offline"; reason="链路已注入，但控制器离线"
				else
					code="ready"; reason="链路已注入，设备流量会被接管"
				fi
			fi
			[ "$first" -eq 0 ] && printf ','
			first=0
			printf '{"sid":%s,"device":%s,"enabled":%s,"valid":%s,"runtime":%s,"intercepted":%s,"code":%s,"reason":%s}' \
				"$(json_quote "$sid")" "$(json_quote "$device")" "$enabled" "$valid" "$runtime" "$intercepted" \
				"$(json_quote "$code")" "$(json_quote "$reason")"
		done
	})
	printf '{"controller":%s,"run_config":%s,"acl_mode":%s,"tun_enabled":%s,"landing_total":%s,"landing_enabled":%s,"landing_valid":%s,"link_total":%s,"link_enabled":%s,"link_valid":%s,"runtime_landing":%s,"runtime_links":%s,"links":[%s]}\n' \
		"$controller" "$([ -f "$run_config" ] && echo 1 || echo 0)" "$(json_quote "$acl_mode")" "$tun_enabled" \
		"${landing_total:-0}" "${landing_enabled:-0}" "${landing_valid:-0}" "${link_total:-0}" "${link_enabled:-0}" \
		"${link_valid:-0}" "${runtime_landing:-0}" "${runtime_links:-0}" "$links"
}

get_chain_log() {
	local lines="${1:-100}"
	case "$lines" in ''|*[!0-9]*) lines=100 ;; esac
	[ "$lines" -gt 500 ] && lines=500
	logread -e mihomo-chain 2>/dev/null | tail -n "$lines"
}

data_link_health_file="${MIHOMO_DATA_LINK_HEALTH_FILE:-/tmp/mihomo_data_link_health}"
data_link_metrics_file="${MIHOMO_DATA_LINK_METRICS_FILE:-/tmp/mihomo_data_link_metrics}"
data_link_conn_state_file="${MIHOMO_DATA_LINK_CONN_STATE_FILE:-/tmp/mihomo_data_link_conn_state}"
data_link_last_poll_file="${MIHOMO_DATA_LINK_LAST_POLL_FILE:-/tmp/mihomo_data_link_last_poll}"
data_link_flush_file="${MIHOMO_DATA_LINK_FLUSH_FILE:-/tmp/mihomo_data_link_flush}"
data_link_persist_file="${MIHOMO_DATA_LINK_PERSIST_FILE:-/etc/mihomo/.data_link_traffic}"

mark_data_link_healthy() {
	local sid="$1"
	[ -n "$sid" ] || return 1
	uci -X show mihomo 2>/dev/null | grep -q "^mihomo\.$sid=data_link$" || return 1
	touch "$data_link_health_file"
	grep -qxF "$sid" "$data_link_health_file" 2>/dev/null || echo "$sid" >> "$data_link_health_file"
}

update_data_link_health_from_connections() {
	local raw="$1"
	[ -n "$raw" ] || return 0
	local tmpd
	tmpd=$(mktemp -d) || return 1
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].policy' 2>/dev/null > "$tmpd/policy"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].chains[0]' 2>/dev/null > "$tmpd/chain"
	cat "$tmpd/policy" "$tmpd/chain" | sort -u > "$tmpd/groups"
	uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=data_link$/\\1/p' | while read -r sid; do
		local group
		group=$(data_link_group_name "$sid")
		grep -qxF "$group" "$tmpd/groups" && mark_data_link_healthy "$sid"
	done
	rm -rf "$tmpd"
}

resolve_data_link_sid() {
	local source_ip="$1" policy="$2" chain="$3"
	uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=data_link$/\\1/p' | while read -r sid; do
		[ "$(uci -q get mihomo.$sid.enabled)" = "1" ] || continue
		local device group
		device=$(uci -q get mihomo.$sid.device_ip)
		group=$(data_link_group_name "$sid")
		if [ "$policy" = "$group" ] || [ "$chain" = "$group" ]; then
			echo "$sid"
			break
		fi
		case "$device" in
			*/*) ;;
			*) [ -n "$source_ip" ] && [ "$source_ip" = "$device" ] && { echo "$sid"; break; } ;;
		esac
	done
}

collect_data_link_traffic() {
	local raw="$1" now last elapsed tmpd
	[ -n "$raw" ] || return 0
	update_data_link_health_from_connections "$raw"
	now=$(date +%s)
	last=$(cat "$data_link_last_poll_file" 2>/dev/null)
	case "$last" in ''|*[!0-9]*) last=$((now - 5)) ;; esac
	elapsed=$((now - last)); [ "$elapsed" -lt 1 ] && elapsed=1; [ "$elapsed" -gt 60 ] && elapsed=5
	echo "$now" > "$data_link_last_poll_file"
	tmpd=$(mktemp -d)
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].id' 2>/dev/null > "$tmpd/id"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].metadata.sourceIP' 2>/dev/null > "$tmpd/ip"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].policy' 2>/dev/null > "$tmpd/policy"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].chains[0]' 2>/dev/null > "$tmpd/chain"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].upload' 2>/dev/null > "$tmpd/up"
	printf '%s' "$raw" | jsonfilter -e '$.connections[@].download' 2>/dev/null > "$tmpd/down"
	touch "$data_link_conn_state_file" "$data_link_metrics_file"
	if [ ! -f "$data_link_persist_file" ]; then
		mkdir -p "$(dirname "$data_link_persist_file")"
		: > "$data_link_persist_file"
	fi
	: > "$tmpd/map"
	uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=data_link$/\\1/p' | while read -r sid; do
		[ "$(uci -q get mihomo.$sid.enabled)" = "1" ] || continue
		local device group
		device=$(uci -q get mihomo.$sid.device_ip)
		group=$(data_link_group_name "$sid")
		printf '%s|%s|%s\n' "$sid" "$device" "$group"
	done > "$tmpd/map"
	: > "$tmpd/state.new"
	: > "$tmpd/delta"
	awk -F'|' -v mapf="$tmpd/map" -v statef="$data_link_conn_state_file" \
		-v idf="$tmpd/id" -v ipf="$tmpd/ip" -v policyf="$tmpd/policy" \
		-v chainf="$tmpd/chain" -v upf="$tmpd/up" -v downf="$tmpd/down" \
		-v statenew="$tmpd/state.new" -v deltaf="$tmpd/delta" '
		FILENAME == mapf {
			group_sid[$3]=$1
			if ($2 != "" && $2 !~ /\//) device_sid[$2]=$1
			next
		}
		FILENAME == statef { prev_up[$1]=$3+0; prev_down[$1]=$4+0; next }
		FILENAME == idf { id[FNR]=$0; if (FNR > maxn) maxn=FNR; next }
		FILENAME == ipf { ip[FNR]=$0; next }
		FILENAME == policyf { policy[FNR]=$0; next }
		FILENAME == chainf { chain[FNR]=$0; next }
		FILENAME == upf { up[FNR]=$0+0; next }
		FILENAME == downf { down[FNR]=$0+0; next }
		END {
			for (i=1; i<=maxn; i++) {
				cid=id[i]; if (cid == "") continue
				sid=group_sid[policy[i]]
				if (sid == "") sid=group_sid[chain[i]]
				if (sid == "") sid=device_sid[ip[i]]
				if (sid == "") continue
				du=up[i] - prev_up[cid]; if (!(cid in prev_up) || du < 0) du=up[i]
				dd=down[i] - prev_down[cid]; if (!(cid in prev_down) || dd < 0) dd=down[i]
				print cid "|" sid "|" up[i] "|" down[i] > statenew
				print sid "|" du "|" dd > deltaf
			}
		}' "$tmpd/map" "$data_link_conn_state_file" "$tmpd/id" "$tmpd/ip" "$tmpd/policy" "$tmpd/chain" "$tmpd/up" "$tmpd/down"
	mv "$tmpd/state.new" "$data_link_conn_state_file"
	awk -F'|' '{ up[$1]+=$2; down[$1]+=$3 } END { for (sid in up) print sid "|" up[sid] "|" down[sid] }' "$tmpd/delta" > "$tmpd/aggregate"
	awk -F'|' -v mapf="$tmpd/map" -v metricsf="$data_link_metrics_file" \
		-v persistf="$data_link_persist_file" -v aggregatef="$tmpd/aggregate" \
		-v now="$now" -v elapsed="$elapsed" '
		FILENAME == mapf { sid[$1]=1; next }
		FILENAME == metricsf { total_up[$1]=$4+0; total_down[$1]=$5+0; current[$1]=1; next }
		FILENAME == persistf {
			if (!($1 in current)) { total_up[$1]=$2+0; total_down[$1]=$3+0 }
			next
		}
		FILENAME == aggregatef { delta_up[$1]=$2+0; delta_down[$1]=$3+0; next }
		END {
			for (s in sid) {
				tu=total_up[s] + delta_up[s]; td=total_down[s] + delta_down[s]
				print s "|" int(delta_up[s]/elapsed) "|" int(delta_down[s]/elapsed) "|" tu "|" td "|" now
			}
		}' "$tmpd/map" "$data_link_metrics_file" "$data_link_persist_file" "$tmpd/aggregate" > "$tmpd/metrics.new"
	mv "$tmpd/metrics.new" "$data_link_metrics_file"
	local last_flush
	last_flush=$(cat "$data_link_flush_file" 2>/dev/null); case "$last_flush" in ''|*[!0-9]*) last_flush=0 ;; esac
	if [ $((now - last_flush)) -ge 60 ]; then
		mkdir -p "$(dirname "$data_link_persist_file")"
		awk -F'|' '{ print $1 "|" $4 "|" $5 }' "$data_link_metrics_file" > "${data_link_persist_file}.tmp" && mv "${data_link_persist_file}.tmp" "$data_link_persist_file"
		echo "$now" > "$data_link_flush_file"
	fi
	rm -rf "$tmpd"
}

get_data_link_health() {
	echo "["
	local first=1
	uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=data_link$/\\1/p' | while read -r sid; do
		grep -qxF "$sid" "$data_link_health_file" 2>/dev/null || continue
		[ $first -eq 0 ] && printf ','
		first=0
		json_quote "$sid"
	done
	echo "]"
}

get_data_link_metrics() {
	echo "["
	local first=1
	uci -X show mihomo 2>/dev/null | sed -n 's/^mihomo\.\(.*\)=data_link$/\\1/p' | while read -r sid; do
		local row up_rate=0 down_rate=0 total_up=0 total_down=0 updated=0 healthy=0
		row=$(awk -F'|' -v sid="$sid" '$1==sid { print $2 "|" $3 "|" $4 "|" $5 "|" $6; exit }' "$data_link_metrics_file" 2>/dev/null)
		if [ -n "$row" ]; then
			up_rate=${row%%|*}; row=${row#*|}; down_rate=${row%%|*}; row=${row#*|}
			total_up=${row%%|*}; row=${row#*|}; total_down=${row%%|*}; updated=${row#*|}
		else
			row=$(awk -F'|' -v sid="$sid" '$1==sid { print $2 "|" $3; exit }' "$data_link_persist_file" 2>/dev/null)
			if [ -n "$row" ]; then total_up=${row%%|*}; total_down=${row#*|}; fi
		fi
		grep -qxF "$sid" "$data_link_health_file" 2>/dev/null && healthy=1
		[ $first -eq 0 ] && printf ','
		first=0
		printf '{"sid":%s,"up_rate":%s,"down_rate":%s,"total_up":%s,"total_down":%s,"updated":%s,"healthy":%s}' \
			"$(json_quote "$sid")" "${up_rate:-0}" "${down_rate:-0}" "${total_up:-0}" "${total_down:-0}" "${updated:-0}" "$healthy"
	done
	echo "]"
}

reset_data_link_health() {
	: > "$data_link_health_file"
	: > "$data_link_metrics_file"
	: > "$data_link_conn_state_file"
	: > "$data_link_persist_file"
	: > "$data_link_last_poll_file"
	: > "$data_link_flush_file"
	echo "OK"
}

test_landing_node() {
	local sid="$1"
	[ -n "$sid" ] || { echo "ERROR: landing-node section id required" >&2; return 1; }
	landing_node_exists "$sid" || { echo "ERROR: landing node not found" >&2; return 1; }
	[ "$(uci -q get mihomo.$sid.enabled)" = "1" ] || { echo "ERROR: landing node is disabled" >&2; return 1; }
	landing_node_valid "$sid" 1 || { echo "ERROR: landing node configuration is incomplete" >&2; return 1; }
	local proxy run_config="/tmp/mihomo_run.yaml"
	proxy=$(landing_proxy_name "$sid")
	grep -q "name: \\"$proxy\\"" "$run_config" 2>/dev/null || { echo "ERROR: landing node is not applied; save and apply first" >&2; return 1; }
	local url proxy_enc url_enc resp
	url=$(uci -q get mihomo.config.test_url); [ -n "$url" ] || url="https://www.gstatic.com/generate_204"
	proxy_enc=$(urlencode "$proxy")
	url_enc=$(urlencode "$url")
	resp=$(mihomo_curl -fsS -m 12 "http://127.0.0.1:${API_PORT}/proxies/${proxy_enc}/delay?url=${url_enc}&timeout=8000" 2>&1)
	local code=$?
	if [ $code -ne 0 ] || ! printf '%s' "$resp" | grep -q '"delay"'; then
		chain_log "landing_node $sid test failed: ${resp:-controller returned no delay}"
		echo "ERROR: ${resp:-landing node is unreachable}" >&2
		return 1
	fi
	echo "$resp"
}

test_data_link() {
	local sid="$1"
	[ -n "$sid" ] || { echo "ERROR: data-link section id required" >&2; return 1; }
	uci -X show mihomo 2>/dev/null | grep -q "^mihomo\.$sid=data_link$" || { echo "ERROR: data-link section not found" >&2; return 1; }
	local group url group_enc url_enc
	group=$(data_link_group_name "$sid")
	url=$(uci -q get mihomo.config.test_url); [ -n "$url" ] || url="https://www.gstatic.com/generate_204"
	group_enc=$(urlencode "$group")
	url_enc=$(urlencode "$url")
	local resp
	resp=$(mihomo_curl -fsS -m 12 "http://127.0.0.1:${API_PORT}/proxies/${group_enc}/delay?url=${url_enc}&timeout=8000" 2>&1)
	local code=$?
	if [ $code -ne 0 ] || ! printf '%s' "$resp" | grep -q '"delay"'; then
		chain_log "data_link $sid test failed: ${resp:-controller returned no delay}"
		echo "ERROR: ${resp:-data link is unreachable}" >&2
		return 1
	fi
	mark_data_link_healthy "$sid"
	echo "$resp"
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
		else
			echo "add rule inet mihomo prerouting ip saddr 0.0.0.0/0 return"
		fi
		if [ -n "$acl_v6" ]; then
			echo "add rule inet mihomo prerouting ip6 saddr != { $acl_v6 } return"
		else
			echo "add rule inet mihomo prerouting ip6 saddr ::/0 return"
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
	check_controller)
		check_controller
		;;
	validate_acl_ip)
		validate_acl_ip "$2"
		;;
	wait_controller)
		wait_controller "$2"
		;;
	download_core)
		download_core "$2" "$3"
		;;
	update_geox)
		update_geox "$2" "$3"
		;;
	update_subscription)
		update_subscription "$2"
		;;
	update_subscriptions)
		update_subscriptions
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
	get_chain_status)
		get_chain_status
		;;
	add_data_link_to_acl)
		add_data_link_to_acl "$2"
		;;
	set_chain_front_node)
		set_chain_front_node "$2"
		;;
	get_chain_log)
		get_chain_log "$2"
		;;
	get_data_link_health)
		get_data_link_health
		;;
	get_data_link_metrics)
		get_data_link_metrics
		;;
	reset_data_link_health)
		reset_data_link_health
		;;
	test_landing_node)
		test_landing_node "$2"
		;;
	test_data_link)
		test_data_link "$2"
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
	clear_access_log)
		clear_access_log
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
		add_access_rule "$2" "$3" "$4" "$5" "$6" "$7"
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
	get_adblock_sources)
		get_adblock_sources
		;;
	add_adblock_source)
		add_adblock_source "$2" "$3" "$4" "$5"
		;;
	del_adblock_source)
		del_adblock_source "$2"
		;;
	toggle_adblock_source)
		toggle_adblock_source "$2" "$3"
		;;
	set_adblock_enabled)
		set_adblock_enabled "$2"
		;;
	*)
		echo "Usage: $0 {get_arch|get_lan_ip|get_lan_ip6|emit_tproxy_rules|check_core|check_controller|wait_controller|download_core|update_geox|update_subscription|update_subscriptions|clear_subscription|save_subscription_url|restore_subscription_url|auto_update_now|auto_update_loop|get_schedule|prepare_config|get_proxies|get_proxy_groups|select_node|get_chain_status|add_data_link_to_acl|set_chain_front_node|get_chain_log|get_data_link_health|get_data_link_metrics|reset_data_link_health|test_landing_node|test_data_link|get_connections|collect_connections|collect_loop|get_history|clear_access_log|get_access_rules|get_op_state|get_core_log|add_access_rule|del_access_rule|clear_access_rules|import_rules|test_node_delay|test_all_nodes|test_connectivity|traffic_loop|get_traffic|reset_traffic_domains|get_adblock_sources|add_adblock_source|del_adblock_source|toggle_adblock_source}"
		exit 1
		;;
esac
""",

    # LuCI Menu definition (JSON)
    "root/usr/share/luci/menu.d/luci-app-ssproxy.json": """{
    "admin/services/mihomo": {
        "title": "水杉代理",
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
    "admin/services/mihomo/chain": {
        "title": "链式代理",
        "order": 3,
        "action": {
            "type": "view",
            "path": "mihomo/chain"
        }
    },
    "admin/services/mihomo/accesslog": {
        "title": "访问日志",
        "order": 4,
        "action": {
            "type": "view",
            "path": "mihomo/accesslog"
        }
    },
    "admin/services/mihomo/rules": {
        "title": "规则管理",
        "order": 5,
        "action": {
            "type": "view",
            "path": "mihomo/rules"
        }
    },
    "admin/services/mihomo/traffic": {
        "title": "流量统计",
        "order": 6,
        "action": {
            "type": "view",
            "path": "mihomo/traffic"
        }
    },
    "admin/services/mihomo/adblock": {
        "title": "广告过滤",
        "order": 7,
        "action": {
            "type": "view",
            "path": "mihomo/adblock"
        }
    }
}
""",

    # RPCD ACL Permissions for Web UI execution
    "root/usr/share/rpcd/acl.d/luci-app-ssproxy.json": """{
	"luci-app-ssproxy": {
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
'require dom';

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
		return Promise.all([
			fs.exec('/usr/share/mihomo/helper.sh', ['get_history', '300']).catch(function() { return { stdout: '[]' }; })
		]);
	},

	render: function(results) {
		var self = this;
		if (self._timer) { clearInterval(self._timer); self._timer = null; }

		var hist_raw = (results[0] && results[0].stdout) ? results[0].stdout.trim() : '[]';

		var connections = [];
		var conn_error = null;

		var history = [];
		try { history = JSON.parse(hist_raw); } catch (e) { history = []; }

		var proxy_groups = {};

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
					E('td', { 'colspan': 5, 'style': 'text-align:center;color:#999;padding:24px;' }, _('暂无网络访问日志'))
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
				var traffic = fmt_bytes(h.up) + ' / ' + fmt_bytes(h.down);
				
				var tr = E('tr', {}, [
					E('td', {}, time),
					E('td', {}, dev || ip || '-'),
					E('td', {}, domain),
					E('td', {}, policy),
					E('td', {}, traffic)
				]);
				box.appendChild(tr);
			}
		}

		var view_html = E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, _('网络访问日志')),

			// IP列表板块（红杏 vs 设备）
			E('div', { 'class': 'cbi-section', 'style': 'display:none;' }, [
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
			E('div', { 'class': 'cbi-section', 'style': 'display:none;' }, [
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
				E('div', { 'style': 'display:flex;align-items:center;margin-bottom:12px;' }, [
					E('h3', { 'style': 'margin:0;' }, _('访问记录')),
					E('button', {
						'type': 'button',
						'class': 'cbi-button cbi-button-reset',
						'style': 'margin-left:auto;',
						'click': function(ev) {
							ev.preventDefault();
							if (!confirm(_('确定要清空网络访问日志吗？'))) return;
							return fs.exec('/usr/share/mihomo/helper.sh', ['clear_access_log']).then(function(res) {
								if (res.code !== 0) throw new Error(res.stderr || res.stdout || _('清空失败'));
								history = [];
								render_history();
								ui.addNotification(null, E('p', {}, _('网络访问日志已清空')), 'info');
							}).catch(function(err) {
								ui.addNotification(null, E('p', {}, _('清空失败：') + err.message), 'danger');
							});
						}
					}, _('清空'))
				]),
				E('div', { 'style': 'max-height: 380px; overflow-y: auto; border: 1px solid rgba(0,0,0,0.08); border-radius: 6px;' }, [
					E('table', { 'class': 'table', 'style': 'margin: 0;' }, [
						E('thead', {}, [
							E('tr', {}, [
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('时间')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('设备')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('域名 / 目标')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('策略')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02); position: sticky; top: 0;' }, _('流量 (↑/↓)'))
							])
						]),
						E('tbody', { 'id': 'hist-body' })
					])
				])
			])
		]);

		setTimeout(function() {
			render_history();
		}, 0);

		self._timer = setInterval(function() {
			fs.exec('/usr/share/mihomo/helper.sh', ['get_history', '300']).then(function(res) {
				try {
					history = JSON.parse((res.stdout || '[]').trim());
					render_history();
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
						var args = ['add_access_rule', ip, d, ac, (ac === 'proxy' ? gp : ''), rt, cm];
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

    "root/www/luci-static/resources/view/mihomo/adblock.js": """'use strict';
'require view';
'require ui';
'require fs';
'require uci';

return view.extend({
	load: function() {
		return uci.load('mihomo').then(function() {
			return Promise.all([
				fs.exec('/usr/share/mihomo/helper.sh', ['get_adblock_sources']).catch(function() { return { stdout: '[]' }; })
			]);
		});
	},

	render: function(results) {
		var sources = [];
		var raw = (results[0] && results[0].stdout) ? results[0].stdout.trim() : '[]';
		try { sources = JSON.parse(raw); } catch (e) { sources = []; }
		var ad_enabled = uci.get('mihomo', 'config', 'adblock_enabled') === '1';

		function btn(label, cls, fn) {
			return E('button', { 'class': 'cbi-button ' + cls, 'style': 'margin: 1px 2px; padding: 2px 8px;', 'click': function(ev) {
				ev.preventDefault(); fn();
			} }, label);
		}
		function notify(msg, level) { ui.addNotification(null, E('p', _(msg)), level || 'info'); }

		function del_source(sid) {
			return fs.exec('/usr/share/mihomo/helper.sh', ['del_adblock_source', sid]).then(function(res) {
				if (res.code === 0) { notify('规则源已删除（需重启核心后生效）。'); loadSources(); }
				else { notify('删除失败：' + (res.stderr || res.stdout || ''), 'danger'); }
			}).catch(function(err) { notify('通信错误：' + err.message, 'danger'); });
		}
		function toggle_source(sid, val) {
			return fs.exec('/usr/share/mihomo/helper.sh', ['toggle_adblock_source', sid, val]).then(function(res) {
				if (res.code === 0) { notify(val === '1' ? '已启用（需重启核心后生效）。' : '已禁用（需重启核心后生效）。'); loadSources(); }
				else { notify('操作失败：' + (res.stderr || res.stdout || ''), 'danger'); }
			}).catch(function(err) { notify('通信错误：' + err.message, 'danger'); });
		}
		function toggle_master(val) {
			return fs.exec('/usr/share/mihomo/helper.sh', ['set_adblock_enabled', val]).then(function(res) {
				if (res.code === 0) { notify(val === '1' ? '广告过滤已开启（需重启核心后生效）。' : '广告过滤已关闭（需重启核心后生效）。'); }
				else { notify('操作失败：' + (res.stderr || res.stdout || ''), 'danger'); }
			}).catch(function(err) { notify('通信错误：' + err.message, 'danger'); });
		}

		function render_sources() {
			var box = document.getElementById('src-body');
			if (!box) return;
			box.innerHTML = '';
			if (!sources.length) {
				box.appendChild(E('tr', {}, [
					E('td', { 'colspan': 5, 'style': 'text-align:center;color:#999;padding:15px;' }, _('暂无规则源（首次启动会自动添加预设源，请刷新页面）'))
				]));
				return;
			}
			for (var i = 0; i < sources.length; i++) {
				var s = sources[i];
				var enabled = s.enabled !== '0';
				box.appendChild(E('tr', {}, [
					E('td', {}, s.name || ''),
					E('td', { 'style': 'word-break:break-all;' }, s.url || ''),
					E('td', {}, s.behavior || 'domain'),
					E('td', { 'style': 'color:' + (enabled ? '#2ed573' : '#999') + ';' }, enabled ? _('启用') : _('禁用')),
					E('td', {}, [
						btn(enabled ? _('禁用') : _('启用'), enabled ? 'cbi-button-reset' : 'cbi-button-apply', (function(sid, v) {
							return function() { toggle_source(sid, v); };
						})(s.sid, enabled ? '0' : '1')),
						btn(_('删除'), 'cbi-button-reset', (function(sid) {
							return function() { if (confirm(_('确定删除此规则源？'))) del_source(sid); };
						})(s.sid))
					])
				]));
			}
		}

		var card = 'background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); padding: 15px; margin-bottom: 20px; border: 1px solid rgba(0,0,0,0.06);';

		var master_form = E('div', { 'class': 'cbi-section', 'style': card }, [
			E('h3', { 'style': 'margin-top: 0; margin-bottom: 12px; border-bottom: 1px solid rgba(0,0,0,0.06); padding-bottom: 8px;' }, _('广告过滤总开关')),
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, _('启用广告过滤')),
				E('div', { 'class': 'cbi-value-field' }, [
					E('input', { 'id': 'ad_master', 'type': 'checkbox', 'checked': ad_enabled, 'change': function(ev) {
						toggle_master(ev.target.checked ? '1' : '0');
					} }),
					E('span', { 'style': 'margin-left: 8px; color: #666; font-size: 13px;' }, _('开启后，已启用规则源将通过规则层 REJECT + DNS 层 nameserver-policy 双重拦截广告域名。'))
				])
			]),
			E('div', { 'class': 'cbi-value' }, [
				E('div', { 'class': 'cbi-value-field' }, [
					btn(_('应用并重启核心'), 'cbi-button-apply', function(ev) {
						ev.preventDefault();
						return fs.exec('/etc/init.d/mihomo', ['restart']).then(function() {
							notify('核心已重启，广告过滤配置已生效。');
							setTimeout(function() { location.reload(); }, 1500);
						}).catch(function(err) { notify('重启失败：' + err.message, 'danger'); });
					})
				])
			])
		]);

		var add_form = E('div', { 'class': 'cbi-section', 'style': card }, [
			E('h3', { 'style': 'margin-top: 0; margin-bottom: 12px; border-bottom: 1px solid rgba(0,0,0,0.06); padding-bottom: 8px;' }, _('新增广告规则源')),
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, _('名称')),
				E('div', { 'class': 'cbi-value-field' }, [
					E('input', { 'id': 'src_name', 'type': 'text', 'class': 'cbi-input-text', 'placeholder': '如 antiad（自动加 ssproxy-ad- 前缀）', 'style': 'width: 60%;' })
				])
			]),
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, _('规则集 URL')),
				E('div', { 'class': 'cbi-value-field' }, [
					E('input', { 'id': 'src_url', 'type': 'text', 'class': 'cbi-input-text', 'placeholder': 'Mihomo 兼容的 rule-provider 订阅地址', 'style': 'width: 80%;' })
				])
			]),
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, _('behavior')),
				E('div', { 'class': 'cbi-value-field' }, [
					E('select', { 'id': 'src_behavior', 'class': 'cbi-input-select', 'style': 'width: 200px;' }, [
						E('option', { 'value': 'domain' }, _('domain（推荐，支持 DNS 层拦截）')),
						E('option', { 'value': 'classical' }, _('classical（仅规则层拦截）'))
					])
				])
			]),
			E('div', { 'class': 'cbi-value' }, [
				E('div', { 'class': 'cbi-value-field' }, [
					btn(_('添加规则源'), 'cbi-button-add', function() {
						var nm = document.getElementById('src_name').value.trim();
						var u = document.getElementById('src_url').value.trim();
						var bv = document.getElementById('src_behavior').value;
						if (!u) { notify('请填写规则集 URL。', 'danger'); return; }
						if (!nm) { nm = 'custom'; }
						return fs.exec('/usr/share/mihomo/helper.sh', ['add_adblock_source', nm, u, bv, 'yaml']).then(function(res) {
							if (res.code === 0) {
								notify('规则源已添加（需重启核心后生效）。');
								document.getElementById('src_name').value = '';
								document.getElementById('src_url').value = '';
								loadSources();
							} else { notify('添加失败：' + (res.stderr || res.stdout || ''), 'danger'); }
						}).catch(function(err) { notify('通信错误：' + err.message, 'danger'); });
					})
				])
			])
		]);

		function loadSources() {
			return fs.exec('/usr/share/mihomo/helper.sh', ['get_adblock_sources']).then(function(res) {
				var rr = (res && res.stdout) ? res.stdout.trim() : '[]';
				try { sources = JSON.parse(rr); } catch (e) { sources = []; }
				render_sources();
			}).catch(function () {});
		}

		var view_html = E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, _('广告过滤')),
			E('p', {}, _('基于 Mihomo rule-provider 拦截广告/追踪域名。启用的规则源会在核心配置中生成 rule-providers + REJECT 规则，并对 domain 类型源在 DNS 层用 nameserver-policy 返回空解析，实现双重拦截。')),
			E('div', { 'style': 'background: #fff8e1; border: 1px solid #ffe082; border-radius: 6px; padding: 10px 14px; margin-bottom: 15px;' }, [
				E('p', { 'style': 'margin: 0; font-size: 13px; color: #6d5b00; line-height: 1.6;' }, _('<b>规则源需为 domain behavior</b> 才能同时参与 DNS 层拦截；classical 类型仅规则层 REJECT。任何修改都需点击「应用并重启核心」生效。'))
			]),
			master_form,
			E('div', { 'class': 'cbi-section', 'style': 'margin-bottom: 20px;' }, [
				E('h3', { 'style': 'margin-top: 0;' }, _('规则源列表')),
				E('div', { 'style': 'max-height: 400px; overflow-y: auto; border: 1px solid rgba(0,0,0,0.08); border-radius: 6px;' }, [
					E('table', { 'class': 'table', 'style': 'margin: 0;' }, [
						E('thead', {}, [
							E('tr', {}, [
								E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('名称')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('URL')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('behavior')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('状态')),
								E('th', { 'style': 'background: rgba(0,0,0,0.02);' }, _('操作'))
							])
						]),
						E('tbody', { 'id': 'src-body' })
					])
				])
			]),
			add_form
		]);

		setTimeout(function() { render_sources(); }, 0);
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
			var sha256_input = document.getElementById('core_download_sha256');
			var url = url_input ? url_input.value.trim() : '';
			var sha256 = sha256_input ? sha256_input.value.trim().toLowerCase() : '';
			if (url && !/^[0-9a-f]{64}$/.test(sha256)) {
				ui.addNotification(null, E('p', _('自定义核心地址必须提供有效的 SHA256。')), 'danger');
				return;
			}
			
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
				args.push(sha256);
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
			]),
			E('label', { 'class': 'cbi-value-title' }, _('SHA256（自定义地址必填）')),
			E('div', { 'class': 'cbi-value-field' }, [
				E('input', {
					'id': 'core_download_sha256',
					'type': 'text',
					'class': 'cbi-input-text',
					'autocomplete': 'off',
					'placeholder': '64 位 SHA256',
					'style': 'width: 60%; font-family: monospace;'
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

		var subscription_count = Number(schedule.subscription_count) || 0;

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
		var node_list_header_children = [ E('h3', { 'style': 'margin-top: 0; margin-bottom: 0;' }, _('全部订阅节点')), node_header_right ];
		var node_list_header = E('div', { 'style': 'display: flex; align-items: center; justify-content: space-between;' }, node_list_header_children);

		var node_list_schedule = null;
		if (schedule.auto_update === '1') {
			var sched_txt = _('订阅：') + subscription_count + _(' 个　|　自动更新：每 ') + schedule.interval + _(' 小时');
			if (schedule.last_update && schedule.last_update !== '') {
				sched_txt += _('　|　上次更新：') + new Date(parseInt(schedule.last_update, 10) * 1000).toLocaleString();
			}
			if (schedule.next_update && schedule.next_update !== '') {
				sched_txt += _('　|　下次更新：') + new Date(parseInt(schedule.next_update, 10) * 1000).toLocaleString();
			} else if (schedule.has_url !== '1') {
					sched_txt += _('　|　未配置启用的订阅');
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
					ui.showModal(_('正在更新全部订阅'), [
						E('p', {}, _('正在批量下载并合并所有启用的订阅...'))
					]);
					return fs.exec('/usr/share/mihomo/helper.sh', ['update_subscriptions']).then(function(res) {
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
			}, _('重新更新全部订阅'));
			node_list_body = E('div', { 'style': 'padding: 20px; text-align: center; color: #ff4757; background: rgba(255, 71, 87, 0.05); border-radius: 6px; border: 1px dashed #ff4757; line-height: 1.6;' }, [
				E('p', { 'style': 'font-weight: bold; margin: 0;' }, parse_error),
				retry_update_btn
			]);
		} else if (subscription_count > 0) {
			var quick_update_btn = E('button', {
				'class': 'cbi-button cbi-button-action',
				'style': 'margin-top: 10px;',
				'click': function(ev) {
					ev.preventDefault();
					ui.showModal(_('正在更新全部订阅'), [
						E('p', {}, _('正在批量下载并合并所有启用的订阅...'))
					]);
					return fs.exec('/usr/share/mihomo/helper.sh', ['update_subscriptions']).then(function(res) {
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
			}, _('更新全部订阅'));
			node_list_body = E('div', { 'style': 'padding: 20px; text-align: center; color: #ff9f43; background: rgba(255, 159, 67, 0.05); border-radius: 6px; border: 1px dashed #ff9f43;' }, [
				E('p', { 'style': 'font-weight: bold; margin: 0;' }, _('已配置订阅，但本地尚未生成合并节点数据。')),
				quick_update_btn
			]);
		} else {
			node_list_body = E('div', { 'style': 'padding: 15px; text-align: center; color: #999;' }, _('暂无可用节点信息。'));
		}

		// 系统代理日志：统一识别 Mihomo level= 与 OpenWrt syslog 级别。
		var log_text = String(logs || '');
		var log_level_filter = 'all';
		function detectLogLevel(line) {
			line = String(line || '');
			var match = line.match(/\\blevel\\s*=\\s*["']?(debug|info|notice|warn|warning|err|error|fatal|panic)\\b/i);
			if (!match) match = line.match(/\\b(?:daemon|user|kern|local[0-7])\\.(debug|info|notice|warn|warning|err|error|crit|alert|emerg)\\b/i);
			var level = match ? match[1].toLowerCase() : '';
			if (level === 'fatal' || level === 'panic' || level === 'crit' || level === 'alert' || level === 'emerg') return 'fatal';
			if (level === 'error' || level === 'err') return 'error';
			if (level === 'warning' || level === 'warn') return 'warning';
			if (level === 'debug') return 'debug';
			if (level === 'info' || level === 'notice') return 'info';
			if (/\\b(FATAL|PANIC)\\b/i.test(line)) return 'fatal';
			if (/\\b(ERROR|ERR)\\b/i.test(line)) return 'error';
			if (/\\b(WARN|WARNING)\\b/i.test(line)) return 'warning';
			if (/\\bDEBUG\\b/i.test(line)) return 'debug';
			if (/\\bINFO\\b/i.test(line)) return 'info';
			return 'other';
		}
		var logs_pre = document.createElement('pre');
		logs_pre.setAttribute('style', 'width: 100%; height: 250px; overflow-y: auto; font-family: monospace; padding: 12px; border-radius: 6px; border: 1px solid rgba(0,0,0,0.12); background: rgba(0,0,0,0.02); resize: vertical; margin-bottom: 12px; font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;');
		function renderLogs(text) {
			log_text = String(text || '');
			while (logs_pre.firstChild) { logs_pre.removeChild(logs_pre.firstChild); }
			var lines = log_text.split('\\n');
			if (lines.length === 0 || (lines.length === 1 && lines[0] === '')) {
				var empty = document.createElement('div');
				empty.textContent = _('暂无日志记录。');
				empty.style.color = '#999';
				logs_pre.appendChild(empty);
				return;
			}
			var visible = 0;
			for (var i = 0; i < lines.length; i++) {
				var line = lines[i];
				var level = detectLogLevel(line);
				if (log_level_filter !== 'all' && level !== log_level_filter) continue;
				var row = document.createElement('div');
				row.textContent = line;
				if (level === 'fatal' || level === 'error') {
					row.style.color = '#e03131';
					row.style.fontWeight = 'bold';
				} else if (level === 'warning') {
					row.style.color = '#e8590c';
				}
				logs_pre.appendChild(row);
				visible++;
			}
			if (visible === 0) {
				var no_match = document.createElement('div');
				no_match.textContent = _('暂无匹配该级别的日志。');
				no_match.style.color = '#999';
				logs_pre.appendChild(no_match);
			}
		}
		renderLogs(logs);
		var log_level_select = E('select', {
			'id': 'system-log-level-filter',
			'class': 'cbi-input-select',
			'style': 'min-width: 128px; margin: 0;',
			'change': function(ev) {
				log_level_filter = ev.target.value;
				renderLogs(log_text);
			}
		}, [
			E('option', { 'value': 'all' }, _('全部级别')),
			E('option', { 'value': 'debug' }, 'Debug'),
			E('option', { 'value': 'info' }, 'Info'),
			E('option', { 'value': 'warning' }, 'Warning'),
			E('option', { 'value': 'error' }, 'Error'),
			E('option', { 'value': 'fatal' }, 'Fatal'),
			E('option', { 'value': 'other' }, _('其他'))
		]);
		var clear_logs_btn = E('button', {
			'class': 'cbi-button cbi-button-neutral',
			'style': 'margin: 0;',
			'click': function(ev) {
				ev.preventDefault();
				log_text = '';
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
			E('h2', {}, _('水杉代理仪表盘')),
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
				E('div', { 'style': 'display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px;' }, [
					E('h3', { 'style': 'margin: 0;' }, _('系统代理日志')),
					E('div', { 'style': 'display: flex; flex-wrap: wrap; align-items: center; gap: 8px;' }, [
						E('label', { 'for': 'system-log-level-filter', 'style': 'margin: 0;' }, _('级别')),
						log_level_select,
						download_logs_btn,
						clear_logs_btn
					])
				]),
				logs_pre
			]),

			// 联系作者
			E('div', { 'style': 'text-align: center; color: #888; font-size: 12px; margin-top: 20px; padding-top: 15px; border-top: 1px solid rgba(0,0,0,0.06);' }, [
				'联系作者：',
				E('a', { 'href': 'mailto:chanf@me.com', 'style': 'color: #59569d; text-decoration: none;' }, 'chanf@me.com')
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

    "root/www/luci-static/resources/view/mihomo/chain.js": """'use strict';
'require view';
'require form';
'require fs';
'require ui';
'require uci';

function parse_json(res, fallback) {
	try { return JSON.parse((res && res.stdout || '').trim()); }
	catch (e) { return fallback; }
}

function protocol_label(type) {
	var labels = { socks5: 'SOCKS5', http: 'HTTP', ss: 'Shadowsocks', trojan: 'Trojan', vmess: 'VMess', vless: 'VLESS' };
	return labels[type] || type || '-';
}

function format_bytes(value, per_second) {
	var units = ['B', 'KB', 'MB', 'GB', 'TB'];
	var size = Math.max(0, Number(value) || 0);
	var index = 0;
	while (size >= 1024 && index < units.length - 1) { size /= 1024; index++; }
	var digits = size >= 100 || index === 0 ? 0 : (size >= 10 ? 1 : 2);
	return size.toFixed(digits) + ' ' + units[index] + (per_second ? '/s' : '');
}

return view.extend({
	load: function() {
		return Promise.all([
			uci.load('mihomo'),
			fs.exec('/usr/share/mihomo/helper.sh', ['get_proxies']).catch(function() { return { stdout: '[]' }; }),
			fs.exec('/usr/share/mihomo/helper.sh', ['get_chain_status']).catch(function() { return { stdout: '{}' }; }),
			fs.exec('/usr/share/mihomo/helper.sh', ['get_data_link_metrics']).catch(function() { return { stdout: '[]' }; })
		]);
	},

	render: function(data) {
		var self = this;
		var proxies = parse_json(data[1], []);
		var status = parse_json(data[2], {});
		var metric_rows = parse_json(data[3], []);
		var front_node = uci.get('mihomo', 'config', 'chain_front_node') || 'individual';
		var link_health = {};
		var link_metrics = {};
		var link_status = {};
		if (Array.isArray(status.links)) status.links.forEach(function(row) {
			link_status[safe_section_id(row.sid)] = row;
		});
		if (Array.isArray(metric_rows)) metric_rows.forEach(function(row) {
			var sid = safe_section_id(row.sid);
			link_metrics[sid] = row;
			if (row.healthy) link_health[sid] = true;
		});
		if (!Array.isArray(proxies)) proxies = [];
		proxies = proxies.filter(function(p) {
			return p && p.name && p.name.indexOf('ssproxy-landing-') !== 0 && p.name.indexOf('ssproxy-chain-') !== 0;
		});

		var landing_labels = {};
		uci.sections('mihomo', 'landing_node', function(sec) {
			landing_labels[sec['.name']] = sec.name || sec['.name'];
		});

		var m = new form.Map('mihomo', _('链式代理'));
		m.restart = 'mihomo';
		var s, o;

		s = m.section(form.GridSection, 'landing_node', _('落地节点'));
		s.anonymous = true;
		s.addremove = true;
		s.sortable = true;
		s.nodescriptions = true;
		s.sectiontitle = function(section_id) {
			return uci.get('mihomo', section_id, 'name') || section_id;
		};

		o = s.option(form.Flag, 'enabled', _('启用'));
		o.default = '1';
		o.rmempty = false;

		o = s.option(form.Value, 'name', _('名称'));
		o.rmempty = false;
		o.placeholder = _('香港落地');
		o.validate = function(section_id, value) {
			value = (value || '').trim();
			if (!value) return _('请输入名称');
			var duplicate = false;
			uci.sections('mihomo', 'landing_node', function(sec) {
				if (sec['.name'] !== section_id && (sec.name || '').trim() === value) duplicate = true;
			});
			return duplicate ? _('落地节点名称不能重复') : true;
		};

		o = s.option(form.ListValue, 'type', _('协议'));
		o.value('socks5', 'SOCKS5');
		o.value('http', 'HTTP');
		o.value('ss', 'Shadowsocks');
		o.value('trojan', 'Trojan');
		o.value('vmess', 'VMess');
		o.value('vless', 'VLESS');
		o.default = 'socks5';
		o.rmempty = false;
		o.textvalue = function(section_id) { return protocol_label(uci.get('mihomo', section_id, 'type')); };

		o = s.option(form.Value, 'server', _('服务器'));
		o.rmempty = false;
		o.placeholder = '203.0.113.10';

		o = s.option(form.Value, 'port', _('端口'));
		o.datatype = 'port';
		o.rmempty = false;
		o.placeholder = '1080';

		o = s.option(form.Value, 'username', _('用户名'));
		o.modalonly = true;
		o.depends('type', 'socks5');
		o.depends('type', 'http');

		o = s.option(form.Value, 'password', _('密码'));
		o.password = true;
		o.modalonly = true;
		o.depends('type', 'socks5');
		o.depends('type', 'http');
		o.depends('type', 'ss');
		o.depends('type', 'trojan');

		o = s.option(form.ListValue, 'cipher', _('加密方式'));
		o.modalonly = true;
		o.value('aes-128-gcm', 'aes-128-gcm');
		o.value('aes-256-gcm', 'aes-256-gcm');
		o.value('chacha20-ietf-poly1305', 'chacha20-ietf-poly1305');
		o.value('2022-blake3-aes-128-gcm', '2022-blake3-aes-128-gcm');
		o.value('2022-blake3-aes-256-gcm', '2022-blake3-aes-256-gcm');
		o.depends('type', 'ss');
		o.rmempty = false;

		o = s.option(form.Value, 'uuid', _('UUID'));
		o.modalonly = true;
		o.depends('type', 'vmess');
		o.depends('type', 'vless');
		o.rmempty = false;

		o = s.option(form.Value, 'alter_id', _('Alter ID'));
		o.modalonly = true;
		o.datatype = 'uinteger';
		o.default = '0';
		o.depends('type', 'vmess');

		o = s.option(form.ListValue, 'vmess_cipher', _('VMess 加密'));
		o.modalonly = true;
		o.value('auto', 'auto');
		o.value('none', 'none');
		o.value('aes-128-gcm', 'aes-128-gcm');
		o.value('chacha20-poly1305', 'chacha20-poly1305');
		o.default = 'auto';
		o.depends('type', 'vmess');
		o.write = function(section_id, value) { return uci.set('mihomo', section_id, 'cipher', value); };
		o.cfgvalue = function(section_id) { return uci.get('mihomo', section_id, 'cipher') || 'auto'; };

		o = s.option(form.Flag, 'tls', _('TLS'));
		o.modalonly = true;
		o.default = '0';
		o.depends('type', 'socks5');
		o.depends('type', 'http');
		o.depends('type', 'vmess');
		o.depends('type', 'vless');

		o = s.option(form.Value, 'sni', _('SNI / Server Name'));
		o.modalonly = true;
		o.depends('type', 'trojan');
		o.depends('type', 'vmess');
		o.depends('type', 'vless');

		o = s.option(form.ListValue, 'flow', _('Flow'));
		o.modalonly = true;
		o.value('', _('无'));
		o.value('xtls-rprx-vision', 'xtls-rprx-vision');
		o.depends('type', 'vless');

		o = s.option(form.Flag, 'skip_cert_verify', _('跳过证书验证'));
		o.modalonly = true;
		o.default = '0';
		o.depends('type', 'socks5');
		o.depends('type', 'http');
		o.depends('type', 'trojan');
		o.depends('type', 'vmess');
		o.depends('type', 'vless');

		o = s.option(form.Button, '_test_landing', _('测试'));
		o.inputtitle = _('测试');
		o.inputstyle = 'action';
		o.onclick = function(section_id) {
			return fs.exec('/usr/share/mihomo/helper.sh', ['test_landing_node', section_id]).then(function(res) {
				var result = parse_json(res, {});
				if (res.code === 0 && typeof result.delay === 'number') {
					ui.addNotification(null, E('p', {}, _('落地节点可用，延时：') + result.delay + ' ms'), 'info');
				} else {
					ui.addNotification(null, E('p', {}, _('落地节点测试失败：') + (res.stderr || res.stdout || '')), 'danger');
				}
			}).catch(function(err) {
				ui.addNotification(null, E('p', {}, _('落地节点测试失败：') + err.message), 'danger');
			});
		};

		function safe_section_id(section_id) {
			return String(section_id || '').replace(/[^A-Za-z0-9_]/g, '_');
		}

		function update_link_metrics() {
			var dots = document.querySelectorAll('.ssproxy-data-link-dot');
			for (var i = 0; i < dots.length; i++) {
				var sid = dots[i].getAttribute('data-section');
				var healthy = !!link_health[sid];
				var state = link_status[sid] || {};
				var color = '#868e96';
				if (state.code === 'ready') color = healthy ? '#16845b' : '#1971c2';
				else if (state.code === 'bypassed' || state.code === 'not_applied') color = '#e8590c';
				else if (state.code && state.code !== 'disabled') color = '#d92d20';
				dots[i].style.backgroundColor = color;
				dots[i].title = state.reason || (healthy ? _('链路已通讯') : _('尚未检测到成功通讯'));
			}
			var speeds = document.querySelectorAll('.ssproxy-data-link-speed');
			for (var j = 0; j < speeds.length; j++) {
				var speed_sid = speeds[j].getAttribute('data-section');
				var speed_row = link_metrics[speed_sid] || {};
				speeds[j].textContent = '↑ ' + format_bytes(speed_row.up_rate, true) + '  ↓ ' + format_bytes(speed_row.down_rate, true);
			}
			var totals = document.querySelectorAll('.ssproxy-data-link-total');
			for (var k = 0; k < totals.length; k++) {
				var total_sid = totals[k].getAttribute('data-section');
				var total_row = link_metrics[total_sid] || {};
				totals[k].textContent = '↑ ' + format_bytes(total_row.total_up, false) + '  ↓ ' + format_bytes(total_row.total_down, false);
			}
		}

		function refresh_link_metrics() {
			return fs.exec('/usr/share/mihomo/helper.sh', ['get_data_link_metrics']).then(function(res) {
				var rows = parse_json(res, []);
				link_health = {};
				link_metrics = {};
				if (Array.isArray(rows)) rows.forEach(function(row) {
					var sid = safe_section_id(row.sid);
					link_metrics[sid] = row;
					if (row.healthy) link_health[sid] = true;
				});
				update_link_metrics();
			});
		}

		var reset_button = E('button', {
			'type': 'button',
			'class': 'cbi-button cbi-button-reset',
			'style': 'margin-left:8px;',
			'click': function(ev) {
				ev.preventDefault();
				ev.stopPropagation();
				return fs.exec('/usr/share/mihomo/helper.sh', ['reset_data_link_health']).then(function() {
					link_health = {};
					link_metrics = {};
					update_link_metrics();
					ui.addNotification(null, E('p', {}, _('数据链路状态和流量已重置')), 'info');
				});
			}
		}, _('重置'));

		var front_options = [ E('option', {
			'value': 'individual',
			'selected': front_node === 'individual' ? 'selected' : null
		}, _('非统一前置')) ];
		var front_found = front_node === 'individual';
		for (var front_index = 0; front_index < proxies.length; front_index++) {
			var front_name = proxies[front_index].name;
			if (front_name === front_node) front_found = true;
			front_options.push(E('option', {
				'value': front_name,
				'selected': front_name === front_node ? 'selected' : null
			}, front_name));
		}
		if (!front_found) {
			front_options.push(E('option', {
				'value': front_node,
				'selected': 'selected',
				'disabled': 'disabled'
			}, front_node + ' (' + _('已失效') + ')'));
		}

		var front_select = E('select', {
			'class': 'cbi-input-select',
			'style': 'min-width:180px;max-width:320px;',
			'change': function(ev) {
				var selected = ev.target.value;
				if (selected === front_node) return;
				if (!confirm(_('切换前置节点将重启 Mihomo，现有连接可能短暂中断。是否继续？'))) {
					ev.target.value = front_node;
					return;
				}
				front_select.disabled = true;
				return fs.exec('/usr/share/mihomo/helper.sh', ['set_chain_front_node', selected]).then(function(res) {
					if (!res || res.code !== 0) throw new Error((res && (res.stderr || res.stdout)) || _('保存失败'));
					return fs.exec('/etc/init.d/mihomo', ['restart']);
				}).then(function(res) {
					if (!res || res.code !== 0) throw new Error((res && (res.stderr || res.stdout)) || _('重启失败'));
					ui.addNotification(null, E('p', {}, _('前置节点已应用')), 'info');
					setTimeout(function() { location.reload(); }, 600);
				}).catch(function(err) {
					front_select.value = front_node;
					front_select.disabled = false;
					ui.addNotification(null, E('p', {}, _('前置节点应用失败：') + err.message), 'danger');
				});
			}
		}, front_options);

		var data_link_title = E('div', { 'style': 'display:flex;align-items:center;width:100%;gap:12px;' }, [
			E('span', {}, _('数据链路')),
			E('div', { 'style': 'display:flex;align-items:center;margin-left:auto;gap:8px;' }, [
				E('span', {}, _('前置节点')),
				front_select,
				reset_button
			])
		]);

		s = m.section(form.GridSection, 'data_link', data_link_title);
		s.anonymous = true;
		s.addremove = true;
		s.sortable = true;
		s.nodescriptions = true;

		o = s.option(form.DummyValue, '_health', _('状态'));
		o.cfgvalue = function(section_id) {
			var sid = safe_section_id(section_id);
			var row = link_status[sid] || {};
			return row.reason || (link_health[sid] ? _('链路已通讯') : _('尚未检测到成功通讯'));
		};
		o.textvalue = function(section_id) {
			var sid = safe_section_id(section_id);
			var healthy = !!link_health[sid];
			var row = link_status[sid] || {};
			var code = row.code || 'unknown';
			var color = '#868e96';
			if (code === 'ready') color = healthy ? '#16845b' : '#1971c2';
			else if (code === 'bypassed' || code === 'not_applied') color = '#e8590c';
			else if (code !== 'disabled') color = '#d92d20';
			var children = [E('span', {
				'class': 'ssproxy-data-link-dot',
				'data-section': sid,
				'title': row.reason || (healthy ? _('链路已通讯') : _('尚未检测到成功通讯')),
				'style': 'display:inline-block;flex:0 0 11px;width:11px;height:11px;border-radius:50%;background-color:' + color + ';'
			}), E('span', { 'style': 'line-height:1.35;' }, row.reason || _('状态未知'))];
			if (code === 'bypassed') {
				children.push(E('button', {
					'type': 'button',
					'class': 'cbi-button cbi-button-action',
					'style': 'margin:4px 0 0 19px;padding:2px 8px;',
					'click': function(ev) {
						ev.preventDefault(); ev.stopPropagation();
						if (!confirm(_('将该设备加入受控 IP 列表并重启 Mihomo。是否继续？'))) return;
						return fs.exec('/usr/share/mihomo/helper.sh', ['add_data_link_to_acl', section_id]).then(function(res) {
							if (!res || res.code !== 0) throw new Error((res && (res.stderr || res.stdout)) || _('加入失败'));
							return fs.exec('/etc/init.d/mihomo', ['restart']);
						}).then(function(res) {
							if (!res || res.code !== 0) throw new Error((res && (res.stderr || res.stdout)) || _('重启失败'));
							location.reload();
						}).catch(function(err) {
							ui.addNotification(null, E('p', {}, _('加入受控 IP 失败：') + err.message), 'danger');
						});
					}
				}, _('加入受控 IP')));
			}
			return E('span', { 'style': 'display:flex;flex-wrap:wrap;align-items:center;gap:8px;min-width:180px;' }, children);
		};

		o = s.option(form.DummyValue, '_speed', _('当前速率'));
		o.cfgvalue = function(section_id) {
			var sid = safe_section_id(section_id);
			var row = link_metrics[sid] || {};
			return '↑ ' + format_bytes(row.up_rate, true) + '  ↓ ' + format_bytes(row.down_rate, true);
		};
		o.textvalue = function(section_id) {
			var sid = safe_section_id(section_id);
			var row = link_metrics[sid] || {};
			return E('span', {
				'class': 'ssproxy-data-link-speed',
				'data-section': sid,
				'style': 'white-space:nowrap;'
			}, '↑ ' + format_bytes(row.up_rate, true) + '  ↓ ' + format_bytes(row.down_rate, true));
		};

		o = s.option(form.DummyValue, '_total', _('累计流量'));
		o.cfgvalue = function(section_id) {
			var sid = safe_section_id(section_id);
			var row = link_metrics[sid] || {};
			return '↑ ' + format_bytes(row.total_up, false) + '  ↓ ' + format_bytes(row.total_down, false);
		};
		o.textvalue = function(section_id) {
			var sid = safe_section_id(section_id);
			var row = link_metrics[sid] || {};
			return E('span', {
				'class': 'ssproxy-data-link-total',
				'data-section': sid,
				'style': 'white-space:nowrap;'
			}, '↑ ' + format_bytes(row.total_up, false) + '  ↓ ' + format_bytes(row.total_down, false));
		};

		o = s.option(form.Flag, 'enabled', _('启用'));
		o.default = '1';
		o.rmempty = false;

		o = s.option(form.Value, 'device_ip', _('设备 IP'));
		o.rmempty = false;
		o.placeholder = '192.168.66.158';
		o.validate = function(section_id, value) {
			value = (value || '').trim();
			if (!/^[0-9A-Fa-f:.]+(?:\\/\\d{1,3})?$/.test(value)) return _('请输入有效的 IP 或 CIDR');
			var duplicate = false;
			uci.sections('mihomo', 'data_link', function(sec) {
				if (sec['.name'] !== section_id && sec.enabled !== '0' && (sec.device_ip || '').trim() === value) duplicate = true;
			});
			return duplicate ? _('同一设备只能配置一条启用链路') : true;
		};

		o = s.option(form.Value, 'subscription_node', _('订阅节点'));
		o.rmempty = false;
		o.readonly = front_node !== 'individual';
		for (var i = 0; i < proxies.length; i++) o.value(proxies[i].name, proxies[i].name);

		o = s.option(form.ListValue, 'landing_node', _('落地节点'));
		o.rmempty = false;
		Object.keys(landing_labels).forEach(function(sid) { o.value(sid, landing_labels[sid]); });
		o.textvalue = function(section_id) {
			var sid = uci.get('mihomo', section_id, 'landing_node');
			return landing_labels[sid] || sid || '-';
		};

		o = s.option(form.Button, '_test', _('测试'));
		o.inputtitle = _('测试');
		o.inputstyle = 'action';
		o.onclick = function(section_id) {
			var state = link_status[safe_section_id(section_id)] || {};
			if (!state.runtime || state.code === 'bypassed') {
				ui.addNotification(null, E('p', {}, state.reason || _('请先保存并应用链路配置。')), 'warning');
				return;
			}
			return fs.exec('/usr/share/mihomo/helper.sh', ['test_data_link', section_id]).then(function(res) {
				var result = parse_json(res, {});
				if (res.code === 0 && typeof result.delay === 'number') {
					link_health[safe_section_id(section_id)] = true;
					update_link_metrics();
					ui.addNotification(null, E('p', {}, _('链路延时：') + result.delay + ' ms'), 'info');
				} else {
					ui.addNotification(null, E('p', {}, _('链路测试失败：') + (res.stderr || res.stdout || '')), 'danger');
				}
			});
		};

		var issue_count = Array.isArray(status.links) ? status.links.filter(function(row) {
			return row && row.enabled && row.code !== 'ready';
		}).length : 0;
		var state_color = !status.controller || issue_count ? '#b54708' : '#16845b';
		var state_text = status.controller ? _('控制器在线') : _('控制器离线');
		var status_panel = E('div', { 'class': 'cbi-section', 'style': 'padding: 14px 16px; margin-bottom: 18px; border-left: 4px solid ' + state_color + ';' }, [
			E('div', { 'style': 'display:flex; flex-wrap:wrap; align-items:center; gap:18px;' }, [
				E('strong', { 'style': 'color:' + state_color + ';' }, state_text),
				E('span', {}, _('落地节点') + ': ' + (status.runtime_landing || 0) + ' / ' + (status.landing_enabled || 0)),
				E('span', {}, _('有效链路') + ': ' + (status.runtime_links || 0) + ' / ' + (status.link_enabled || 0)),
				issue_count ? E('strong', { 'style': 'color:#b54708;' }, _('需处理链路') + ': ' + issue_count) : null,
				E('button', { 'class': 'cbi-button cbi-button-neutral', 'click': function(ev) {
					ev.preventDefault();
					return fs.exec('/usr/share/mihomo/helper.sh', ['get_chain_log', '120']).then(function(res) {
						ui.showModal(_('链式代理日志'), [
							E('pre', { 'style': 'max-height:420px;overflow:auto;white-space:pre-wrap;' }, res.stdout || _('暂无日志')),
							E('div', { 'class': 'right' }, [ E('button', { 'class': 'cbi-button', 'click': ui.hideModal }, _('关闭')) ])
						]);
					});
				} }, _('日志'))
			].filter(function(item) { return item !== null; }))
		]);

		return m.render().then(function(form_node) {
			if (self._health_timer) clearInterval(self._health_timer);
			self._health_timer = setInterval(refresh_link_metrics, 5000);
			return E('div', { 'class': 'cbi-map' }, [
				E('h2', {}, _('链式代理')),
				status_panel,
				form_node
			]);
		});
	},

	unload: function() {
		if (this._health_timer) {
			clearInterval(this._health_timer);
			this._health_timer = null;
		}
	}
});
""",

    "root/www/luci-static/resources/view/mihomo/settings.js": """'use strict';
'require view';
'require form';
'require ui';
'require fs';
'require uci';

return view.extend({
	render: function() {
		var m, s, o;

		m = new form.Map('mihomo', _('水杉代理设置'),
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

		// 所有已保存且启用的订阅在一个事务中批量更新和合并。
		o = s.option(form.DummyValue, '_update_btn', _('订阅管理'));
		o.rawhtml = true;
		o.depends('config_mode', 'subscription');
		o.depends('config_mode', 'mixed');
		o.cfgvalue = function(section_id) {
			var update_btn = E('button', {
				'class': 'cbi-button cbi-button-action',
				'click': function(ev) {
					ev.preventDefault();
					ui.showModal(_('正在更新全部订阅'), [
						E('p', {}, _('正在批量下载并合并所有启用的订阅...'))
					]);

					return fs.exec('/usr/share/mihomo/helper.sh', ['update_subscriptions']).then(function(res) {
						ui.hideModal();
						if (res.code === 0) {
							var result = {};
							try { result = JSON.parse((res.stdout || '{}').trim()); } catch (e) {}
							ui.addNotification(null, E('p', _('订阅更新完成：') + (result.available || 0) + _(' 个可用，') + (result.nodes || 0) + _(' 个节点。')), 'info');
						} else {
							ui.addNotification(null, E('p', _('更新配置失败：') + (res.stderr || res.stdout || '')), 'danger');
						}
					}).catch(function(err) {
						ui.hideModal();
						ui.addNotification(null, E('p', _('下载订阅失败：') + err.message), 'danger');
					});
				}
			}, _('更新全部订阅'));
			
			return E('div', {}, [update_btn]);
		};

		o = s.option(form.Flag, 'auto_update', _('定时更新订阅'), _('开启后，系统会每小时检查一次，并按下方设置的时间间隔批量更新所有启用的订阅。'));
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

		var subscriptions = m.section(form.GridSection, 'subscription', _('订阅列表'));
		subscriptions.anonymous = true;
		subscriptions.addremove = true;
		subscriptions.sortable = true;
		subscriptions.nodescriptions = true;
		subscriptions.addbtntitle = _('添加订阅');
		subscriptions.sectiontitle = function(section_id) {
			return uci.get('mihomo', section_id, 'name') || section_id;
		};

		o = subscriptions.option(form.Flag, 'enabled', _('启用'));
		o.default = '1';
		o.rmempty = false;

		o = subscriptions.option(form.Value, 'name', _('名称'));
		o.rmempty = false;
		o.placeholder = _('机场订阅');
		o.validate = function(section_id, value) {
			value = (value || '').trim();
			if (!value) return _('请输入订阅名称');
			var duplicate = false;
			uci.sections('mihomo', 'subscription', function(sec) {
				if (sec['.name'] !== section_id && (sec.name || '').trim() === value) duplicate = true;
			});
			return duplicate ? _('订阅名称不能重复') : true;
		};

		o = subscriptions.option(form.Value, 'url', _('订阅地址'));
		o.modalonly = true;
		o.rmempty = false;
		o.placeholder = 'https://example.com/clash.yaml';
		o.validate = function(section_id, value) {
			value = (value || '').trim();
			return /^https?:\\/\\/[^\\s"']+$/.test(value) ? true : _('请输入有效的 HTTP 或 HTTPS 订阅地址');
		};

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

# Replace the legacy multi-panel access-log view with the delivered minimal
# network-log view. Keeping this assignment next to src_files makes the final
# package content explicit while the historical implementation is removed in a
# later source-layout cleanup.
src_files["root/www/luci-static/resources/view/mihomo/accesslog.js"] = """'use strict';
'require view';
'require ui';
'require fs';
'require dom';

function formatTime(timestamp) {
	var value = Number(timestamp) || 0;
	if (!value) return '-';
	try { return new Date(value * 1000).toLocaleString(); }
	catch (e) { return String(timestamp); }
}

function formatBytes(value) {
	var size = Math.max(0, Number(value) || 0);
	var units = ['B', 'KB', 'MB', 'GB', 'TB'];
	var unit = 0;
	while (size >= 1024 && unit < units.length - 1) {
		size /= 1024;
		unit++;
	}
	var digits = unit === 0 ? 0 : (size >= 100 ? 0 : (size >= 10 ? 1 : 2));
	return size.toFixed(digits) + ' ' + units[unit];
}

function parseLogs(result) {
	if (!result || result.code !== 0)
		throw new Error((result && (result.stderr || result.stdout)) || _('读取日志失败'));
	var payload = JSON.parse((result.stdout || '[]').trim() || '[]');
	if (!Array.isArray(payload)) throw new Error(_('日志接口返回格式错误'));
	return payload;
}

return view.extend({
	deviceFilterKey: function(row) {
		return JSON.stringify([
			String((row && row.device) || ''),
			String((row && row.ip) || '')
		]);
	},

	deviceFilterLabel: function(row) {
		var device = String((row && row.device) || '');
		var ip = String((row && row.ip) || '');
		if (device && ip && device !== ip) return device + ' / ' + ip;
		return device || ip || _('未识别设备');
	},

	outboundFilterValue: function(row) {
		return String((row && (row.policy || row.rule)) || '');
	},

	outboundFilterKey: function(row) {
		return JSON.stringify(this.outboundFilterValue(row));
	},

	updateFilterOptions: function() {
		if (!this._deviceFilter || !this._outboundFilter) return;

		for (var i = 0; i < this._logs.length; i++) {
			var row = this._logs[i] || {};
			var deviceKey = this.deviceFilterKey(row);
			var outbound = this.outboundFilterValue(row);
			var outboundKey = this.outboundFilterKey(row);
			this._deviceOptions[deviceKey] = this.deviceFilterLabel(row);
			this._outboundOptions[outboundKey] = outbound || _('未识别策略');
		}

		var selectedDevice = this._deviceFilter.value;
		var selectedOutbound = this._outboundFilter.value;
		var deviceOptions = [
			E('option', { 'value': '' }, _('全部设备 / IP'))
		];
		var outboundOptions = [
			E('option', { 'value': '' }, _('全部出站策略'))
		];

		Object.keys(this._deviceOptions).sort(function(a, b) {
			return String(this._deviceOptions[a]).localeCompare(String(this._deviceOptions[b]));
		}.bind(this)).forEach(function(key) {
			deviceOptions.push(E('option', { 'value': key }, this._deviceOptions[key]));
		}.bind(this));
		Object.keys(this._outboundOptions).sort(function(a, b) {
			return String(this._outboundOptions[a]).localeCompare(String(this._outboundOptions[b]));
		}.bind(this)).forEach(function(key) {
			outboundOptions.push(E('option', { 'value': key }, this._outboundOptions[key]));
		}.bind(this));

		dom.content(this._deviceFilter, deviceOptions);
		dom.content(this._outboundFilter, outboundOptions);
		this._deviceFilter.value = selectedDevice;
		this._outboundFilter.value = selectedOutbound;
	},

	load: function() {
		return fs.exec('/usr/share/mihomo/helper.sh', ['get_history', '300']).then(function(result) {
			try { return { logs: parseLogs(result), error: '' }; }
			catch (e) { return { logs: [], error: e.message }; }
		}).catch(function(error) {
			return { logs: [], error: error.message || String(error) };
		});
	},

	renderRows: function() {
		var body = this._body;
		if (!body) return;
		dom.content(body, []);

		if (this._error) {
			body.appendChild(E('tr', {}, E('td', {
				'colspan': 5,
				'style': 'padding:24px;text-align:center;color:#d92d20;'
			}, this._error)));
			return;
		}

		var selectedDevice = this._deviceFilter ? this._deviceFilter.value : '';
		var selectedOutbound = this._outboundFilter ? this._outboundFilter.value : '';
		var filteredLogs = this._logs.filter(function(row) {
			if (selectedDevice && this.deviceFilterKey(row) !== selectedDevice) return false;
			if (selectedOutbound && this.outboundFilterKey(row) !== selectedOutbound) return false;
			return true;
		}.bind(this));

		if (!filteredLogs.length) {
			body.appendChild(E('tr', {}, E('td', {
				'colspan': 5,
				'style': 'padding:24px;text-align:center;color:#777;'
			}, this._logs.length ? _('没有符合筛选条件的访问日志') : _('暂无网络访问日志'))));
			return;
		}

		for (var i = 0; i < filteredLogs.length; i++) {
			var row = filteredLogs[i] || {};
			var device = row.device || row.ip || '-';
			var deviceCell = row.device && row.ip ? E('div', {}, [
				E('div', {}, row.device),
				E('div', { 'style': 'font-size:12px;color:#777;' }, row.ip)
			]) : device;
			var target = row.domain || row.dst || '-';
			var outbound = row.policy || row.rule || '-';
			body.appendChild(E('tr', {}, [
				E('td', { 'style': 'white-space:nowrap;' }, formatTime(row.ts)),
				E('td', {}, deviceCell),
				E('td', { 'style': 'word-break:break-all;' }, target),
				E('td', {}, outbound),
				E('td', { 'style': 'white-space:nowrap;' },
					'↑ ' + formatBytes(row.up) + '  ↓ ' + formatBytes(row.down))
			]));
		}
	},

	refreshLogs: function() {
		var self = this;
		return fs.exec('/usr/share/mihomo/helper.sh', ['get_history', '300']).then(function(result) {
			self._logs = parseLogs(result);
			self._error = '';
			self.updateFilterOptions();
			self.renderRows();
		}).catch(function(error) {
			self._error = error.message || String(error);
			self.renderRows();
		});
	},

	clearLogs: function(button) {
		var self = this;
		if (!confirm(_('确定要清空网络访问日志吗？'))) return;
		button.disabled = true;
		return fs.exec('/usr/share/mihomo/helper.sh', ['clear_access_log']).then(function(result) {
			if (!result || result.code !== 0)
				throw new Error((result && (result.stderr || result.stdout)) || _('清空失败'));
			self._logs = [];
			self._error = '';
			self._deviceOptions = {};
			self._outboundOptions = {};
			if (self._deviceFilter) self._deviceFilter.value = '';
			if (self._outboundFilter) self._outboundFilter.value = '';
			self.updateFilterOptions();
			self.renderRows();
			ui.addNotification(null, E('p', {}, _('网络访问日志已清空')), 'info');
		}).catch(function(error) {
			ui.addNotification(null, E('p', {}, _('清空失败：') + (error.message || error)), 'danger');
		}).then(function() {
			button.disabled = false;
		});
	},

	render: function(data) {
		var self = this;
		if (this._timer) clearInterval(this._timer);
		this._logs = data.logs || [];
		this._error = data.error || '';
		this._deviceOptions = {};
		this._outboundOptions = {};

		var clearButton = E('button', {
			'type': 'button',
			'class': 'cbi-button cbi-button-reset',
			'click': function(ev) {
				ev.preventDefault();
				return self.clearLogs(clearButton);
			}
		}, _('清空'));

		this._body = E('tbody');
		this._deviceFilter = E('select', {
			'id': 'accesslog-device-filter',
			'class': 'cbi-input-select',
			'change': function() { self.renderRows(); }
		});
		this._outboundFilter = E('select', {
			'id': 'accesslog-outbound-filter',
			'class': 'cbi-input-select',
			'change': function() { self.renderRows(); }
		});
		var viewNode = E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, _('网络访问日志')),
			E('div', { 'class': 'cbi-section' }, [
				E('div', { 'style': 'display:flex;align-items:center;margin-bottom:12px;' }, [
					E('h3', { 'style': 'margin:0;' }, _('访问记录')),
					E('div', { 'style': 'margin-left:auto;' }, clearButton)
				]),
				E('div', { 'style': 'display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;margin-bottom:12px;' }, [
					E('label', { 'style': 'display:flex;flex-direction:column;gap:4px;min-width:220px;' }, [
						E('span', { 'style': 'font-size:12px;color:#666;' }, _('设备 / IP')),
						this._deviceFilter
					]),
					E('label', { 'style': 'display:flex;flex-direction:column;gap:4px;min-width:220px;' }, [
						E('span', { 'style': 'font-size:12px;color:#666;' }, _('出站策略')),
						this._outboundFilter
					])
				]),
				E('div', { 'style': 'overflow-x:auto;max-height:560px;overflow-y:auto;border:1px solid #ddd;' },
					E('table', { 'class': 'table', 'style': 'margin:0;min-width:760px;' }, [
						E('thead', {}, E('tr', {}, [
							E('th', {}, _('时间')),
							E('th', {}, _('设备 / IP')),
							E('th', {}, _('访问目标')),
							E('th', {}, _('出站策略')),
							E('th', {}, _('流量 (↑/↓)'))
						])),
						this._body
					]))
			])
		]);

		this.updateFilterOptions();
		this.renderRows();
		this._timer = setInterval(function() { self.refreshLogs(); }, 5000);
		return viewNode;
	},

	unload: function() {
		if (this._timer) clearInterval(this._timer);
		this._timer = null;
		this._body = null;
		this._deviceFilter = null;
		this._outboundFilter = null;
		this._deviceOptions = null;
		this._outboundOptions = null;
	}
});
"""

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

@contextlib.contextmanager
def _reproducible_tar_writer(output_filename):
    """Open a deterministic gzip/tar writer and close every layer on failure."""
    with open(output_filename, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=1700000000) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                yield tar


def make_tar_gz(source_dir, output_filename, is_control=False):
    """Generates a reproducible tar.gz archive with root:root ownership and correct modes, including directories and using './' prefix."""
    print(f"Archiving '{source_dir}' -> '{output_filename}'...")
    # Pin the gzip header (fixed mtime, no embedded filename) so two builds of
    # the same inputs produce byte-identical archives.
    with _reproducible_tar_writer(output_filename) as tar:
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

def write_tar_gz_outer_archive(archive_path, file_list):
    """Writes the final .ipk as a gzipped tarball containing the three components."""
    print(f"Creating IPK archive (tar.gz format) '{archive_path}'...")
    with _reproducible_tar_writer(archive_path) as tar:
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
    print("Initializing source tree for luci-app-ssproxy...")
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
