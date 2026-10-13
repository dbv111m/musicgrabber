@echo off
setlocal EnableDelayedExpansion

title MusicGrabber

:: -------------------------------------------------------
:: Find config - prefer local copy, fall back to AppData
:: -------------------------------------------------------
set CONFIG_DIR=%APPDATA%\MusicGrabber
if not exist "%CONFIG_DIR%\docker-compose.yml" (
    echo.
    echo  MusicGrabber does not appear to be set up yet.
    echo  Please run setup.bat first.
    echo.
    pause
    exit /b 1
)

:: -------------------------------------------------------
:: Check Docker is running
:: -------------------------------------------------------
docker info >nul 2>&1
if errorlevel 1 (
    echo  Docker is not running. Starting Docker Desktop...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe" 2>nul
    :: Also try the non-default install path
    if errorlevel 1 (
        start "" "%LOCALAPPDATA%\Docker\Docker Desktop.exe" 2>nul
    )
    echo  Waiting for Docker to start (this can take up to 60 seconds)...
    set WAITED=0
    :wait_loop
    timeout /t 5 /nobreak >nul
    set /a WAITED+=5
    docker info >nul 2>&1
    if not errorlevel 1 goto docker_ready
    if !WAITED! geq 90 (
        echo.
        echo  Docker did not start in time. Please open Docker Desktop manually,
        echo  wait for it to finish loading, then run this script again.
        echo.
        pause
        exit /b 1
    )
    goto wait_loop
)

:docker_ready
:: -------------------------------------------------------
:: Start the container
:: -------------------------------------------------------
cd /d "%CONFIG_DIR%"
docker compose up -d
if errorlevel 1 (
    echo.
    echo  ERROR: Failed to start MusicGrabber.
    echo  Try running setup.bat again, or check Docker Desktop for errors.
    echo.
    pause
    exit /b 1
)

:: Give it a moment to come up, then open the browser
timeout /t 2 /nobreak >nul
start http://localhost:38274
