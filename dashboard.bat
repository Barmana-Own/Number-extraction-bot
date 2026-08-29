@echo off
cd /d "%~dp0"
start "IranCell extraction dashboard" /min python "%~dp0dashboard.py" --output "%~dp0output" --host 127.0.0.1 --port 8765 --open-browser
