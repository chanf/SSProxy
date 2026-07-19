# 自动化闭环开发流程（luci-app-mihomo）

本仓库的开发在一个"构建 → 部署 → 自检 → 改 → 再循环"的闭环上进行。本文记录这条流水线、关键坑点、以及常用自检命令，方便复现与排障。

## 1. 总览

```
改 build_ipk.py（唯一源文件）
   │  python3 build_ipk.py        # 自动版本号自增，产出 dist/*.ipk
   ▼
./deploy.sh                       # expect 自动输密码，scp + opkg install + 重启服务
   │
   ▼
SSH 自检（免密 ssh root@192.168.66.1）  # 看日志 / 跑 helper.sh / curl 控制器 / uci / nft
   │  发现问题
   ▼
回到第一步
```

核心思想：**把"人工转日志、人工点页面"从循环里去掉**。改完代码一条命令上线，然后我自己 SSH 进路由器看真实状态、定位问题，再改。一两个循环就能收敛。

## 2. 构建与部署

- `python3 build_ipk.py` —— 仅用 Python3 标准库。`main()` 第一步 `increment_version()` 会**原地改写 `PKG_VERSION`**（如 `1.0.0-145 → 1.0.0-146`），所以构建后 `build_ipk.py` 一定有 diff，属预期。产出 `dist/luci-app-mihomo_<ver>_all.ipk`。
- `./deploy.sh` —— 用 macOS 自带 `expect` 自动输 root 密码，把最新 ipk `scp` 到路由器 `/tmp/`，再 `opkg install` + `/etc/init.d/mihomo restart`。
- 合并：`python3 build_ipk.py && ./deploy.sh`。

> **规则**：每次构建新版本后必须部署到软路由，保证远程测试环境与本地代码同步。

## 3. SSH 自检闭环（关键）

路由器 `root@192.168.66.1` 已授权本机公钥（`~/.ssh/id_ed25519.pub`），**免密**。这覆盖了 deploy.sh 之外的所有读/诊断/临时操作：

```sh
ssh root@192.168.66.1 '<远程命令>'      # 单条
ssh root@192.168.66.1                   # 进交互 shell
```

控制器（mihomo external-controller `:9090`）的 secret 存在 UCI：
```sh
SEC=$(ssh root@192.168.66.1 'uci -q get mihomo.config.secret')
```

## 4. 常用自检命令清单

```sh
# 版本 / 进程
ssh root@192.168.66.1 'opkg list-installed luci-app-mihomo; pgrep -a mihomo'

# 核心日志（mihomo stdout，init.d 重定向到此）
ssh root@192.168.66.1 'tail -80 /tmp/mihomo_core.log'
ssh root@192.168.66.1 'grep -E "level=(error|warning)" /tmp/mihomo_core.log | tail -20'

# 跑任意后端子命令
ssh root@192.168.66.1 '/usr/share/mihomo/helper.sh get_core_log'
ssh root@192.168.66.1 '/usr/share/mihomo/helper.sh get_traffic'

# 控制器 API（需 secret）
ssh root@192.168.66.1 'curl -s -H "Authorization: Bearer $(uci -q get mihomo.config.secret)" http://127.0.0.1:9090/proxies/FTQ'

# 当前运行配置（prepare_config 生成）
ssh root@192.168.66.1 'sed -n "/^rules:/,/^[a-z]/p" /tmp/mihomo_run.yaml | head -20'

# tproxy / 路由 / FD
ssh root@192.168.66.1 'nft list table inet mihomo'
ssh root@192.168.66.1 'ip rule; ip route show table 100'
ssh root@192.168.66.1 'pid=$(pgrep mihomo|head -1); grep "open files" /proc/$pid/limits; ls /proc/$pid/fd | wc -l'

# 连通性（走代理 mixed-port 7890）
ssh root@192.168.66.1 'curl -x http://127.0.0.1:7890 -I -m 8 -o /dev/null -s -w "code=%{http_code} t=%{time_total}\n" https://www.google.com'
```

