# 访问日志 功能设计文档

> 状态：**设计稿（待确认）**。确认后再进入开发。
> 目标：在「运行状态」「服务设置」同级新增「访问日志」页面，提供
> ① 局域网设备访问链接的可见性；② 按域名对每条链接进行「走代理 / 直连 / 拦截」的策略管理。

---

## 1. 背景与目标

当前插件只暴露「节点列表」「策略组切换」与核心管理，缺乏对**流量去向**的可观测性，也无法针对具体网站做精细化放行/拦截。

本功能要解决两件事：

1. **罗列每个局域网设备的访问链接** —— 谁（设备/IP）访问了哪些域名、走了哪个出口（代理/直连）。
2. **管理每条链接是否走代理还是被拦截** —— 基于观察到的域名，一键生成「代理 / 直连 / 拦截」规则，并即时生效。

---

## 2. 现状与约束（基于现有架构，避免凭空设计）

| 现有能力 | 对本案的利用 |
|---|---|
| Mihomo 外部控制器 `127.0.0.1:9090`（`external-controller: 0.0.0.0:9090`，由 `prepare_config` 注入） | 直接复用 `GET /connections` 获取实时连接；复用 `GET /proxies` 获取策略组名（目标代理出口） |
| TProxy 透明代理（nftables 表 `inet mihomo`，fwmark 1，路由表 100） | 只有被 TProxy 接管的流量才会出现在 Mihomo 连接里；绕行设备不可见 |
| `helper.sh` 子命令 + LuCI RPC（`fs.exec` 调 `/usr/share/mihomo/helper.sh`，ACL 已放行） | 新增子命令即可，无需改 ACL |
| `prepare_config` 生成运行配置 `/tmp/mihomo_run.yaml` | 用户规则在此注入，再重启核心生效 |
| UCI 配置 `mihomo` + LuCI `form.Map` | 规则存 UCI，前端用 `form.Map` 管理 |
| 菜单 `admin/services/mihomo/{dashboard,settings}` | 新增平级节点 `accesslog` |

**硬约束（必须在设计里正视）：**

- 连接数据**仅在 Mihomo 核心运行且流量经 TProxy 时存在**。核心未启动 / TUN 模式 / 设备绕行时，本页应显示「服务未运行」空态。
- Mihomo **不支持通过 API 热更新规则**，规则变更需重启核心（`/etc/init.d/mihomo restart`），会有短暂断流。
- `/connections` 只返回**当前活跃连接**，不是历史。要做「日志/历史」必须自行采集与存储。

---

## 3. 功能范围

### 3.1 访问日志（连接列表）
- 表格/卡片展示当前活跃连接，字段：
  - 设备（hostname/IP/MAC，优先显示 dnsmasq 租约名）
  - 目标域名（`metadata.host`，无 host 时退化为 `destinationIP`）
  - 目标 IP:端口
  - 当前出口（`policy`：代理组名 / `DIRECT`）
  - 匹配规则（`rule`，如 `DOMAIN-SUFFIX,example.com`）
  - 上下行流量、建立时间
- 前端按设备分组、可按域名搜索、自动轮询刷新（默认 5s）。

### 3.2 代理/拦截管理
- 每条连接提供快捷操作：**代理 / 直连 / 拦截**。
  - 代理：将该域名路由到指定策略组（默认主选择器，如订阅里的 `Proxy`/`🔰 节点选择`）。
  - 直连：路由到 `DIRECT`。
  - 拦截：`REJECT`（丢弃，达到「网站打不开」效果）。
- 点击后写入 UCI 规则并**重启核心**使规则生效；成功后该域名后续连接即按新策略走。
- 独立「规则管理」区：列出全部已建规则（域名 + 动作 + 备注），支持删除/临时禁用。

### 3.3 不在本期范围（建议后续）
- 按**设备/IP** 维度的批量策略（本期聚焦按域名）。
- 历史日志落盘与按时间检索（见 §9 与 §10 决策点）。
- 连接级实时阻断（Mihomo 无此能力，只能按域名规则）。

---

## 4. 数据来源与技术选型

