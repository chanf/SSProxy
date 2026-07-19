# LuCI 插件自动化闭环开发流程（通用自包含指南）

> 本文是一份**完全自包含**的通用指南。下文内联了「打包器 / 版本自增 / 自动发布说明 / 一键部署」的全部可复用核心代码——**复制即可起步一个全新的 iStoreOS / OpenWrt LuCI 插件**，无需参考任何外部项目。把示例里的 `myplugin` 换成你的包名即可。

---

## 0. 闭环一句话

```
改 src_files 字符串  →  python3 build_ipk.py（版本号自动 +1，并生成 releaseNote）  →  ./deploy.sh（上传+安装+重启）  →  ssh 查日志/自检  →  修 bug  →  回到第一步
```

单次往返约 30 秒，足以支撑「改一行、验一行」的高频迭代。

---

## 1. 单源架构：一切交付物都是字符串

整个插件**只有一个源文件** `build_ipk.py`：它既是构建器，又以字符串形式内嵌全部要打包的文件（shell 脚本、UCI 配置、LuCI JS 视图、JSON）：

```python
src_files = {
    "CONTROL/control":                   """ ... """,   # 包元信息
    "CONTROL/conffiles":                 """ ... """,   # 升级时保留的配置
    "root/etc/init.d/myplugin":          """ ... """,   # procd 编排
    "root/usr/share/myplugin/helper.sh": """ ... """,   # 后端
    "root/www/luci-static/resources/view/myplugin/dashboard.js": """ ... """,  # 前端
    ...
}
```

- **改任何交付文件 = 改 `src_files` 里对应字符串**，然后 `python3 build_ipk.py`。
- ❌ 绝不手编 `src/`、`build/`、`dist/` 下任何文件——它们是构建「先删后建」的纯产物，下次构建会被覆盖。
- ✅ 好处：一个文件就是全部真相，无需 `src/` 目录、无需编译、无需 npm/SDK。

---

## 2. 打包器 `build_ipk.py`（完整可复制）

下面给出**全部可复用核心代码**。复制成 `build_ipk.py`，改顶部常量和 `src_files`，就能产出 `.ipk`。仅依赖 Python 3 标准库（`gzip, io, os, re, shutil, subprocess, tarfile, datetime`）。

### 2.1 顶部：常量与可执行路径声明

```python
import datetime
import gzip
import io
import os
import re
import shutil
import subprocess
import tarfile

PKG_NAME = "myplugin"          # 改成你的包名（也是 init 服务名、菜单前缀）
PKG_VERSION = "1.0.0-1"        # 唯一版本真相源；每次构建自动 +1，别手改
PKG_ARCH = "all"
IPK_FILENAME = f"{PKG_NAME}_{PKG_VERSION}_{PKG_ARCH}.ipk"

# data.tar 里需要可执行权限（0o755）的文件路径（相对 root/）。
# init.d 脚本和你的后端脚本要在这里声明。
EXECUTABLE_DATA_PATHS = (
    "etc/init.d/",
    f"usr/share/{PKG_NAME}/helper.sh",
)

def _is_executable_data(rel_path):
    return any(rel_path == p or rel_path.startswith(p) for p in EXECUTABLE_DATA_PATHS)
```

### 2.2 `src_files`：包元信息模板（直接复用）

这几个 CONTROL 文件对所有 LuCI 插件都通用，照抄即可；业务文件（init/helper/view/config）换成你自己的字符串。

