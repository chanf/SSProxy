# luci-app-mihomo 节点延时测试调试全记录

## 项目背景

luci-app-mihomo 是 OpenWrt/iStoreOS 上的 LuCI 应用，集成了 Mihomo (Clash Meta) 透明代理客户端。节点列表提供实时延时测试功能——点击「测试」按钮后，前端逐个调用后端 `helper.sh test_node_delay`，通过 Mihomo 外部控制器 API (`http://127.0.0.1:9090/proxies/<name>/delay`) 获取每个节点的网络延迟。

本文记录了从「测试全部失败」到「全部通过」的完整调试过程，涉及 7 类不同根因，横跨 shell 编码、LuCI 前端架构、BusyBox 兼容性、订阅数据解析等多个层面。

---

## 问题时间线

### 第一阶段：`urlencode` 编码错误（v58）

**现象**：所有节点延时测试显示 `失败:Resource not found`。

**根因**：`urlencode` 函数使用 `od -An -v -tx1 | tr -d ' \n' | sed 's/../%&/g'` 将 UTF-8 字节转换为 `%XX` 形式。在某些 BusyBox 构建中，`od` 的行为不一致，导致多字节 UTF-8 字符（如中文节点名「美国 205」）被错误编码为 `%FFFF...`（无效字节）。Mihomo 控制器收到无效编码的名称后找不到对应节点，返回 `Resource not found`。

**验证**：通过在路由器上直接运行 `sh /usr/share/mihomo/helper.sh urlencode "美国 205"` 对比本地编码结果，确认编码值不同。

**修复**：确认 `od` + `tr` + `sed` 管道在目标 BusyBox 上可用且行为正确。编码后的值与 Python `urllib.parse.quote` 结果一致（`%e7%be%8e%e5%9b%bd%20%32%30%35`）。

---

### 第二阶段：`get_proxies` 引号未剥离（v61）

**现象**：延时测试仍然 `失败:Resource not found`。

**根因**：`get_proxies` 使用 awk 解析订阅 YAML，提取代理节点名。当 YAML 中的 `name` 值被引号包裹时（如 `name: "美国 205"` 或 `name: '美国 205'`），awk 的 `stripq()` 函数未正确去除引号，导致传给控制器的名称变成 `"美国 205"`（含引号），自然找不到。

**修复**：在 awk 的 `getf()` 和字段解析中加入 `stripq()` 函数，去除首尾单/双引号。

---

### 第三阶段：CRLF 行尾符污染（v67）

**现象**：部分订阅源解析出的节点名末尾带有 `\r`，导致 `Resource not found`。

**根因**：某些 Clash 订阅源使用 Windows 风格的 CRLF (`\r\n`) 行尾。`get_proxies` 的 awk 解析未去除 `\r`，导致名称变为 `美国 205\r`。在 curl 请求中，`\r` 被编码为 `%0D`，控制器无法识别。

**修复**：在 `get_proxies` 和 `test_node_delay` 中添加 `tr -d '\r'`，统一去除 CRLF。

---

### 第四阶段：`od` 不存在导致编码完全失效（v73）

**现象**：安装 ipk 后，仪表盘「测试」按钮点击后全部显示 `测试中...`，很久后变成 `超时/失败`。

**根因**：用户的 iStoreOS BusyBox 构建**未包含 `od` 应用**（`od: not found`）。这导致 `urlencode` 函数完全失效——返回空字符串。所有延时测试请求变成 `/proxies//delay`（名称为空），返回 `Resource not found`。

**验证**：用户通过 `node_test.sh` 脚本测试，发现节点全部超时。排查 `od` 缺失后，编写了不依赖 `od` 的纯 shell 实现。

**修复**：`urlencode` 增加兼容逻辑：优先使用 `od`，不可用时回退到纯 shell 实现（通过 `${var%"${var#?}"}` 逐字节提取 + `printf '%d' "'c"` 获取字节值 + `printf '%02X'` 生成十六进制）。

```sh
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
            *) val=$(printf '%d' "'$c" 2>/dev/null)
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
```

---

### 第五阶段：30 个并发 `fs.exec` 调用导致超时（v75-76）

**现象**：`node_test.sh` 命令行测试全部通过（30/30 成功，延时 76-417ms），但仪表盘点击「测试」仍然 `超时/失败`。

**根因**：仪表盘的 `run_delay_test()` 函数对每个节点**分别发起**一次 `fs.exec('/usr/share/mihomo/helper.sh', ['test_node_delay', p.name'])`。30 个节点 → 30 个**并发** `fs.exec` 调用。

`fs.exec` 通过 LuCI 的 rpcd → ubus → HTTP 链路执行命令。当同时发起 30 个调用时，rpcd 的 `file exec` 插件无法可靠地处理如此多的并发进程，导致所有调用超时。

**关键对比**：

| 方式 | 行为 | 结果 |
|------|------|------|
| `node_test.sh`（SSH 执行） | 30 个 curl **串行**执行 | 全部成功 |
| 仪表盘 `fs.exec`（30 个并发） | 30 个进程**同时**启动 | 全部超时 |

**修复**：新增 `test_all_nodes` 后端命令，在**一个** `fs.exec` 调用中完成所有测试：
- 从 `get_proxies` 获取节点名列表
- 后台并行执行所有 curl 测试（`( ... ) &` + `wait`）
- 将结果组装为 JSON 数组返回
- 仪表盘一次性获取所有结果，按索引映射到对应节点卡片

