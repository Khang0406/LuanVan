# syntax=docker/dockerfile:1.7
FROM docker:29-cli AS docker_cli

FROM python:3.12-slim

ARG KUBECTL_VERSION=v1.29.6

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLATFORM_HOST=0.0.0.0 \
    PLATFORM_PORT=8000 \
    DOCKER_BUILDKIT=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/docker-buildx \
    /usr/local/libexec/docker/cli-plugins/docker-buildx

RUN architecture="$(dpkg --print-architecture)" \
    && curl --fail --location --retry 3 \
       "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${architecture}/kubectl" \
       --output /usr/local/bin/kubectl \
    && chmod 0755 /usr/local/bin/kubectl

RUN groupadd --gid 10001 platform \
    && useradd --uid 10001 --gid platform --create-home platform \
    && install -d -o platform -g platform /app /var/lib/platform

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --chown=platform:platform . .
USER 10001:10001

EXPOSE 8000

# SQLite requires one web replica. Pipeline execution runs in a separate
# container/process with ``python -m app.pipeline_worker``.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "120", "--graceful-timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "run:app"]
