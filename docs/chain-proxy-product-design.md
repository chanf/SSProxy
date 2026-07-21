# 链式代理产品设计

> 项目：luci-app-ssproxy（水杉代理）
>
> 模块：独立「链式代理」Tab
>
> 目标版本：1.0.0-174+

## 1. 产品目标

链式代理用于把指定局域网设备的流量固定到一条两跳链路：

```text
本地设备 -> 订阅节点 -> 落地节点 -> 目标网站
```

用户不需要理解或手写 Mihomo `relay` 配置，只管理两类业务数据：

1. 落地节点：用户自行购买或维护的第二跳代理服务器。
2. 数据链路：设备 IP、订阅节点、落地节点三者的绑定关系。

旧版 `relay_chain`/`relay_bind` 允许填写任意跳列表，概念偏底层，也没有独立的落地节点资产管理。本次重新设计后删除旧数据模型和旧页面逻辑，不做旧配置自动迁移。

## 2. 功能范围

### 2.1 落地节点管理

提供列表、新增、编辑、删除、启停能力。每个节点包含通用字段：

| 字段 | 说明 |
| --- | --- |
| 名称 | 页面展示名称，要求唯一 |
| 类型 | SOCKS5 / HTTP / SS / Trojan / VMess / VLESS |
| 服务器 | IPv4、IPv6 或域名 |
| 端口 | 1-65535 |
| 启用 | 关闭后不注入运行配置，引用它的链路也不生效 |

不同类型的协议字段：

| 类型 | 协议字段 |
| --- | --- |
| SOCKS5 | 用户名、密码、TLS、跳过证书验证 |
| HTTP | 用户名、密码、TLS、跳过证书验证 |
| SS | 加密方式、密码 |
| Trojan | 密码、SNI、跳过证书验证 |
| VMess | UUID、Alter ID、加密方式、传输方式、TLS、SNI、跳过证书验证 |
| VLESS | UUID、Flow、传输方式、TLS、SNI、跳过证书验证 |

首期支持常见 TCP 传输。WebSocket、gRPC、Reality 等需要额外结构化字段，后续按协议扩展，不在首期用自由文本 YAML 绕过校验。

### 2.2 数据链路管理

使用表格展示，每条记录的主要列固定为：

| 设备 IP / CIDR | 订阅节点 | 落地节点 |
| --- | --- | --- |
| `192.168.66.158` | `香港 01` | `香港落地` |

附加能力：

- 启用/停用单条链路。
- 编辑和删除。
- 对单条链路执行连通性/延时测试。
- 支持 IPv4、IPv6、IPv4 CIDR、IPv6 CIDR。
- 同一个设备只允许存在一条启用链路，避免多条 `SRC-IP-CIDR` 规则因顺序产生歧义。

订阅节点来自当前订阅或运行配置的节点列表；落地节点来自「落地节点管理」。链路保存后需应用并重启 Mihomo 才进入运行配置。

## 3. UCI 数据模型

### 3.1 landing_node

```uci
config landing_node
    option enabled '1'
    option name '香港落地'
    option type 'socks5'
    option server '203.0.113.10'
    option port '1080'
    option username 'user'
    option password 'pass'
    option tls '0'
    option skip_cert_verify '0'
```

协议专用字段按需使用：`cipher`、`uuid`、`alter_id`、`network`、`sni`、`flow`。

运行时代理名称不直接使用用户输入名称，而使用稳定且无冲突的内部名称：

```text
ssproxy-landing-<UCI section id>
```

### 3.2 data_link

```uci
config data_link
    option enabled '1'
    option device_ip '192.168.66.158'
    option subscription_node '香港 01'
    option landing_node 'cfg0ab123'
```

`landing_node` 保存 UCI section id，避免落地节点改名后链路失效。对应的内部链路代理名称为：

```text
ssproxy-chain-<UCI section id>
```

全局统一前置保存在主配置段：

```uci
config mihomo 'config'
    option chain_front_node 'individual'
```

`individual` 在界面显示为「非统一前置」。此时每条链路使用自己的 `subscription_node`；选择具体订阅节点后，所有链路的有效前置节点统一使用该值，各行原值仅暂时忽略且不会被覆盖。

## 4. 运行配置生成

`helper.sh prepare_config` 在订阅、自定义覆盖和 UCI 规则合并完成后执行链式代理注入。

### 4.1 注入落地节点

每个启用且校验通过的 `landing_node` 转换为一个 Mihomo `proxies:` 条目。例如：

```yaml
proxies:
  - name: "ssproxy-landing-cfg0ab123"
    type: socks5
    server: "203.0.113.10"
    port: 1080
    username: "user"
    password: "pass"
    udp: true
```

### 4.2 注入两跳 dialer-proxy

当前 Mihomo 已移除 `relay` 组类型。每条启用且引用有效的数据链路，会生成一个落地节点代理副本，并通过 `dialer-proxy` 指定订阅节点为其拨号出口：

