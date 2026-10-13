@echo off
title MusicGrabber - Stop

set CONFIG_DIR=%APPDATA%\MusicGrabber
if not exist "%CONFIG_DIR%\docker-compose.yml" (
    echo  MusicGrabber does not appear to be running.
    pause
    exit /b 0
)

echo  Stopping MusicGrabber...
cd /d "%CONFIG_DIR%"
docker compose down

echo  MusicGrabber stopped.
timeout /t 2 /nobreak >nul
