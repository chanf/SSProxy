# 水杉代理 订阅配置与分流规则说明

本文以一份**范例** Mihomo（Clash Meta）订阅配置为蓝本，抽象出其配置结构、规则语法、匹配顺序与分流组织策略。文中服务器地址、UUID 等均为占位符，不代表真实节点。目的是让维护者快速理解订阅文件各段含义，以及本插件如何接管/合并它。

> 相关运行机制见 [data-flow-design.md](data-flow-design.md)；本插件对配置的合并逻辑见 [build_ipk.py](../build_ipk.py) 中的 `prepare_config` / `emit_access_rules_yaml`。

---

## 1. 配置文件顶层结构

订阅配置是一个 YAML，顶层由若干固定段组成：

| 段 | 作用 | 本插件是否接管 |
| :--- | :--- | :--- |
| 通用参数（`mixed-port` 等） | 监听端口、运行模式、外部控制器 | **接管**：端口/控制器被 `prepare_config` 重写 |
| `dns:` | DNS 解析策略（fake-ip / 上游 / 兜底） | **接管**：整段被删除后由插件重写 |
| `proxies:` | 节点定义（来自订阅，通常不改） | 保留原样 |
| `proxy-groups:` | 策略组（手动选择 / 自动测速 / 故障转移） | 保留原样 |
| `rules:` | 分流规则（核心） | **部分接管**：插件 UCI 规则注入到段首，其余保留 |
| `tun:` | TUN 虚拟网卡（订阅一般不带） | **接管**：由插件按 `tun_enabled` 重写 |

### 1.1 通用参数（范例）

```yaml
mixed-port: 7890        # HTTP+SOCKS 混合端口
allow-lan: true         # 允许局域网连接
bind-address: '*'       # 监听地址，* 表示全部
mode: rule              # rule / global / direct
log-level: info         # debug/info/warning/error/silent
external-controller: '127.0.0.1:9090'   # 外部控制器 API
```

> ⚠️ 这些值在运行时会被 `prepare_config` 覆盖：`mixed-port`/`tproxy-port`/`allow-lan`/`external-controller` 被前置重写为插件 UCI 设定（控制器固定 `0.0.0.0:9090`，TProxy 端口默认 `7893`）。因此订阅里的端口写什么不影响实际监听。

---

## 2. DNS 配置

```yaml
dns:
    enable: true
    ipv6: false
    default-nameserver: [223.5.5.5, 119.29.29.29]   # 引导解析器：用于解析下面 DoH/DoT 主机名本身
    enhanced-mode: fake-ip                          # fake-ip 模式：返回假 IP，内核再反查域名
    fake-ip-range: 198.18.0.1/16                    # 假 IP 地址池
    use-hosts: true
    nameserver: ['https://doh.pub/dns-query', 'https://dns.alidns.com/dns-query']        # 主解析器（国内 DoH）
    fallback: ['https://doh.dns.sb/dns-query', 'https://dns.cloudflare.com/dns-query']   # 兜底解析器（境外 DoH）
    fallback-filter:
        geoip: true                # 主解析器返回非 CN 的 IP 时，改用 fallback
        ipcidr: [240.0.0.0/4, 0.0.0.0/32]   # 这些段也触发 fallback
```

要点：
- **`default-nameserver`** 必须是普通 IP（非 DoH），否则无法引导解析 DoH 域名。
- **`fake-ip`** 让客户端拿到 `198.18.x.x` 假 IP，连接进入 Mihomo 后再还原成真实域名做规则匹配——这是 TProxy 透明代理下分流准确性的关键。
- **`fallback-filter` + `geoip`** 实现“国内域名走国内 DNS、境外域名走境外 DNS”的防污染策略。

> 本插件的 `prepare_config` 会**删除订阅自带 `dns:` 段**，重写为受控版本：`listen: 0.0.0.0:<dns_port>`（默认 1053）、`enhanced-mode: fake-ip`、`nameserver` 用国内 DNS。因此订阅里的 DNS 配置实际不生效——以插件设置页为准。

---

## 3. proxies：节点定义

每个节点是一个 map，范例全部为 vmess：

```yaml
proxies:
    - { name: <节点名>, type: vmess, server: <服务器域名>, port: <端口>,
        uuid: <UUID>, alterId: 0, cipher: auto, udp: true }
```

| 字段 | 含义 |
| :--- | :--- |
| `name` | 节点显示名（规则与策略组通过它引用） |
| `type` | 协议：`vmess` / `shadowsocks` / `trojan` / `vless` 等 |
| `server` / `port` | 服务器地址与端口 |
| `uuid` | vmess 用户 ID（敏感，范例为占位） |
| `alterId` | vmess alterId，通常 0（AEAD） |
| `cipher` | 加密方式，`auto` 由内核决定 |
| `udp` | 是否转发 UDP |

> 节点段来自订阅，插件原样保留，不做改写。节点名命名建议带地区/用途后缀（如 `香港A`、`台湾-流媒体-ChatGPT`），便于在策略组与规则里语义化引用。

