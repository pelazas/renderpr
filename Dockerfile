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

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Package managers beyond npm: corepack ships the pnpm and yarn shims with Node;
# bun is installed standalone. Lets stack detection run whichever a repo uses.
# Install into shared, world-readable locations (not /root) so the unprivileged
# `runner` user that executes untrusted installs/dev servers can use them too.
ENV COREPACK_HOME=/opt/corepack
ENV BUN_INSTALL=/opt/bun
RUN corepack enable \
    && corepack prepare pnpm@latest --activate \
    && corepack prepare yarn@stable --activate \
    && curl -fsSL https://bun.sh/install | bash \
    && ln -s /opt/bun/bin/bun /usr/local/bin/bun \
    && chmod -R a+rX /opt/bun \
    && chown -R 10001:10001 /opt/corepack

RUN pip install --no-cache-dir playwright \
    && playwright install chromium \
    && playwright install-deps chromium

COPY src/agent/requirements.txt /tmp/agent-requirements.txt
RUN pip install --no-cache-dir -r /tmp/agent-requirements.txt

# Unprivileged user that runs the untrusted PR processes (dependency install and
# the dev server). The container still starts as root so the agent can hold
# secrets and drop privileges for those children; running them as `runner` keeps
# them out of root's /proc and the task-role credentials (threat-model F-1).
RUN groupadd -g 10001 runner \
    && useradd -m -u 10001 -g 10001 -s /usr/sbin/nologin runner

WORKDIR /app

COPY src/ /app/src/

CMD ["python", "-u", "-m", "src.agent.main"]
