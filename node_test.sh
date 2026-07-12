#!/bin/sh
# node_test.sh — iStoreOS/OpenWrt 上直接测试 Mihomo 每个节点的连通性 (纯 shell, 无需 python/jq)
# 用法:
#   sh node_test.sh
#   sh node_test.sh --url http://connect.rom.miui.com/generate_204
#   sh node_test.sh --timeout 8000
#   sh node_test.sh --host 127.0.0.1 --port 9090

HOST=127.0.0.1
PORT=9090
TEST_URL="https://www.gstatic.com/generate_204"
TIMEOUT_MS=5000

# 解析简易参数
while [ $# -gt 0 ]; do
	case "$1" in
		--host) HOST="$2"; shift 2 ;;
		--port) PORT="$2"; shift 2 ;;
		--url) TEST_URL="$2"; shift 2 ;;
		--timeout) TIMEOUT_MS="$2"; shift 2 ;;
		*) echo "未知参数: $1" >&2; shift ;;
	esac
done

BASE="http://$HOST:$PORT"

# 与 helper.sh 一致的 UTF-8 urlencode (逐字节 %XX)。od 不可用时回退到纯 shell 实现。
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

# 1) 核心连通性
if ! curl -s -m 3 "$BASE/version" >/dev/null 2>&1; then
	echo "✗ 无法连接 Mihomo 控制器 $BASE"
	echo "  请确认核心已启动: /etc/init.d/mihomo restart"
	exit 2
fi

# 2) 核心实际加载的代理条目数 (含组)
core_count=$(curl -s -m 5 "$BASE/proxies" | grep -o '"name"' | wc -l | tr -d ' ')
echo "核心已连接。核心返回的代理条目数(含组): $core_count"

# 3) 从订阅文件取节点名 (复用 helper 的 get_proxies, 与仪表盘一致)
nodes_json=$(sh /usr/share/mihomo/helper.sh get_proxies 2>/dev/null)
if [ -z "$nodes_json" ] || [ "$nodes_json" = "[]" ]; then
	echo "✗ 订阅文件未解析出节点。请先更新订阅: sh /usr/share/mihomo/helper.sh update_subscription"
	exit 3
fi
printf '%s' "$nodes_json" | grep -o '"name":"[^"]*"' | sed 's/"name":"//; s/"$//' > /tmp/node_names.txt
total=$(wc -l < /tmp/node_names.txt | tr -d ' ')
echo "订阅文件解析节点数: $total"

url_enc=$(urlencode "$TEST_URL")
curl_max=$((TIMEOUT_MS / 1000 + 5))

ok=0
fail=0
ok_lines=""
fail_lines=""

echo ""
echo "==== 测试结果 ===="

while IFS= read -r name; do
	[ -z "$name" ] && continue
	enc=$(urlencode "$name")
	resp=$(curl -s -m "$curl_max" "$BASE/proxies/$enc/delay?url=$url_enc&timeout=$TIMEOUT_MS" 2>/dev/null)
	delay=$(printf '%s' "$resp" | sed -n 's/.*"delay"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p')
	msg=$(printf '%s' "$resp" | grep -o '"message"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:"//; s/"$//')
	if [ -n "$delay" ] && [ "$delay" -ge 0 ] 2>/dev/null; then
		ok=$((ok + 1))
		printf '  %-8s %s\n' "$delay ms" "$name"
		ok_lines="$ok_lines $name"
	else
		fail=$((fail + 1))
		reason=${msg:-超时/无响应}
		printf '  ✗ %s  ->  %s\n' "$name" "$reason"
		fail_lines="$fail_lines $name"
	fi
done < /tmp/node_names.txt

echo ""
echo "==== 统计 ===="
echo "  总: $total   成功: $ok   失败: $fail"

if [ "$fail" -gt 0 ]; then
	echo ""
	echo "提示: 若失败原因为 'Resource not found'，说明核心里没有这些名字的节点 —— 即核心未加载最新订阅。"
	echo "      执行以下两步后重跑本脚本:"
	echo "        sh /usr/share/mihomo/helper.sh update_subscription"
	echo "        /etc/init.d/mihomo restart"
fi
if [ "$core_count" -eq 0 ]; then
	echo ""
	echo "注意: 核心返回的代理条目数为 0，证明核心当前没有加载任何节点 (跑的是空/旧配置)。"
	echo "      必须先更新订阅并重启核心，测试才有意义。"
fi

rm -f /tmp/node_names.txt
