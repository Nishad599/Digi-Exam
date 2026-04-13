FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.12 and system dependencies
RUN apt-get update && apt-get install -y software-properties-common && \
    add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && apt-get install -y \
    python3.12 \
    python3.12-venv \
    build-essential \
    cmake \
    libopenblas-dev \
    liblapack-dev \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    wget \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set Python 3.12 as default
RUN ln -sf /usr/bin/python3.12 /usr/bin/python
RUN ln -sf /usr/bin/python3.12 /usr/bin/python3

# Install pip properly (fix for Python 3.12)
RUN python3.12 -m ensurepip --upgrade
RUN python3.12 -m pip install --upgrade pip setuptools wheel

WORKDIR /app

# Copy requirements
COPY requirements-linux.txt .

# Install dependencies
RUN python -m pip install --no-cache-dir -r requirements-linux.txt
RUN python -m pip install --no-cache-dir insightface==0.7.3 gunicorn

# Copy project
COPY . .

# Create required folder
RUN mkdir -p uploads_profile_pics

EXPOSE 8001

CMD ["gunicorn", "main:app", \
    "--worker-class", "uvicorn.workers.UvicornWorker", \
    "--workers", "4", \
    "--bind", "0.0.0.0:8001", \
    "--timeout", "120"]