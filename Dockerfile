FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright \
    && playwright install chromium \
    && playwright install-deps chromium

WORKDIR /app

COPY src/agent/ /app/

RUN pip install --no-cache-dir -r /app/requirements.txt 2>/dev/null || true

CMD ["python", "main.py"]
