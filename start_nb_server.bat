@echo off
title NotebookLM Local Server
echo Запускаю локальный NotebookLM сервер...

set UV_PYTHON=C:\Users\Admin\AppData\Roaming\uv\tools\notebooklm-mcp-2026\Scripts\python.exe
set NOTEBOOKLM_LOCAL_SECRET=soul2026

cd /d "%~dp0"
"%UV_PYTHON%" nb_local_server.py
pause
