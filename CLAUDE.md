# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

`luci-app-mihomo` —— 面向 iStoreOS / OpenWrt (Firewall4 + nftables) 的 LuCI 应用，是 Mihomo (Clash Meta) 代理核心的前端，集成 TProxy 透明代理、Dnsmasq DNS 劫持、订阅自动更新、节点延时测试与访问日志/规则管理。整个仓库**只有一个源文件** `build_ipk.py`：它既是构建器，也以字符串形式内嵌了全部要打包的文件（shell 脚本、UCI 配置、LuCI JS 视图、JSON）。

## 构建

```bash
python build_ipk.py
```

- 仅依赖 Python 3 标准库（`os, tarfile, io, shutil, re`），无需虚拟环境或第三方包
- 产出 `dist/luci-app-mihomo_<version>_all.ipk`；中间产物 `build/control.tar.gz`、`build/data.tar.gz`
- 没有测试套件、没有 lint 配置

⚠️ **每次构建会自改 `build_ipk.py`**：`main()` 第一步调用 `increment_version()`，把 `PKG_VERSION`（如 `1.0.0-81` → `1.0.0-82`）原地重写并刷新 `IPK_FILENAME`。构建后一定有"源文件被改"的 diff，属预期；编辑该文件请以磁盘最新内容为准。

## 部署

可以使用 `./deploy.sh` 脚本将最新构建的 IPK 自动上传并安装到软路由器。该脚本使用 macOS 自带的 `expect` 自动输入密码并完成以下操作：
1. 查找 `dist/` 目录下最新生成的 IPK 文件。
2. 通过 `scp` 上传文件至路由器的 `/tmp/` 目录。
3. 通过 `ssh` 在路由器执行 `opkg install`，安装完成后自动重启 `mihomo` 服务。

运行部署：
```bash
./deploy.sh
```

## 核心架构：单源真相 `src_files`

**所有打进 .ipk 的文件都是 `build_ipk.py` 顶部 `src_files` 字典里的字符串值**（key=相对路径，value=内容）。`src/`、`build/`、`dist/` 全部由构建**先删后建**生成，是纯产物：

- ❌ 不要手编 `src/` 下任何文件——下次构建会被覆盖
- ✅ 改任何交付文件就改 `src_files` 里对应字符串，然后 `python build_ipk.py`

`CONTROL/control` 里写死的 `Version: 1.0.0-1` 只是占位，`create_source_tree` 写盘时用正则替换成当前 `PKG_VERSION`——**版本号真正来源是顶部 `PKG_VERSION` 变量**。

IPK = gzip tar，含 `debian-binary`(`2.0\n`) + `control.tar.gz`(由 `src/CONTROL/`) + `data.tar.gz`(由 `src/root/`)。`make_tar_gz` / `write_tar_gz_outer_archive` 强制 root:root、`mtime=1700000000`、`./` 前缀以实现可复现构建；脚本类文件 `0o755`，其余 `0o644`。

`Depends: luci-base, ip-full, kmod-nft-tproxy, curl`（curl 用于 helper.sh 调核心外部控制器 API 与下载）。`CONTROL/conffiles` 标记 `/etc/config/mihomo` 为配置文件，使升级时用户设置（尤其 subscription_url）不被包默认值覆盖。

## 运行时架构（多文件协作的关键）

### `/etc/init.d/mihomo` —— procd 编排者，启动 **3 个实例**
`START=95`，`start_service` 依次拉起：
1. **核心**：先 `helper.sh restore_subscription_url` 恢复订阅链接 → `helper.sh prepare_config` 生成 `/tmp/mihomo_run.yaml` → 以 `-f /tmp/mihomo_run.yaml` 启动核心（**不直接用订阅原文件**）→ 非 TUN 时 `enable_tproxy`（nftables 表 `inet mihomo`、fwmark 1、路由表 100）→ 开 dns_hijack 时 `enable_dns_hijack`（UCI 改 `dhcp.@dnsmasq[0]` 的 server/noresolv）。
2. **连接采集器**：后台循环 `collect_connections`，每 15s 把实时连接去重持久化到 `/tmp/mihomo_access.log`（供访问日志历史视图）。
3. **自动更新循环**：`auto_update_loop` 每 10min 轮询一次 `auto_update_now`——**自包含、不依赖系统 cron**。

