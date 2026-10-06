@echo off
chcp 65001 >nul
set "APP_EXE=%~dp0desktop-v3\MailGroup-3.0.0-x64-portable.exe"
if exist "%APP_EXE%" (
    start "" "%APP_EXE%"
    exit /b 0
)
echo 未找到桌面版程序，请打开 release\desktop-v3 或先运行 build_desktop.py。
pause
exit /b 1
