#!/bin/bash
echo "========================================================"
echo "  Building Digi-Exam Edge Terminal Docker Image"
echo "========================================================"
echo ""

docker build -t digi-edge-terminal -f Dockerfile .
if [ $? -ne 0 ]; then
    echo ""
    echo "[ERROR] Docker build failed."
    exit 1
fi

echo ""
echo "========================================================"
echo "  Saving Image to digi-edge.tar... This may take a minute."
echo "========================================================"
docker save -o digi-edge.tar digi-edge-terminal

echo ""
echo "========================================================"
echo "  DONE! "
echo "========================================================"
echo "Package generation complete."
echo ""
echo "Instructions for conductors:"
echo "1. Give them the 'digi-edge.tar' file, the '.env' file, and 'start_terminal.sh'."
echo "2. Tell them to run './start_terminal.sh' (they may need to run 'chmod +x start_terminal.sh' first)."
echo ""
