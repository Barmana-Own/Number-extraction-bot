@echo off
cd /d "%~dp0"
chcp 65001 >nul
python "%~dp0status.py"