### 4.1 实时连接（核心数据源）
`curl -s http://127.0.0.1:9090/connections` 返回：
```json
{
  "connections": [
    {
      "id": "ab12...",
      "metadata": {
        "network": "tcp", "type": "HTTPS",
        "sourceIP": "192.168.1.50",
        "destinationIP": "1.2.3.4",
        "sourcePort": "54321", "destinationPort": "443",
        "host": "www.example.com"
      },
      "rule": "DOMAIN-SUFFIX,www.example.com",
      "chains": ["Proxy"], "policy": "Proxy",
      "start": "2026-07-12T10:00:00Z",
      "upload": 120, "download": 480
    }
  ]
}
```
`helper.sh get_connections` 调此接口，压扁为前端友好的精简 JSON：
`[{id, device, ip, mac, domain, dst, policy, rule, up, down, start}]`。

### 4.2 设备名解析
读取 dnsmasq 租约 `/tmp/dhcp.leases`（格式 `<过期秒> <mac> <ip> <hostname> <id>`），
将 `sourceIP` 映射为「hostname(IP)」。无租约时退化为 IP；MAC 可一并展示。
（注：LuCI 本身有 `luci-app-firewall` 等也用此文件，可靠性高。）

### 4.3 规则语义 → Clash `rules:`
Mihomo 规则自上而下匹配、首中即止。用户规则应**插到订阅规则之前**以保证优先：

| 用户动作 | 注入的规则行 |
|---|---|
| 代理 | `DOMAIN-SUFFIX,<domain>,<主策略组名>` |
| 直连 | `DOMAIN-SUFFIX,<domain>,DIRECT` |
| 拦截 | `DOMAIN-SUFFIX,<domain>,REJECT` |

- 顶层域名用 `DOMAIN-SUFFIX`；如需精确匹配可用 `DOMAIN`。本期默认 `DOMAIN-SUFFIX`。
- 主策略组名从 `GET /proxies` 的第一个 Selector 组取（或固定读 UCI 指定）。

---

## 5. 数据模型（UCI）

在现有 `mihomo` 配置旁新增规则段（建议独立段类型，便于 list/删除）：

```uci
# /etc/config/mihomo
config mihomo_rule
    option domain 'example.com'
    option action 'block'      # proxy | direct | block
    option group  'Proxy'      # action=proxy 时的目标组（可缺省）
    option enabled '1'
    option comment '可选备注'
```

- 读取：`uci show mihomo.@mihomo_rule[]`
- 新增：`uci add mihomo mihomo_rule` + `uci set ...` + `uci commit mihomo`
- 删除：`uci delete mihomo.<sid>` + commit

---

## 6. 后端设计（`helper.sh` 新增子命令）

| 子命令 | 作用 |
|---|---|
| `get_connections` | `curl 9090/connections`，解析为精简 JSON（含设备名解析） |
| `get_access_rules` | 读 UCI `mihomo_rule` → JSON 数组 |
| `add_access_rule <domain> <action> [group]` | 写 UCI + `apply_access_rules` |
| `del_access_rule <sid>` | 删 UCI + `apply_access_rules` |
| `apply_access_rules` | 把 UCI 规则转成 `rules:` 块，**插入** `/tmp/mihomo_run.yaml` 顶部；重启 `/etc/init.d/mihomo` 使生效 |

`apply_access_rules` 实现要点（复用 `prepare_config` 思路）：
1. 在正常 `prepare_config` 生成的运行配置基础上，在订阅自带 `rules:` **之前**插入用户 `rules:` 块；
   - 若订阅无 `rules:`，直接在 `dns:` 前追加。
2. 重启核心：`/etc/init.d/mihomo restart`（依赖 `procd`，会先停后起）。
3. 全程 `logger -t mihomo` 记录，便于排查。

> 说明：规则的「插入位置」是关键。Mihomo/Clash 规则首中即止，因此用户规则必须排在订阅规则之前，
> 否则会被订阅默认规则（如 `GEOIP,LAN,DIRECT` 或兜底 `MATCH,Proxy`）抢先匹配。

---

## 7. 前端设计（LuCI 视图与菜单）

### 7.1 菜单（`root/usr/share/luci/menu.d/luci-app-mihomo.json`）
新增平级节点：
```json
"admin/services/mihomo/accesslog": {
    "title": "访问日志",
    "order": 3,
    "action": { "type": "view", "path": "mihomo/accesslog" }
}
```