### `/usr/share/mihomo/helper.sh` —— 单体后端，~20 个子命令
由末尾 `case "$1"` 分发，按子系统分组：

- **核心/架构**：`get_arch`（含 `cpu_amd64_v3` 检测 AVX2/BMI2 等，区分 `amd64` 与 `amd64-compatible`）、`check_core`、`download_core [url]`（硬编码 `v1.19.28`，GitHub MetaCubeX/mihomo，用 curl）。
- **订阅**：`update_subscription [url]`、`clear_subscription`、`save_subscription_url`/`restore_subscription_url`（持久化到包外文件 `/etc/mihomo/.subscription_url`，**即使 opkg remove+install 重装也能恢复订阅链接**）、`get_proxies`（健壮 awk 解析器，兼容 inline `- name:` 与 flow-map `{name:...}` 两种 YAML；对未下载/空/HTML 拦截页/无节点/解析失败返回带中文提示的结构化 JSON）。
- **自动更新**：`auto_update_now`、`auto_update_loop`、`get_schedule`（按 `update_interval` 小时数节流，报告上次/下次更新时间 JSON）。
- **配置合并**：`prepare_config`——拷贝订阅 YAML 到 `/tmp/mihomo_run.yaml`，awk 删原有 `dns:`/`tun:` 块、sed 删顶层端口，前置受控端口、追加受控 `dns:`/`tun:` 块，**再用 `emit_access_rules_yaml` 把 UCI 访问规则注入 `rules:` 段**。正因为有它，UCI 的端口/DNS/TUN 设置才真正生效。
- **实时控制（依赖核心已启动，走 9090 外部控制器 API）**：`get_proxy_groups`、`select_node <group> <node>`、`get_connections`、`collect_connections`、`test_node_delay <name>`、`test_all_nodes`。延时测试用可配置的 `test_url`，并对节点名做 `resolve_proxy_name` 容错匹配（容忍订阅文件与运行配置间的引号/CRLF 差异）。
- **访问规则（UCI `mihomo_rule` 段）**：`get_access_rules`、`add_access_rule`、`del_access_rule`。注意 Mihomo 规则是**全局**的，记录的 `src_ip` 仅用于管理追溯、不按来源 IP 生效。

> `test_all_nodes` 用有限并发一次跑完所有节点延时——这是为替代旧版"前端每节点一个 fs.exec（30 路并发）把 rpcd/file-exec 打满超时"的设计；新增类似批量需求时沿用此后端批量模式。

### LuCI 前端（4 个视图，纯 JS API，无 npm/无编译）
`dashboard.js`（运行状态 + 策略组实时切换 + 节点卡片/延时测试/清空 + 自动更新排程 + 核心管理 + 日志）、`settings.js`（UCI 表单：订阅/自动更新/更新间隔/清空/延时测试 URL/TUN/DNS 劫持/高级端口路径）、`accesslog.js`（实时连接 5s 刷新 + 历史访问，支持快捷追加规则，自带 `setInterval` 与 `unload` 清理）、`rules.js`（自定义访问规则管理：添加/删除规则，应用并重启核心）。菜单在 `menu.d/*.json` 注册 4 项；`rpcd/acl.d/*.json` 授予 helper.sh/logread/init.d 的 exec 权限。

## 约定与注意

- **`\t` 是 Python 转义不是字面量**：`src_files` 里的 shell/JS 字符串用 `\t` 缩进；普通三引号字符串（非 `r"""`）写盘时解释成真实 Tab。UCI 配置和 shell 脚本要求 Tab 缩进，保留 `\t` 写法，别误当字面反斜杠。
- **shell 输出 JSON 的引号转义**：helper.sh 里 `echo` 的 JSON 用转义双引号（源码写成 `\\"`，写出为 `\"`）；awk 程序里改用八进制 `\042`/`\047`（源码 `\\042`/`\\047`）以避开 awk 字符串引号冲突。新增类似代码务必沿用这两种模式，否则会复现"整脚本 syntax error 无法加载"的坑（历史上 get_proxies 的 echo 曾因引号未转义导致整个 helper.sh 在路由器上无法加载）。
- **两个 JSON 缩进风格不一致**：`menu.d/*.json` 用 4 空格、`acl.d/*.json` 用 Tab，各自保留即可。
- `increment_version()` 靠正则定位 `PKG_VERSION = "..."` 行原地改写脚本——重命名该变量会破坏自增。
