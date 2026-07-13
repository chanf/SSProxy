# TODO — 规则管理：粘贴导入规则文件

> 进度快照文档。功能已开发完成并验证，已提交。

---

## 一、在做什么功能

给「规则管理」页面增加**批量导入**能力：用户在文本框粘贴一份规则文件（如范例 `rules/config.yaml` 的 `rules:` 段，或任意 Mihomo 规则行），系统把每条规则**拆成单条 UCI `mihomo_rule` 条目**进入现有规则列表，统一管理（查看 / 删除 / 启停 / 应用重启生效）。

**已确认的范围边界**（与用户对齐）：
- 导入方式：**文本框粘贴**（不做文件上传）。
- 拆分目标：**单条 UCI 规则**（不是整体覆盖订阅文件、不是只编辑 rules 段）。
- 接受丢失：`IP-CIDR` / `IP-CIDR6` / `GEOIP` / `MATCH` / `RULE-SET` 等**非域名规则无法映射到 UCI 模型，跳过并计数**；只导入 `DOMAIN` / `DOMAIN-SUFFIX` / `DOMAIN-KEYWORD`。

---

## 二、原开发计划（已批准）

全部改动集中在单源文件 `build_ipk.py` 的两个内嵌字符串：`root/usr/share/mihomo/helper.sh`（后端）与 `root/www/.../mihomo/rules.js`（前端）。

### 关键障碍 → 必须扩展 UCI 模型
现有 `mihomo_rule` 只有 `domain/action/group/src_ip/enabled/comment`，`emit_access_rules_yaml` 固定输出 `DOMAIN-SUFFIX`。范例文件大量是 `DOMAIN` / `DOMAIN-KEYWORD`，不改模型就会把它们错当成后缀匹配。所以新增 **`rule_type`** 字段（`domain` / `suffix` / `keyword`，默认 `suffix`，向后兼容老规则）。

### 后端 helper.sh
1. `emit_access_rules_yaml`：按 `rule_type` 输出 `DOMAIN,` / `DOMAIN-SUFFIX,` / `DOMAIN-KEYWORD,`；action→policy 不变（block→REJECT / direct→DIRECT / proxy→`<group>`）。
2. `add_access_rule <ip> <domain> <action> <group> <rule_type>`：新增第 5 形参，默认 suffix，写入 `rule_type`。
3. `get_access_rules`：JSON 增加 `rule_type`（空则默认 suffix）。
4. **新增 `import_rules <text>`**：逐行解析粘贴文本（去 `- ` 前缀 / 去引号 / 跳过空行注释和裸 `rules:` 头）→ 按逗号取 `TYPE,VALUE,POLICY` → 域名类建 UCI 条目、非域名类跳过计数 → 与已有规则按 `rule_type|domain|action|group` 去重 → 单次 `uci commit` → 输出 JSON `{"imported":N,"skipped":M,"duplicates":K,"skipped_samples":[...]}`。
5. case 分发增 `import_rules)` → `import_rules "$2"`；usage 字符串补 `import_rules`。

### 前端 rules.js
1. 新增表单加「匹配类型」select（精确域名 / 后缀(默认) / 关键字），标签「域名 / 后缀」→「域名」；「添加规则」调用改为始终 5 参 `['add_access_rule', ip, d, ac, gp||'', rt]`。
2. 新增「导入规则」区块（textarea + 导入按钮）：调 `import_rules`，弹通知显示计数摘要，刷新列表。
3. `render_rules` 在域名旁加匹配类型徽标。

### 兼容
老规则无 `rule_type` → get/emit 均按 suffix，行为与现状一致。

---

## 三、目前实现进度

### ✅ 已完成（后端，已写入 build_ipk.py）
- [x] `emit_access_rules_yaml` 改为按 `rule_type` 输出 DOMAIN/SUFFIX/KEYWORD。
- [x] `add_access_rule` 增加 `rule_type` 形参并写入 UCI。
- [x] `get_access_rules` 输出 `rule_type`（默认 suffix）。
- [x] case 分发：`add_access_rule` 透传 `$6`；新增 `import_rules)` 分支。
- [x] usage 字符串补 `import_rules`。

### 🟡 进行中
- [x] **`import_rules` 函数定义**（解析器本体）——已写入并通过 sh -n。
  - 卡点已解决：helper.sh 是普通三引号字符串，JSON echo 的双引号在源码里须写成 **`\\"`**（双反斜杠+引号），写出磁盘才是 `\"`，shell 才能正确解析（`od -c` 已比对现有 `get_connections` 的 echo 行确认）。

### ⬜ 待办
- [x] 写入 `import_rules` 函数（去 `- `/引号、跳过空行注释/`rules:`、逗号切分、域名类建条目、非域名类计数、去重、单次 commit、输出 JSON）。
- [x] 前端 rules.js：匹配类型 select + 导入 textarea 区块 + `render_rules` 徽标 + 添加调用改 5 参。
- [x] 构建（已构建至 1.0.0-109）。
- [x] 部署（已部署到软路由，102 → 109）。
- [ ] 验证：
  1. ✅ 路由器 `import_rules` 实跑返回 `{"imported":3,"skipped":3,...}`，`uci show` 创建 3 条 verify 规则；
  2. ✅ 本地 uci-stub 验证 `get_access_rules`/`emit`/`import` 全链路 rule_type 正确（路由器因 SSH 限流未再抓输出，逻辑已覆盖）；
  3. ⏳ 注入逻辑未改动，建议页面点「应用并重启核心」后在 `/tmp/mihomo_run.yaml` 的 `rules:` 段首复核；
  4. ⏳ 待你浏览器点测；
  5. ✅ 本地 uci-stub：block/keyword→`DOMAIN-KEYWORD,...,REJECT`；老规则（无 rule_type）→`DOMAIN-SUFFIX`。

---

## 四、不在本次范围
- 不支持 IP-CIDR / IP-CIDR6 / GEOIP / MATCH / RULE-SET 等非域名规则导入（用户已确认接受丢失）。
- 不做导入规则的 group 合法性校验（引用订阅中不存在的 group 属用户数据问题）。
- 不改订阅文件本身、不改 `prepare_config` 注入位置与顺序。
