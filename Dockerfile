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
    unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Package managers beyond npm: corepack ships the pnpm and yarn shims with Node;
# bun is installed standalone. Lets stack detection run whichever a repo uses.
#
# We deliberately do NOT global-pin Yarn 4 via `corepack prepare yarn@stable`:
# corepack honors each repo's own `packageManager` field (downloading that exact
# version on first use), so a Yarn 1 repo gets Yarn 1 and a Yarn 4 repo gets
# Yarn 4. We only pre-activate a pinned classic fallback (yarn@1.22.22) for
# repos that declare no `packageManager` field, plus a pinned pnpm.
RUN corepack enable \
    && corepack prepare pnpm@9.15.4 --activate \
    && corepack prepare yarn@1.22.22 --activate \
    && curl -fsSL https://bun.sh/install | bash -s "bun-v1.1.42" \
    && ln -s /root/.bun/bin/bun /usr/local/bin/bun

RUN pip install --no-cache-dir playwright \
    && playwright install chromium \
    && playwright install-deps chromium

COPY src/agent/requirements.txt /tmp/agent-requirements.txt
RUN pip install --no-cache-dir -r /tmp/agent-requirements.txt

WORKDIR /app

COPY src/ /app/src/

CMD ["python", "-u", "-m", "src.agent.main"]
