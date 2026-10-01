@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

REM 优先尝试直接运行 EXE（无黑框、免命令行）
if exist "久坐休息提醒.exe" (
    start "" "久坐休息提醒.exe"
    goto :eof
)

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.8+ and check "Add to PATH".
    pause
    exit /b 1
)

python -c "import PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing Pillow ^(needed for tray icon^)...
    python -m pip install Pillow
    if errorlevel 1 (
        echo [ERROR] Pillow install failed. Check your network.
        pause
        exit /b 1
    )
)

python break_reminder.py
