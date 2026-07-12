# Mihomo 分流策略组管理 设计文档

> 状态：**已实现（随「运行状态」仪表盘一并提供，早于访问日志模块）**
> 模块：`luci-app-mihomo` → 「运行状态」页面内的「分流策略组管理」区
> 适用：OpenWrt + LuCI，依赖 Mihomo 核心已启动且外部控制器 `127.0.0.1:9090` 可达

---

## 1. 文档信息

| 项 | 内容 |
|---|---|
| 模块定位 | 仪表盘（dashboard）内的一个区段，非独立菜单页 |
| 入口路径 | LuCI → 服务 → Mihomo 代理 → 运行状态 →「分流策略组管理 (实时切换节点)」 |
| 核心能力 | 列出订阅中所有 Selector 策略组，下拉切换每组当前选中的节点，热生效、无需重启核心 |
| 实现载体 | `helper.sh` 两个子命令（`get_proxy_groups` / `select_node`）+ `dashboard.js` 的 `proxy_groups_panel` |
| 数据源 | Mihomo 外部控制器 `127.0.0.1:9090`：`GET /proxies`、`PUT /proxies/{group}` |
| 变更生效 | 即时生效（核心内存中切换，不走 `prepare_config`、不重启、不断流） |

---

## 2. 背景与目标

订阅配置里的 `proxy-groups` 决定流量分流到哪个节点。其中 `select` 类型组（Selector）允许手动指定当前走哪个节点；`url-test` / `fallback` / `load-balance` 等组由核心按延时/可用性自动选择，不可手动指定。

原插件仅在「节点列表」展示静态节点，用户无法在 LuCI 内切换某组分流到哪个节点，只能跳转第三方 Web 控制台（metacube-xd）。本功能解决三件事：

1. **就地切换**：在路由器管理界面内直接选择每个 Selector 组的出口节点，免跳转。
2. **状态可见**：展示每个组的类型与「当前选中节点（now）」，切换后立即可见。
3. **热生效**：利用核心外部控制器的 PUT 接口，切换在内存中即时完成，不触发核心重启、不中断现有代理状态。

---

## 3. 关键决策（已确认）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 可切换范围 | 仅 Selector 类型组可手动切换；URLTest/Fallback/LoadBalance 等自动组只读、不下拉（核心不接受其 PUT）。 |
| 2 | 生效方式 | 即时热切换（PUT /proxies/{group}），与访问日志规则「需重启核心」相反；不走 prepare_config、不写 UCI。 |
| 3 | 控制器可达性判定 | get_proxy_groups 在 curl 失败时回显哨兵 `{"proxies":{}}`；前端据此判断核心是否运行/控制器是否可达。 |
| 4 | 不持久化 | 当前选中节点不写 UCI。核心重启后回到订阅里每组定义的默认节点（Mihomo 本身行为，本模块不覆盖）。 |
| 5 | 面板可见条件 | 控制器可达 且 至少存在一个 Selector 组时才显示切换面板；否则显示降级提示。 |
| 6 | 反馈方式 | 切换结果以 LuCI 通知（info/danger）呈现，不自动刷新整页。 |

---

## 4. 总体架构

```
+-----------------------------+
|  dashboard.js (panel)       |
|   load():   get_proxy_groups|
|   change(): select_node g n |
+----+-----------------+------+
     | GET /proxies    | PUT /proxies/{group}
     v                 v
   +--------------------------+
   |  helper.sh               |
   |  get_proxy_groups /      |
   |  select_node             |
   +-----------+--------------+
               | curl 127.0.0.1:9090
               v
        +---------------+
        | Mihomo 核心    |
        | 控制器 :9090   |
        +---------------+
```

数据流：
1. 进入「运行状态」页，`load()` 并发拉取多项数据，其中 `get_proxy_groups` 取 `GET /proxies` 的全量分组 JSON。
2. `render()` 解析 `proxies` 字段，筛出所有 `type === 'Selector'` 的组，每组渲染一个 `<select>`：选项 = `group.all`（该组全部可选节点），默认选中 = `group.now`（当前节点）。
3. 用户切换下拉 → 调 `select_node <group> <node>` → `PUT /proxies/{group}` 体 `{"name":"<node>"}` → 核心即时切换该组出口。
4. 切换成功/失败以通知反馈；不刷新页面（避免打断，且核心状态已是最新）。
5. 若 `get_proxy_groups` 返回哨兵 `{"proxies":{}}`（控制器不可达），整块面板替换为「核心未运行/控制器不可达」提示。

---

## 5. 数据来源与技术约束

