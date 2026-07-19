# luci-app-ssproxy

OpenWrt LuCI 应用：轻量级 Mihomo (Clash Meta) 客户端，集成 Firewall4 (nftables) 透明代理。

## 构建

```bash
python build_ipk.py
```

- 仅依赖 Python 3 标准库，无需虚拟环境或第三方包
- 没有测试套件、没有 lint 配置
- 版本号 (`PKG_VERSION`) **每次构建自动递增**并原地重写脚本文件——构建后出现 "modified" diff 属预期行为
- 产出：`dist/luci-app-ssproxy_<version>_all.ipk`
- 构建会**完全重建** `src/` 目录（先删后建），**永远不要手动编辑 `src/` 下的文件**

## 源码结构

**所有要打进 .ipk 的文件都以字符串内嵌在 `build_ipk.py` 的 `src_files` 字典中。** 这是唯一需要编辑的地方。

修改流程：编辑 `src_files` 对应值 → `python build_ipk.py` → 取 `dist/*.ipk`

## 编辑注意事项

- **`\t` 是 Python 转义不是字面量**：`src_files` 里的 shell/JS 字符串大量使用 `\t` 缩进，Python 写盘时解释成真实 Tab。OpenWrt shell 脚本和 UCI 配置要求 Tab 缩进，编辑时保留 `\t` 写法
- **两个 JSON 缩进风格不一致**（已知）：`menu.d/*.json` 用 4 空格，`acl.d/*.json` 用 Tab，修改时保持各自原有风格
- `increment_version()` 依赖 `PKG_VERSION = "..."` 行格式做正则替换——重命名该变量会破坏自增

## 运行时架构

部署后文件协作关系：

**`/etc/init.d/mihomo`**（procd 服务，`START=95`）启动编排：
1. 调用 `helper.sh prepare_config` 生成运行配置 `/tmp/mihomo_run.yaml`
2. 以 `-f /tmp/mihomo_run.yaml` 启动核心（**不是**直接用订阅原始配置）
3. TProxy 模式：nftables 表 `inet mihomo`，路由表 100，fwmark 1
4. DNS 劫持：通过 UCI 改写 `dhcp.@dnsmasq[0]` 的 `server`/`noresolv`

**`helper.sh prepare_config`** 是运行时配置合并的核心——订阅 YAML 不能直接喂给核心，必须经过此步骤 UCI 端口/DNS/TUN 设置才生效。做法：拷贝订阅配置 → awk 删除 `dns:`/`tun:` 块 → sed 删除顶层端口键 → 前置注入受控端口、追加受控 `dns:` 和 `tun:` 块

helper.sh 子命令分两类：
- **静态/文件类**：`get_arch`、`check_core`、`download_core [url]`、`update_subscription <url>`、`prepare_config`、`get_proxies`
- **实时控制类**（需核心已启动）：`get_proxy_groups`、`select_node <group> <node>` — 通过 `curl` 调用 Mihomo 外部控制器 API (`127.0.0.1:9090`)，这就是 `Depends` 要带 `curl` 的原因

核心下载版本硬编码为 `v1.19.28`（GitHub `MetaCubeX/mihomo`）。架构靠 `uname -m` 推断，且对 x86_64 会进一步检测微架构：支持 AVX2/BMI1/BMI2/FMA/F16C 的 v3 CPU 用 `amd64` 构建，否则用 `amd64-compatible` 兼容构建（J4125 等仅 v1/v2 的 CPU 必须用兼容构建，否则启动即崩溃）。

> 已知瑕疵：`case` 的 `*` 兜底分支 `Usage` 提示未同步 `get_proxy_groups`/`select_node`，新增子命令时记得更新帮助文本。

**LuCI 前端**：纯 LuCI JS API（`require view/form/fs/rpc/uci`），无 npm、无编译，源码即交付。dashboard 并发调 `check_core`/`get_proxies`/`get_proxy_groups`/`logread`，提供实时节点切换和 `metacube-xd` Web 控制台跳转（依赖 9090 端口）。settings.js 是标准 UCI `form.Map`。
