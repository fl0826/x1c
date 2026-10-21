@echo off
cd /d "%~dp0"
echo ========================================
echo  3D 打印助手 Web 服务
echo ========================================
echo.
echo 正在启动...
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未安装 Python
    echo 请从 https://python.org 下载安装
    pause
    exit /b 1
)

REM 检查 .env
if not exist .env (
    echo [警告] 未找到 .env 配置文件
    echo 请参考 README.md 创建配置
    echo.
    pause
)

echo 服务地址: http://127.0.0.1:8080
echo 公网访问: 需要先运行 cloudflared tunnel run
echo.
echo 按 Ctrl+C 停止服务
echo ----------------------------------------
echo.

python server.py

if errorlevel 1 (
    echo.
    echo [错误] 启动失败，请检查错误信息
    pause
)
