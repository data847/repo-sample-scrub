# syntax=docker/dockerfile:1.7
#
# sample-scrub UI in a container. Build with `docker compose build` (see
# docker-compose.yml): codebase-profiler and repo-pii-scrub come in as named build
# contexts (public GitHub repos by default, or local checkouts via PROFILER_SRC /
# SCRUB_SRC), so this repo does not have to vendor them.
FROM python:3.12-slim

ARG GITLEAKS_VERSION=8.24.2
ARG TARGETARCH
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl build-essential \
 && rm -rf /var/lib/apt/lists/*

# gitleaks: the independent secret assertion in the package gate
RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in amd64|x86_64) a=x64;; arm64|aarch64) a=arm64;; *) echo "unsupported arch"; exit 1;; esac; \
    curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${a}.tar.gz" \
      | tar -xz -C /usr/local/bin gitleaks; \
    gitleaks version

# the two projects this tool drives
COPY --from=profiler-src / /opt/codebase-profiler
COPY --from=scrub-src    / /opt/repo-pii-scrub

# one interpreter for everything: profiler deps (repo listing/cloning) + the scrub engine
RUN pip install -r /opt/codebase-profiler/requirements.txt \
 && pip install -r /opt/repo-pii-scrub/requirements.txt \
 && pip install "git+https://github.com/Fermatix/repo-sanitizer" \
 && (pip check || true)   # informational: the profiler pins and the sanitizer overlap loosely

# the package gate shells out to zip
RUN apt-get update && apt-get install -y --no-install-recommends zip unzip \
 && rm -rf /var/lib/apt/lists/*

COPY sample_scrub /app/sample_scrub
WORKDIR /app

# unprivileged user; /repos is where the host's repos get bind-mounted (read-only)
RUN useradd -m -u 1000 app \
 && mkdir -p /repos /data/outputs /data/clones /data/hf \
 && chown -R app:app /data /opt/repo-pii-scrub /app
USER app

ENV CODEBASE_PROFILER_DIR=/opt/codebase-profiler \
    REPO_PII_SCRUB_DIR=/opt/repo-pii-scrub \
    SAMPLE_SCRUB_OUTPUTS_DIR=/data/outputs \
    SAMPLE_SCRUB_CLONES_DIR=/data/clones \
    SAMPLE_SCRUB_LOCAL_ROOTS=/repos \
    HF_HOME=/data/hf \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=*

EXPOSE 8795
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8795/api/config',timeout=4)" || exit 1

# listens on all container interfaces; publish the port to the HOST'S LOOPBACK only
ENTRYPOINT ["python", "-m", "sample_scrub", "ui", "--bind", "0.0.0.0", "--port", "8795"]
