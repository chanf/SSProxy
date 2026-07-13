# 开发总结：规则管理批量导入功能

> 本文档总结「规则管理」页面粘贴导入规则文件功能的开发过程、设计决策、实现要点与验证结果。

## 1. 背景与目标

「规则管理」页面此前只能逐条手工添加 UCI `mihomo_rule`（域名 + 动作），无法批量导入。用户希望粘贴一份 Mihomo 规则文件（如范例 `rules/config.yaml` 的 `rules:` 段），由系统拆成单条 UCI 规则进入列表统一管理。

**范围边界**（与用户对齐）：
- 导入方式：文本框粘贴（不做文件上传）。
- 拆分目标：单条 UCI 规则。
- 接受丢失：`IP-CIDR` / `IP-CIDR6` / `GEOIP` / `MATCH` / `RULE-SET` 等非域名规则无法映射，跳过并计数；只导入 `DOMAIN` / `DOMAIN-SUFFIX` / `DOMAIN-KEYWORD`。

## 2. 关键设计决策

### 扩展 UCI 模型新增 `rule_type`
现有 `mihomo_rule` 只有 `domain/action/group/src_ip/enabled/comment`，`emit_access_rules_yaml` 固定输出 `DOMAIN-SUFFIX`。范例文件大量是 `DOMAIN` / `DOMAIN-KEYWORD`，不改模型就会把它们错当成后缀匹配、丢失语义。

因此新增 `rule_type` 字段（`domain` / `suffix` / `keyword`，默认 `suffix`，**向后兼容老规则**——无该字段的旧规则按 suffix 处理，行为不变）。

### 单源改动
所有改动集中在 `build_ipk.py` 两个内嵌字符串：`helper.sh`（后端）与 `rules.js`（前端）。`prepare_config` 的注入位置与顺序不变。

## 3. 实现要点

### 后端 helper.sh
- `emit_access_rules_yaml`：读 `rule_type`，按值输出 `DOMAIN,` / `DOMAIN-SUFFIX,` / `DOMAIN-KEYWORD,`；action→policy 映射不变。
- `add_access_rule <ip> <domain> <action> <group> <rule_type>`：新增第 5 形参，默认 suffix，写入 `rule_type`。
- `get_access_rules`：JSON 增加 `rule_type`（空则默认 suffix）。
- **新增 `import_rules <text>`**：
  - 预清洗输入（去空白 / 去 `- ` 前缀 / 去首尾引号 / 丢弃空行、注释、裸 `rules:` 头）。
  - 按逗号取 `TYPE,VALUE,POLICY`：域名类建 UCI 条目（`DIRECT`→direct、`REJECT`→block、其它→proxy+group）；非域名类跳过计数。
  - 与已有规则按 `rule_type|domain|action|group` 去重。
  - 主循环用文件重定向（`done < file`）而非管道，确保计数器在当前 shell 内累加。
  - 单次 `uci commit`，输出 JSON `{"imported":N,"skipped":M,"duplicates":K,"skipped_samples":[...]}`。
- case 分发增 `import_rules)`；usage 补 `import_rules`。

### 前端 rules.js
- 新增表单加「匹配类型」select（精确 / 后缀(默认) / 关键字），标签改为「域名」；添加调用改为始终 5 参。
- 新增「批量导入规则」区块：textarea + 导入按钮，调 `import_rules`，弹通知显示计数摘要并刷新列表。
- `render_rules` 在域名旁加匹配类型徽标（颜色区分）。

### 踩坑：helper.sh 字符串转义
`build_ipk.py` 的 `helper.sh` 是**普通三引号字符串**（非 raw），写盘时 Python 会处理转义：
- `sed` 正则 `\.` `\(` `\)` 用**单反斜杠**（Python 保留未知转义），反向引用 `\1` 必须用**双反斜杠 `\\1`**（否则被当八进制转义）。
- `echo` JSON 内层双引号须在源码写成 `\\"`（写出磁盘为 `\"`，shell 才能正确解析）。
- 用 `od -c` 比对现有 `get_connections` 的 echo 行确认了上述规律，并以"写 snippet 文件 + Python 读取插入 + 4 空格转 Tab"的方式可靠落地，避免逐字符计数出错。

## 4. 测试与验证

| 维度 | 方法 | 结果 |
| :--- | :--- | :--- |
| 静态-后端 | `sh -n` 生成的 helper.sh | ✅ 语法 OK |
| 静态-前端 | `node --check` 生成的 rules.js | ✅ 语法 OK |
| 逻辑-import | 本地 `uci` stub 喂样本规则 | ✅ imported=3（suffix/domain/keyword 动作与 rule_type 正确）、skipped=3（IP-CIDR/GEOIP/MATCH）、JSON 合法 |
| 逻辑-emit | 本地 `uci` stub 四种组合 | ✅ direct/suffix→`DOMAIN-SUFFIX,...,DIRECT`；block/keyword→`DOMAIN-KEYWORD,...,REJECT`；proxy/domain→`DOMAIN,...,<group>`；老规则（无 rule_type）→`DOMAIN-SUFFIX` |
| 运行-路由器 | 部署后实跑 `import_rules` | ✅ 返回 `{"imported":3,"skipped":3,"duplicates":0,...}`，`uci show` 创建 3 条规则 |
| 构建+部署 | `python3 build_ipk.py` + `./deploy.sh` | ✅ 1.0.0-101 → 1.0.0-109，opkg 升级、服务重启、`prepare_config` SUCCESS |

## 5. 遗留与后续

- **路由器侧两项确认**（rule_type 落库值、注入 `/tmp/mihomo_run.yaml` 的 `rules:` 段首）因开发期频繁 SSH 触发路由器限流未再抓到输出；二者逻辑已由本地 uci-stub 全链路覆盖（import 写 rule_type、emit 按 rule_type 输出、prepare_config 注入未改动）。建议页面点一次「应用并重启核心」后在 `/tmp/mihomo_run.yaml` 复核。
- **浏览器点测**（粘贴 → 导入 → 列表徽标 → 应用重启）待用户在 UI 验证。
- **清理**：路由器上残留 3 条 `verify-*` 测试规则（DIRECT/REJECT，无害），可在「规则管理」页删除。
- **未支持**：IP-CIDR / GEOIP / MATCH / RULE-SET 等非域名规则导入（已确认接受）；未做导入规则的 group 合法性校验。

## 6. 关联文档
- [config-rules-guide.md](config-rules-guide.md)：范例配置抽象出的规则说明文档。
- [todo.md](../todo.md)：功能开发与测试进度快照。
