# ==============================================================================
# Multi-stage Dockerfile for CustomBot
#
# Supply-chain hardened:
#   - Base images pinned by SHA256 digest for reproducible builds
#   - Production deps installed with --require-hashes for integrity verification
#   - Hash-locked deps in requirements-lock.txt (regenerate with: pip-compile --generate-hashes)
#
# Stages:
#   1. builder  — install all deps (including dev) for optional test runs
#   2. runtime  — production image with only hash-verified runtime deps
#
# Usage:
#   docker build -t custombot .
#   docker run -d \
#     -p 8080:8080 \
#     -v ./config.json:/app/config.json \
#     -v ./workspace:/app/workspace \
#     custombot
# ==============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Builder — installs dependencies
# ---------------------------------------------------------------------------
FROM python:3.14.0-slim-bookworm@sha256:d13fa0424035d290decef3d575cea23d1b7d5952cdf429df8f5542c71e961576 AS builder

WORKDIR /build

# Install build dependencies for packages that need compilation
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy dependency manifests first for layer caching
COPY requirements.txt requirements-lock.txt pyproject.toml ./

# Install production dependencies into a clean prefix (hash-verified)
RUN pip install --no-cache-dir --require-hashes --prefix=/install \
      -r requirements-lock.txt

# Install dev dependencies into a separate prefix (for optional test stage)
RUN pip install --no-cache-dir --prefix=/install-dev \
      -r requirements.txt ".[dev]"

# ---------------------------------------------------------------------------
# Stage 2: Runtime — minimal production image
# ---------------------------------------------------------------------------
FROM python:3.14.0-slim-bookworm@sha256:d13fa0424035d290decef3d575cea23d1b7d5952cdf429df8f5542c71e961576 AS runtime

LABEL org.opencontainers.image.title="CustomBot"
LABEL org.opencontainers.image.description="A lightweight WhatsApp AI assistant"
LABEL org.opencontainers.image.version="1.0.0"

# Create non-root user for security
RUN groupadd --gid 1000 custombot && \
    useradd --uid 1000 --gid custombot --create-home custombot

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY src/ src/
COPY main.py .

# Create workspace directories with correct ownership
RUN mkdir -p workspace/logs/llm workspace/skills workspace/whatsapp_data && \
    chown -R custombot:custombot /app

# Switch to non-root user
USER custombot

# Default environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Configurable health check parameters (override at build time)
ARG HEALTH_INTERVAL=30s
ARG HEALTH_TIMEOUT=5s
ARG HEALTH_START_PERIOD=30s
ARG HEALTH_RETRIES=3

# Health check — requires --health-port to be passed at runtime
HEALTHCHECK --interval=${HEALTH_INTERVAL} --timeout=${HEALTH_TIMEOUT} --start-period=${HEALTH_START_PERIOD} --retries=${HEALTH_RETRIES} \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Default port for the health endpoint
EXPOSE 8080

# Persistent data volumes
VOLUME ["/app/workspace", "/app/config.json"]

ENTRYPOINT ["python", "main.py"]
CMD ["start", "--health-port", "8080"]
