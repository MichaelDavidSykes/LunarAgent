FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir .

RUN useradd --system --uid 10001 --no-create-home lunaragent

USER 10001:10001

EXPOSE 8310

HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8310/live', timeout=2).read()"]

CMD ["uvicorn", "lunar_agent.main:app", "--host", "0.0.0.0", "--port", "8310"]
