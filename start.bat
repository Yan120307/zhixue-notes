@echo off
setlocal enabledelayedexpansion
title 智学笔记平台
cd /d "%~dp0"

echo ============================================
echo          智学笔记平台 启动中...
echo ============================================
echo.

rem ============================================================
rem 1. 定位可用的 Python（依次尝试以下来源）
rem    - Windows 商店的 python.exe 是 0 字节占位别名，运行无输出，
rem      下面的探测会自动排除它
rem ============================================================
set "PY="

rem 1.1 py 启动器（Windows 自带，指向已安装的 Python）
where py >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
)

rem 1.2 python / python3 命令（真实可运行的才算数）
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
    )
)
if not defined PY (
    where python3 >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('python3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
    )
)

rem 1.3 常见安装路径
if not defined PY (
    for %%d in ("%LocalAppData%\Programs\Python\Python*\python.exe" "%ProgramFiles%\Python\Python*\python.exe" "C:\Python*\python.exe" "C:\develop\python\python.exe") do (
        if exist %%d set "PY=%%~d"
    )
)

rem 1.4 本机内置 Python（TeleAgent 运行时兜底，无需安装）
if not defined PY (
    if exist "C:\Users\windows\.local\share\TeleAgent\runtimes\python\python.exe" set "PY=C:\Users\windows\.local\share\TeleAgent\runtimes\python\python.exe"
)

rem 最终验证：必须真的能执行（再次排除占位别名）
if defined PY (
    "%PY%" -c "import sys" >nul 2>&1
    if errorlevel 1 set "PY="
)

if not defined PY (
    echo.
    echo [错误] 未找到可用的 Python 环境。
    echo        本机只有 Windows 商店的 Python 占位应用，
    echo        请先安装 Python 3.8+，然后重新双击本脚本。
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

echo [信息] 使用 Python: %PY%
echo.

rem ============================================================
rem 2. 依赖检查与首次安装
rem ============================================================
"%PY%" -c "import requests, reportlab, matplotlib" >nul 2>&1
if errorlevel 1 (
    echo [首次运行] 正在安装依赖库（requests / reportlab / matplotlib）...
    echo           请耐心等待，可能需要几分钟。
    "%PY%" -m pip install requests reportlab matplotlib --quiet
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络后重新双击本脚本。
        pause
        exit /b 1
    )
    echo [完成] 依赖安装完成
)

rem ============================================================
rem 3. 启动服务（端口已被占用时自动跳过，避免重复启动）
rem ============================================================
echo [1/3] 检查后端服务（端口 8766）...
powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient;try{$c.Connect('127.0.0.1',8766);exit 0}catch{exit 1}" >nul 2>&1
if errorlevel 1 (
    start /min "" "%PY%" "%~dp0backend\server.py" --port 8766
    echo       后端服务启动中...
) else (
    echo       后端服务已在运行，跳过
)

echo [2/3] 检查前端服务（端口 8765）...
powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient;try{$c.Connect('127.0.0.1',8765);exit 0}catch{exit 1}" >nul 2>&1
if errorlevel 1 (
    start /min "" "%PY%" -m http.server 8765 --bind 127.0.0.1 --directory "%~dp0frontend"
    echo       前端服务启动中...
) else (
    echo       前端服务已在运行，跳过
)

rem ============================================================
rem 4. 等待两个服务就绪（最多 20 秒）
rem ============================================================
echo [3/3] 等待服务就绪...
for /l %%i in (1,1,20) do (
    powershell -NoProfile -Command "$ok=$true;foreach($p in 8765,8766){$c=New-Object Net.Sockets.TcpClient;try{$c.Connect('127.0.0.1',$p)}catch{$ok=$false};$c.Close()};if($ok){exit 0}else{exit 1}" >nul 2>&1
    if not errorlevel 1 goto :ready
    timeout /t 1 /nobreak >nul
)
echo.
echo [错误] 服务启动超时。常见原因：
echo        1. 杀毒软件拦截了 Python 进程
echo        2. 防火墙阻止了本机端口
echo  请尝试手动启动：cd backend ^&^& python server.py
pause
exit /b 1

:ready
echo [就绪] 服务已就绪，正在打开浏览器...
start "" "http://127.0.0.1:8765/index.html"

echo.
echo ============================================
echo   平台已启动！
echo   访问地址: http://127.0.0.1:8765/index.html
echo.
echo   关闭本窗口不会影响平台运行
echo   停止服务: 关闭两个最小化的 Python 窗口
echo   或任务管理器结束 python 进程
echo ============================================
echo.
pause