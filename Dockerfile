FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.12 via deadsnakes
RUN apt-get update && apt-get install -y software-properties-common && \
    add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && apt-get install -y \
    python3.12 \
    python3.12-venv \
    python3-pip \
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

# Set python3.12 as default
RUN ln -sf /usr/bin/python3.12 /usr/bin/python
RUN ln -sf /usr/bin/python3.12 /usr/bin/python3

# Install pip for 3.12 properly
RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3.12

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