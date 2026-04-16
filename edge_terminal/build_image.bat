@echo off
echo ========================================================
echo   Building Digi-Exam Edge Terminal Docker Image
echo ========================================================
echo.

docker build -t digi-edge-terminal -f Dockerfile .
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Docker build failed.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ========================================================
echo   Saving Image to digi-edge.tar... This may take a minute.
echo ========================================================
docker save -o digi-edge.tar digi-edge-terminal

echo.
echo ========================================================
echo   DONE! 
echo ========================================================
echo Package generation complete.
echo.
echo Instructions for conductors:
echo 1. Give them the `digi-edge.tar` file, the `.env` file, and `start_terminal.bat`.
echo 2. Tell them to double-click `start_terminal.bat`.
echo.
pause