```python
src_files = {
    # —— 包元信息（通用，复用）——
    "CONTROL/control": """Package: myplugin
Version: 1.0.0-1
Depends: luci-base
Architecture: all
Maintainer: You <you@example.com>
Section: luci
Priority: optional
Description: One-line summary of your plugin.
""",
    # 升级时保留用户改过的配置（避免用包默认覆盖现场设置）
    "CONTROL/conffiles": """/etc/config/myplugin
""",
    # 安装/卸载后清掉 LuCI 缓存并重启 rpcd，让新视图生效
    "CONTROL/postinst": """#!/bin/sh
[ -z "$IPKG_INSTROOT" ] || exit 0
rm -f /tmp/luci-indexcache /tmp/luci-modulecache
(sleep 3; /etc/init.d/rpcd restart) &
exit 0
""",
    "CONTROL/postrm": """#!/bin/sh
[ -z "$IPKG_INSTROOT" ] || exit 0
rm -f /tmp/luci-indexcache /tmp/luci-modulecache
(sleep 3; /etc/init.d/rpcd restart) &
exit 0
""",

    # —— 业务文件（换成你的实现）——
    "root/etc/init.d/myplugin":          """#!/bin/sh /etc/rc.common\n...""",  # procd 服务
    "root/usr/share/myplugin/helper.sh": """#!/bin/sh\n...""",                 # 后端子命令
    "root/etc/config/myplugin":          """\nconfig myplugin 'main'\n\toption enabled '1'\n""",
    "root/www/luci-static/resources/view/myplugin/dashboard.js": """'use strict';\n...""",
    "root/usr/share/luci/menu.d/luci-app-myplugin.json": """{ ... }""",
    "root/usr/share/rpcd/acl.d/luci-app-myplugin.json":  """{ ... }""",
}
```

> 注意 `CONTROL/control` 里的 `Version: 1.0.0-1` 只是占位，构建时会被真实版本号覆盖（见 2.5）。`postinst`/`postrm` 在 `IPKG_INSTROOT` 非空时（即正在往镜像里安装）直接退出，避免影响构建机。

### 2.3 版本号自动 +1：`_bump_version_string` + `increment_version`

每次 `python3 build_ipk.py`，`main()` 的**第一步**就调用 `increment_version()`，把顶部 `PKG_VERSION` 原地 +1 并改写脚本自身。

```python
def _bump_version_string(current_ver):
    """1.0.0-N  ->  1.0.0-(N+1)；纯点分形式则最后一段 +1。"""
    if '-' in current_ver:
        ver_part, rev_part = current_ver.rsplit('-', 1)
        try:
            return f"{ver_part}-{int(rev_part) + 1}"
        except ValueError:
            return current_ver + ".1"
    parts = current_ver.split('.')
    try:
        parts[-1] = str(int(parts[-1]) + 1)
        return '.'.join(parts)
    except ValueError:
        return current_ver + "-1"


def increment_version(script_path=None):
    """正则定位 PKG_VERSION 行，原地 +1 改写脚本，并刷新内存变量。"""
    global PKG_VERSION, IPK_FILENAME
    script_path = script_path or __file__
    with open(script_path, "r", encoding="utf-8") as f:
        content = f.read()
    match = re.search(r'PKG_VERSION\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        print("Warning: PKG_VERSION not found."); return
    current_ver = match.group(1)
    new_ver = _bump_version_string(current_ver)
    content = re.sub(r'PKG_VERSION\s*=\s*["\']([^"\']+)["\']',
                     f'PKG_VERSION = "{new_ver}"', content, count=1)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Incremented version: {current_ver} -> {new_ver}")
    PKG_VERSION = new_ver
    IPK_FILENAME = f"{PKG_NAME}_{PKG_VERSION}_{PKG_ARCH}.ipk"
```

⚠️ 不要重命名 `PKG_VERSION`——`increment_version()` 靠这个名字定位，改名会破坏自增。每次构建后脚本必然有一行版本 diff，属预期。

### 2.4 可复现打包：`_compute_file_mode` + `make_tar_gz` + `write_tar_gz_outer_archive`

这三段是「同样的输入 → 字节完全一致的 .ipk」的关键：固定 `root:root`、固定 `mtime=1700000000`、gzip 头不嵌文件名、条目排序、`./` 前缀。可复现让你能对两次构建做 `diff`、安心回滚。

