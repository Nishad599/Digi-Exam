@echo off
echo ========================================================
echo   Starting Digi-Exam Edge Terminal
echo ========================================================
echo.

:: Check if the image exists in Docker already
docker image inspect digi-edge-terminal >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [INFO] Edge Terminal image not found locally. Loading from digi-edge.tar...
    if not exist digi-edge.tar (
        echo [ERROR] digi-edge.tar file is missing! Please place it in this folder.
        pause
        exit /b 1
    )
    docker load -i digi-edge.tar
)

echo.
echo [INFO] Ensuring no old terminal is running...
docker rm -f digi-edge-container >nul 2>&1

echo.
if exist .env (
    echo [INFO] Found .env file, passing to container...
    set ENV_FLAG=--env-file .env
) else (
    echo [WARNING] No .env file found. Make sure EDGE_HMAC_SECRET is set inside it if it fails!
    set ENV_FLAG=
)

echo [INFO] Starting Terminal on http://localhost:8200 ...
docker run -d --name digi-edge-container -p 8200:8200 %ENV_FLAG% digi-edge-terminal

echo.
echo ========================================================
echo   Edge Terminal is Running!
echo   Open your browser to: http://localhost:8200
echo ========================================================
echo.
pause
