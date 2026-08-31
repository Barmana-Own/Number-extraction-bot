@echo off
cd /d "%~dp0"
python "%~dp0irancell_number_bot.py" --products all --delay 8 --max-retries 10 --retry-forever-429 --rate-limit-cooldown 600 --output "%~dp0output"