```python
def _compute_file_mode(rel_path, basename, is_control):
    """control tar 里只有 maintainer 脚本可执行；data tar 里 init.d 脚本和
    EXECUTABLE_DATA_PATHS 里声明的后端脚本可执行；其余 0o644。"""
    if is_control:
        return 0o755 if basename in ("postinst", "postrm", "preinst", "prerm") else 0o644
    return 0o755 if _is_executable_data(rel_path) else 0o644


def make_tar_gz(source_dir, output_filename, is_control=False):
    _raw = open(output_filename, "wb")
    _gz = gzip.GzipFile(filename="", fileobj=_raw, mode="wb", mtime=1700000000)
    with tarfile.open(fileobj=_gz, mode="w") as tar:
        entries = []
        for root, dirs, files in os.walk(source_dir):
            for d in dirs:
                fp = os.path.join(root, d)
                entries.append((os.path.relpath(fp, source_dir), fp, True))
            for f in files:
                fp = os.path.join(root, f)
                entries.append((os.path.relpath(fp, source_dir), fp, False))
        entries.sort(key=lambda x: x[0])

        root_ti = tarfile.TarInfo(name=".")
        root_ti.type = tarfile.DIRTYPE
        root_ti.mode = 0o755
        root_ti.uid = root_ti.gid = 0
        root_ti.uname = root_ti.gname = "root"
        root_ti.mtime = 1700000000
        tar.addfile(root_ti)

        for rel_path, full_path, is_dir in entries:
            ti = tar.gettarinfo(full_path, arcname="./" + rel_path)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = 1700000000
            if is_dir:
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tar.addfile(ti)
            else:
                ti.type = tarfile.REGTYPE
                ti.mode = _compute_file_mode(rel_path, os.path.basename(full_path), is_control)
                with open(full_path, "rb") as f:
                    tar.addfile(ti, f)
    _gz.close()
    _raw.close()


def write_tar_gz_outer_archive(archive_path, file_list):
    """把最终 .ipk 写成 gzipped tar，内含 debian-binary/control.tar.gz/data.tar.gz。"""
    _raw = open(archive_path, "wb")
    _gz = gzip.GzipFile(filename="", fileobj=_raw, mode="wb", mtime=1700000000)
    with tarfile.open(fileobj=_gz, mode="w") as tar:
        for name, data in file_list:
            ti = tarfile.TarInfo(name="./" + name)
            ti.size = len(data)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = 1700000000
            ti.mode = 0o644
            ti.type = tarfile.REGTYPE
            tar.addfile(ti, io.BytesIO(data))
    _gz.close()
    _raw.close()
```

> IPK 的本质就是 gzip tar，含三件：`debian-binary`（内容固定 `2.0\n`）+ `control.tar.gz`（由 `src/CONTROL/` 打）+ `data.tar.gz`（由 `src/root/` 打）。

### 2.5 写盘与版本注入：`create_source_tree`

把 `src_files` 字典写盘成 `src/` 真实文件树，并在写盘时完成两处版本号注入：① `CONTROL/control` 的 `Version:` 行；② 前端 JS 里的 `__PKG_VERSION__` 占位符。可执行文件顺便设 `0o755`。

```python
def create_source_tree(src_dir):
    print(f"Creating source tree in '{src_dir}'...")
    if os.path.exists(src_dir):
        shutil.rmtree(src_dir)
    for rel_path, content in src_files.items():
        full_path = os.path.join(src_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        # ① 动态替换 control 里的 Version 行
        if rel_path == "CONTROL/control":
            content = re.sub(r'Version:\s*.*', f'Version: {PKG_VERSION}', content)
        # ② 把前端占位符 __PKG_VERSION__ 替换成真实版本号
        content = content.replace('__PKG_VERSION__', PKG_VERSION)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        # 可执行文件设 0o755（maintainer 脚本 + init.d + 你声明的后端脚本）
        if (rel_path.startswith("CONTROL/") and rel_path != "CONTROL/control") \
                or _is_executable_data(rel_path):
            os.chmod(full_path, 0o755)
    print("Source tree created successfully.")
```

