# The platform as ONE image: MCP transport + REST twin + embed host, same origin.
#
# The image is CODE ONLY. Everything the platform reads or writes at runtime (the
# rasters, the corpus, the contributed catalogs, the receipt database) is mounted at
# /app/cache and /app/conf by the host and never baked in: a build input that lives
# outside git cannot be a build input in CI, and one image then serves every target
# byte for byte. Each base is pinned by digest (Dependabot bumps them), the Litestream
# tarball is checksummed, uv is a pinned release, and the server runs as uid 1000.
#
# Platform pinned: the Litestream asset is linux-amd64 and every target is amd64;
# without this an arm64 build host produces an image that dies on boot.

# ---- web: the embed host, built to static files ----
FROM --platform=linux/amd64 node:22-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS web
WORKDIR /w
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web/ ./
RUN npm run build

# ---- runtime ----
FROM --platform=linux/amd64 python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

# Litestream replicates SQLite continuously to object storage. Cloud Run's disk is
# ephemeral, so without it a redeploy silently destroys every receipt ever minted.
# The SHA-256 is of the v0.3.13 linux-amd64 release asset, computed on 2 Nov 2026.
ARG LITESTREAM_VERSION=0.3.13
ARG LITESTREAM_SHA256=eb75a3de5cab03875cdae9f5f539e6aedadd66607003d9b1e7a9077948818ba0
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl libexpat1 \
 && curl -fsSL -o /tmp/litestream.tar.gz \
      "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz" \
 && echo "${LITESTREAM_SHA256}  /tmp/litestream.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/litestream.tar.gz -C /usr/local/bin litestream \
 && rm /tmp/litestream.tar.gz \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

# uv as a pinned release copied from its own image, not pip-installed unpinned.
COPY --from=ghcr.io/astral-sh/uv:0.11.13@sha256:841c8e6fe30a8b07b4478d12d0c608cba6de66102d29d65d1cc423af86051563 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies from the LOCKFILE, hash-verified, in their own layer so app edits don't
# re-resolve them. --frozen never touches the lock; --no-install-workspace leaves the
# app itself out of the venv: it runs from PYTHONPATH below, exactly as on a checkout.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace --package global-risk-api \
 && rm -rf /root/.cache/uv
# The native wheels must load here, not at the first request: rasterio needs libexpat1
# from the base image, and a base-image bump that drops it fails this layer, not the boot.
RUN /app/.venv/bin/python -c "import rasterio, shapely, numpy, fastmcp, uvicorn"

# Layout mirrors the repo so config.py's repo-root-relative defaults resolve unchanged
# (/app/conf, /app/cache): no path rewriting, no drift from local.
COPY apps/api/src ./apps/api/src
COPY conf ./conf
COPY deploy ./deploy
# The platform SERVES trace-emit/trace-visualize as MCP resources; without this the
# deployed server declines them as not installed.
COPY .claude/skills/trace-emit ./.claude/skills/trace-emit
COPY .claude/skills/trace-visualize ./.claude/skills/trace-visualize
COPY .claude/skills/contribute-logic ./.claude/skills/contribute-logic
COPY --from=web /w/dist ./web

# One non-root user. The gate writes conf/*.contrib.yml and everything under cache/ at
# runtime, so those two trees belong to it; the code does not. Hugging Face Spaces run
# containers as uid 1000 regardless, so the same uid works on every target.
RUN chmod +x ./deploy/entrypoint.sh \
 && mkdir -p /app/cache/mcp /app/cache/rag \
 && groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app \
 && chown -R app:app /app/cache /app/conf

# The commit this image was built from, baked at build time and reported on /api/health
# so a deploy is confirmed over HTTPS. The deployer adds GRP_IMAGE_DIGEST at run time.
ARG GIT_COMMIT=unknown
ENV GRP_BUILD_COMMIT=${GIT_COMMIT}
LABEL org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.source="https://github.com/SERVIR-AI/global-platform"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/apps/api/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CACHE_DIR=/app/cache \
    GRP_DB_PATH=/app/cache/mcp/grp.db \
    GRP_WEB_DIST=/app/web \
    PORT=8080 \
    # Behind a hosted proxy the Host header is the service's public domain.
    # WITHOUT this, FastMCP auto-enables its localhost-only DNS-rebinding check
    # and answers 421 to every single tool call. Narrow it to the concrete
    # hostname once the URL is known; the OAuth transport gate is the real
    # access control.
    GRP_MCP_ALLOWED_HOSTS=*

USER 1000:1000
EXPOSE 8080
# Cold start is about 14 s on the production host; the start period covers a slow one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os, sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/api/health', timeout=4).status == 200 else 1)"
ENTRYPOINT ["./deploy/entrypoint.sh"]
