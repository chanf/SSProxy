# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

`luci-app-mihomo` —— 面向 iStoreOS / OpenWrt (Firewall4 + nftables) 的 LuCI 应用，是 Mihomo (Clash Meta) 代理核心的轻量前端，集成 TProxy 透明代理与 Dnsmasq DNS 劫持。整个仓库**只有一个源文件** `build_ipk.py`，它既是构建器，也内嵌了所有要打包的文件内容。

## 构建

```bash
python build_ipk.py
```

- 仅依赖 Python 3 标准库（`os, tarfile, io, shutil, re`），无需虚拟环境或第三方包
- 产出：`dist/luci-app-mihomo_<version>_all.ipk`
- 中间产物：`build/control.tar.gz`、`build/data.tar.gz`
- 没有测试套件、没有 lint 配置

⚠️ **每次构建会自改 `build_ipk.py`**：`main()` 第一步调用 `increment_version()`，会把脚本里的 `PKG_VERSION`（如 `1.0.0-23` → `1.0.0-24`）原地重写并刷新 `IPK_FILENAME`。因此构建后一定会出现"源文件被修改"的 diff，属预期行为；编辑该文件时请以磁盘最新内容为准。

## 核心架构：单源真相 `src_files`

**所有要打进 .ipk 的文件都以字符串形式内嵌在 `build_ipk.py` 顶部的 `src_files` 字典里**（key 是相对路径，value 是文件内容）。`src/`、`build/`、`dist/` 三个目录全部由构建过程**先删后建**生成，是纯产物：

- ❌ 永远不要手动编辑 `src/` 下的任何文件——下次构建会被覆盖
- ✅ 要改任何交付文件（shell 脚本、UCI 配置、LuCI JS 视图、JSON），改 `src_files` 里对应的字符串值
- 修改流程：编辑 `src_files` → `python build_ipk.py` → 取 `dist/*.ipk`

## 构建机制（IPK 组装细节）

IPK 本质是一个 gzip 压缩的 tar，内含三个成员：
1. `debian-binary` —— 固定内容 `b"2.0\n"`
2. `control.tar.gz` —— 由 `src/CONTROL/` 打包
3. `data.tar.gz` —— 由 `src/root/` 打包

为追求**可复现构建**，`make_tar_gz` / `write_tar_gz_outer_archive` 强制：
- 所有条目 `uid/gid=0`、`uname/gname=root`
- `mtime` 固定为 `1700000000`
- arcname 以 `./` 前缀开头，并显式加入根目录条目 `.`
- 文件权限：脚本类（`postinst/postrm/preinst/prerm`、`etc/init.d/*`、`usr/share/mihomo/helper.sh`）= `0o755`，其余 `0o644`

`CONTROL/control` 字符串里写死了 `Version: 1.0.0-1`，但 `create_source_tree` 在写盘时会用正则把 Version 替换成当前 `PKG_VERSION` —— 所以**版本号的真正来源是顶部的 `PKG_VERSION` 变量**，control 里的那行只是占位。

## 运行时架构（理解多文件协作的关键）

部署到路由器后，各文件协作关系如下：

**`/etc/init.d/mihomo`**（procd 服务，`START=95`）—— 启动编排者：
1. 调用 `helper.sh prepare_config` 生成运行配置 `/tmp/mihomo_run.yaml`
2. 以 `-f /tmp/mihomo_run.yaml` 启动核心（**不是**直接用订阅原始配置）
3. 若非 TUN 模式：`enable_tproxy` 注入 nftables 规则（表 `inet mihomo`、fwmark `1`、路由表 `100`）做透明代理
4. 若开启 dns_hijack：`enable_dns_hijack` 通过 UCI 改写 `dhcp.@dnsmasq[0]` 的 `server`/`noresolv`，把 DNS 转发给 Mihomo
5. `stop_service` 做相反清理（删 nft 表、还原 dnsmasq、删 `/tmp/mihomo_run.yaml`）

**`/usr/share/mihomo/helper.sh prepare_config`** —— **运行时配置合并的核心**。订阅下载的 YAML 不能直接喂给核心，因为 UCI 设置（端口、DNS、TUN）需要覆盖它。做法：
1. 拷贝订阅原始配置到 `/tmp/mihomo_run.yaml`
2. 用 `awk` 删除其中已有的 `dns:`/`tun:` 块（避免重复键报错）
3. 用 `sed` 删除顶层端口键（`mixed-port`/`tproxy-port`/`port`/`socks-port` 等）
4. 前置注入受 UCI 控制的端口、追加受控的 `dns:` 和 `tun:` 块

正因为有 `prepare_config`，UCI 里的端口/DNS/TUN 设置才真正生效——不要以为改了端口没用。

helper.sh 子命令（由末尾 `case "$1"` 分发）分两类：

- **静态/文件类**：`get_arch`、`check_core`、`download_core [url]`、`update_subscription <url>`、`prepare_config`、`get_proxies` —— 读写本地配置或 UCI，不需要核心在运行。
- **实时控制类**（依赖核心已启动）：`get_proxy_groups`、`select_node <group> <node>` —— 通过 `curl` 调用 Mihomo 外部控制器 RESTful API（`http://127.0.0.1:9090/proxies`，`GET` 查询分组、`PUT /proxies/<group>` 切换选中节点）。这就是 `CONTROL/control` 的 `Depends` 里要带 `curl` 的原因。

`get_proxies` 的错误处理值得注意：它不简单返回空数组，而是对各种订阅失败场景返回带中文提示的结构化 JSON（`not_found`/`empty`/`html`（检测到订阅链接实际返回了 HTML/WAF 拦截页）/`no_nodes`/`parse_failed`），前端据此给出可读错误。核心下载版本硬编码为 `v1.18.9`（GitHub `MetaCubeX/mihomo` releases），架构靠 `uname -m` 推断。

> ⚠️ 已知小瑕疵：`case` 的 `*` 兜底分支里 `Usage` 提示字符串未同步新增的两个命令（`get_proxy_groups`/`select_node`），新增子命令时记得一并更新帮助文本。

**LuCI 前端**（`dashboard.js` + `settings.js`）：纯 LuCI JS API（`require view/form/fs/rpc/uci`），无 npm、无编译步骤，源码即交付。dashboard 在 `load()` 里并发调 `check_core`、`get_proxies`、`get_proxy_groups`、`logread` 取数据：既展示从订阅文件解析出的静态节点列表（`get_proxies`），又通过 `get_proxy_groups`+`select_node` 提供**对运行中核心的实时分组与节点切换**面板，并提供跳转到 `metacube-xd` Web 控制台的链接（同样依赖 9090 端口）。settings.js 是标准 UCI `form.Map`。

## 约定与注意

- **`\t` 是 Python 转义不是字面量**：`src_files` 里的 shell/JS 字符串大量使用 `\t` 缩进。这些是普通三引号字符串（非 `r"""`），Python 写盘时会解释成真实 Tab。OpenWrt 的 UCI 配置和 shell 脚本要求 Tab 缩进，故请保留 `\t` 写法，编辑时不要误以为是字面反斜杠。
- **两个 JSON 缩进风格不一致**（已知）：`menu.d/*.json` 用 4 空格，`acl.d/*.json` 用 Tab。修改时保持各自原有风格即可，不必强行统一。
- `increment_version()` 通过正则定位 `PKG_VERSION` 行原地改写脚本，依赖该行格式（`PKG_VERSION = "..."`）——重命名该变量会破坏自增。
