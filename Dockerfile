FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright \
    && playwright install chromium \
    && playwright install-deps chromium

COPY src/agent/requirements.txt /tmp/agent-requirements.txt
RUN pip install --no-cache-dir -r /tmp/agent-requirements.txt

WORKDIR /app

COPY src/ /app/src/

CMD ["python", "-u", "-m", "src.agent.main"]
