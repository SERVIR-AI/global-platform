# The platform as ONE image: MCP transport + REST twin + embed host, same origin.
#
# Deliberately excluded: cache/tiffs (1.8GB of flood/risk rasters this deployment
# does not serve). The RAG corpus IS baked in — it is read-only evidence, so it
# belongs in the image; only grp.db is runtime state and it is replicated out.

# Platform pinned: the litestream asset below is linux-amd64, and Cloud Run runs
# amd64 — without this an arm64 build host produces an image that dies on boot.
# ---- web: the embed host, built to static files ----
FROM --platform=linux/amd64 node:22-slim AS web
WORKDIR /w
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

# ---- runtime ----
FROM --platform=linux/amd64 python:3.12-slim

# Litestream replicates SQLite continuously to object storage. Cloud Run's disk is
# ephemeral, so without it a redeploy silently destroys every receipt ever minted.
ARG LITESTREAM_VERSION=0.3.13
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz" \
    | tar -xz -C /usr/local/bin litestream \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependencies from the LOCKFILE, hash-verified, in their own layer so app edits
# don't re-resolve them.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
RUN uv export --frozen --no-dev --package global-risk-api --no-emit-workspace \
      --format requirements-txt > /tmp/requirements.txt \
 && uv pip install --system --no-cache -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# Layout mirrors the repo so config.py's repo-root-relative defaults resolve
# unchanged (/app/conf, /app/cache) — no path rewriting, no drift from local.
COPY apps/api/src ./apps/api/src
COPY conf ./conf
COPY cache/rag ./cache/rag
COPY deploy ./deploy
COPY --from=web /w/dist ./web
# Some hosts (Hugging Face Spaces) run the container as a non-root uid, so the
# receipts dir has to be writable by whoever ends up owning the process.
RUN chmod +x ./deploy/entrypoint.sh && mkdir -p /app/cache/mcp && chmod 777 /app/cache/mcp

ENV PYTHONPATH=/app/apps/api/src \
    PYTHONUNBUFFERED=1 \
    CACHE_DIR=/app/cache \
    GRP_DB_PATH=/app/cache/mcp/grp.db \
    GRP_WEB_DIST=/app/web \
    PORT=8080 \
    # Behind a hosted proxy the Host header is the service's public domain.
    # WITHOUT this, FastMCP auto-enables its localhost-only DNS-rebinding check
    # and answers 421 to every single tool call. Narrow it to the concrete
    # hostname once the URL is known; the token gate is the real access control.
    GRP_MCP_ALLOWED_HOSTS=*

EXPOSE 8080
ENTRYPOINT ["./deploy/entrypoint.sh"]