> 这就是「版本号如何进入包元信息和前端界面」的全部秘密：源头只有一个 `PKG_VERSION`，写盘时派生到两处。前端要显示版本号，只需在任意视图里写 `E('strong', {}, '__PKG_VERSION__')`，构建自动替换，无需后端子命令、无需读 opkg 元数据。

### 2.6 releaseNote 自动生成：`_git` + `generate_release_note`

每次构建除产出 `.ipk`，还自动产出 `dist/releaseNote.md`（与 ipk 同目录，每次覆盖）。内容从 git 提交自动提取，零维护。机制：仓库根 `.release_baseline`（需 gitignore）记录上次打包时的 HEAD，本次提取 `baseline..HEAD` 的提交；并标注「未提交改动」（因为本闭环常**先构建后 commit**，这能提示 ipk 里带了还没 commit 的改动）。

```python
def _git(args):
    """在仓库根跑 git 命令；任何失败（非仓库/无 git/非零退出/超时）返回空串。"""
    workspace = os.path.dirname(os.path.abspath(__file__))
    try:
        result = subprocess.run(["git"] + args, cwd=workspace,
                                capture_output=True, text=True, timeout=15)
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def generate_release_note(dist_dir):
    workspace = os.path.dirname(os.path.abspath(__file__))
    note_path = os.path.join(dist_dir, "releaseNote.md")
    baseline_path = os.path.join(workspace, ".release_baseline")

    head = _git(["rev-parse", "HEAD"])
    baseline = ""
    if os.path.exists(baseline_path):
        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline = f.read().strip()

    log_lines, note = [], ""
    if not head:
        note = "（当前目录非 git 仓库，无法提取提交记录）"
    elif baseline and baseline != head:
        raw = _git(["log", "--pretty=format:%h %s", f"{baseline}..HEAD"])
        if raw:
            log_lines, note = raw.splitlines(), "自上次打包以来的提交"
        else:
            raw = _git(["log", "--pretty=format:%h %s", "-15"])
            log_lines = raw.splitlines() if raw else []
            note = "（上次基准不可达，列出最近提交）"
    elif baseline == head:
        note = "（自上次打包以来无新提交）"
    else:
        raw = _git(["log", "--pretty=format:%h %s", "-15"])
        log_lines = raw.splitlines() if raw else []
        note = "（首次打包基准，列出最近提交）"

    dirty = _git(["status", "--porcelain"])
    today = datetime.date.today().isoformat()

    out = [f"# Release Note — {PKG_NAME}", "",
           f"**版本：** v{PKG_VERSION}",
           f"**发布日期：** {today}",
           f"**安装包：** `{IPK_FILENAME}`", "",
           "## 变更记录", "", f"_{note}_", ""]
    out += [f"- {ln}" for ln in log_lines] or ["- （无）"]
    out += [""]
    if dirty:
        out += ["## ⚠️ 未提交改动", "",
                "本次打包时工作树含未提交改动（未进入 git 提交，但已打进 ipk）：", "", "```"]
        out += dirty.splitlines()
        out += ["```", ""]
    out += ["## 安装", "", "```bash", "python3 build_ipk.py && ./deploy.sh", "```", ""]

    with open(note_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    if head:  # 记录本次 HEAD 作为下次基线
        with open(baseline_path, "w", encoding="utf-8") as f:
            f.write(head)
    print(f"Release note generated at: {note_path}")
```

> releaseNote 不进 ipk、不参与可复现构建校验，所以写真实打包日期没问题。`.release_baseline` 会被 `.gitignore` 忽略（见 2.8）。

### 2.7 主流程：`main`

```python
def main():
    # 1. 版本号自动 +1（并原地改写脚本自身）
    increment_version()

    workspace = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(workspace, "src")
    build_dir = os.path.join(workspace, "build")
    dist_dir = os.path.join(workspace, "dist")

    # 2. 重建源码树（写盘 + 版本注入）
    print("Initializing source tree...")
    create_source_tree(src_dir)

    # 3. 重建 build / dist
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir, exist_ok=True)
    os.makedirs(dist_dir, exist_ok=True)

    # 4. 打 control.tar.gz 与 data.tar.gz
    control_tar = os.path.join(build_dir, "control.tar.gz")
    data_tar = os.path.join(build_dir, "data.tar.gz")
    make_tar_gz(os.path.join(src_dir, "CONTROL"), control_tar, is_control=True)
    make_tar_gz(os.path.join(src_dir, "root"), data_tar, is_control=False)

    # 5. 组装最终 .ipk
    with open(control_tar, "rb") as f:
        control_bytes = f.read()
    with open(data_tar, "rb") as f:
        data_bytes = f.read()
    write_tar_gz_outer_archive(
        os.path.join(dist_dir, IPK_FILENAME),
        [("debian-binary", b"2.0\n"),
         ("control.tar.gz", control_bytes),
         ("data.tar.gz", data_bytes)],
    )
    print("\nSUCCESS!")
    print(f"Packaged IPK file created at: {os.path.join(dist_dir, IPK_FILENAME)}")

    # 6. 自动生成 dist/releaseNote.md
    generate_release_note(dist_dir)


