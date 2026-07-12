# Mihomo 访问日志 产品设计文档

> 状态：**已实现（v1.0.0-35 起随包发布）**
> 模块：`luci-app-mihomo` → 「访问日志」页面（`admin/services/mihomo/accesslog`）
> 适用：OpenWrt + LuCI，依赖 Mihomo（Clash Meta）核心经 TProxy / TUN 透明接管流量

---

## 1. 文档信息

| 项 | 内容 |
|---|---|
| 模块定位 | 与「运行状态」「服务设置」平级的第三个 LuCI 页面 |
| 入口路径 | LuCI 左侧菜单 → 服务 → Mihomo 代理 → **访问日志** |
| 核心能力 | ① 局域网设备实时连接可见性；② 历史访问记录落盘；③ 按域名配置代理/直连/拦截规则（UCI 持久化） |
| 实现载体 | `helper.sh` 新增 6 个子命令 + 常驻采集进程 + `accesslog.js` 视图 + 菜单节点 + `prepare_config` 规则注入 |
| 数据源 | Mihomo 外部控制器 `127.0.0.1:9090` 的 `GET /connections`；`/tmp/dhcp.leases` 设备名解析；UCI `mihomo` 规则段 |
| 变更生效 | 规则写入 UCI 后**需手动重启核心**（`/etc/init.d/mihomo restart`）生效，重启有 1~2s 短暂断流 |

---

## 2. 背景与目标

原插件只暴露「节点列表」「策略组切换」「核心管理」，缺乏对**流量去向**的可观测性，也无法针对具体网站做精细化放行/拦截。

本功能解决三件事：

1. **看见谁访问了什么**：罗列每个局域网设备的当前活跃连接（设备名/IP、域名、目标 IP、出口策略、流量、建立时间）。
2. **看见历史**：核心运行时周期性采集连接并落盘，形成可按时间回溯的访问历史。
3. **精细管控**：基于观察到的域名，一键生成「代理 / 直连 / 拦截」规则，持久化到 UCI；重启核心后对该域名**所有设备**统一生效。

---

## 3. 关键决策（已确认）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 日志形态 | **实时连接 + 历史日志落盘** 两者都要。历史由常驻采集进程每 15s 采集并写入 `/tmp/mihomo_access.log`。 |
| 2 | 管理粒度 | 按**域名**建规则（符合 Clash 规则模型），同时**记录来源 IP** 用于管理与溯源；规则对所有设备全局生效（见 §6 限制）。 |
| 3 | 代理默认出口 | 「代理」动作默认取订阅中**第一个 Selector 策略组**；用户也可在新增规则时指定具体组。 |
| 4 | 规则生效方式 | **保存即写 UCI，手动「应用并重启核心」后生效**（不自动重启，避免频繁断流）。 |
| 5 | 拦截语义 | 使用 `REJECT`（静默丢弃，表现为网站打不开）。 |
| 6 | 存储位置 | 规则持久存 **`/etc/config/mihomo`**（UCI，重启保留）；连接日志/历史暂存 **`/tmp`**（重启即丢，轻量、无需清理）。 |

---

## 4. 总体架构

```
┌─────────────┐         GET /connections          ┌──────────────────┐
│ 浏览器/LuCI  │ ───────────────────────────────►  │  Mihomo 核心      │
│ accesslog.js │ ◄───────────────────────────────  │  :9090 (9090)     │
└──────┬──────┘   精简 JSON (设备/域名/策略/...)    └──────────────────┘
       │ fs.exec('/usr/share/mihomo/helper.sh', [...])
       ▼
┌──────────────────────────────────────────────────────────────┐
│ helper.sh (新增子命令)                                          │
│  get_connections / collect_connections / get_history /          │
│  get_access_rules / add_access_rule / del_access_rule           │
│  + emit_access_rules_yaml (UCI → YAML 规则行)                   │
└───┬───────────────────┬───────────────────────┬───────────────┘
    │ 读/写             │ 采集(常驻)            │ 注入
    ▼                   ▼                       ▼
┌──────────┐   ┌────────────────────┐   ┌──────────────────────┐
│ UCI       │   │ /tmp/mihomo_        │   │ /tmp/mihomo_run.yaml │
│ mihomo     │   │ access.log (历史)   │   │ (prepare_config 注入  │
│ mihomo_rule│   │ /tmp/mihomo_access. │   │  用户规则到 rules: 顶 │
│           │   │ seen (去重集)        │   │  部, 重启核心后生效)  │
└──────────┘   └────────────────────┘   └──────────────────────┘
                        ▲
                        │ 每 15s (procd 常驻实例)
                  ┌─────┴─────────┐
                  │ init.d/mihomo │ (start 时拉起采集循环)
                  └───────────────┘
```

