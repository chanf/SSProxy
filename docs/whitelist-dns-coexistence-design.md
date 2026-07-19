# 白名单 + DNS 劫持共存设计

## 背景

「受控 IP 列表」(`mihomo.config.acl_mode=whitelist` + `acl_ips`) 长期与「劫持系统 DNS」(`dns_hijack=1`) **互斥**：一旦开启 DNS 劫持，`start_service` 会把 `acl_mode` 强制改写成 `all`，白名单静默失效（实测：`dns_hijack=1` 时 nft 里没有 `ip saddr != {...} return` 规则，日志打印 `acl_mode: all`）。

根因是设计层面的：DNS 劫持走的是**全局** dnsmasq 转发（`enable_dns_hijack` 把 `dhcp.@dnsmasq[0].server` 改成 `127.0.0.1#1053` + `noresolv=1`），于是**所有**客户端都拿到 Mihomo 的 fake-ip；而非白名单设备拿到 fake-ip 却不被 tproxy → 直连一个不存在的 `198.18.x.x` → 断网。作者因此一禁了之。

## 目标

让 `whitelist + dns_hijack` 真正共存：

- 白名单内设备：DNS 经 Mihomo（fake-ip）/ 真实 AAAA，流量走 tproxy 代理；
- 白名单外设备：DNS 走 dnsmasq 真实上游，流量直连（旁路 tproxy）。

## 机制：把 DNS 劫持从「全局」改为「按源 IP 作用域」

核心一刀：**白名单内设备的 53 端口被 nft DNAT 重定向到 Mihomo DNS；其余设备的 DNS 不动，继续用 dnsmasq 真实上游。** 不再调用全局 `enable_dns_hijack`（改 dnsmasq），改由 nft 在 prerouting 按源地址接管。

### nft 表与 hook 优先级

- 新增独立表 `inet mihomo_dns`，其 prerouting 链类型为 `type nat hook prerouting priority dstnat`（= -100）。nat 链与 filter 链类型不同，不能与 `inet mihomo`（`priority mangle` = -150）共用同一张表的同类型链，故另起一张表。fw4 不会清掉非 `fw4` 命名的自定义表（现有 `inet mihomo` 已与之共存）。
- hook 按优先级数值升序执行：mangle(-150) → nat dstnat(-100)。mangle 里的 `return` 只结束 mangle 链，**不影响**后续 nat 钩子，所以「mangle 放行白名单 DNS → nat DNAT 到 1053」成立。
- 依赖 `nf_conntrack`（fw4 必加载）做反向 NAT；Mihomo 在 `0.0.0.0:1053` 监听，DNAT 后 dst=路由器 IP，可达。回复经 conntrack 自动还原，客户端无感。
- 用 **DNAT** 而非 `redirect`：`redirect` 只匹配目标是本机的包，硬编码 DNS（如 `8.8.8.8`）的客户端会漏；`dnat to $router_ip:1053` 按源地址无条件改写 dst，全兜住。

### 关键规则顺序（mangle 链内，白名单+dns_hijack 模式）

必须有一条「白名单源 + 53 端口 return」**早于**白名单旁路 return、**早于** tproxy 兜底：否则白名单设备的 DNS（尤其硬编码 DNS 客户端）会被兜底规则 tproxy 到 `:7893` 而非走 nat DNAT 到 `:1053`。对「DNS 发给路由器」的普通客户端，私网 daddr return 已先行放行（冗余但无害）；对硬编码 DNS 客户端，这条早 return 是必需的。

## 四象限 × 双族 规则矩阵

| 模式 | `inet mihomo`（mangle） | `inet mihomo_dns`（nat） | dnsmasq |
| :--- | :--- | :--- | :--- |
| **A. all + dns=1**（默认，逐字节同旧） | daddr 私网 return（v4/v6）+ tproxy 兜底 | 不存在 | 全局转发到 Mihomo |
| **B. whitelist + dns=0**（同旧） | daddr return + `saddr != {...} return`（v4/v6）+ tproxy 兜底 | 不存在 | 不改 |
| **C. whitelist + dns=1**（新功能核心） | daddr return + 白名单 DNS 放行 return（v4/v6）+ `saddr != {...} return`（v4/v6）+ tproxy 兜底 | 按源 `dnat ... to $rip:1053`（v4/v6 × udp/tcp） | **不改** |
| **D. all + dns=0** | daddr return + tproxy 兜底 | 不存在 | 不改 |

