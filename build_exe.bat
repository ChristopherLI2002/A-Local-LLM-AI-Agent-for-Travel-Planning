@echo off
REM Build LocalLLMTravelAgent.exe with PyInstaller (Windows).
setlocal
cd /d "%~dp0"

echo Installing build tools...
python -m pip install -q --upgrade pip
python -m pip install -q "pyinstaller>=6.0" -r requirements.txt
if errorlevel 1 (
  echo Failed to install dependencies.
  exit /b 1
)

echo.
echo Ensuring Playwright Chromium is installed...
set "PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\ms-playwright"
python -m playwright install chromium
if errorlevel 1 (
  echo Warning: could not install Chromium. Install Google Chrome or Edge instead.
)

echo.
echo Close any running LocalLLMTravelAgent.exe window first if rebuild fails with Access Denied.
echo Building LocalLLMTravelAgent.exe...
python -m PyInstaller --noconfirm launch.spec
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)

if exist ".env.example" copy /Y ".env.example" "dist\LocalLLMTravelAgent\.env.example" >nul
if exist ".env" copy /Y ".env" "dist\LocalLLMTravelAgent\.env" >nul

echo.
echo Done.
echo   EXE:  %cd%\dist\LocalLLMTravelAgent\LocalLLMTravelAgent.exe
echo.
echo Needs on this PC:
echo   1. Ollama + ollama pull qwen2.5:3b
echo   2. Chromium in %%LOCALAPPDATA%%\ms-playwright  (or Chrome / Edge)
echo   Close old app windows before rebuilding.
echo.
endlocal
