@echo off
cd /d "%~dp0"
echo ============================================
echo  CF SHIVA - INSTALADOR V3
echo ============================================
where py >nul 2>&1
if errorlevel 1 (
  echo Python nao encontrado. Instale Python 3.11+ e marque ADD PYTHON TO PATH.
  pause
  exit /b 1
)
py -m pip install --upgrade pip
py -m pip install selenium requests
if errorlevel 1 (
  echo Falha ao instalar Selenium/requests.
  pause
  exit /b 1
)
echo.
echo Instalacao concluida.
echo Criando atalho na Area de Trabalho...
call "%~dp0CRIAR_ATALHO.bat"
echo.
echo Execute o atalho "CF SHIVA" na Area de Trabalho para iniciar.
pause
