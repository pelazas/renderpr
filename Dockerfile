FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    libcairo2-dev \
    libjpeg-dev \
    libpango1.0-dev \
    libpixman-1-dev \
    libpng-dev \
    libsqlite3-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Package managers beyond npm: corepack ships the pnpm and yarn shims with Node;
# bun is installed standalone. Lets stack detection run whichever a repo uses.
RUN corepack enable \
    && corepack prepare pnpm@latest --activate \
    && corepack prepare yarn@stable --activate \
    && curl -fsSL https://bun.sh/install | bash \
    && ln -s /root/.bun/bin/bun /usr/local/bin/bun

RUN pip install --no-cache-dir playwright \
    && playwright install chromium \
    && playwright install-deps chromium

COPY src/agent/requirements.txt /tmp/agent-requirements.txt
RUN pip install --no-cache-dir -r /tmp/agent-requirements.txt

WORKDIR /app

COPY src/ /app/src/

CMD ["python", "-u", "-m", "src.agent.main"]
