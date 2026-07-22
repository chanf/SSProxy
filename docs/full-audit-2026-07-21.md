# 全量代码审核 · luci-app-ssproxy

> 实施状态（2026-07-22，v1.0.0-197）：本报告的 H1、M1-M6、L1-L4 及 JSON/YAML 输入边界加固均已在 `build_ipk.py` 落地。自动化测试为 167 项；六种 TProxy/TUN/DNS/ACL 模式、非法配置回滚、DNS 恢复和链式状态闭环已在真实路由器验收。官方核心实网下载因测试路由器无法连接 `github.com:443` 未完成端到端下载，但固定 SHA256、TLS、失败清理和旧核心保护已有自动化与真机失败路径覆盖。详见 [优化实施记录-2026-07-22.md](./优化实施记录-2026-07-22.md)。

## 整改结论

| 项目 | 状态 | 实施摘要 |
| --- | --- | --- |
| H1 | 已完成 | 后台自动更新使用 `return`，不再退出循环 |
| M1-M4 | 已完成 | JSON 转义、实际端口、`rules: []`、最终运行配置校验均已覆盖 |
| M5 | 已完成 | 移除 `curl -k`；官方核心固定 GitHub Release digest；自定义核心强制 HTTPS + SHA256；原子替换 |
| M6 | 已完成 | 单一遥测循环复用 `/connections` 快照，字段批量导出并由 awk 聚合 |
| L1 | 已完成 | controller Secret 写入 0600 临时 header 文件，不出现在 curl 参数 |
| L2 | 已完成 | gzip/tar 三层句柄由上下文管理器统一关闭 |
| L3 | 已完成 | DNS 劫持启停去重、保存并恢复原始配置 |
| L4 | 已完成 | 仅注入被有效启用链路引用的落地节点 |

> 项目：luci-app-ssproxy（水杉代理）
> 日期：2026-07-21
> 基线版本：1.0.0-189 前后
> 范围：Python 构建器 + `helper.sh` 后端 + LuCI 前端（**全项目**，不限于链式代理）
> 关联：链式代理专项评审见 [`chain-proxy-optimization-review.md`](./chain-proxy-optimization-review.md)；其中 §4.3 / §5.2 / §5.5 与本文件 L4 / M4 / M6 重叠

---

## 审核方式

并行审查 agent 全部触发速率 / token 限制失败，改为人工逐模块通读。

- **逐行通读**：Python 构建器、`init.d`、`prepare_config`、链式代理块、订阅 / 实时控制 / 流量、`get_proxies`
- **风险模式 grep 覆盖（非逐行）**：`case` 分发（`build_ipk.py` 1810–2727）、7 个 JS 视图（`innerHTML` / `setInterval` / `fs.exec` / `exit`）

严重度：🔴 HIGH（影响功能正确性 / 安全）／🟠 MEDIUM（特定条件下出错或性能问题）／🟡 LOW（健壮性 / 卫生问题）。

---

## 🔴 HIGH

### H1. 自动更新循环被 `exit 0` 杀死——后台循环实际没在循环
`build_ipk.py:673` / `:675` / `:685`

`auto_update_now` 内有三处 `exit 0`（关闭时 / 无 url 时 / 间隔未到时）。该函数现由后台 `auto_update_loop`（`:662`）当普通函数调用，而 `exit` 会**终止整个 helper.sh 进程**。循环第一次遇到「未到时间」（最常见情形）即整体退出。注释 `# Called hourly by cron`（`:669`）是旧 cron 时代的残留，改成自包含 loop 后漏改。

现状「看似还在更新」是因为 procd `respawn` 把退出的进程又拉起、意外充当循环——但会产生 respawn 日志噪声，且若 procd 判定频繁退出为 flapping 而放弃重启，**自动更新会永久停止**；自动更新关闭时更每 600s 空转一次。

**修法**：三处 `exit 0` → `return 0`（1 行级改动）。

---

## 🟠 MEDIUM

### M1. `get_proxies` 用 `printf %s` 拼 JSON，节点名未转义
`build_ipk.py:1540` / `:1545` / `:1556`

`printf("  {\"name\":\"%s\",…}", name, type, server)` 直接把节点名塞进 JSON 字符串。订阅节点名含 `"` 或 `\` 时整个 JSON 非法，前端 `JSON.parse` 失败 → 仪表盘节点列表全空。属 CLAUDE.md 记载的同类历史坑（`get_proxies` 引号问题曾让整个 helper.sh 加载失败），改 awk 后未补 JSON 转义。

**修法**：输出前 `gsub` 转义 `"` / `\` / 控制字符。

### M2. `update_subscription` 的 `--resolve` 写死 443 端口
`build_ipk.py:570`

`resolve_arg="--resolve ${host}:443:${realip}"`。订阅为 `http://` 或非 443 端口时 `--resolve` 不生效，curl 回退 DNS；而 DNS 劫持开启时 dnsmasq 返回 fake-ip，下载失败或打到假 IP。

**修法**：从 URL 解析实际端口（https 默认 443、http 默认 80），分别构造 `--resolve host:PORT:ip`。

### M3. `prepare_config` 未处理 `rules: []`（与 `proxies: []` 不对称）
`build_ipk.py:1344-1358`（对照 `proxies: []` 专门分支 `:1378`）

