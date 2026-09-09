#!/usr/bin/env bash
# Deploy the platform to Cloud Run.
#
# The instance cap is LOAD-BEARING, which is why this is a script and not a
# command someone remembers. Litestream is a single-writer replicator with no
# leader election: two instances both restore the same generation, then each
# forks its own under the same bucket path. Receipts minted on the loser are
# invisible to the resolver and are destroyed on the next restore. Cloud Run
# overlaps instances on EVERY revision rollout, so this is not a spike scenario
# — it is the default one.
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT=<gcp-project-id>}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-servirplatform}"
BUCKET="${BUCKET:-${PROJECT}-servirplatform-receipts}"
EMBED_MODEL="${EMBED_MODEL:-gemini-embedding-001}"

: "${GRP_OAUTH_ENABLED:-0}"
if [ "${GRP_OAUTH_ENABLED:-0}" != "1" ] && [ "${GRP_ALLOW_ANONYMOUS:-}" != "1" ]; then
  echo "Refusing to deploy: authentication is off. Set GRP_OAUTH_ENABLED=1 (with" >&2
  echo "the AuthKit settings below) or GRP_ALLOW_ANONYMOUS=1 to deploy with no gate." >&2
  exit 1
fi
if [ "${GRP_OAUTH_ENABLED:-0}" = "1" ]; then
  : "${GRP_AUTHKIT_DOMAIN:?set GRP_AUTHKIT_DOMAIN=<authkit domain> — the login server}"
  : "${GRP_PUBLIC_URL:?set GRP_PUBLIC_URL=<service url, no trailing slash> — must match an AuthKit-registered origin}"
  : "${GRP_AUTHKIT_CLIENT_ID:?set GRP_AUTHKIT_CLIENT_ID=<web-login OAuth client id>}"
fi

PROJNUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
RUNTIME_SA="${RUNTIME_SA:-${PROJNUM}-compute@developer.gserviceaccount.com}"

echo "==> receipts bucket: gs://${BUCKET}"
gcloud storage buckets describe "gs://${BUCKET}" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://${BUCKET}" --project "$PROJECT" --location "$REGION"
# Versioning is the backstop behind litestream's retention: it makes an
# accidental overwrite recoverable rather than final.
gcloud storage buckets update "gs://${BUCKET}" --versioning --project "$PROJECT"

echo "==> runtime identity: ${RUNTIME_SA}"
# Embeddings authenticate as the service itself — that is why no API key exists
# on disk. Without aiplatform.user every retrieval call declines.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/aiplatform.user" \
  --condition=None --quiet >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/storage.objectAdmin" \
  --project "$PROJECT" >/dev/null

echo "==> deploying ${SERVICE} to ${REGION}"
gcloud run deploy "$SERVICE" \
  --project "$PROJECT" \
  --region "$REGION" \
  --source . \
  --service-account "$RUNTIME_SA" \
  --allow-unauthenticated \
  --max-instances=1 \
  --min-instances=1 \
  --no-cpu-throttling \
  --concurrency=16 \
  --cpu=1 \
  --memory=2Gi \
  --timeout=300 \
  --set-env-vars="LITESTREAM_BUCKET=${BUCKET},GRP_OAUTH_ENABLED=${GRP_OAUTH_ENABLED:-0},GRP_PUBLIC_URL=${GRP_PUBLIC_URL:-},GRP_AUTHKIT_DOMAIN=${GRP_AUTHKIT_DOMAIN:-},GRP_AUTHKIT_CLIENT_ID=${GRP_AUTHKIT_CLIENT_ID:-},EMBEDDING_PROVIDER=vertex,EMBEDDING_MODEL=${EMBED_MODEL},VERTEX_PROJECT=${PROJECT},VERTEX_LOCATION=${REGION},GRP_MCP_ALLOWED_HOSTS=*,CORS_ORIGINS=*"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
        --format='value(status.url)')

cat <<EOF

Deployed: ${URL}

  Standard app  ${URL}        (sign in with AuthKit)
  MCP endpoint  ${URL}/mcp    (OAuth: the client logs the user in)
  Receipt       ${URL}/api/resolve/receipt/<id>   (public, by design)
  Embed host    ${URL}/?embed=provenance_graph&receipt_id=<id>

Connect an MCP client (browser login; no secret to paste):
  claude mcp add --transport http servirplatform ${URL}/mcp

Reminders:
  - --allow-unauthenticated is Cloud Run's IAM, not ours: the AuthKit login is
    the real gate (the MCP transport and the web app's session gate).
  - Register ${URL} in the WorkOS dashboard first: the /auth/callback redirect
    URI and the ${URL}/mcp resource indicator must exist or logins fail with a
    redirect/audience mismatch.
  - --max-instances=1 is required, not tuning. See the note at the top.
  - --no-cpu-throttling keeps litestream replicating between requests.
EOF