### 5.1 Mihomo 外部控制器 API
- `GET /proxies` 返回 `{"proxies": { "<组名>": { "type": "Selector|URLTest|Fallback|LoadBalance|Direct|Reject", "now": "<当前节点>", "all": ["<节点>", ...] } }}`。注意顶层包了一层 `proxies`。
- `PUT /proxies/{name}` 体 `{"name":"<节点名>"}` 仅对 Selector 组有效，立即改变其 `now`；对自动类组返回错误。
- 控制器地址/端口由 `prepare_config` 固定写入 `external-controller: 0.0.0.0:9090`。

### 5.2 硬约束
- **依赖核心运行**：核心未启动时 9090 不可达，面板自动降级为提示，不报错。
- **Selector 以外的组不可切**：核心对自动类组的 PUT 返回错误，故前端只对 Selector 渲染下拉。
- **本功能不依赖 TProxy/TUN**：切换只改变后续分流决策；实际是否走代理取决于透明代理是否启用。

---

## 6. 策略组模型与限制

### 6.1 组类型与可操作性
| 类型 | 来源 | 可手动切换 | 本模块处理 |
|---|---|---|---|
| Selector | 订阅 `proxy-groups` 里 `type: select` | 是 | 渲染下拉，PUT 切换 |
| URLTest / Fallback / LoadBalance | 订阅里自动类 | 否 | 不渲染（核心自动选） |
| Direct / Reject | 核心内置 | 否 | 不渲染 |

### 6.2 与其他模块的关系
- **访问日志模块**：其「代理」动作默认取订阅里第一个 Selector 组作为出口（见 `access-log-design.md` §3 决策 3）。本模块让用户能即时调整该组实际走哪个节点，两者互补。
- **prepare_config**：本功能**完全绕过** prepare_config 与 `/tmp/mihomo_run.yaml`——切换是运行时内存操作，不落配置文件。

### 6.3 不持久化（重启回退）
切换结果只存在于核心运行内存。**核心重启后，每组回到订阅配置定义的默认节点**（通常是 `proxies` 列表首位）。本模块刻意不把选择写 UCI，因为持久化需写入订阅配置或单独存储并在 `prepare_config` 注入，会与订阅自动更新产生覆盖冲突；且多数用户期望「重启回到订阅默认」这一可预测行为。若需持久化偏好，见 §13。

---

## 7. 后端设计（`helper.sh` 子命令规格）

### 7.1 `get_proxy_groups`
- 行为：`curl -s -m 2 http://127.0.0.1:9090/proxies`；2s 超时。
- 成功：直接把核心返回的 JSON（含顶层 `proxies`）打到 stdout（curl 输出即函数输出）。
- 失败（curl 非零退出）：回显哨兵 `{"proxies":{}}`，作为「控制器不可达」的统一信号。
- 无需鉴权（`external-controller` 未设 `secret`；若未来启用 secret，需在此与 `select_node` 补 `Authorization` 头）。

### 7.2 `select_node <group> <node>`
- 参数：`group`、`node` 均必填，缺一报错退出 1。
- 行为：使用 `curl` 执行 `PUT` 请求，并捕获响应体与退出状态码。
- 成功：核心返回 `204 No Content`，无 body，退出码为 0。
- 失败处理（可靠性设计）：
  1. 若 `curl` 因网络/连接问题退出非零，则输出错误信息至 stderr 并返回该退出码。
  2. 若 `curl` 成功通信但核心返回 `4xx`（例如组非 Selector、组/节点名不存在，此时返回非空 JSON 错误体），则将响应体输出至 stderr 并退出为 1。
  3. 前端据此能通过 `res.code !== 0` 准确捕捉到任何错误，并在提示框中回显 stderr 里的具体错误原因。

> 实现注意（已踩坑固化）：`echo`/`curl -d` 里的 JSON 双引号在 Python 源中写成 `\\"`，写出文件后为 `\"`；否则部署后被 shell 提前闭合导致整脚本 syntax error 无法加载。

---

## 8. 前端设计（`dashboard.js` 的 `proxy_groups_panel`）

### 8.1 数据获取与解析
- `load()` 中 `get_proxy_groups` 为第 5 个并发请求（results[4]）。
- `proxy_groups = JSON.parse(raw).proxies || {}`。
- `controller_up = raw.indexOf('"proxies":{}') === -1`（哨兵缺席即可达）。

### 8.2 状态徽章联动
控制器可达性同时驱动顶部状态徽章：可达 → RUNNING；不可达但 procd 实例在 → 运行异常；否则 → STOPPED。即「策略组面板能否切换」与「整体运行状态判定」共用同一信号源。

### 8.3 渲染逻辑
- 遍历 `Object.keys(proxy_groups)`，对每个 `g.type === 'Selector'` 的组：
  - 选项：遍历 `g.all`，`<option value=节点 selected=(节点===g.now)>`。
  - 绑定 `change`：读取 `data-group` 与选中值 → `fs.exec(helper.sh ['select_node', group, node])` → 通知结果。
- 统计 `selector_groups_count`。

