#!/bin/bash

# ==============================================================================
# 软路由 IPK 自动部署与安装脚本
# 
# 功能说明：
#   1. 自动寻找本地 dist/ 目录下最新构建生成的 luci-app-ssproxy_*.ipk 软件包。
#   2. 使用 macOS 系统自带的 expect 自动化交互工具，安全自动地填充密码。
#   3. 通过 SCP 命令将软件包上传到软路由的临时目录 /tmp/。
#   4. 通过 SSH 命令在软路由上远程执行 opkg 安装，并自动重启 mihomo 服务使其即时生效。
# ==============================================================================

# 基础连接配置信息
# 密码不再硬编码：优先读环境变量 MIHOMO_DEPLOY_PASSWORD，未设置则交互式读取，
# 避免明文密码进入版本库。历史中曾硬编码的密码应已清除，请务必更改路由器密码。
if [ -z "$MIHOMO_DEPLOY_PASSWORD" ]; then
    read -s -p "请输入软路由 root 密码: " PASSWORD
    echo
else
    PASSWORD="$MIHOMO_DEPLOY_PASSWORD"
fi
ROUTER_IP="192.168.66.1"       # 软路由 LAN IP 地址
ROUTER_USER="root"             # SSH/SCP 登录用户名
ROUTER_PATH="/tmp/"            # 文件上传的目标临时目录

# ------------------------------------------------------------------------------
# 步骤 1：扫描并寻找最新构建生成的 .ipk 软件包
# ------------------------------------------------------------------------------
# 使用 ls -t 命令按“修改时间”从新到旧排列，匹配最新的 luci-app-ssproxy_*.ipk 文件并取第一行
LATEST_IPK=$(ls -t dist/luci-app-ssproxy_*.ipk 2>/dev/null | head -n 1)

# 若没有找到任何 IPK 文件，提示用户需要先执行编译构建脚本
if [ -z "$LATEST_IPK" ]; then
    echo "错误：未能在 dist/ 目录中找到任何 IPK 文件。请先执行 'python3 build_ipk.py' 进行构建。"
    exit 1
fi

# 获取 IPK 的纯文件名（去掉路径前缀），用于后续安装命令
IPK_BASENAME=$(basename "$LATEST_IPK")

echo "=================================================="
echo "发现最新构建包 : $LATEST_IPK"
echo "目标路由器 IP   : $ROUTER_USER@$ROUTER_IP"
echo "软路由临时路径  : $ROUTER_PATH"
echo "=================================================="

# ------------------------------------------------------------------------------
# 步骤 2：使用 expect 自动化上传 IPK 文件到软路由
# ------------------------------------------------------------------------------
echo "正在上传 $IPK_BASENAME 到软路由..."

# 启动 expect 执行内嵌交互逻辑：
# - spawn: 启动 scp 子进程进行网络文件拷贝。
# - expect {...}: 捕获控制台输出。
#   - "*yes/no*": 若是首次连接，捕获到 SSH 密钥指纹确认，自动输入 yes 并回车，接着继续等待密码（exp_continue）。
#   - "*password:*": 捕获到密码输入提示时，自动输入密码并回车。
# - expect eof: 等待 SCP 进程传输结束。
# - catch wait result: 捕获子进程的退出状态列表。
# - exit [lindex \$result 3]: 返回状态列表中第四个元素（即子进程的退出状态码）。
#   注意：在双引号内，必须使用 \$result 逃逸，防止 bash 尝试解析。
expect -c "
spawn scp \"$LATEST_IPK\" \"$ROUTER_USER@$ROUTER_IP:$ROUTER_PATH\"
expect {
    \"*yes/no*\" { send \"yes\r\"; exp_continue }
    \"*password:*\" { send \"$PASSWORD\r\" }
}
expect eof
catch wait result
exit [lindex \$result 3]
"
# 获取上面 expect 语句块执行退出后的状态码
SCP_STATUS=$?

# 判断上传状态是否成功，若失败则提前终止执行
if [ $SCP_STATUS -ne 0 ]; then
    echo "错误：SCP 上传文件失败，请检查网络或密码。"
    exit $SCP_STATUS
fi
echo "成功：软件包上传完成。"

# ------------------------------------------------------------------------------
# 步骤 3：连接 SSH 执行安装命令并重启服务
# ------------------------------------------------------------------------------
echo "--------------------------------------------------"
echo "正在软路由上安装该软件包并重启代理服务..."

# 使用 expect 自动化 SSH 连接并执行远程命令：
# - opkg install: 强制覆盖安装刚刚上传的 IPK 包。
# - /etc/init.d/mihomo restart: 重启 Mihomo 服务载入最新 helper.sh 后端和 TProxy 拦截规则。
expect -c "
spawn ssh \"$ROUTER_USER@$ROUTER_IP\" \"opkg install /tmp/$IPK_BASENAME && /etc/init.d/mihomo restart\"
expect {
    \"*yes/no*\" { send \"yes\r\"; exp_continue }
    \"*password:*\" { send \"$PASSWORD\r\" }
}
expect eof
catch wait result
exit [lindex \$result 3]
"
# 获取上面 SSH 命令执行的退出状态码
SSH_STATUS=$?

# 输出最终的安装及部署状态
if [ $SSH_STATUS -eq 0 ]; then
    echo "=================================================="
    echo "部署成功：安装完毕且 Mihomo service 已成功重启！"
    echo "=================================================="
else
    echo "错误：软路由上 opkg 安装过程执行失败。"
    exit $SSH_STATUS
fi
