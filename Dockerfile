FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --fix-missing \
    python3.12 \
    python3.12-venv \
    python3-pip \
    build-essential \
    cmake \
    libopenblas-dev \
    liblapack-dev \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    wget \
    git \
    && rm -rf /var/lib/apt/lists/*

# Make python3.12 default
RUN ln -sf /usr/bin/python3.12 /usr/bin/python3
RUN ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /app

COPY requirements-linux.txt .
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements-linux.txt
RUN pip install --no-cache-dir insightface==0.7.3 gunicorn

COPY . .

RUN mkdir -p uploads_profile_pics

EXPOSE 8001

CMD ["gunicorn", "main:app", \
    "--worker-class", "uvicorn.workers.UvicornWorker", \
    "--workers", "4", \
    "--bind", "0.0.0.0:8001", \
    "--timeout", "120"]