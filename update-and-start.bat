@echo off
setlocal

cd /d "%~dp0"

echo Pulling latest code...
git pull
if errorlevel 1 goto failed

echo Starting Docker stack...
docker compose up --build -d
if errorlevel 1 goto failed

echo Done.
exit /b 0

:failed
echo Failed. Check the error above.
exit /b 1