if __name__ == "__main__":
    main()
```

> 注意 `main()` 第 1 步就 `increment_version()`，所以**版本号永远是构建时自动 +1**，你永远不需要手改 `PKG_VERSION`。第 3 步只删 `build/`（`dist/` 用 `exist_ok=True`，保留历史 ipk，方便回滚和让 deploy 脚本按时间找最新）。

### 2.8 `.gitignore`

构建产物与基线文件不入库：

```
build/
dist/
src/
.release_baseline
__pycache__/
*.pyc
```

---

## 3. 版本号三件套（汇总）

| 关注点 | 做法 |
|---|---|
| **唯一来源** | 顶部 `PKG_VERSION = "1.0.0-1"`，别处都从它派生 |
| **每次打包 +1** | `main()` 第 1 步调 `increment_version()`，正则改写脚本，`1.0.0-N → 1.0.0-(N+1)` |
| **进包元信息** | `create_source_tree` 用 `re.sub('Version:\\s*.*', ...)` 注入 control |
| **显示在界面** | 视图里写 `'__PKG_VERSION__'` 占位符，`create_source_tree` 用 `replace` 注入 |

⚠️ 不要重命名 `PKG_VERSION`，否则自增失效；每次构建后那一行版本 diff 是正常的。

## 4. 一键部署：`deploy.sh`（完整可复制）

用 macOS 自带的 `expect` 自动填密码：找最新 ipk → `scp` 上传到路由 `/tmp` → `ssh` 远程 `opkg install && /etc/init.d/<服务> restart`。改顶部 4 个变量即可复用。

```bash
#!/bin/bash
# 一键部署：找最新 ipk → scp 上传 → ssh 安装+重启服务。依赖 expect（自动填密码）。

PKG_GLOB="myplugin_*.ipk"     # dist/ 下包名 glob，改成你的
SERVICE="myplugin"            # /etc/init.d/ 下的服务名
PASSWORD="你的路由器密码"
ROUTER_IP="192.168.1.1"
ROUTER_USER="root"
ROUTER_PATH="/tmp/"

LATEST_IPK=$(ls -t dist/$PKG_GLOB 2>/dev/null | head -n 1)
[ -z "$LATEST_IPK" ] && { echo "错误：未找到 ipk，请先 python3 build_ipk.py"; exit 1; }
IPK_BASENAME=$(basename "$LATEST_IPK")
echo "发现最新构建包 : $LATEST_IPK"
echo "目标路由器     : $ROUTER_USER@$ROUTER_IP"