```yaml
proxies:
  - name: "ssproxy-chain-cfg0cd456"
    type: socks5
    server: "203.0.113.10"
    port: 1080
    username: "user"
    password: "pass"
    dialer-proxy: "香港 01"
    udp: true
```

订阅节点必须存在于当前合并后的配置；落地节点必须存在、启用并成功生成。任何校验失败都跳过该链路，并写入 `mihomo-chain` 日志，避免无效引用导致 Mihomo 启动失败。

### 4.3 注入设备规则

```yaml
rules:
  - 'SRC-IP-CIDR,192.168.66.158/32,ssproxy-chain-cfg0cd456'
```

IPv6 使用 `SRC-IP-CIDR6`，单地址自动补 `/32` 或 `/128`。链路规则位于普通 UCI 访问规则之前，但仍位于局域网/组播内置直连规则之后。

## 5. 独立 Tab

菜单新增「链式代理」，页面由三个区域组成：

1. 运行状态：控制器状态、落地节点数量、有效链路数量、运行配置注入数量。
2. 落地节点：GridSection 列表，使用模态表单新增和编辑，字段随协议类型变化。
3. 数据链路：GridSection 表格，主要列为设备 IP、订阅节点、落地节点，并提供测试操作；标题栏包含全局「前置节点」下拉框。

「前置节点」第一项固定为「非统一前置」。选择具体订阅节点后立即保存并重启 Mihomo，逐行订阅节点字段切换为只读；切回「非统一前置」后恢复逐行节点。订阅更新导致统一节点消失时，页面标记其已失效，后端跳过受影响链路并记录日志，不静默切换到其他节点。

数据链路状态以运行期圆点展示：初始为红色；手动测试成功或后台连接采集发现真实流量经过该链路后变为绿色；标题栏「重置」清空所有绿色状态。状态仅保存在 `/tmp`，不写入 UCI。

每条链路同时展示上下行实时速率和累计流量，按 5 秒连接快照计算增量，自动选择 B/KB/MB/GB/TB 单位。运行数据保存在 `/tmp`，累计值每 60 秒批量写入 `/etc/mihomo/.data_link_traffic`；「重置」同时清空通讯状态、速率和累计值。

落地节点与数据链路作为 UCI 段保存在 `/etc/config/mihomo`。包安装前和卸载前会将完整文件备份到 `/etc/mihomo/.uci_config_backup`，安装后原子恢复，确保普通升级与「卸载后重装」均不丢失链路记录。

「服务设置」不再包含任何链式代理表单；「运行状态」不再提供旧版 Relay 逐跳热切换。

## 6. 状态、日志与测试接口

接口复用 LuCI `file.exec` HTTP RPC，不额外开放监听端口：

| helper.sh 命令 | 用途 |
| --- | --- |
| `get_chain_status` | 返回控制器状态、UCI 记录数、运行配置中的落地节点和链路数量 |
| `set_chain_front_node <individual|node>` | 校验并持久化全局前置节点 |
| `get_chain_log [lines]` | 返回 `mihomo-chain` 最近日志 |
| `test_landing_node <section_id>` | 测试已应用落地节点的连通性并返回延时 |
| `test_data_link <section_id>` | 调用 Mihomo 控制器的链路代理 delay API，返回 JSON |

通过 LuCI HTTP RPC 调用时，实际执行路径为 `/usr/share/mihomo/helper.sh`，现有 rpcd ACL 已授权该路径。

## 7. 安全和可靠性

- 用户名、密码、UUID 等保存在 `/etc/config/mihomo`；该文件仅应由 root 和受权 LuCI 会话访问。
- 密码字段在页面中使用 password 输入框，列表不回显凭据。
- 所有字符串在写入 YAML 前进行双引号、反斜杠、换行转义。
- 端口、协议必填字段、设备 IP/CIDR、引用关系均在页面和后端双重校验。
- 内部代理名和链路名使用 UCI section id，避免用户名称冲突或 YAML 注入。
- 无效落地节点或链路只记录错误并跳过，不允许拖垮 Mihomo 主服务。

## 8. 验收标准

1. 独立「链式代理」Tab 可正常打开。
2. 六种落地节点类型可增删改查，协议字段按类型显示。
3. 数据链路以设备 IP、订阅节点、落地节点三列展示并可增删改查。
4. `/tmp/mihomo_run.yaml` 正确包含落地 `proxies`、两跳 `relay` 组和源 IP 规则。
5. 无效引用不会导致 Mihomo 启动失败，错误可从 `get_chain_log` 查看。
6. `get_chain_status` 能区分 UCI 配置数量与实际运行注入数量。
7. `test_data_link` 能返回链路延时或明确错误。
8. 构建器和新增 shell 测试通过，生成的新版本 IPK 可安装到路由器。
9. 路由器安装后 Mihomo 控制器、TProxy 表和链式代理状态均可检查。
10. 「非统一前置」使用每行节点；选择统一节点后所有生成的 `dialer-proxy` 均使用该节点，切回后逐行配置保持不变。
