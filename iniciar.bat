@echo off
cd /d "%~dp0"
echo Iniciando Audio para Texto em http://127.0.0.1:8000 ...
start "" http://127.0.0.1:8000
uv run main.py
