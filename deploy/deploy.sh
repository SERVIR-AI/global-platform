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

: "${GRP_API_TOKEN:?set GRP_API_TOKEN=<a long random string> — this gates the tools}"

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
  --set-env-vars="LITESTREAM_BUCKET=${BUCKET},GRP_API_TOKEN=${GRP_API_TOKEN},EMBEDDING_PROVIDER=vertex,EMBEDDING_MODEL=${EMBED_MODEL},VERTEX_PROJECT=${PROJECT},VERTEX_LOCATION=${REGION},GRP_MCP_ALLOWED_HOSTS=*,CORS_ORIGINS=*"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
        --format='value(status.url)')

cat <<EOF

Deployed: ${URL}

  MCP endpoint   ${URL}/mcp          (needs the bearer token)
  Receipt        ${URL}/api/resolve/receipt/<id>   (public, by design)
  Embed host     ${URL}/?embed=provenance_graph&receipt_id=<id>

Connect a client:
  claude mcp add --transport http servirplatform ${URL}/mcp --header "Authorization: Bearer \$GRP_API_TOKEN"

Reminders:
  - --allow-unauthenticated is Cloud Run's IAM, not ours: MCP clients cannot do
    GCP IAM, so the bearer token is the actual gate.
  - --max-instances=1 is required, not tuning. See the note at the top.
  - --no-cpu-throttling keeps litestream replicating between requests.
EOF
