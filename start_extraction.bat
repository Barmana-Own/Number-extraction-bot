@echo off
cd /d "%~dp0"
if exist "%~dp0dist\HamshmarehExtractor.exe" (
  start "Hamshmareh Extractor" "%~dp0dist\HamshmarehExtractor.exe"
) else (
  python "%~dp0start_extraction.py"
)