### 8.4 面板可见性
- `controller_up && selector_groups_count > 0` → 渲染策略组表格（组名 / 类型徽章 / 节点下拉）。
- 否则 → 渲染降级提示卡片：
  - 控制器可达但无 Selector 组：「该订阅配置中暂无可选的策略组(selector)。」
  - 控制器不可达：「核心未运行或控制器不可达……请先启动并刷新。」

---

## 9. 控制器可达性判定（哨兵约定）

`get_proxy_groups` 的回退值 `{"proxies":{}}` 是本模块的「不可达哨兵」：
- 前端用**字符串包含判定**（`indexOf('"proxies":{}')`）而非 JSON 字段，避免核心恰好返回空 proxies 时误判。
- 该判定同时服务于状态徽章与节点延时测试按钮的显隐（控制器不可达时隐藏「测试」按钮）。
- 设计取舍：用固定哨兵字符串而非 HTTP 状态码，是因为 `fs.exec` 通道只回传 stdout，curl 的退出码/HTTP 码不直接暴露给前端；哨兵是最简的统一信号。

---

## 10. 权限与 ACL

`root/usr/share/rpcd/acl.d/luci-app-mihomo.json` 已覆盖所需权限，无需新增：
- `write.file.exec` 含 `/usr/share/mihomo/helper.sh`（`get_proxy_groups`/`select_node` 均经此执行）。
- 不需要 `read.uci`/`write.uci`（本功能不读写 UCI）。

---

## 11. 构建与部署

- 所有交付文件内嵌于 `build_ipk.py` 的 `src_files`；改实现就改对应字符串，**不要手改 `src/`**。
- `python build_ipk.py` 产出 `dist/luci-app-mihomo_<version>_all.ipk`；`PKG_VERSION` 每次构建自增（预期 diff）。
- 反斜杠转义约定见 §7.2 注意事项（与全仓一致）。

---

## 12. 验证清单

下表为上线/改动后应在真机核对的验证项，**「已执行」栏如实标注**；未执行项需在带运行核心的路由器上验证。

| # | 验证项 | 已执行 |
|---|---|---|
| 1 | `sh -n` 校验 `helper.sh` 语法通过（含 `select_node` 的 JSON body 转义） | ✅ 本地实跑通过 |
| 2 | 核心运行时 `get_proxy_groups` 返回合法 `proxies` JSON；核心停止时回退哨兵 `{"proxies":{}}` | ⬜ 待真机验证 |
| 3 | `select_node <group> <node>` 对 Selector 组返回成功、组内 `now` 随之改变；对自动类组/不存在的组名返回 4xx，前端正确提示失败 | ⬜ 待真机验证 |
| 4 | `dashboard.js` 控制器不可达时面板正确降级为提示，不抛异常 | ⬜ 待真机验证（未在浏览器实跑） |

> 说明：除第 1 项在本地 `sh -n` 实跑通过外，其余项依赖运行中的 Mihomo 核心 / 浏览器环境，文档作者未亲自执行，留作真机自测对照清单，不构成"已验证"结论。

---

## 13. 已知限制与后续规划

### 已知限制
1. **组名/节点名未做 URL 编码**：`select_node` 把 `${group}` 原样拼进 URL 路径。含空格/中文/特殊字符的组名（部分订阅的组名含 emoji 或空格）可能导致 PUT 失败。对照 `test_node_delay` 已用 `urlencode`，本处尚未对齐（见后续规划）。
2. **JSON body 未转义节点名**：`-d "{\"name\":\"${node}\"}"` 当节点名含双引号时会破坏 JSON；实际节点名极少含引号，属低概率。
3. **不持久化**：核心重启回退到订阅默认节点（见 §6.3）。
4. **仅 Selector 可切**：自动类组不可手动指定（核心限制）。
5. **无组内延时展示**：切换面板不显示各候选节点延时（延时测试在「节点列表」区，按节点而非按组）。
6. **无 secret 鉴权支持**：若用户在订阅里给 `external-controller` 设了 `secret`，本模块的 curl 调用会因未带 Authorization 头而失败。

### 后续规划
- 对 `select_node` 的 group/node 做 URL 编码，对齐 `test_node_delay`，修复含特殊字符组名/节点名的切换失败。
- 支持 `external-controller` 的 `secret`：从运行配置或 UCI 读取并在 curl 补鉴权头。
- 组内候选节点延时展示（复用 `test_all_nodes` 思路，限定组内节点）。
- 可选「记住选择」：把用户切换持久化到 UCI，并在 `prepare_config` 注入到订阅 `proxy-groups` 的对应组默认值（需处理与订阅自动更新的覆盖冲突）。

---

*本文档为已实现的权威设计说明，覆盖目标、架构、后端/前端规格、API 语义、可达性判定、限制与后续规划。*
