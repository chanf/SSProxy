#!/bin/bash

# ==============================================================================
# 软路由 IPK 自动部署与安装脚本
#
# 功能说明：
#   1. 自动寻找本地 dist/ 目录下最新构建生成的 luci-app-ssproxy_*.ipk 软件包。
#   2. 通过 SSH key 免密认证（需事先 ssh-copy-id 配置好），无需密码与 expect。
#   3. 通过 SCP 命令将软件包上传到软路由的临时目录 /tmp/。
#   4. 通过 SSH 命令在软路由上远程执行 opkg 安装，并自动重启 mihomo 服务使其即时生效。
# ==============================================================================

# 基础连接配置信息
ROUTER_IP="192.168.66.1"       # 软路由 LAN IP 地址
ROUTER_USER="root"             # SSH/SCP 登录用户名
ROUTER_PATH="/tmp/"            # 文件上传的目标临时目录

# SSH 公共选项：
#   BatchMode=yes                纯 key 免密认证，禁用任何交互式密码提示（key 未授权则立即失败，不挂起）。
#   StrictHostKeyChecking=accept-new  首次连接自动接受并记录主机指纹，避免 yes/no 交互。
SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=accept-new"

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
echo "目标路由器     : $ROUTER_USER@$ROUTER_IP"
echo "软路由临时路径 : $ROUTER_PATH"
echo "=================================================="

# ------------------------------------------------------------------------------
# 步骤 2：通过 SCP 上传 IPK 文件到软路由（SSH key 免密）
# ------------------------------------------------------------------------------
echo "正在上传 $IPK_BASENAME 到软路由..."

scp $SSH_OPTS "$LATEST_IPK" "$ROUTER_USER@$ROUTER_IP:$ROUTER_PATH"
SCP_STATUS=$?

# 判断上传状态是否成功，若失败则提前终止执行
if [ $SCP_STATUS -ne 0 ]; then
    echo "错误：SCP 上传文件失败，请检查 SSH key 是否已授权（ssh-copy-id）或网络是否可达。"
    exit $SCP_STATUS
fi
echo "成功：软件包上传完成。"

# ------------------------------------------------------------------------------
# 步骤 3：通过 SSH 执行安装命令并重启服务（SSH key 免密）
# ------------------------------------------------------------------------------
echo "--------------------------------------------------"
echo "正在软路由上安装该软件包并重启代理服务..."

# - opkg install: 强制覆盖安装刚刚上传的 IPK 包。
# - /etc/init.d/mihomo restart: 重启 Mihomo 服务载入最新 helper.sh 后端和 TProxy 拦截规则。
ssh $SSH_OPTS "$ROUTER_USER@$ROUTER_IP" "opkg install /tmp/$IPK_BASENAME && /etc/init.d/mihomo restart"
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