```sh
# 后台并行测试
while IFS= read -r name; do
    i=$((i + 1))
    (
        resp=$(curl -s -m 10 "http://127.0.0.1:9090/proxies/${enc}/delay?...")
        # 提取结果写入临时文件
        printf '{"delay":%s}' "$delay" > "$tmpd/$i"
    ) &
done < "$tmpd/names"
wait  # 等待全部完成
# 按顺序组装 JSON 数组
```

---

### 第六阶段：`sed` 分组提取在 BusyBox 子 shell 中失效（v76）

**现象**：`test_all_nodes` 在本地 macOS 测试正常，但在路由器上返回 `-1`（超时）。

**根因**：延时提取使用 `sed -n 's/.*\([0-9]*\).*/\1/p'` 的分组反向引用（`\1`）。在某些 BusyBox sed 变体中，当 sed 在后台子 shell `( ... ) &` 中执行时，`\(` 分组无法正确捕获，反向引用 `\1` 返回 Control-A 字符。

**验证**：通过 `set -x` 追踪发现 `delay=$'\001'`（Control-A），而非预期的数字。

**修复**：改用**无分组**的 `grep -o` 提取：
```sh
# 之前（fragile）
delay=$(printf '%s' "$resp" | sed -n 's/.*"delay"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p')

# 之后（portable）
delay=$(printf '%s' "$resp" | grep -o '"delay"[[:space:]]*:[[:space:]]*[0-9]*' | grep -o '[0-9]*$')
```

---

## 附录：其他已知问题与修复

### `cron` 依赖导致安装失败（v65）

**问题**：自动更新订阅功能依赖 `cron` 包，但 iStoreOS 默认未安装 `cron`，导致 `opkg install` 报错。

**修复**：改用 procd 实例（`auto_update_loop`）替代 cron，每 10 分钟轮询检查是否需要更新。

### 订阅链接跨重装丢失（v67）

**问题**：`opkg remove/install` 会删除 `/etc/config/mihomo`（conffile），导致订阅链接丢失。

**修复**：将订阅链接持久化到包外部文件 `/etc/mihomo/.subscription_url`，在更新成功后写入，启动时读取恢复。

### 核心未加载最新订阅（v69）

**问题**：手动「立即更新订阅」后，订阅文件已更新，但核心仍在运行旧配置（无节点），导致所有延时测试 `Resource not found`。

**修复**：`update_subscription` 成功后自动检测核心是否运行，若运行则重启核心使其加载新配置。

---

## 调试方法论

### 1. 命令行工具先行

在 OpenWrt/LuCI 环境下调试，首先创建独立的命令行工具（`node_test.sh`），绕过 LuCI 框架直接与后端交互。这能快速定位问题是前端还是后端：

```bash
# 在路由器上直接测试
ssh root@router 'sh /usr/share/mihomo/helper.sh test_node_delay "美国 205"'
# 输出: {"delay":244}
```

### 2. 逐层排除法

从最外层（LuCI JS 前端）到最内层（Mihomo 控制器 API），逐层验证：
- LuCI `fs.exec` → helper.sh → curl → controller
- 每一层都可能引入新问题（编码、并发、超时等）

### 3. BusyBox 兼容性

OpenWrt 的 BusyBox 是精简版，许多标准工具（`od`、`hexdump`、`jq`）不可用。所有脚本必须：
- 仅依赖 POSIX shell 语法
- 避免 bashisms（如 `[[ ]]`、数组、`let`）
- 使用 `command -v` 检测可用工具，提供 fallback

### 4. 并发 vs 串行

`fs.exec` 的并发数有限。当需要测试多个节点时，**后端串行/有限并发 + 单次 fs.exec** 远优于 **前端多次并发 fs.exec**。

---

## 版本演进

| 版本 | 修复内容 | 关键发现 |
|------|----------|----------|
| v58 | `urlencode` UTF-8 编码 | `od` 输出需要逐字节 `%XX` |
| v61 | `get_proxies` 去引号 | YAML 引号被保留到控制器 |
| v63 | 清空节点功能 | — |
| v65 | procd 定时更新（替代 cron） | iStoreOS 无 cron |
| v67 | CRLF 处理 + 订阅链接持久化 | 部分订阅源用 CRLF |
| v69 | 更新订阅后自动重启核心 | 核心加载旧配置 |
| v70 | 诊断日志 | 辅助排查 |
| v73 | `urlencode` 无 `od` fallback | BusyBox 可能无 od |
| v76 | `test_all_nodes` 单次调用 + grep 提取 | 30 并发 fs.exec 全超时 |

---

## 结论

一个看似简单的「节点延时测试」功能，在实际部署环境中可能涉及 7 类完全不同的根因。调试的关键在于：

1. **创建独立测试工具**，绕过复杂框架（LuCI）直接验证后端
2. **逐层定位**，从 UI → fs.exec → helper.sh → curl → controller
3. **注意 BusyBox 兼容性**，所有工具都可能是精简版
4. **理解框架限制**，`fs.exec` 的并发能力远低于 SSH 直接执行
5. **在真实环境测试**，macOS/Ubuntu 上正常的代码在 BusyBox 上可能失败