---

## 4. proxy-groups：策略组

策略组把多个节点/子组聚合，供规则按“组名”引用。范例定义了三种类型：

```yaml
proxy-groups:
    - { name: LMout, type: select, proxies: [自动选择, 故障转移, <节点...>] }
    - { name: 自动选择, type: url-test,  proxies: [<节点...>], url: 'http://www.gstatic.com/generate_204', interval: 86400 }
    - { name: 故障转移, type: fallback,  proxies: [<节点...>], url: 'http://www.gstatic.com/generate_204', interval: 7200 }
```

| `type` | 行为 |
| :--- | :--- |
| `select` | 手动选择一个出口（默认第一项） |
| `url-test` | 周期性向 `url` 发探测，自动选**延迟最低**的节点 |
| `fallback` | 按顺序使用第一个**可用**节点，不可用则切下一个 |

- `url`：健康检查目标，常用 `generate_204`（轻量 204 响应）。
- `interval`：探测间隔（秒）。范例 `url-test` 用 86400s（每天），`fallback` 用 7200s（每 2 小时）。
- 组的 `proxies` 列表里可嵌套引用其它组名（如 `LMout` 引用了 `自动选择`、`故障转移`），形成层级。

> 规则里的 `LMout` 就是引用名为 `LMout` 的 `select` 组——这是“域名→策略组→具体节点”的解耦点：改出口不必改规则，只需在组里切换。

---

## 5. rules：分流规则

### 5.1 规则语法

每条规则形如 `类型,匹配值,策略[,附加标志]`：

| 类型 | 匹配对象 | 示例 |
| :--- | :--- | :--- |
| `DOMAIN` | 精确域名 | `DOMAIN,developer.apple.com,LMout` |
| `DOMAIN-SUFFIX` | 域名后缀（含子域） | `DOMAIN-SUFFIX,apple.com,DIRECT` |
| `DOMAIN-KEYWORD` | 域名包含关键字 | `DOMAIN-KEYWORD,google,LMout` |
| `IP-CIDR` | IPv4 网段 | `IP-CIDR,91.108.4.0/22,LMout,no-resolve` |
| `IP-CIDR6` | IPv6 网段 | `IP-CIDR6,2001:67c:4e8::/48,LMout,no-resolve` |
| `GEOIP` | 按 GeoIP 国家码 | `GEOIP,CN,DIRECT` |
| `MATCH` | 兜底全匹配 | `MATCH,LMout` |

### 5.2 策略目标

第三段是命中后的去向，可为：
- `DIRECT` —— 直连，不走代理
- `REJECT` —— 拦截丢弃（常用于广告/追踪）
- `<组名>` —— 交给某个 `proxy-group`（如 `LMout`）

### 5.3 `no-resolve` 标志

仅用于 IP 类规则（`IP-CIDR` / `IP-CIDR6`）。**不加**时，遇到域名连接会先做 DNS 解析再比对 IP 段——既慢又可能泄漏 DNS；**加 `no-resolve`** 则只在目标已是 IP 时匹配，域名连接跳过本条。范例的 IP 规则全部带 `no-resolve`，是规范写法。

### 5.4 匹配顺序（关键）

规则**自上而下、首条命中即停止**。因此顺序就是优先级。范例的组织顺序见下节。

---

## 6. 规则组织策略（范例抽象）

范例的 `rules:` 段并非随意堆砌，而是按“从特殊到一般、先精确后宽泛”的策略分层。抽象如下：

| 顺序 | 分层 | 典型策略 | 作用 |
| :--- | :--- | :--- | :--- |
| ① | **例外覆盖** | 部分苹果/谷歌域名强制 `LMout` | 覆盖后面会被判 `DIRECT` 的国内 CDN（如 OCSP、TestFlight） |
| ② | **苹果服务** | 多数 `DIRECT`，少量 `LMout` | 区分苹果国内 CDN 与需代理的服务 |
| ③ | **国内直连** | 大量 `DOMAIN-SUFFIX` → `DIRECT` | 国内站点（网易、阿里、腾讯、B 站…）直连不走代理 |
| ④ | **境外代理关键字** | `DOMAIN-KEYWORD` → `LMout` | google/facebook/twitter/youtube… 命中即代理 |
| ⑤ | **广告拦截** | `DOMAIN-KEYWORD`/`SUFFIX` → `REJECT` | admaster/doubleclick/umeng 等广告追踪直接拦截 |
| ⑥ | **境外域名** | 大量 `DOMAIN-SUFFIX` → `LMout` | 境外站点（GitHub/Telegram/Wikipedia…）走代理 |
| ⑦ | **特定 IP 段** | `IP-CIDR` → `LMout,no-resolve` | Telegram、Google CN 等 IP 段走代理 |
| ⑧ | **本地/保留地址** | `IP-CIDR` → `DIRECT` | 127/10/172.16/192.168/224 等内网与保留段直连 |
| ⑨ | **国家兜底** | `DOMAIN-SUFFIX,cn`、`GEOIP,CN` → `DIRECT` | .cn 域名与国内 IP 默认直连 |
| ⑩ | **最终兜底** | `MATCH,LMout` | 未命中任何规则的流量走代理 |

