@echo off
setlocal
cd /d "%~dp0"
set "TARGET=%~dp0ABRIR.bat"
set "ICON=%~dp0CF_SHIVA.ico"
set "DESKTOP=%USERPROFILE%\Desktop"
set "SHORTCUT=%DESKTOP%\CF SHIVA.lnk"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut($env:SHORTCUT); $s.TargetPath=$env:TARGET; $s.WorkingDirectory='%~dp0'; $s.IconLocation=$env:ICON; $s.Description='CF SHIVA - Extrator de Certificados'; $s.Save()"
if errorlevel 1 (
  echo Nao foi possivel criar o atalho.
  pause
  exit /b 1
)
echo.
echo Atalho criado na Area de Trabalho: CF SHIVA
pause
endlocal
