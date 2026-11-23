FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /service

# Install only the Docker CLI client; container runtime access is delegated via DOCKER_HOST proxy
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /service/pyproject.toml
COPY sandbox_runner /service/sandbox_runner

RUN python -m pip install --upgrade pip \
    && python -m pip install .

EXPOSE 8001

CMD ["uvicorn", "sandbox_runner.main:app", "--host", "0.0.0.0", "--port", "8001"]