### 7.2 新视图 `root/www/luci-static/resources/view/mihomo/accesslog.js`
沿用 `dashboard.js` 的 `view.extend` + `fs.exec` + `uci` 模式，页面分两块：

1. **连接监控区**
   - `load()` 调 `get_connections` + `get_access_rules` + `get_proxy_groups`。
   - 表格列：设备 / 域名 / 目标 / 出口 / 规则 / 流量 / 操作（代理·直连·拦截）。
   - 自动刷新：`setInterval` 每 5s 重新 `get_connections`（离开页面 `clearInterval`）。
   - 服务未运行时显示空态提示。

2. **规则管理区**
   - `form.Map` 或手写表格列出 `mihomo_rule`，每行：域名、动作（下拉）、目标组、启用、删除。
   - 变更后 `uci.set/commit` + 调 `apply_access_rules`（或复用 `form.Map` 的保存即触发重启）。

### 7.3 交互闭环
用户在某条连接上点「拦截」→ `add_access_rule <domain> block` → 后端写 UCI 并重启核心
→ 该域名后续新建连接被 `REJECT`。列表里该域名的新连接「出口」列即显示 `REJECT`/拦截态。

---

## 8. 交互流程（典型路径）

```
打开「访问日志」
  └─ 服务未运行？→ 显示提示，引导去「运行状态」启动
  └─ 运行中 → get_connections 拉取实时连接，按设备分组展示
       └─ 发现某设备访问 example.com
            └─ 点「拦截」
                 └─ add_access_rule example.com block
                 └─ apply_access_rules：写入运行配置 + 重启核心
                 └─ 该域名新连接被 REJECT（老连接仍在，结束即失效）
```

---

## 9. 技术风险与限制

1. **实时连接 ≠ 历史日志**：`/connections` 只给当下活跃连接。要「日志/历史」必须后台轮询采集并落盘
   （新增常驻进程 / procd 服务 / cron），带来复杂度与存储增长。
2. **规则变更需重启核心**：会有 1~2s 断流；频繁改规则体验略顿。
3. **设备可见性依赖 TProxy**：TUN 模式下连接也可见（核心仍处理），但**绕行核心的设备/IP 不可见**。
4. **域名粒度**：Clash 规则按域名生效，不是按「某一次具体连接」。因此「管理每条链接」本质是「管理该域名」，
   同一域名的所有连接统一策略。
5. **`REJECT` 行为**：Clash `REJECT` 直接丢弃（部分客户端表现为「加载中转圈」），如需即时拒绝可用 `REJECT`；
   若想返回拒绝页可后续扩展，本期从简。
6. **隐私**：连接日志含浏览历史，存于路由器本地；需提供「清空日志/规则」入口。

---

## 10. 待确认 / 决策点（请评审时拍板）

1. **日志形态**：仅「实时连接监控」（简单、零常驻进程）✅ 推荐先期；
   还是也要「历史日志落盘」（需后台采集进程，复杂度↑）？
2. **管理粒度**：按**域名**建规则（推荐，符合 Clash 模型）是否可接受？还是要求按设备/IP？
3. **代理出口默认值**：「代理」动作默认用哪个策略组？（建议取订阅第一个 Selector 组；或固定 `Proxy`）
4. **规则生效方式**：改规则即自动重启核心（简单）✅，还是先「草稿」再手动「应用」？
5. **拦截语义**：用 `REJECT`（静默丢弃）是否满足？是否需要「仅对该设备拦截」的设备级扩展（本期不做）？
6. **历史/规则的存储位置**：存 `/tmp`（重启即丢，轻量）还是 `work_dir`（持久但需清理）？

---

## 11. 实施阶段建议（确认后）

- **P1 实时连接监控**：`get_connections` + `accesslog.js` 连接列表 + 设备名解析 + 自动刷新。
- **P2 规则管理**：UCI `mihomo_rule` + `add/del/apply_access_rules` + 前端规则表 + 连接行快捷操作。
- **P3（可选）历史日志**：后台采集进程 + 落盘 + 检索/清空。
- **P4（可选）**：规则导入/导出、按设备维度策略、拦截返回页。

---

*本设计为待确认稿，所有「待确认/决策点」答复后进入具体编码。*
