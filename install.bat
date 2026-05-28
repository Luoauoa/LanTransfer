@echo off
REM LanTransfer 一键安装脚本 (Windows)
REM 双击运行此文件即可安装

set INSTALL_DIR=%USERPROFILE%\lantransfer
echo === LanTransfer 安装 ===
echo 安装目录: %INSTALL_DIR%

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!] 未找到 Python，请先从 https://python.org 下载安装
    echo    安装时务必勾选 "Add Python to PATH"
    pause
    exit /b 1
)

REM 检查 tkinter
python -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo [!] Python tkinter 模块不可用
    echo    请重新安装 Python 并确保勾选 "tcl/tk and IDLE" 组件
    pause
    exit /b 1
)

REM 复制 lantransfer.py 到安装目录
if exist "%~dp0lantransfer.py" (
    copy /Y "%~dp0lantransfer.py" "%INSTALL_DIR%\lantransfer.py" >nul
    echo [+] 已复制 lantransfer.py
)

REM 创建桌面快捷方式
set SHORTCUT=%USERPROFILE%\Desktop\LanTransfer.lnk
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath = 'python'; $s.Arguments = '%INSTALL_DIR%\lantransfer.py'; $s.WorkingDirectory = '%INSTALL_DIR%'; $s.IconLocation = 'python.exe,0'; $s.Save()" 2>nul
if exist "%SHORTCUT%" (
    echo [+] 已在桌面创建 LanTransfer 快捷方式
)

echo.
echo [√] 安装完成!
echo 双击桌面上的 LanTransfer 即可启动
pause