## 5. 关键坑点（踩过的）

### 5.1 build_ipk.py 是普通三引号 Python 串 —— 反斜杠会被吃掉
内嵌的 shell/JS 用 `\t` 缩进、写盘时解释成真实 Tab。但 **`\1`（sed 反引用）、`\n` 也会被当 Python 转义吃掉**，导致写出的脚本静默坏掉。
- 现象：`sed 's|.*://\(.*\)|\1|'` 里的 `\1` 变成空，host 提取永远为空。
- 对策：sed 反引用写成 `\\1`，或**改用 shell 参数展开**（`${url#*://}` 等）彻底回避。awk 里的 `\t` 同理写 `\\t`。

### 5.2 mihomo 控制器 SAFE_PATHS
`PUT /configs?force=true` 只允许从 **`/etc/mihomo`** 重载，不能重载 `/tmp/mihomo_run.yaml`（报 `path is not subpath of ... SAFE_PATHS`）。
- 对策：想"热改规则试试"行不通；必须改源 `/etc/mihomo/config.yaml`（或走 `prepare_config`）+ `/etc/init.d/mihomo restart`。

### 5.3 fake-ip + dns_hijack 会污染路由器自身 DNS
`dns_hijack` 开时，dnsmasq 把所有查询转给 mihomo:1053，fake-ip 模式下**任何域名都返回 198.18.x.x**——包括"更新订阅"时 curl 解析订阅 host。于是订阅 host 连不上、死循环。
- 对策：`update_subscription` 已改为直连公共 DNS（`nslookup host 223.5.5.5`，路由器本机 UDP 不经 tproxy）拿真实 IP，再 `curl --resolve` 强制。`clear_subscription` 删前会备份 `config.yaml.bak`，下载失败时自动回滚。

### 5.4 FD 上限
procd 默认 `nofile=4095`，被大量 UDP/P2P 连接撑爆会报 `too many open files`，所有新连接失败。
- 对策：init.d 启动核心前 `ulimit -Hn 65535; ulimit -n 65535`；并把 IPv6 组播 `ff00::/8` 等排除出 tproxy，减少无谓 socket。

### 5.5 版本号与 conffiles
- 版本号唯一来源是顶部 `PKG_VERSION`（`CONTROL/control` 里写死的会被 `create_source_tree` 正则替换）。
- `/etc/config/mihomo` 是 conffile（opkg 升级保留用户设置）；**`/etc/mihomo/config.yaml` 不是**（订阅用户数据，绝不打进包）。

## 6. 端到端验证清单（改完一个功能后走一遍）

1. `python3 build_ipk.py && ./deploy.sh` 无报错。
2. `sh -n src/root/usr/share/mihomo/helper.sh`、`node -e "$(cat src/root/www/.../dashboard.js)"` 或 `node --check` 无语法错。
3. SSH 自检：进程在跑、日志无 fatal/error、`get_*` 子命令返回合法 JSON。
4. 浏览器（Ctrl+F5 清缓存）页面功能正常。
5. 边界 / 回归：重启服务后状态正确；动过的旧功能仍可用。

## 7. 四个后台 procd 实例（init.d 拉起）

| 实例 | 作用 | 频率 |
|---|---|---|
| 核心 mihomo | 代理核心，`-f /tmp/mihomo_run.yaml` | 常驻 |
| `collect_loop` | 实时连接去重落 `/tmp/mihomo_access.log` | 每 15s |
| `auto_update_loop` | 订阅自动更新（自包含，不用 cron） | 每 10min 轮询 |
| `traffic_loop` | 代理流量统计累计（总量永不清 + 按域名可清零） | 每 5s |

---

闭环的价值：**改代码 → 一键上线 → 自己看真实状态 → 再改**，中间不靠人工转述。排障时这条链路能把"猜测"换成"实测"。