# 1. 上传
expect -c "
spawn scp \"$LATEST_IPK\" \"$ROUTER_USER@$ROUTER_IP:$ROUTER_PATH\"
expect {
    \"*yes/no*\" { send \"yes\r\"; exp_continue }
    \"*password:*\" { send \"$PASSWORD\r\" }
}
expect eof
catch wait result
exit [lindex \$result 3]
" || { echo "SCP 上传失败"; exit 1; }

# 2. 安装并重启服务
expect -c "
spawn ssh \"$ROUTER_USER@$ROUTER_IP\" \"opkg install /tmp/$IPK_BASENAME && /etc/init.d/$SERVICE restart\"
expect {
    \"*yes/no*\" { send \"yes\r\"; exp_continue }
    \"*password:*\" { send \"$PASSWORD\r\" }
}
expect eof
catch wait result
exit [lindex \$result 3]
" && echo "部署成功" || { echo "安装失败"; exit 1; }
```

日常开发就敲一条（构建 + 部署合一）：

```bash
python3 build_ipk.py && ./deploy.sh
```

> opkg 升级时若用户配置与新包默认值不同，会提示 `Existing conffile ... is different`，并把新默认放在 `<name>-opkg`，**用户现场配置被保留**——这是 `CONTROL/conffiles` 起的作用，属预期。建议把路由 SSH 配成免密（`ssh-copy-id`），自检时更顺。

## 5. 查日志与自检（闭环的关键反馈环）

部署后必须在路由上验证，否则等于盲发。建议把路由 SSH 配成免密（`ssh-copy-id`），自检时一条命令进。

### 5.1 服务与进程

```bash
ssh root@<路由器IP> '
  /etc/init.d/<service> status     # procd 状态
  pgrep -af <pkg>                  # 看后台 loop 实例是否在跑、是否只有预期份数