回退：当白名单模式下检测不到路由器 LAN IP（或 `acl_ips` 为空），`emit_tproxy_rules` 不建 DNS 表、不插 DNS 放行规则（`dns_scope=0`），`start_service` 退回全局 `enable_dns_hijack`，即退化为「all 模式 DNS」行为，保证不因检测失败而断网。

## 代码落点（`build_ipk.py`）

- `helper.sh`：新增 `get_lan_ip` / `get_lan_ip6`（UCI→接口→全局→默认路由源 的回退链）、纯函数 `emit_tproxy_rules`（按模式输出 nft 规则文本，`nft -f -` 一次应用），均位于 dispatcher 之前，可被 pytest 的 `run_fn` 直接调用。
- `init.d/mihomo`：`enable_tproxy` 改用 `emit_tproxy_rules | nft -f -`，并补 IPv6 策略路由（`ip -6 rule/route ... table 100`）；`disable_tproxy` 一并清理 `inet mihomo_dns` 与 v6 路由；`start_service` 删除「dns_hijack 强制 all」、按族拆分 `acl_ips`、按需探测路由器 IP、门控全局 `enable_dns_hijack`（仅 all/TUN/降级时调用）。
- `prepare_config`：DNS 块 `ipv6: false` → `ipv6: true`（v6 共存必需，否则白名单 v6 客户端拿不到 AAAA）。`fake-ip-range` 保持默认（Mihomo 不支持 v6 fake-ip，v6 走真实 AAAA + tproxy）。
- `CONTROL/control`：`Depends` 追加 `kmod-nft-nat`（声明新增 nat 链依赖；fw4 已带）。
- `settings.js`：解除 dns_hijack 对白名单的置灰与强制 all（仅 TUN 仍与白名单互斥）；放宽 `acl_ips` 校验以接受 v4/v6 地址与 CIDR；更新帮助文案。

## IPv4 / IPv6 差异与已知限制

- fake-ip 仅 IPv4（`fake-ip-range` 默认 `198.18.0.1/16`）；IPv6 走真实 AAAA + tproxy，域名规则依赖 Mihomo 看到 AAAA 后的规则匹配。
- **必须验证**：Mihomo 的 tproxy-port 是否在 IPv6 上监听（`ss -lunp | grep 7893`）。若不绑定，需在 `prepare_config` 调整 Mihomo 绑定；本次未改绑定，留作路由器验证项。
- 路由器 IPv6 地址：取 br-lan link-local `fe80::`（LAN 段可达、单次启动内稳定）作为 DNAT 目标，GUA 作为回退。接口重建或 ISP 换前缀时需重启服务刷新。
- RA/DHCPv6 DNS 下发不单独处理——按源地址 DNAT 会无视客户端配置的目标 DNS，统一改写到路由器。

## 降级预案

若 IPv6 tproxy 在目标 Mihomo 版本上确不可用：v6 仅做 DNS 按源 DNAT（白名单 v6 客户端用 Mihomo 真实 AAAA 但不 tproxy → 直连）。这会让 v6「白名单内也直连」，体验不一致，仅作降级，不在默认实现里。

## 测试

- 单元：`tests/shell/test_tproxy_rules.py` 断言 `emit_tproxy_rules` 四象限 + v4/v6/空/混合 + 缺 LAN IP 回退的精确输出；`get_lan_ip`/`get_lan_ip6` 的回退链（配合 `tests/fixtures/stubs/ip` 与 `uci_env`）。`tests/shell/test_prepare_config.py` 新增 `ipv6: true` 断言。
- 端到端：见 `docs/whitelist-test-cases.md` 的 TC-06（白名单+DNS 劫持共存）。
