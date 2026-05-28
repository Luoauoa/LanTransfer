# LanTransfer 局域网文件传输工具

跨平台局域网文件传输工具，单文件即可运行。支持 GUI 图形界面和 CLI 命令行两种模式。

---

## 安装

### macOS / Linux（一键安装）

```bash
# 方式一：GitHub 直连（推荐）
curl -fsSL https://raw.githubusercontent.com/Luoauoa/LanTransfer/main/install.sh | bash

# 方式二：jsDelivr CDN 镜像（国内访问）
curl -fsSL https://cdn.jsdelivr.net/gh/Luoauoa/LanTransfer@main/install.sh | bash
```

脚本自动完成：下载程序 → 检测 Python+tkinter（缺了自动装）→ 创建启动器。若下载失败，脚本会自动切换镜像源重试。

### Windows（下载即用）

从 [Releases 页面](https://github.com/Luoauoa/LanTransfer/releases) 下载 `LanTransfer.exe`，双击运行。无需安装 Python。

### 源码运行

```bash
git clone https://github.com/Luoauoa/LanTransfer.git
cd LanTransfer
python3 lantransfer.py         # macOS/Linux
python lantransfer.py          # Windows
```

---

## 快速开始（GUI 图形界面）

### 启动

- **macOS：** 双击 `LanTransfer.app`，或 `python3 lantransfer.py`
- **Windows：** 双击 `LanTransfer.exe`，或 `python lantransfer.py`
- **Linux：** `python3 lantransfer.py`

### 接收文件

1. 切换到 **「接收 (Receive)」** 标签页
2. 点击「浏览...」选择文件保存目录
3. 点击 **「启动服务」**

无需配置，即刻就绪。

### 发送文件

1. 切换到 **「发送 (Send)」** 标签页
2. 点击「浏览...」选择要发送的文件
3. 点击 **「扫描网络」**，选择列表中的接收端
4. 点击 **「发送文件」**

三步完成，无需输入任何内容。

### GUI 界面说明

| 功能 | 说明 |
|------|------|
| 扫描网络 | 一键发现局域网内所有接收端，无需手动输入 IP |
| Token 认证开关 | 默认关闭（无需验证）。勾选后启用，适用于公共网络 |
| 进度条 | 发送和接收双端均显示实时进度百分比 |
| 传输日志 | 记录每次传输的时间、文件名、状态和校验结果 |
| 复制地址 / Token | 启用 Token 后可一键复制，发给对方粘贴 |
| QR 码 | 安装 `qrcode` + `Pillow` 后显示二维码，手机扫一扫获取连接信息 |

---

## CLI 命令行模式

### 接收端

```bash
python3 lantransfer.py receive
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--dir DIR` | 文件保存目录 | 当前目录 |
| `--port PORT` | 监听端口 | 37292 |
| `--token` | 启用 Token 认证（默认关闭） | — |

### 发送端

```bash
# 自动发现接收端（无需 Token 时直接发送，有 Token 时提示输入）
python3 lantransfer.py send ~/document.pdf

# 手动指定地址（Token 可选，回车跳过）
python3 lantransfer.py send ~/document.pdf 192.168.1.5:37292
```

### CLI 示例（默认无 Token）

```
# 机器 A（接收端）
$ python3 lantransfer.py receive --dir ~/Downloads
==================================================
局域网文件接收服务已启动
==================================================
监听地址: https://0.0.0.0:37292
本机 IP : 192.168.1.10
接收目录: /home/user/Downloads
访问地址: https://192.168.1.10:37292
Token   : 未启用（无需验证）
==================================================

# 机器 B（发送端）
$ python3 lantransfer.py send ~/report.pdf
正在搜索局域网接收端...
发现以下接收端:
  1. my-laptop (192.168.1.10:37292)
输入序号 (或按回车取消): 1
检测到对端使用 HTTPS
[发送] 文件: report.pdf (2048000 bytes)
[发送] SHA256: a1b2c3d4...
[发送] 目标: 192.168.1.10:37292
[发送] 进度: 100.0%
[发送] 成功: {"status": "ok", "hash": "a1b2c3d4..."}
```

---

## 可选增强

```bash
pip install qrcode Pillow
```

安装后接收端 GUI 会自动显示二维码，手机扫描即可获取连接信息。

---

## 原理介绍

LanTransfer 是一个基于 HTTP 的点对点局域网文件传输工具，整体架构分为四层：

### 1. 服务发现（UDP 广播）

接收端启动后，在后台线程监听 UDP 端口 **37291**。发送端向 `255.255.255.255:37291` 发送魔术包 `LANTRANSFER_DISCOVER`，所有接收端回复自己的 IP、端口、主机名和是否需要 Token。发送端收集响应展示列表，无需手动记 IP。

### 2. 传输通道（HTTP/HTTPS）

- 接收端运行多线程 HTTP 服务器，暴露 `/upload` 端点处理 POST，`/health` 端点用于连通性探测
- 发送端将文件内容作为 POST body 流式上传，64KB 分块读写，避免大文件撑爆内存
- 文件名通过 URL query string 传递，接收端解析后写入本地
- 发送端连接前先探测对端 `/health`，自动判断 HTTPS/HTTP

### 3. 安全保障

**Token 认证（可选）** — 默认关闭。在家庭/办公室局域网中无需验证即可传输；在公共网络中勾选「启用 Token 认证」后，接收端生成随机 Token（`secrets.token_urlsafe(16)`，128 位熵），发送端必须通过带外方式（屏幕显示 → 人工输入）获得 Token 并携带在 `X-Transfer-Token` 头中。

**传输加密** — 接收端调用系统 `openssl` 生成临时自签名证书（有效期 1 天），为 HTTP 服务包裹 TLS。证书在服务停止后自动删除。无 openssl 时自动降级为 HTTP 并显示警告。

**完整性校验** — 发送端在 `X-File-Hash` 头中携带文件 SHA256 摘要。接收端边写边算哈希，不一致则删除已写入文件并返回 400 错误。

**文件名冲突处理** — 目标文件已存在时自动追加 `_1`、`_2` 序号后缀，不会覆盖已有文件。

### 4. GUI 线程模型

GUI 基于 Python 标准库 Tkinter，所有后台操作在独立线程中运行，通过 `queue.Queue` 与主线程通信，每 100ms 轮询一次更新界面：

| 线程 | 用途 | 通信方式 |
|------|------|----------|
| HTTP Server | `server.serve_forever()` | `queue.Queue` 上报传输进度 |
| UDP Discovery Listener | 响应广播探测 | `threading.Event` 停止信号 |
| Peer Scan | 调用 `discover_peers()` | `queue.Queue` 回传结果列表 |
| File Upload | 调用 `send_file()` | `queue.Queue` 上报发送进度 |

---

## 版本记录

### v2.1 — 免 Token 传输 + GitHub 发布（2026-05-28）

**变更：**
- Token 认证改为**默认关闭**，局域网内直接传输，无需任何输入
- 接收端 GUI 新增「启用 Token 认证」复选框，按需开启
- 发送端 GUI Token 输入框按需显示（扫描结果标注"需要"/"无需"）
- 发送端列表新增 Token 列，一眼看出是否需要 Token
- CLI `receive` 新增 `--token` 参数，默认不启用
- CLI `send` 自动发现模式下根据接收端需要决定是否提示 Token
- UDP 发现协议新增 `token_required` 字段
- QR 码内容不包含 Token（除非启用）
- `send_file()` 仅在有 Token 时才发送认证头

**分发：**
- `install.sh`：macOS/Linux 一键安装，自动检测并安装依赖（Homebrew/apt/dnf）
- `install.bat`：Windows 一键安装
- GitHub Actions：push tag 自动构建 Windows `.exe`，发布到 Releases
- Windows 用户下载 `.exe` 即用，无需安装 Python

### v2.0 — GUI 图形界面（2026-05-28）

- Tkinter GUI 图形界面（接收/发送双标签页）
- 文件选择对话框，无需手动输入路径
- 一键扫描网络，可视化接收端列表
- Token 复制/粘贴按钮
- 双端实时进度条
- 带时间戳的传输日志
- 可选 QR 码显示
- `send_file()` 新增 `progress_callback` 参数
- `TransferHandler` 新增 `progress_queue`，接收端上报进度

### v1.0 — CLI 命令行（初始版本）

- UDP 广播自动发现接收端
- HTTP/HTTPS 文件传输
- SHA256 完整性校验
- Token 认证
- 临时自签名证书加密
- 文件名冲突自动序号
