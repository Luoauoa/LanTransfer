#!/bin/bash
# LanTransfer 一键安装脚本 (macOS / Linux)
# 用法: curl -fsSL https://raw.githubusercontent.com/Luoauoa/LanTransfer/main/install.sh | bash

set -e
INSTALL_DIR="${HOME}/lantransfer"
SCRIPT_URL="https://raw.githubusercontent.com/Luoauoa/LanTransfer/main/lantransfer.py"

echo "=== LanTransfer 安装 ==="
echo "安装目录: ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"

# 下载脚本
echo "下载 lantransfer.py ..."
if command -v curl &>/dev/null; then
    curl -fsSL "${SCRIPT_URL}" -o "${INSTALL_DIR}/lantransfer.py"
elif command -v wget &>/dev/null; then
    wget -q "${SCRIPT_URL}" -O "${INSTALL_DIR}/lantransfer.py"
else
    echo "错误: 需要 curl 或 wget"
    exit 1
fi
chmod +x "${INSTALL_DIR}/lantransfer.py"

# 查找可用的 Python + tkinter
find_python() {
    # 先检查 Homebrew Python（可能不在 PATH 中）
    for brew_py in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11; do
        if [ -x "$brew_py" ] && "$brew_py" -c "import tkinter" 2>/dev/null; then
            echo "$brew_py"
            return 0
        fi
    done
    # 再检查 Intel Mac Homebrew 路径
    for brew_py in /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11; do
        if [ -x "$brew_py" ] && "$brew_py" -c "import tkinter" 2>/dev/null; then
            echo "$brew_py"
            return 0
        fi
    done
    # 最后检查 PATH 中的 Python
    for p in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
        if command -v "$p" &>/dev/null && "$p" -c "import tkinter" 2>/dev/null; then
            command -v "$p"
            return 0
        fi
    done
    return 1
}

PYTHON=$(find_python)

if [ -z "$PYTHON" ]; then
    echo ""
    echo "未找到 Python 3 + tkinter，需要安装。"

    if [ "$(uname)" = "Darwin" ]; then
        echo ""
        echo "将通过 Homebrew 安装 Python 3.13 + tkinter（已安装则自动跳过）"
        read -p "是否继续? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            if ! command -v brew &>/dev/null; then
                echo "先安装 Homebrew..."
                /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
                if [ -x "/opt/homebrew/bin/brew" ]; then
                    eval "$(/opt/homebrew/bin/brew shellenv)"
                elif [ -x "/usr/local/bin/brew" ]; then
                    eval "$(/usr/local/bin/brew shellenv)"
                fi
            fi
            brew install python@3.13 python-tk@3.13
            PYTHON=$(find_python)
        fi
    elif [ -f /etc/debian_version ]; then
        echo "将通过 apt 安装 python3-tk（已安装则自动跳过）"
        read -p "是否继续? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            sudo apt update && sudo apt install -y python3-tk
            PYTHON=$(find_python)
        fi
    elif [ -f /etc/fedora-release ] || [ -f /etc/redhat-release ]; then
        echo "将通过 dnf 安装 python3-tkinter（已安装则自动跳过）"
        read -p "是否继续? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            sudo dnf install -y python3-tkinter
            PYTHON=$(find_python)
        fi
    elif [ -f /etc/arch-release ]; then
        echo "将通过 pacman 安装 tk"
        read -p "是否继续? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            sudo pacman -S --noconfirm tk
            PYTHON=$(find_python)
        fi
    else
        echo ""
        echo "无法自动安装，请手动安装 Python 3.9+ 及 tkinter："
        echo "  Debian/Ubuntu: sudo apt install python3-tk"
        echo "  Fedora/RHEL:   sudo dnf install python3-tkinter"
        echo "  Arch:          sudo pacman -S tk"
        echo "  macOS:         brew install python@3.13 python-tk@3.13"
    fi

    if [ -z "$PYTHON" ]; then
        echo ""
        echo "未能完成安装。你可以手动安装 Python+tkinter 后运行:"
        echo "  python3 ${INSTALL_DIR}/lantransfer.py"
        exit 1
    fi
fi

echo "使用 Python: ${PYTHON} ($($PYTHON --version))"

# 可选: 安装 qrcode（扫码增强）
echo ""
read -p "是否安装二维码增强? (手机扫一扫获取连接信息) [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    "$PYTHON" -m pip install --user qrcode Pillow 2>/dev/null || true
fi

# 创建启动脚本（使用检测到的 Python 路径）
cat > "${INSTALL_DIR}/start.sh" << LAUNCHER
#!/bin/bash
DIR="\$(cd "\$(dirname "\$0")" && pwd)"
exec ${PYTHON} "\$DIR/lantransfer.py" "\$@"
LAUNCHER
chmod +x "${INSTALL_DIR}/start.sh"

# 创建 macOS .app 快捷方式
if [ "$(uname)" = "Darwin" ]; then
    osacompile -o "${INSTALL_DIR}/LanTransfer.app" \
        -e "do shell script \"${PYTHON} ${INSTALL_DIR}/lantransfer.py gui > /dev/null 2>&1 &\"" 2>/dev/null || true
fi

echo ""
echo "✓ 安装完成!"
echo ""
echo "启动方式:"
echo "  ${INSTALL_DIR}/start.sh              # 启动 GUI"
if [ -f "${INSTALL_DIR}/LanTransfer.app" ]; then
    echo "  open ${INSTALL_DIR}/LanTransfer.app  # 或双击 .app"
fi
echo "  ${INSTALL_DIR}/start.sh receive      # CLI 接收"
echo "  ${INSTALL_DIR}/start.sh send <文件>  # CLI 发送"
