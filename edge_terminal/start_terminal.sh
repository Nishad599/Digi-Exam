#!/bin/bash
echo "========================================================"
echo "  Starting Digi-Exam Edge Terminal"
echo "========================================================"
echo ""

# Check if the image exists in Docker already
if ! docker image inspect digi-edge-terminal >/dev/null 2>&1; then
    echo "[INFO] Edge Terminal image not found locally. Loading from digi-edge.tar..."
    if [ ! -f "digi-edge.tar" ]; then
        echo "[ERROR] digi-edge.tar file is missing! Please place it in this folder."
        exit 1
    fi
    docker load -i digi-edge.tar
fi

echo ""
echo "[INFO] Ensuring no old terminal is running..."
docker rm -f digi-edge-container >/dev/null 2>&1

echo ""
if [ -f ".env" ]; then
    echo "[INFO] Found .env file, passing to container..."
    ENV_FLAG="--env-file .env"
else
    echo "[WARNING] No .env file found. Make sure EDGE_HMAC_SECRET is set inside it if it fails!"
    ENV_FLAG=""
fi

echo "[INFO] Starting Terminal on http://localhost:8200 ..."
docker run -d --name digi-edge-container -p 8200:8200 $ENV_FLAG digi-edge-terminal

echo ""
echo "========================================================"
echo "  Edge Terminal is Running!"
echo "  Open your browser to: http://localhost:8200"
echo "========================================================"
echo ""
