@echo off
cd /d "%~dp0backend"
title Mail Group - Server

echo.
echo   ========================================
echo     Mail Group System  v1.0
echo   ========================================
echo.
echo   Starting server...
echo   The browser will open automatically.
echo   Close this window to stop the server.
echo.

.venv\Scripts\python.exe main.py

echo.
echo   Server has been stopped.
pause
