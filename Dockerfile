FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/lunaragent \
    CODEX_HOME=/codex-auth \
    LUNAR_AGENT_APP_ROOT=/app

WORKDIR /app

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY package.json package-lock.json pyproject.toml README.md ./
RUN npm ci --omit=dev --ignore-scripts

COPY src ./src
COPY codex_runtime ./codex_runtime

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir .

RUN useradd --system --uid 10001 --create-home --home-dir /home/lunaragent lunaragent \
    && mkdir -p /tmp/lunar-agent-codex-workspaces /codex-auth \
    && chown -R lunaragent:lunaragent /tmp/lunar-agent-codex-workspaces /codex-auth /home/lunaragent

USER 10001:10001

EXPOSE 8310

HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8310/live', timeout=2).read()"]

CMD ["uvicorn", "lunar_agent.main:app", "--host", "0.0.0.0", "--port", "8310"]
