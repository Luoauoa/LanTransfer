#!/opt/homebrew/bin/python3.13
"""LAN File Transfer - 局域网文件传输工具

Zero-dependency, single-file tool for transferring files between computers
on the same local network. Supports any file type, SHA256 integrity check,
random token authentication, and optional HTTPS encryption.

Usage:
    python3 lantransfer.py receive [--dir DIR] [--port PORT]
    python3 lantransfer.py send <file> [<ip:port>]
"""

import argparse
import hashlib
import http.client
import http.server
import json
import os
import queue
import secrets
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from urllib.parse import quote, unquote

HAS_QR = False
try:
    import qrcode
    from PIL import Image, ImageTk
    HAS_QR = True
except ImportError:
    pass

DISCOVERY_PORT = 37291
DEFAULT_PORT = 37292
CHUNK_SIZE = 65536
DISCOVER_MAGIC = b"LANTRANSFER_DISCOVER"


def get_local_ip():
    """获取本机局域网 IP 地址。"""
    def _is_private(ip):
        try:
            parts = [int(p) for p in ip.split(".")]
            if len(parts) != 4:
                return False
            a, b = parts[0], parts[1]
            if a == 10:
                return True
            if a == 172 and 16 <= b <= 31:
                return True
            if a == 192 and b == 168:
                return True
            return False
        except (ValueError, IndexError):
            return False

    candidates = []

    # Strategy 1: default route via public DNS IP
    for target in (("1.1.1.1", 1), ("8.8.8.8", 1)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(target)
            ip = s.getsockname()[0]
            if ip not in candidates and not ip.startswith("127."):
                candidates.append(ip)
        except Exception:
            pass
        finally:
            s.close()

    # Strategy 2: enumerate local addresses from hostname
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in candidates and not ip.startswith("127."):
                candidates.append(ip)
    except Exception:
        pass

    # Strategy 3: original approach (connect to 10.x)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        if ip not in candidates and not ip.startswith("127."):
            candidates.append(ip)
    except Exception:
        pass
    finally:
        s.close()

    # Prefer standard private LAN IPs
    for ip in candidates:
        if _is_private(ip):
            return ip

    # Fallback: any non-special IPv4
    for ip in candidates:
        try:
            first = int(ip.split(".")[0])
            if first not in (127, 0, 169, 198, 224, 240, 255):
                return ip
        except Exception:
            continue

    return candidates[0] if candidates else "127.0.0.1"


def compute_sha256(filepath):
    """流式计算文件 SHA256。"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def generate_self_signed_cert():
    """调用系统 openssl 生成临时自签名证书，返回 (cert_path, key_path)。
    如果系统没有 openssl，返回 (None, None)。"""
    cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
    key_fd, key_path = tempfile.mkstemp(suffix=".key")
    os.close(cert_fd)
    os.close(key_fd)

    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", key_path, "-out", cert_path,
        "-days", "1", "-nodes", "-subj", "/CN=lantransfer",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return cert_path, key_path
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            os.unlink(cert_path)
        except OSError:
            pass
        try:
            os.unlink(key_path)
        except OSError:
            pass
        return None, None


def unique_filepath(directory, filename):
    """如果目标文件已存在，自动添加序号后缀。"""
    base, ext = os.path.splitext(filename)
    filepath = os.path.join(directory, filename)
    counter = 1
    while os.path.exists(filepath):
        filepath = os.path.join(directory, f"{base}_{counter}{ext}")
        counter += 1
    return filepath


class TransferHandler(http.server.BaseHTTPRequestHandler):
    expected_token = None
    receive_dir = "."
    progress_queue = None

    def log_message(self, format, *args):
        pass

    def _send_json(self, status_code, data):
        body = json.dumps(data).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.path.startswith("/upload"):
            self.send_error(404)
            return

        token = self.headers.get("X-Transfer-Token", "")
        if self.expected_token and token != self.expected_token:
            self.send_error(403, "Invalid token")
            return

        # 提取文件名
        filename = "received_file"
        if "filename=" in self.path:
            try:
                filename = unquote(self.path.split("filename=", 1)[1])
            except Exception:
                pass
        filename = os.path.basename(filename) or "received_file"

        filepath = unique_filepath(self.receive_dir, filename)
        expected_hash = self.headers.get("X-File-Hash", "")
        content_length = int(self.headers.get("Content-Length", 0))

        display_name = os.path.basename(filepath)
        print(f"[接收] 开始写入: {display_name} ({content_length} bytes)")

        if self.progress_queue:
            try:
                self.progress_queue.put_nowait({
                    "type": "start", "filename": display_name,
                    "total": content_length,
                })
            except Exception:
                pass

        hasher = hashlib.sha256()
        received = 0
        try:
            with open(filepath, "wb") as f:
                while received < content_length:
                    to_read = min(CHUNK_SIZE, content_length - received)
                    chunk = self.rfile.read(to_read)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    received += len(chunk)

                    if self.progress_queue:
                        try:
                            self.progress_queue.put_nowait({
                                "type": "progress", "filename": display_name,
                                "received": received, "total": content_length,
                            })
                        except Exception:
                            pass

                    if content_length > 0:
                        pct = received / content_length * 100
                        print(f"\r[接收] 进度: {pct:.1f}%", end="", flush=True)
        except Exception as e:
            print(f"\n[接收] 错误: {e}")
            if self.progress_queue:
                try:
                    self.progress_queue.put_nowait({
                        "type": "error", "filename": display_name, "error": str(e),
                    })
                except Exception:
                    pass
            try:
                os.remove(filepath)
            except OSError:
                pass
            self.send_error(500, str(e))
            return

        print()
        actual_hash = hasher.hexdigest()
        if expected_hash and actual_hash != expected_hash:
            print(f"[接收] 校验失败! 期望: {expected_hash}, 实际: {actual_hash}")
            if self.progress_queue:
                try:
                    self.progress_queue.put_nowait({
                        "type": "error", "filename": display_name,
                        "error": "Hash mismatch",
                    })
                except Exception:
                    pass
            try:
                os.remove(filepath)
            except OSError:
                pass
            self.send_error(400, "Hash mismatch")
            return

        print(f"[接收] 完成: {filepath}")
        if expected_hash:
            print(f"[接收] SHA256 校验通过: {actual_hash}")

        if self.progress_queue:
            try:
                self.progress_queue.put_nowait({
                    "type": "complete", "filename": display_name,
                    "hash": actual_hash,
                })
            except Exception:
                pass

        self._send_json(200, {"status": "ok", "hash": actual_hash})


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_discovery_listener(info, stop_event):
    """在后台线程监听 UDP 发现请求。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind(("", DISCOVERY_PORT))
    sock.settimeout(1.0)

    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(1024)
            if data == DISCOVER_MAGIC:
                sock.sendto(json.dumps(info).encode(), addr)
        except socket.timeout:
            continue
        except OSError:
            break
    sock.close()


def discover_peers(timeout=3):
    """广播发现局域网内的接收端。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)

    sock.sendto(DISCOVER_MAGIC, ("255.255.255.255", DISCOVERY_PORT))

    peers = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(1024)
            info = json.loads(data.decode())
            info.setdefault("ip", addr[0])
            if info not in peers:
                peers.append(info)
        except socket.timeout:
            break
        except Exception:
            continue
    sock.close()
    return peers


def prompt_choice(options, prompt_text="请选择"):
    """命令行交互选择。"""
    if not options:
        return None
    if len(options) == 1:
        return options[0]
    print(f"\n{prompt_text}:")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt['display']}")
    while True:
        choice = input("输入序号 (或按回车取消): ").strip()
        if not choice:
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print("无效输入，请重试。")


def send_file(filepath, host, port, token, use_ssl=True, progress_callback=None):
    """发送单个文件到指定接收端。

    progress_callback(sent_bytes, total_bytes) -> bool
    返回 False 表示取消传输。
    """
    if not os.path.isfile(filepath):
        print(f"错误: 文件不存在: {filepath}")
        if progress_callback:
            progress_callback(-1, 0)
        return False

    filename = os.path.basename(filepath)
    file_size = os.path.getsize(filepath)
    file_hash = compute_sha256(filepath)

    print(f"[发送] 文件: {filename} ({file_size} bytes)")
    print(f"[发送] SHA256: {file_hash}")
    print(f"[发送] 目标: {host}:{port}")

    context = ssl._create_unverified_context() if use_ssl else None
    if use_ssl:
        conn = http.client.HTTPSConnection(host, port, context=context, timeout=30)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=30)

    try:
        conn.putrequest("POST", f"/upload?filename={quote(filename)}")
        if token:
            conn.putheader("X-Transfer-Token", token)
        conn.putheader("X-File-Hash", file_hash)
        conn.putheader("Content-Length", str(file_size))
        conn.endheaders()

        with open(filepath, "rb") as f:
            sent = 0
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                if progress_callback:
                    if not progress_callback(sent, file_size):
                        print("\n[发送] 已取消")
                        return False
                if file_size > 0:
                    pct = sent / file_size * 100
                    print(f"\r[发送] 进度: {pct:.1f}%", end="", flush=True)

        print()
        response = conn.getresponse()
        body = response.read().decode()

        if response.status == 200:
            print(f"[发送] 成功: {body}")
            if progress_callback:
                progress_callback(file_size, file_size)
            return True
        else:
            print(f"[发送] 失败: HTTP {response.status} - {body}")
            if progress_callback:
                progress_callback(-1, file_size)
            return False
    except Exception as e:
        print(f"\n[发送] 错误: {e}")
        if progress_callback:
            progress_callback(-2, file_size)
        return False
    finally:
        conn.close()


def cmd_receive(args):
    """启动接收服务。"""
    receive_dir = os.path.abspath(args.dir)
    os.makedirs(receive_dir, exist_ok=True)

    token = secrets.token_urlsafe(16) if args.token else ""
    local_ip = get_local_ip()
    port = args.port

    # 尝试生成 SSL 证书
    cert_path, key_path = generate_self_signed_cert()
    use_ssl = cert_path is not None

    TransferHandler.expected_token = token
    TransferHandler.receive_dir = receive_dir

    server = ThreadedHTTPServer(("", port), TransferHandler)
    if use_ssl:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)

    stop_event = threading.Event()
    discovery_info = {
        "ip": local_ip, "port": port, "name": socket.gethostname(),
        "token_required": bool(token),
    }
    disc_thread = threading.Thread(
        target=start_discovery_listener, args=(discovery_info, stop_event), daemon=True
    )
    disc_thread.start()

    proto = "https" if use_ssl else "http"
    print("=" * 50)
    print("局域网文件接收服务已启动")
    print("=" * 50)
    print(f"监听地址: {proto}://0.0.0.0:{port}")
    print(f"本机 IP : {local_ip}")
    print(f"接收目录: {receive_dir}")
    print(f"访问地址: {proto}://{local_ip}:{port}")
    if token:
        print(f"Token   : {token}")
    else:
        print("Token   : 未启用（无需验证）")
    if not use_ssl:
        print("警告    : 系统未安装 openssl，已降级为 HTTP 传输（内容未加密）")
    print("=" * 50)
    print("按 Ctrl+C 停止服务\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        if cert_path:
            try:
                os.unlink(cert_path)
            except OSError:
                pass
        if key_path:
            try:
                os.unlink(key_path)
            except OSError:
                pass
        print("已退出。")


def cmd_send(args):
    """发送文件。"""
    filepath = os.path.abspath(args.file)
    destination = args.destination

    host = None
    port = DEFAULT_PORT
    use_ssl = True
    token_required = False

    if destination:
        if ":" in destination:
            host, port_str = destination.rsplit(":", 1)
            port = int(port_str)
        else:
            host = destination
    else:
        print("正在搜索局域网接收端...")
        peers = discover_peers(timeout=3)
        if not peers:
            print("未找到任何接收端。请手动指定地址: python3 lantransfer.py send <文件> <IP:端口>")
            return

        options = []
        for p in peers:
            ip = p.get("ip")
            pport = p.get("port", DEFAULT_PORT)
            name = p.get("name", "Unknown")
            token_needed = p.get("token_required", False)
            suffix = " [需要Token]" if token_needed else ""
            display = f"{name} ({ip}:{pport}){suffix}"
            options.append({"host": ip, "port": pport, "display": display, "token_required": token_needed})

        chosen = prompt_choice(options, prompt_text="发现以下接收端")
        if not chosen:
            print("已取消。")
            return
        host = chosen["host"]
        port = chosen["port"]
        token_required = chosen.get("token_required", False)

    # 探测对端是否使用 HTTPS
    print(f"正在连接 {host}:{port} ...")
    try:
        test_conn = http.client.HTTPSConnection(host, port, context=ssl._create_unverified_context(), timeout=3)
        test_conn.request("GET", "/health")
        test_conn.close()
    except Exception:
        use_ssl = False
        try:
            test_conn = http.client.HTTPConnection(host, port, timeout=3)
            test_conn.request("GET", "/health")
            test_conn.close()
        except Exception:
            print(f"无法连接到 {host}:{port}，请确认接收端已启动且地址正确。")
            return

    proto = "https" if use_ssl else "http"
    print(f"检测到对端使用 {proto.upper()}")

    token = ""
    if token_required:
        token = input("请输入接收端显示的 Token: ").strip()
        if not token:
            print("Token 不能为空。")
            return
    elif not destination:
        # 自动发现模式已知无需 Token，直接发送
        pass
    else:
        # 手动指定地址，不确定是否需要 Token，提示但允许留空
        token = input("Token (如不需要可直接回车): ").strip()

    success = send_file(filepath, host, port, token, use_ssl=use_ssl)
    if not success:
        sys.exit(1)


class LanTransferGUI:
    """Tkinter 图形界面封装。"""

    def __init__(self, root):
        self.root = root
        self.root.title("LanTransfer - 局域网文件传输")
        self.root.minsize(680, 520)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 共享状态
        self._server = None
        self._server_thread = None
        self._stop_event = threading.Event()
        self._cert_path = None
        self._key_path = None
        self._token = ""
        self._use_ssl = True
        self._local_ip = get_local_ip()
        self._port = DEFAULT_PORT

        # 队列：后台线程 -> GUI 主线程
        self._recv_queue = queue.Queue()
        self._send_queue = queue.Queue()
        self._scan_queue = queue.Queue()

        # 构建界面
        self._build_ui()

        # 启动轮询
        self._poll_queues()

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        recv_frame = ttk.Frame(nb)
        send_frame = ttk.Frame(nb)
        nb.add(recv_frame, text="接收 (Receive)")
        nb.add(send_frame, text="发送 (Send)")

        self._build_receive_tab(recv_frame)
        self._build_send_tab(send_frame)

    # ------------------------- 接收标签页 -------------------------

    def _build_receive_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(10, weight=1)

        # 第 0 行：状态 + 启停按钮
        top = ttk.Frame(parent)
        top.grid(row=0, column=0, sticky="ew", pady=(6, 4))
        top.columnconfigure(0, weight=1)

        self._recv_status_lbl = ttk.Label(top, text="状态: ● 已停止", font=("", 10, "bold"))
        self._recv_status_lbl.grid(row=0, column=0, sticky="w")
        self._recv_start_btn = ttk.Button(top, text="启动服务", command=self._on_toggle_receiver)
        self._recv_start_btn.grid(row=0, column=1, sticky="e")

        # 第 1 行：端口 + 目录
        cfg = ttk.Frame(parent)
        cfg.grid(row=1, column=0, sticky="ew", pady=2)
        ttk.Label(cfg, text="端口:").pack(side=tk.LEFT)
        self._recv_port_var = tk.StringVar(value=str(DEFAULT_PORT))
        ttk.Entry(cfg, textvariable=self._recv_port_var, width=7).pack(side=tk.LEFT, padx=(2, 4))
        ttk.Button(cfg, text="默认", command=lambda: self._recv_port_var.set(str(DEFAULT_PORT))).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Label(cfg, text="保存到:").pack(side=tk.LEFT)
        self._recv_dir_var = tk.StringVar(value=os.path.abspath("."))
        ttk.Entry(cfg, textvariable=self._recv_dir_var, width=30).pack(side=tk.LEFT, padx=(2, 4))
        ttk.Button(cfg, text="浏览...", command=self._on_browse_recv_dir).pack(side=tk.LEFT)

        # Token 复选框
        self._recv_token_cb_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="启用 Token 认证", variable=self._recv_token_cb_var).pack(side=tk.RIGHT)

        # 分隔线
        ttk.Separator(parent, orient=tk.HORIZONTAL).grid(row=2, column=0, sticky="ew", pady=6)

        # 连接信息区域
        info_frame = ttk.LabelFrame(parent, text="连接信息 (服务启动后显示)", padding=6)
        info_frame.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        info_frame.columnconfigure(1, weight=1)

        ttk.Label(info_frame, text="访问地址:").grid(row=0, column=0, sticky="w", pady=1)
        self._recv_url_var = tk.StringVar(value="—")
        url_entry = ttk.Entry(info_frame, textvariable=self._recv_url_var, state="readonly", font=("", 10))
        url_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=1)
        self._copy_url_btn = ttk.Button(info_frame, text="复制", command=self._on_copy_url)
        self._copy_url_btn.grid(row=0, column=2, padx=2)

        # Token 区域（整体显示/隐藏）
        self._token_frame = ttk.Frame(info_frame)
        self._token_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=2)
        self._token_frame.columnconfigure(1, weight=1)
        ttk.Label(self._token_frame, text="Token:").grid(row=0, column=0, sticky="w", pady=1)
        self._recv_token_var = tk.StringVar(value="—")
        token_entry = ttk.Entry(self._token_frame, textvariable=self._recv_token_var, state="readonly", font=("Courier", 11))
        token_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=1)
        self._copy_token_btn = ttk.Button(self._token_frame, text="复制", command=self._on_copy_token)
        self._copy_token_btn.grid(row=0, column=2, padx=2)
        self._token_frame.grid_remove()  # 默认隐藏

        # QR 码区域（可选）
        self._qr_label = ttk.Label(info_frame)
        self._qr_label.grid(row=2, column=0, columnspan=3, pady=6)
        if not HAS_QR:
            self._qr_label.config(
                text="提示: pip install qrcode Pillow  可启用二维码扫描",
                foreground="gray",
            )

        # 传输进度
        prog_frame = ttk.Frame(parent)
        prog_frame.grid(row=4, column=0, sticky="ew", pady=(4, 2))
        prog_frame.columnconfigure(1, weight=1)
        self._recv_prog_label = ttk.Label(prog_frame, text="当前传输: —", width=30, anchor="w")
        self._recv_prog_label.grid(row=0, column=0, sticky="w")
        self._recv_prog_bar = ttk.Progressbar(prog_frame, mode="determinate")
        self._recv_prog_bar.grid(row=0, column=1, sticky="ew", padx=(8, 4))
        self._recv_prog_pct = ttk.Label(prog_frame, text="", width=5)

        # 日志
        self._recv_log = scrolledtext.ScrolledText(parent, height=10, state="disabled", font=("", 9))
        self._recv_log.grid(row=10, column=0, sticky="nsew", pady=(4, 0))

        # 初始状态：禁用复制按钮
        self._copy_url_btn.state(["disabled"])
        self._copy_token_btn.state(["disabled"])

    # ------------------------- 发送标签页 -------------------------

    def _build_send_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(8, weight=1)

        # 文件选择
        file_frame = ttk.Frame(parent)
        file_frame.grid(row=0, column=0, sticky="ew", pady=(6, 4))
        file_frame.columnconfigure(1, weight=1)
        ttk.Label(file_frame, text="文件:").grid(row=0, column=0, sticky="w")
        self._send_file_var = tk.StringVar()
        ttk.Entry(file_frame, textvariable=self._send_file_var, width=40).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(file_frame, text="浏览...", command=self._on_browse_send_file).grid(row=0, column=2, padx=2)
        self._send_file_size_lbl = ttk.Label(file_frame, text="", width=12)
        self._send_file_size_lbl.grid(row=0, column=3, sticky="e")

        # 接收端选择
        rcvr_label_frame = ttk.LabelFrame(parent, text="接收端", padding=4)
        rcvr_label_frame.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        rcvr_label_frame.columnconfigure(0, weight=1)

        rcvr_top = ttk.Frame(rcvr_label_frame)
        rcvr_top.grid(row=0, column=0, sticky="ew")
        self._scan_btn = ttk.Button(rcvr_top, text="扫描网络", command=self._on_scan)
        self._scan_btn.pack(side=tk.LEFT)
        ttk.Label(rcvr_top, text="  或手动:").pack(side=tk.LEFT)
        self._send_host_var = tk.StringVar()
        ttk.Entry(rcvr_top, textvariable=self._send_host_var, width=14).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(rcvr_top, text=":").pack(side=tk.LEFT)
        self._send_port_var = tk.StringVar(value=str(DEFAULT_PORT))
        ttk.Entry(rcvr_top, textvariable=self._send_port_var, width=6).pack(side=tk.LEFT, padx=2)

        # 接收端列表
        tree_frame = ttk.Frame(rcvr_label_frame)
        tree_frame.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        tree_frame.columnconfigure(0, weight=1)
        columns = ("name", "ip", "port", "token")
        self._peer_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=4)
        self._peer_tree.heading("name", text="主机名")
        self._peer_tree.heading("ip", text="IP 地址")
        self._peer_tree.heading("port", text="端口")
        self._peer_tree.heading("token", text="Token")
        self._peer_tree.column("name", width=150)
        self._peer_tree.column("ip", width=130)
        self._peer_tree.column("port", width=50)
        self._peer_tree.column("token", width=50)
        self._peer_tree.grid(row=0, column=0, sticky="ew")
        peer_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self._peer_tree.yview)
        self._peer_tree.configure(yscrollcommand=peer_scroll.set)
        peer_scroll.grid(row=0, column=1, sticky="ns")
        self._peer_tree.bind("<<TreeviewSelect>>", self._on_peer_selected)

        # 存储 peer 完整信息 (ip, port, token_required)
        self._peer_data = {}

        # Token 输入区域（按需显示）
        self._send_token_frame = ttk.Frame(parent)
        self._send_token_frame.grid(row=2, column=0, sticky="ew", pady=(4, 4))
        self._send_token_frame.columnconfigure(1, weight=1)
        self._send_token_lbl = ttk.Label(self._send_token_frame, text="Token:")
        self._send_token_lbl.grid(row=0, column=0, sticky="w")
        self._send_token_var = tk.StringVar()
        ttk.Entry(self._send_token_frame, textvariable=self._send_token_var, width=30, font=("Courier", 10)).grid(
            row=0, column=1, sticky="ew", padx=4)
        ttk.Button(self._send_token_frame, text="粘贴", command=self._on_paste_token).grid(row=0, column=2, padx=2)
        self._send_token_frame.grid_remove()  # 默认隐藏

        # 发送按钮
        self._send_btn = ttk.Button(parent, text="发送文件", command=self._on_send)
        self._send_btn.grid(row=3, column=0, pady=4)

        # 进度
        prog_frame = ttk.Frame(parent)
        prog_frame.grid(row=4, column=0, sticky="ew", pady=(2, 2))
        prog_frame.columnconfigure(1, weight=1)
        self._send_prog_label = ttk.Label(prog_frame, text="状态: 就绪", width=34, anchor="w")
        self._send_prog_label.grid(row=0, column=0, sticky="w")
        self._send_prog_bar = ttk.Progressbar(prog_frame, mode="determinate")
        self._send_prog_bar.grid(row=0, column=1, sticky="ew", padx=(8, 4))
        self._send_prog_pct = ttk.Label(prog_frame, text="", width=5)

        # 日志
        self._send_log = scrolledtext.ScrolledText(parent, height=10, state="disabled", font=("", 9))
        self._send_log.grid(row=8, column=0, sticky="nsew", pady=(4, 0))

    # ------------------------------------------------------------------
    # 接收端操作
    # ------------------------------------------------------------------

    def _on_toggle_receiver(self):
        if self._server is None:
            self._start_receiver()
        else:
            self._stop_receiver()

    def _start_receiver(self):
        save_dir = self._recv_dir_var.get().strip()
        if not save_dir:
            messagebox.showerror("错误", "请选择文件保存目录。")
            return
        try:
            port = int(self._recv_port_var.get().strip())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "端口号必须是 1-65535 之间的整数。")
            return

        os.makedirs(save_dir, exist_ok=True)
        self._port = port
        use_token = self._recv_token_cb_var.get()
        self._token = secrets.token_urlsafe(16) if use_token else ""

        # SSL
        self._cert_path, self._key_path = generate_self_signed_cert()
        self._use_ssl = self._cert_path is not None

        # 配置 Handler
        TransferHandler.expected_token = self._token
        TransferHandler.receive_dir = os.path.abspath(save_dir)
        TransferHandler.progress_queue = self._recv_queue

        # 启动 HTTP Server 线程
        self._server = ThreadedHTTPServer(("", port), TransferHandler)
        if self._use_ssl:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self._cert_path, self._key_path)
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)

        self._stop_event.clear()
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()

        # 启动 UDP 发现监听
        discovery_info = {
            "ip": self._local_ip, "port": port, "name": socket.gethostname(),
            "token_required": bool(self._token),
        }
        self._disc_thread = threading.Thread(
            target=start_discovery_listener, args=(discovery_info, self._stop_event), daemon=True)
        self._disc_thread.start()

        # 更新 UI
        proto = "https" if self._use_ssl else "http"
        url = f"{proto}://{self._local_ip}:{port}"
        self._recv_url_var.set(url)
        self._recv_status_lbl.config(text="状态: ● 运行中", foreground="green")
        self._recv_start_btn.config(text="停止服务")
        self._copy_url_btn.state(["!disabled"])
        if self._token:
            self._recv_token_var.set(self._token)
            self._token_frame.grid()
            self._copy_token_btn.state(["!disabled"])
        else:
            self._token_frame.grid_remove()

        # 生成 QR 码
        if HAS_QR:
            qr_parts = [f"IP: {self._local_ip}", f"Port: {port}"]
            if self._token:
                qr_parts.append(f"Token: {self._token}")
            self._generate_qr("\n".join(qr_parts))

        self._recv_log_message(f"服务已启动 ({proto.upper()}) - {url}")
        if not self._use_ssl:
            self._recv_log_message("警告: 未安装 openssl，传输未加密")

    def _stop_receiver(self):
        self._stop_event.set()
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server.server_close()
            self._server = None
        TransferHandler.progress_queue = None

        if self._cert_path:
            try:
                os.unlink(self._cert_path)
            except OSError:
                pass
        if self._key_path:
            try:
                os.unlink(self._key_path)
            except OSError:
                pass

        self._recv_status_lbl.config(text="状态: ● 已停止", foreground="")
        self._recv_start_btn.config(text="启动服务")
        self._recv_url_var.set("—")
        self._recv_token_var.set("—")
        self._token_frame.grid_remove()
        self._copy_url_btn.state(["disabled"])
        self._copy_token_btn.state(["disabled"])
        if HAS_QR:
            self._qr_label.config(image="")
            self._qr_label.image = None
            self._qr_label.config(text="")
        self._recv_prog_label.config(text="当前传输: —")
        self._recv_prog_bar["value"] = 0
        self._recv_prog_pct.config(text="")
        self._recv_log_message("服务已停止")

    def _on_browse_recv_dir(self):
        path = filedialog.askdirectory(title="选择文件保存目录")
        if path:
            self._recv_dir_var.set(path)

    def _on_copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self._recv_url_var.get())
        self._recv_log_message("已复制访问地址到剪贴板")

    def _on_copy_token(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self._recv_token_var.get())
        self._recv_log_message("已复制 Token 到剪贴板")

    def _generate_qr(self, data):
        try:
            qr = qrcode.QRCode(box_size=4, border=2)
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            img = img.resize((200, 200), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._qr_label.config(image=photo)
            self._qr_label.image = photo  # 保持引用防 GC
        except Exception as e:
            self._qr_label.config(text=f"二维码生成失败: {e}", foreground="red")

    # ------------------------------------------------------------------
    # 发送端操作
    # ------------------------------------------------------------------

    def _on_browse_send_file(self):
        path = filedialog.askopenfilename(title="选择要发送的文件")
        if path:
            self._send_file_var.set(path)
            try:
                size = os.path.getsize(path)
                self._send_file_size_lbl.config(text=self._format_size(size))
            except OSError:
                self._send_file_size_lbl.config(text="")

    def _on_scan(self):
        self._scan_btn.config(state="disabled", text="扫描中...")
        self._send_log_message("正在扫描局域网接收端...")
        t = threading.Thread(target=self._scan_peers, daemon=True)
        t.start()

    def _scan_peers(self):
        try:
            peers = discover_peers(timeout=3)
            self._scan_queue.put(peers)
        except Exception as e:
            self._scan_queue.put([])

    def _on_peer_selected(self, event):
        sel = self._peer_tree.selection()
        if sel:
            item_id = sel[0]
            item = self._peer_tree.item(item_id)
            values = item["values"]
            self._send_host_var.set(values[1])  # IP
            self._send_port_var.set(str(values[2]))  # Port

            # 显示/隐藏 Token 输入
            peer = self._peer_data.get(item_id, {})
            if peer.get("token_required"):
                self._send_token_frame.grid()
            else:
                self._send_token_frame.grid_remove()
                self._send_token_var.set("")

    def _on_paste_token(self):
        try:
            text = self.root.clipboard_get()
            self._send_token_var.set(text.strip())
        except Exception:
            pass

    def _on_send(self):
        filepath = self._send_file_var.get().strip()
        if not filepath or not os.path.isfile(filepath):
            messagebox.showerror("错误", "请选择要发送的文件。")
            return

        host = self._send_host_var.get().strip()
        if not host:
            messagebox.showerror("错误", "请扫描或手动输入接收端地址。")
            return

        try:
            port = int(self._send_port_var.get().strip())
        except ValueError:
            messagebox.showerror("错误", "端口号无效。")
            return

        token = self._send_token_var.get().strip()
        if self._send_token_frame.winfo_ismapped() and not token:
            messagebox.showerror("错误", "该接收端启用了 Token 认证，请输入 Token。")
            return

        # 探测对端协议
        use_ssl = True
        try:
            ctx = ssl._create_unverified_context()
            c = http.client.HTTPSConnection(host, port, context=ctx, timeout=3)
            c.request("GET", "/health")
            c.close()
        except Exception:
            use_ssl = False
            try:
                c = http.client.HTTPConnection(host, port, timeout=3)
                c.request("GET", "/health")
                c.close()
            except Exception:
                messagebox.showerror("错误", f"无法连接到 {host}:{port}，请确认接收端已启动。")
                return

        proto = "HTTPS" if use_ssl else "HTTP"
        filename = os.path.basename(filepath)
        file_size = os.path.getsize(filepath)

        # 禁用 UI
        self._send_btn.config(state="disabled", text="发送中...")
        self._send_prog_label.config(text=f"状态: 正在发送 {filename}")
        self._send_prog_bar["value"] = 0
        self._send_log_message(f"发送 {filename} ({self._format_size(file_size)}) -> {host}:{port} ({proto})")

        t = threading.Thread(
            target=self._do_send, args=(filepath, host, port, token, use_ssl, filename),
            daemon=True,
        )
        t.start()

    def _do_send(self, filepath, host, port, token, use_ssl, filename):
        def cb(sent, total):
            if total <= 0:
                return True
            self._send_queue.put(("progress", sent, total, filename))
            return True

        ok = send_file(filepath, host, port, token, use_ssl=use_ssl, progress_callback=cb)
        self._send_queue.put(("done", ok, filename))

    # ------------------------------------------------------------------
    # 队列轮询
    # ------------------------------------------------------------------

    def _poll_queues(self):
        # 接收队列
        self._drain_recv_queue()
        # 发送队列
        self._drain_send_queue()
        # 扫描队列
        self._drain_scan_queue()

        self.root.after(100, self._poll_queues)

    def _drain_recv_queue(self):
        while True:
            try:
                evt = self._recv_queue.get_nowait()
            except queue.Empty:
                break
            t = evt.get("type")
            if t == "start":
                self._recv_prog_label.config(text=f"正在接收: {evt['filename']}")
                self._recv_prog_bar["value"] = 0
                self._recv_prog_pct.config(text="0%")
                total = evt.get("total", 0)
                self._recv_log_message(f"开始接收: {evt['filename']} ({self._format_size(total)})")
            elif t == "progress":
                total = evt.get("total", 1)
                if total > 0:
                    pct = evt["received"] / total * 100
                    self._recv_prog_bar["value"] = pct
                    self._recv_prog_pct.config(text=f"{pct:.0f}%")
            elif t == "complete":
                self._recv_prog_bar["value"] = 100
                self._recv_prog_pct.config(text="100%")
                self._recv_prog_label.config(text=f"接收完成: {evt['filename']}")
                self._recv_log_message(f"接收完成: {evt['filename']} (SHA256: {evt.get('hash', 'N/A')})")
            elif t == "error":
                self._recv_prog_label.config(text=f"接收失败: {evt.get('filename', '?')}")
                self._recv_log_message(f"错误: {evt.get('error', '未知错误')}")

    def _drain_send_queue(self):
        while True:
            try:
                evt = self._send_queue.get_nowait()
            except queue.Empty:
                break
            if evt[0] == "progress":
                _, sent, total, filename = evt
                if total > 0:
                    pct = sent / total * 100
                    self._send_prog_bar["value"] = pct
                    self._send_prog_pct.config(text=f"{pct:.0f}%")
                    self._send_prog_label.config(
                        text=f"发送中: {self._format_size(sent)} / {self._format_size(total)}")
            elif evt[0] == "done":
                _, ok, filename = evt
                self._send_btn.config(state="normal", text="发送文件")
                if ok:
                    self._send_prog_bar["value"] = 100
                    self._send_prog_pct.config(text="100%")
                    self._send_prog_label.config(text=f"发送成功: {filename}")
                    self._send_log_message(f"发送成功: {filename}")
                else:
                    self._send_prog_label.config(text=f"发送失败: {filename}")
                    self._send_log_message(f"发送失败: {filename}")
                    messagebox.showerror("发送失败", f"文件 {filename} 发送失败，请检查 Token 和网络连接。")

    def _drain_scan_queue(self):
        while True:
            try:
                peers = self._scan_queue.get_nowait()
            except queue.Empty:
                break
            self._scan_btn.config(state="normal", text="扫描网络")
            self._peer_tree.delete(*self._peer_tree.get_children())
            self._peer_data.clear()
            self._send_token_frame.grid_remove()
            if not peers:
                self._peer_tree.insert("", tk.END, values=("未发现接收端", "", "", ""))
                self._send_log_message("未发现接收端")
            else:
                for p in peers:
                    token_req = "需要" if p.get("token_required") else "无需"
                    item = self._peer_tree.insert("", tk.END, values=(
                        p.get("name", "Unknown"),
                        p.get("ip", ""),
                        str(p.get("port", DEFAULT_PORT)),
                        token_req,
                    ))
                    self._peer_data[item] = {
                        "ip": p.get("ip", ""),
                        "port": str(p.get("port", DEFAULT_PORT)),
                        "token_required": p.get("token_required", False),
                    }
                self._send_log_message(f"发现 {len(peers)} 个接收端")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _format_size(size):
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        else:
            return f"{size / (1024 * 1024 * 1024):.2f} GB"

    def _recv_log_message(self, msg):
        self._log_message(self._recv_log, msg)

    def _send_log_message(self, msg):
        self._log_message(self._send_log, msg)

    def _log_message(self, widget, msg):
        ts = time.strftime("%H:%M:%S")
        widget.config(state="normal")
        widget.insert(tk.END, f"[{ts}] {msg}\n")
        widget.see(tk.END)
        widget.config(state="disabled")

    def _on_close(self):
        if self._server:
            self._stop_receiver()
        self.root.destroy()


def _launch_gui():
    """启动 Tkinter GUI。"""
    try:
        root = tk.Tk()
        app = LanTransferGUI(root)
        root.mainloop()
    except Exception as e:
        print(f"GUI 启动失败: {e}")
        print("请确认系统已安装 tkinter (python-tk / python3-tk)")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="局域网文件传输工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  启动 GUI (默认):
    python3 lantransfer.py

  启动 GUI (显式):
    python3 lantransfer.py gui

  启动接收端 (CLI):
    python3 lantransfer.py receive

  发送文件 (自动发现):
    python3 lantransfer.py send ~/document.pdf

  发送文件 (手动指定):
    python3 lantransfer.py send ~/document.pdf 192.168.1.5:37292
        """.strip(),
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("gui", help="启动图形界面")

    recv_parser = subparsers.add_parser("receive", help="启动接收服务")
    recv_parser.add_argument("--dir", default=".", help="文件保存目录 (默认: 当前目录)")
    recv_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口 (默认: {DEFAULT_PORT})")
    recv_parser.add_argument("--token", action="store_true", help="启用 Token 认证 (默认关闭)")

    send_parser = subparsers.add_parser("send", help="发送文件")
    send_parser.add_argument("file", help="要发送的文件路径")
    send_parser.add_argument("destination", nargs="?", help="目标地址 (格式: IP:端口，可选)")

    args = parser.parse_args()

    if args.command is None or args.command == "gui":
        _launch_gui()
    elif args.command == "receive":
        cmd_receive(args)
    elif args.command == "send":
        cmd_send(args)


if __name__ == "__main__":
    main()
