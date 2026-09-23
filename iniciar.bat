@echo off
rem Abre o Zapscribe sem janela de terminal: ele fica como um ícone ao lado do relógio.
rem Para ver os registros no terminal, rode: uv run main.py
cd /d "%~dp0"
start "" uv run pythonw tray.py
