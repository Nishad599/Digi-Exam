FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# Fix apt sources + install deps
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
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Fix pip / distutils issue
RUN python -m ensurepip --upgrade && \
    pip install --upgrade pip setuptools

WORKDIR /app

COPY requirements-linux.txt .

RUN pip install --no-cache-dir -r requirements-linux.txt
RUN pip install --no-cache-dir insightface==0.7.3 gunicorn

COPY . .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]