数据流：
1. 前端 `load()` 并发调用 `get_connections`、`get_history`、`get_access_rules`、`get_proxy_groups`，渲染三大区。
2. 前段 `setInterval` 每 5s 刷新实时连接与历史。
3. 用户在连接/历史行点「代理/直连/拦截」→ `add_access_rule <ip> <domain> <action> [group]` → 写 UCI `mihomo_rule` + commit。
4. 用户在「规则管理」区点「应用并重启核心」→ `fs.exec('/etc/init.d/mihomo', ['restart'])` → `prepare_config` 重新生成 `/tmp/mihomo_run.yaml`（含用户规则注入）→ 核心以新配置重启。
5. 核心运行期间，`init.d/mihomo` 的常驻采集实例每 15s 调 `collect_connections`，将新连接追加到 `/tmp/mihomo_access.log`。

---

## 5. 数据来源与技术约束

### 5.1 实时连接（核心数据源）
`curl -s http://127.0.0.1:9090/connections` 返回当前活跃连接数组，字段含 `metadata.sourceIP / destinationIP / host`、`rule`、`policy / chains`、`start`、`upload / download`、`id`。`helper.sh get_connections` 调此接口并压扁为前端友好的精简 JSON。

### 5.2 设备名解析
读取 dnsmasq 租约 `/tmp/dhcp.leases`（格式 `<过期秒> <mac> <ip> <hostname> <id>`），将 `sourceIP` 映射为 `hostname`（无租约时退化为 IP）。

### 5.3 硬约束（设计已正视）
- 连接数据**仅在核心运行且流量经透明代理（TProxy/TUN）时存在**；核心未启动或设备绕行时，本页显示空态提示。
- Mihomo **不支持通过 API 热更新规则**，规则变更需重启核心，存在短暂断流。
- `/connections` 仅返回**当前活跃连接**，非历史；历史必须自行采集落盘。
- Clash/Mihomo 规则是**全局**的，无法按源 IP 做作用域限定（见 §6 限制）。

---

## 6. 规则模型与重要限制（务必阅读）

### 6.1 规则语义 → Clash `rules:`
Mihomo 规则自上而下匹配、首中即止。用户规则被**插入到订阅 `rules:` 之前**以保证优先：

| 用户动作 | 注入的规则行 |
|---|---|
| 代理 | `DOMAIN-SUFFIX,<domain>,<策略组名>` |
| 直连 | `DOMAIN-SUFFIX,<domain>,DIRECT` |
| 拦截 | `DOMAIN-SUFFIX,<domain>,REJECT` |

- 默认 `DOMAIN-SUFFIX`（后缀匹配）；用户填写的 `domain` 原样作为后缀。
- 「代理」动作当指定 `group` 时生成 `DOMAIN-SUFFIX,<domain>,<group>`；未指定则不带 group（前端默认取首个 Selector 组，但注入时仅在有 group 才写，避免引用不存在的组名）。

### 6.2 ⚠️ 按 IP 建规则实为「按域名全局生效」限制
Clash/Mihomo **不支持按源 IP 限定某条规则的作用域**。因此：
- 用户在界面上基于某设备的某条连接点「拦截」时，系统**记录该来源 IP**（存入 UCI `src_ip` 字段，仅供管理与溯源展示），但**实际生成的规则对该域名的所有设备统一生效**。
- 即：界面展示「来源 IP = 192.168.1.50 → example.com → 拦截」，但生效效果是「所有设备访问 example.com 均被拦截」。
- 这是 Clash 规则引擎的固有限制，非实现缺陷。文档与界面均在「规则管理」区明确标注此行为，避免误解。
- 后续若需真正设备级策略，需依赖 Mihomo 的 `process-name` / `source-ip` 等规则类型或脚本外挂，本期不做。

### 6.3 UCI 数据模型
```uci
# /etc/config/mihomo
config mihomo_rule
    option src_ip  '192.168.1.50'   # 来源 IP（仅记录/溯源，不影响生效范围）
    option domain  'example.com'    # 必填，按后缀匹配
    option action  'block'          # proxy | direct | block
    option group   'Proxy'          # action=proxy 时的目标组（可缺省）
    option enabled '1'              # 0 = 禁用（保留但不生效）
    option comment '可选备注'
```
- 读取：`uci show mihomo`（筛 `=mihomo_rule` 段）。
- 新增：`uci add mihomo mihomo_rule` + 各 `uci set` + `uci commit mihomo`。
- 删除：`uci delete mihomo.<sid>` + commit。

---

## 7. 后端设计（`helper.sh` 子命令规格）

所有子命令经 `case "$1" in` 分发；前端统一通过 LuCI RPC `fs.exec('/usr/share/mihomo/helper.sh', [<cmd>, ...])` 调用（ACL 已放行 `exec`）。

