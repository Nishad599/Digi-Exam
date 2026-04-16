FROM python:3.10-slim

WORKDIR /app

# Install system dependencies (curl for downloading models, build-essential for some pip packages)
RUN apt-get update && apt-get install -y \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies first to cache them
COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

# Pre-download InsightFace models to bake them directly into the Docker image.
# This makes the image completely offline-ready.
RUN python -c "from insightface.app import FaceAnalysis; app = FaceAnalysis(name='buffalo_l', allowed_modules=['detection', 'recognition']); app.prepare(ctx_id=-1, det_size=(320, 320))"

# Copy the rest of the app code
COPY . .

# Expose the edge terminal port
EXPOSE 8200

# Start command
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8200"]
