FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRACKER_DATA_DIR=/data

WORKDIR /app

FROM base AS operations-base
RUN apt-get update && \
    apt-get install --no-install-recommends -y postgresql-client && \
    rm -rf /var/lib/apt/lists/*

FROM base AS application
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import pathlib,subprocess,sys,tomllib; p=tomllib.loads(pathlib.Path('pyproject.toml').read_text()); requirements=[*p['build-system']['requires'],*p['project']['dependencies'],*p['project']['optional-dependencies']['s3']]; subprocess.check_call([sys.executable,'-m','pip','install',*requirements])"

COPY README.md ./
COPY api ./api
COPY ui ./ui
COPY scripts ./scripts
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

RUN useradd --create-home --uid 10001 tracker && \
    mkdir -p /data && chown -R tracker:tracker /data

FROM application AS server
USER tracker

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]

FROM operations-base AS operations
COPY --from=application /usr/local /usr/local
COPY --from=application --chown=10001:10001 /app /app
RUN useradd --create-home --uid 10001 tracker && \
    mkdir -p /data && chown -R tracker:tracker /data
USER tracker
ENTRYPOINT ["dayfinch-ops"]
