FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5115

WORKDIR /app

# System deps (if needed later)
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy source
COPY . .

EXPOSE 5115

CMD ["python", "app.py"]


