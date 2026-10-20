@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo 用法：把 .3mf 文件拖到这个 bat 上
    echo 或者命令行：python main.py upload 文件.3mf
    pause
    exit /b 1
)

echo 上传: %~nx1
echo ============================================================
python "%~dp0main.py" upload "%~1"
echo.
pause