设计要点：
- **先精确后宽泛**：`DOMAIN` 早于 `DOMAIN-SUFFIX` 早于 `GEOIP` 早于 `MATCH`，避免宽泛规则吃掉精确意图。
- **例外覆盖**：把“本该直连但需代理”的少数域名放在最前，绕过后续 `DIRECT` 判定。
- **`GEOIP,CN` 必须在 `MATCH` 之前**，让国内 IP 兜底直连；最后 `MATCH,LMout` 兜底代理，保证未识别流量默认有出路。
- **IP 规则统一 `no-resolve`**，避免 DNS 解析副作用。

---

## 7. 与本插件的集成

订阅文件**不是**直接喂给内核的配置。多订阅模式下，插件先由 `update_subscriptions` 生成合并源配置，再由 `prepare_config` 加工成 `/tmp/mihomo_run.yaml`：

- **批量下载与缓存**：每条启用订阅独立下载到 `/etc/mihomo/subscriptions/<sid>.yaml`；单源失败时使用该订阅旧缓存，所有源都不可用才整体失败。
- **合并节点池**：第一份启用订阅作为基准，保留其策略组、规则和其它配置；后续订阅只贡献 `proxies:` 节点，按节点名去重，靠前订阅优先。
- **注入聚合组**：多订阅时生成 `SSProxy - 全部订阅` select 组，并加入基准订阅已有的 select 组；兼容 `proxies:` 块式列表、`proxies: [...]` 行内列表和 `{ name, type, proxies }` flow-map 写法。

1. **拷贝**订阅到 `/tmp/mihomo_run.yaml`。
2. **删除**原 `dns:` / `tun:` 块、顶层端口与 `external-controller`（防重复键）。
3. **前置**受控端口：`mixed-port` / `tproxy-port` / `allow-lan` / `external-controller: 0.0.0.0:9090`。
4. **追加**受控 `dns:` 块（`listen: 0.0.0.0:<dns_port>`，`enhanced-mode: fake-ip`）。
5. **追加**受控 `tun:` 块（按 `tun_enabled` 决定 `enable: true/false` + `auto-route`）。
6. **注入 UCI 访问规则**到 `rules:` 段**最前面**（见下）。

### 7.1 UCI 访问规则的注入（最高优先级）

插件「规则管理」页的每条 UCI `mihomo_rule`（`emit_access_rules_yaml` 生成）会以 `DOMAIN-SUFFIX` 形式插入到 `rules:` 行紧后、订阅规则之前：

| UCI 动作 | 生成的规则 |
| :--- | :--- |
| `block` | `DOMAIN-SUFFIX,<domain>,REJECT` |
| `direct` | `DOMAIN-SUFFIX,<domain>,DIRECT` |
| `proxy`（指定组） | `DOMAIN-SUFFIX,<domain>,<group>` |

因为首条命中即停，**UCI 规则优先级高于订阅内所有规则**。注意：
- Mihomo 规则是**全局**的，UCI 里记录的 `src_ip` 仅用于管理追溯，**不按来源 IP 生效**。
- 若 `rules:` 段不存在，插件会自动补一个 `rules:` 头再注入。

### 7.2 多订阅排序语义

设置页「订阅列表」的排序会影响合并结果：

- 第一份启用订阅决定基础规则、策略组和 provider。
- 后续启用订阅只补充新节点，不接管规则出口。
- 同名节点以靠前订阅为准。
- 仪表盘「全部订阅节点」和批量测速覆盖合并后的节点池。

### 7.3 实际生效优先级

```
UCI 访问规则（插件注入，段首）
        ↓ 首条命中即停
订阅自带 rules（按本文件 §6 的分层）
        ↓
MATCH 兜底
```

所以**改单条域名走向**用插件规则管理页即可（即时高优先级）；**整批分流策略**则由订阅文件决定。

---

## 8. 编写/维护建议

1. **节点名语义化**：带地区/用途（`香港A`、`台湾-流媒体-ChatGPT`），规则与组引用更直观。
2. **策略组层级化**：`select`（人工）→ `url-test`（自动）→ `fallback`（容灾），出口切换不污染规则。
3. **规则先精确后宽泛**，IP 规则一律 `no-resolve`。
4. **`GEOIP,CN` 与 `MATCH` 收尾**：保证国内默认直连、其余默认代理。
5. **端口/DNS/TUN 交给插件**：订阅里写这些会被覆盖，无需费心调优。
6. **例外覆盖放最前**：少数“国内 CDN 但需代理”的域名用前置 `DOMAIN,xxx,<组>` 解决。
7. **广告拦截用 `REJECT`**：集中在一段，便于增删。
