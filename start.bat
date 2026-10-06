@echo off
chcp 65001 >nul
title 智学笔记平台
echo ============================================
echo          智学笔记平台 启动中...
echo ============================================
echo.

:: 检查 Python
where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: 首次运行自动安装依赖
python -c "import requests, reportlab, matplotlib" >nul 2>&1
if errorlevel 1 (
    echo [首次运行] 正在安装依赖库，请稍候...
    python -m pip install requests reportlab matplotlib --quiet
    echo [完成] 依赖安装完成
    echo.
)

:: 启动后端服务（8766 端口）
echo [1/3] 启动后端服务...
start /min "" python "%~dp0backend\server.py" --port 8766

:: 启动前端静态服务（8765 端口）
echo [2/3] 启动前端服务...
start /min "" python -m http.server 8765 --bind 127.0.0.1 --directory "%~dp0frontend"

:: 等待服务就绪
timeout /t 3 /nobreak >nul

:: 打开浏览器
echo [3/3] 正在打开浏览器...
start "" "http://127.0.0.1:8765/index.html"

echo.
echo ============================================
echo   平台已启动！浏览器未自动打开时请访问:
echo   http://127.0.0.1:8765/index.html
echo ============================================
echo.
echo   关闭本窗口不会影响平台运行
echo   要停止服务: 关闭两个最小化的 Python 窗口
echo   或打开任务管理器结束 python 进程
echo.
pause