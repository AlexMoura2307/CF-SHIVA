@echo off
cd /d "%~dp0"
py CF_SHIVA.py
if errorlevel 1 pause
