FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y \
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
    && rm -rf /var/lib/apt/lists/*

# Fix distutils issue (important)
RUN python -m ensurepip --upgrade && \
    pip install --upgrade pip setuptools

WORKDIR /app

COPY requirements-linux.txt .

RUN pip install --no-cache-dir -r requirements-linux.txt

# Extra installs
RUN pip install --no-cache-dir insightface==0.7.3 gunicorn

COPY . .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]