### 7.1 `get_connections`
- 行为：`curl -s --connect-timeout 2 http://127.0.0.1:9090/connections`；核心不可达时返回 `{"error":"no_core","msg":"..."}`。
- 内部：经 `jsonfilter` 拆分各字段 → `flatten_connections` 逐条解析（含 `resolve_host` 设备名解析）→ 输出 JSON 数组。
- 输出：`[{"id","ip","device","domain","dst","policy","rule","up","down","start"}, ...]`
  - `domain` 优先取 `metadata.host`，缺失时退化为 `destinationIP`。
  - `up`/`down` 缺省补 `0`。

### 7.2 `collect_connections`
- 行为：同 `get_connections` 取实时连接，逐条去重（用 `/tmp/mihomo_access.seen` 记录已采集的连接 `id`），新连接以 `{"ts":<unix秒>,"id","ip","device","domain","dst","policy","rule","up","down","start"}` 追加到 `/tmp/mihomo_access.log`。
- 去重集超过 2000 行时滚动裁剪，避免无限增长。
- 核心不可达时直接返回（无操作）。
- 由 `init.d/mihomo` 启动时的常驻 procd 实例每 15s 调用一次。

### 7.3 `get_history [limit]`
- 参数：`limit` 默认 200；读取 `/tmp/mihomo_access.log` 末 `limit` 行并逆序（新→旧）输出 JSON 数组。
- 文件不存在时返回 `[]`。

### 7.4 `get_access_rules`
- 行为：遍历 UCI `mihomo_rule` 段，输出 JSON 数组。
- 输出：`[{"sid","ip","domain","action","group","enabled","comment"}, ...]`
  - `sid` 为 UCI 段标识（`@mihomo_rule[N]` 形式），供 `del_access_rule` 使用。

### 7.5 `add_access_rule <ip> <domain> <action> [group]`
- 参数：`ip`（可空）、`domain`（必填，空则报错）、`action`（默认 `block`，可取 `proxy/direct/block`）、`group`（action=proxy 时可选）。
- 行为：`uci add mihomo mihomo_rule` → set 各字段 → `enabled=1` → `uci commit mihomo` → `logger`。
- 输出：`OK`（或 stderr 报错）。**不自动重启核心**。

### 7.6 `del_access_rule <sid>`
- 参数：`sid`（UCI 段标识）。
- 行为：`uci delete mihomo.<sid>` → commit → logger。输出 `OK`。**不自动重启核心**。

### 7.7 规则注入：`emit_access_rules_yaml` + `prepare_config`
- `emit_access_rules_yaml`：遍历启用中的 `mihomo_rule`，按 §6.1 输出 `  - 'DOMAIN-SUFFIX,<domain>,<REJECT|DIRECT|<group>'` 形式的 YAML 规则行（每行一条）。
- `prepare_config`（重启核心时调用）在生成 `/tmp/mihomo_run.yaml` 后，将 `emit_access_rules_yaml` 的结果：
  - 若订阅已有顶层 `rules:`：在 `rules:` 行之后、`awk` 插入用户规则（首中即止，保证优先）；
  - 若无 `rules:`：在文件末尾追加 `rules:` 块。
  - 使用临时文件 + `awk` 读取规则文件（避免多行值经 `awk -v` 传参的换行限制）。

> 实现注意事项（已踩坑并固化）：
> - Python 源中反斜杠转义需成对：`\"` 写 `\\\"`，`\1` 写 `\\1`，`\n`（printf/awk 字面反斜杠n）写 `\\n`；否则部署后 bash 双引号被提前闭合或正则失效。
> - `case` 分支的 `word)` 标签中的 `)` 会提前闭合 `$(...)` 命令替换，**不可将含 `case` 的逻辑包进 `echo "$(...)"`**，改为函数直接 `echo`。
> - `uci get` 的段标识含 `[N]`，需双引号包裹避免被 shell 文件名通配展开。

---

## 8. 前端设计（`accesslog.js`）

沿用 `dashboard.js` 的 `view.extend` + `fs.exec` + `uci` 模式，页面分三大区 + 新增规则表单。

### 8.1 菜单（`menu.d/luci-app-mihomo.json`）
```json
"admin/services/mihomo/accesslog": {
    "title": "访问日志",
    "order": 3,
    "action": { "type": "view", "path": "mihomo/accesslog" }
}
```

### 8.2 页面结构（三大区）
1. **实时连接区**（每 5s 自动刷新）
   - 表格列：设备 / 域名·目标 / 策略 / 流量↑↓ / 操作（代理·直连·拦截）。
   - 服务未运行或核心不可达时显示提示。
   - 操作按钮调用 `add_access_rule <ip> <domain> <action>`（代理默认取首个 Selector 组）。