'
```

> 踩坑：`pgrep -f` 会匹配「命令行文本含关键字」的任意进程，**包括你正在执行的那条 ssh 命令**（因为命令串里写了关键字）。判断真实实例数时，看 `PPID=1`（procd 拉起）的那条，忽略临时 shell 的误匹配。

### 5.2 日志

- procd / 内核日志：`logread -e <service>`。
- helper.sh 里用 `logger -t <pkg> "..."` 打的诊断信息进 `logread`。

### 5.3 后端子命令直接验证

helper.sh 末尾 `case "$1"` 分发各子命令，可直接命令行调用验证 JSON：

```bash
ssh root@<路由器IP> '/usr/share/<pkg>/helper.sh some_cmd'                  # 看原始输出
ssh root@<路由器IP> '/usr/share/<pkg>/helper.sh some_cmd | jsonfilter -e "@.field"'
```

路由自带 `jsonfilter`，是验证「shell 拼出的 JSON 是否合法」的最快手段。

### 5.4 上路由前的本地语法自检（强烈推荐）

避免「整个脚本因引号转义错误在路由器上无法加载」这种坑——**部署前先本地过一遍语法**：

```bash
sh   -n src/root/usr/share/<pkg>/helper.sh                # shell 语法（macOS sh 即可拦住大部分）
node --check src/root/www/.../view.js 2>/dev/null          # JS 语法（有 node 才跑）
od -c src/root/.../helper.sh | head                        # 确认 Tab/转义字节符合预期
```

这套本地检查能把 90% 的低级错误挡在部署之前，显著缩短闭环往返。

## 6. 工程约定与高频踩坑

以下每条都来自真实踩坑，新插件沿用即可。

### 6.1 `\t` 是 Python 转义，不是字面反斜杠

`src_files` 里的 shell/JS 用**真实 Tab** 缩进。判断字节用 `od -c`：真实 Tab 显示为 `\t`，反斜杠+t 显示为 `\\   t`。改文件前先 `od` 一下，别把 Tab 当空格、别把转义当字面量。

### 6.2 shell 输出 JSON 的引号转义（最容易翻车）

- helper.sh 里 `echo`/`printf` 的 JSON 双引号：源码写成 `\"`（写盘为 `"`）。
- **awk 程序里**的引号：改用八进制 `\042`（双引号）/ `\047`（单引号），避开 awk 自身字符串引号冲突。
- awk 里要表达**字面 Tab**：源码写 `"\\t"`（Python 解析后写盘为 `"\t"`，awk 再解释成 Tab）。

历史上曾有 `echo` 因引号未转义，导致**整个 helper.sh 在路由器上无法加载**（syntax error），所有子命令全挂。新增任何拼 JSON 的代码，务必沿用这两种模式，并通过 5.4 的本地自检。

### 6.3 awk：标量与数组不可同名

awk 里把一个名字既当标量又当数组会 `fatal: makearray`。例如要按「当前键」累加到一个键数组，标量传当前键、数组存历史，**用不同标识符**，别图省事同名。

### 6.4 rpcd ACL 是「路径级」授权

`rpcd/acl.d/*.json` 里对 `/usr/share/<pkg>/helper.sh` 授 `exec`，是**整脚本**可执行，不是按子命令授权。所以**新增 helper.sh 子命令无需改 ACL**，前端 `fs.exec` 直接可调。

### 6.5 菜单与视图的对应关系

`menu.d/*.json` 里 `action.path` 指向 `view/<pkg>/<name>`，对应 `root/www/luci-static/resources/view/<pkg>/<name>.js`。新增页面 = 加一个视图字符串 + 加一条菜单项。

### 6.6 conffiles 保护用户配置

把用户会改的 UCI 配置（`/etc/config/<pkg>`）列进 `CONTROL/conffiles`，opkg 升级才不会用包默认覆盖现场设置。

## 7. 从零起步 Checklist

1. 把本文第 2 节的全部代码按顺序组装成 `build_ipk.py`（顶部常量 + src_files + 6 个函数 + main）。
2. 改顶部常量：`PKG_NAME`（如 `myplugin`）、`PKG_VERSION`（如 `1.0.0-1`）、`EXECUTABLE_DATA_PATHS`（你的后端脚本路径）。
3. 填 `src_files`：`control`/`conffiles`/`postinst`/`postrm` 用 2.2 模板照抄；业务文件（`init`/`helper`/`view`/`config`/`menu`/`acl`）换成你的实现。
4. 想在界面显示版本号：视图里写 `'__PKG_VERSION__'` 占位符（无需额外代码，构建自动注入）。
5. 复制第 4 节 `deploy.sh`，改 `PKG_GLOB` / `SERVICE` / `ROUTER_IP` / `PASSWORD` 四个变量；`chmod +x deploy.sh`。
6. 把第 2.8 节的 `.gitignore` 放进仓库根。
7. 首批发布：`python3 build_ipk.py && ./deploy.sh`。
8. 自检：按第 5 节在路由上验证 procd 实例、日志、子命令 JSON。
9. 迭代：进入第 0 节闭环——改 `src_files` → 构建（自动 +1 + 生成 releaseNote）→ 部署 → 自检 → 循环，直到稳定。

---

## 8. 闭环心法

- **改一处，验一处**：闭环足够快（约 30 秒），不要攒一堆改动再部署；每改完一个可验证点就跑一轮。
- **本地自检优先**：`sh -n` / `node --check` / `od -c` 能挡住的错误，不要让它耗一次部署往返。
- **版本号交给构建器**：永远别手改 `PKG_VERSION`，让 `increment_version()` 来；commit 时那一行版本 diff 是正常的。
- **配置即代码**：所有交付物都是 `build_ipk.py` 里的字符串，没有散落的 `src/` 文件需要单独维护——单源架构最大的杠杆。
- **发布说明自动来**：每次构建的 `dist/releaseNote.md` 是免费副产物，记得 commit 后再打包，releaseNote 才会精确反映「自上次发布以来的提交」。
