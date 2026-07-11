FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp

WORKDIR /app

COPY pyproject.toml requirements.lock README.md VERSION ./
COPY src ./src

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps . \
    && python -m playwright install --with-deps chromium \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data /ms-playwright \
    && chown -R 10001:10001 /data /ms-playwright \
    && chmod -R a+rX /ms-playwright

USER 10001:10001

ENTRYPOINT ["xvfb-run", "-a", "-s", "-screen 0 1365x900x24"]
CMD ["python", "-m", "wb_price_bot", "run"]