2. **历史访问记录区**
   - 表格列：时间 / 设备 / 域名·目标 / 策略 / 操作（拦截·直连）。
   - 数据来自 `get_history 300`（读 `/tmp/mihomo_access.log`）。
3. **访问规则管理区**
   - 表格列：来源 IP / 域名 / 动作 / 备注 / 状态 / 操作（删除）。
   - 明确标注「规则按域名对所有设备全局生效；来源 IP 仅用于溯源」。
   - 行内删除调用 `del_access_rule <sid>`。

### 8.3 新增规则表单
- 字段：域名（必填）、来源 IP（选填）、动作（拦截/直连/走代理下拉）、代理组（选填，仅走代理时有效）、备注。
- 「添加规则」→ `add_access_rule` 写 UCI → 刷新规则表（不重启）。
- 「应用并重启核心」→ `fs.exec('/etc/init.d/mihomo', ['restart'])` → 重启后规则生效，1.5s 后自动刷新页面。

### 8.4 自动刷新与清理
- `render()` 内 `setInterval` 每 5s 重新拉取 `get_connections` 与 `get_history` 并更新表格容器（保留滚动位置）。
- `view` 定义 `unload()`，离开页面时 `clearInterval` 停止轮询。

---

## 9. 历史采集机制（常驻进程）

`/etc/init.d/mihomo` 的 `start_service` 在拉起核心 procd 实例之外，额外拉起一个 procd 实例运行：
```
/bin/sh -c "/usr/share/mihomo/helper.sh collect_connections; while true; do /usr/share/mihomo/helper.sh collect_connections; sleep 15; done"
```
- `respawn` 保证进程异常退出自动重启。
- 核心未启动时 `collect_connections` 静默无操作；核心启动后自动开始采集。
- 采集结果落盘 `/tmp/mihomo_access.log`（重启即丢，符合轻量设计）；去重集 `/tmp/mihomo_access.seen`。

---

## 10. 权限与 ACL

`root/usr/share/rpcd/acl.d/luci-app-mihomo.json` 已覆盖所需权限，无需新增：
- `read.uci: ["mihomo"]`、`write.uci: ["mihomo"]`：规则读写。
- `write.file.exec` 含 `/usr/share/mihomo/helper.sh`（新子命令均经此执行）、`/etc/init.d/mihomo`（重启核心）、`/sbin/logread`。

---

## 11. 构建与部署

- 所有交付文件内嵌于 `build_ipk.py` 的 `src_files` 字典；**不要手改 `src/`**（构建会先删后建）。
- `python build_ipk.py` 产出 `dist/luci-app-mihomo_<version>_all.ipk`；`PKG_VERSION` 每次构建自动递增（属预期 diff）。
- 反斜杠转义约定见 §7.7 注意事项。
- JSON 缩进：菜单 JSON 用 4 空格风格，本模块沿用该文件既有风格。

---

## 12. 验证情况（开发期自测）

- `sh -n` 校验 `helper.sh` 语法通过。
- `emit_access_rules_yaml` 在 mock `uci` 下输出正确 YAML 规则行（`REJECT`/`DIRECT`/`组名`）。
- `get_access_rules` 输出合法 JSON 数组（含 `sid`/`domain`/`action` 等）。
- `prepare_config` 规则注入在隔离测试中验证：用户规则被插入到订阅 `rules:` 顶部（首中即止）。
- `accesslog.js` 经 `node --check` 语法校验通过。
- 已知 macOS 本地 `sed -i` 与 OpenWrt（busybox/GNU sed）行为差异属环境差异，不影响目标平台。

---

## 13. 已知限制与后续规划

### 已知限制
1. **规则全局生效**：按域名对所有设备生效，无法限定单设备（Clash 引擎限制，见 §6.2）。
2. **需手动重启**：规则保存后须点「应用并重启核心」才生效，有短暂断流。
3. **设备可见性依赖透明代理**：绕行核心的设备/IP 不可见。
4. **历史存 /tmp**：路由器重启即丢失，不做持久化（轻量设计取舍）。
5. **REJECT 表现**：静默丢弃，部分客户端表现为「加载中转圈」，非即时拒绝页。
6. **隐私**：连接/历史含浏览痕迹，存于路由器本地 `/tmp`；界面未提供「清空日志」入口（后续可加）。

### 后续规划（P4 及以后）
- 历史日志持久化到 `work_dir` 并提供「清空」。
- 规则导入/导出。
- 按设备维度的策略（依赖 Mihomo `source-ip` 规则类型或外挂脚本）。
- 拦截返回拒绝页（如 `REJECT` 之外可选 `HTTP` 拦截）。
- 连接级实时阻断（需核心支持，本期无）。

---

*本文档为已实现的权威设计说明，覆盖目标、架构、后端规格、前端结构、规则模型与关键限制、采集机制、权限、构建与验证。*