代码对 `proxies: []` 有专门改写，但 rules 注入的 awk 只匹配 `^rules:`。若订阅含 `rules: []`（占位空规则，常见），注入后变成 `rules: []` 紧跟 `  - '…'` 列表项 = 非法 YAML → 核心 fatal-exit。

**修法**：rules 注入前同样识别 `rules: []` 并改写为 `rules:`。

### M4. 数据链路 rules vs proxies 校验源不一致 → 规则悬空
`build_ipk.py:1339` vs `:1396` / `:954`

`emit_data_link_rules_yaml` 用 `src_config` 校验；`emit_data_link_proxies_yaml` 用 `run_config` 且在 `:954` 又 `effective_data_link_node … || continue` 复查。mixed / custom 模式下两者节点集合可能不同 → 规则生成了但代理被跳过 → `SRC-IP-CIDR,device,ssproxy-chain-X` 指向不存在的代理 → 设备落回策略组。

> 与链式代理专项评审 §5.2 同源，详见 [`chain-proxy-optimization-review.md`](./chain-proxy-optimization-review.md#52-链路校验所依据的配置文件p1)。

**修法**：两处统一以最终 `run_config` 的节点集合校验。

### M5. 核心 / 订阅 / Geo 下载全部 `curl -k`，核心二进制无校验
`build_ipk.py:457` / `:471` / `:509` / `:572`

禁用 TLS 校验下载 mihomo 核心（最敏感资产），且 tarball 分支 `find … -executable | head -1` 取首个可执行文件、无 checksum / 签名。MITM 可替换核心二进制 → 供应链风险。`-k` 在路由器（时钟 / 证书问题）有现实理由，但核心下载建议至少校验 sha256。

### M6. 连接采集 O(n²)，低端 x86 压力大
`build_ipk.py:1609-1624` / `:1660-1667`

`flatten_connections` 对每条连接、每字段 `echo "$x" | sed -n "${n}p"`（9 字段 = 9 次 subshell × 连接数）；`collect_connections` 再对每条 `grep -qxF "$id" "$seenf"`（seen 文件最多 2000 行）。连接多时每 5–15s 一次采集产生数千次 fork，拖累 J4125 等软路由。

> 与链式代理专项评审 §5.5 同类，详见 [`chain-proxy-optimization-review.md`](./chain-proxy-optimization-review.md#55-流量采集性能p2)。

**修法**：一次 `jsonfilter` / awk 导出结构化行后单次聚合，复用同一份解析结果。

---

## 🟡 LOW

### L1. 控制器 secret 出现在进程命令行
`build_ipk.py:370` — `curl -H "Authorization: Bearer $API_SECRET"` 在 `ps` 可见。单用户路由器影响小，建议改 `--config` 文件或 `-H @file`。

### L2. tar 打包用裸 `open()` 无 `with`
`build_ipk.py:5548` / `:5599` — `make_tar_gz` / `write_tar_gz_outer_archive` 异常时文件句柄泄漏。建议 `with` + try/finally。

### L3. `enable_dns_hijack` 不去重
`build_ipk.py:163` — `uci add_list … server=` 每次启动追加；走 reload（非 restart）路径时可能产生重复 dnsmasq server 条目。

### L4. 所有合法落地节点都被注入 `proxies:`
`build_ipk.py:1375` ——即使无数据链路引用也全量注入 `ssproxy-landing-*`，配置膨胀且易让用户误以为「添加落地 = 全局生效」。

> 即链式代理专项评审 §4.3，详见 [`chain-proxy-optimization-review.md`](./chain-proxy-optimization-review.md#43-落地节点全量注入可能污染语义p1)。

---

## ✅ 做得好的地方（应保留）

- **XSS 防护一致**：`accesslog.js` / `rules.js` 各自定义 `esc()` 并在 `innerHTML` 处使用；DOM 一律走 `E()`，动态数据走 text node。`group_options`、连接错误文案均经转义。
- **定时器清理一致**：accesslog / dashboard / traffic / chain 四个有 `setInterval` 的视图，均在 `unload` 里 `clearInterval`，无累积泄漏。
- **可复现构建扎实**：gzip header 固定 mtime、tar 强制 root:root + `mtime=1700000000` + `./` 前缀 + 排序，逐字节一致。
- **订阅健壮性**：`get_proxies` 对未下载 / 空 / HTML 拦截页 / 空节点均返回结构化中文提示；`update_subscription` 有备份 + 失败回滚 + fake-ip 旁路。
- **失败不拖垮核心**：链式代理 / 访问规则校验失败只 `chain_log` 并跳过，不让核心 fatal-exit。

---

## 建议优先级

| 序号 | 项 | 严重度 | 改动量 |
| --- | --- | --- | --- |
| H1 | `auto_update_now` 三处 `exit 0` → `return 0` | HIGH | 1 行级 |
| M3 | `prepare_config` 处理 `rules: []` | MED | 小 |
| M1 | `get_proxies` JSON 转义节点名 | MED | 小 |
| M4 | 数据链路校验源统一为 `run_config` | MED | 小 |
| M2 | `--resolve` 按实际端口 | MED | 小 |
| M5 | 核心下载 sha256 校验 | MED | 中 |
| M6 | 连接采集合并解析 | MED | 中 |

> H1 影响自动更新可靠性且近乎 1 行修复，建议最先处理。
