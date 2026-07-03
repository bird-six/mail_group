@echo off
chcp 65001 >nul
title 邮件群发系统
cd /d "%~dp0"

echo.
echo   ╔══════════════════════════════════════╗
echo   ║         邮件群发系统 v1.0            ║
echo   ╚══════════════════════════════════════╝
echo.
echo   正在启动服务，请稍候...

:: 启动后端（后台运行）
start "" /B "%~dp0backend\.venv\Scripts\python.exe" "%~dp0backend\main.py" >nul 2>&1

:: 等待 PID 文件生成（最多等 15 秒）
set PID=
set TRY=0
:wait_pid
timeout /t 1 /nobreak >nul
set /a TRY+=1
if exist "%~dp0backend\data\server.pid" (
    set /p PID=<"%~dp0backend\data\server.pid"
    goto :ready
)
if %TRY% LSS 15 goto :wait_pid

echo   服务启动超时，请检查日志: backend\data\server.log
echo   按任意键退出...
pause >nul
exit /b 1

:ready
:: 打开浏览器
start "" http://127.0.0.1:8000

echo.
echo   ╔══════════════════════════════════════╗
echo   ║  系统已启动！                        ║
echo   ║  浏览器已打开: http://127.0.0.1:8000 ║
echo   ║                                      ║
echo   ║  按任意键关闭服务并退出              ║
echo   ╚══════════════════════════════════════╝
echo.
pause >nul

:: 关闭后端
if defined PID (
    echo   正在关闭服务...
    taskkill /f /pid %PID% >nul 2>&1
    if exist "%~dp0backend\data\server.pid" del /q "%~dp0backend\data\server.pid" >nul 2>&1
)
echo   服务已关闭。
timeout /t 1 /nobreak >nul
