#!/usr/bin/env bash
# Deploy Parchi to Cloud Run in asia-south1.
#
# Two flags here are not optional and are easy to lose:
#
#   --no-cpu-throttling  Cloud Run throttles CPU outside a request by default,
#                        which silently freezes the background ingestion started
#                        by /api/upload. Without this, an upload returns 200 and
#                        nothing is ever read.
#   PARCHI_BUCKET        Without it make_blobs() falls back to the container
#                        filesystem, which is ephemeral and per-instance, so
#                        uploaded images appear to save and then vanish.
#
# gcloud's `run` surface needs grpcio, which the Homebrew cask's Python lacks.
# These two exports are the fix; add them to your shell profile to make it stick.
set -euo pipefail
cd "$(dirname "$0")"

export CLOUDSDK_PYTHON="${CLOUDSDK_PYTHON:-$HOME/.gcloud-python/bin/python}"
export CLOUDSDK_PYTHON_SITEPACKAGES=1

PROJECT="${PROJECT:-ascend-473804}"
REGION="${REGION:-asia-south1}"      # BR-19: health data stays in India
SERVICE="${SERVICE:-parchi}"
BUCKET="${BUCKET:-${PROJECT}-parchi-docs}"   # document images, same region
TODAY="${PARCHI_TODAY:-2026-08-26}"  # pinned so the demo is reproducible
TOKEN="$(cat .sweep-token.local)"

gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 4 \
  --timeout 300 \
  --no-cpu-throttling \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION,GOOGLE_GENAI_USE_VERTEXAI=true,PARCHI_TODAY=$TODAY,PARCHI_SWEEP_TOKEN=$TOKEN,PARCHI_BUCKET=$BUCKET"

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" \
  --region "$REGION" --format='value(status.url)')"

# Verify rather than announce. `gcloud run deploy` can leave a failed revision
# behind while traffic stays on the previous good one, so "deployment failed"
# and "the service still answers" are both true and it is easy to read the
# second as success. Worse, piping this script's output through anything makes
# the pipeline report tail's exit status instead of gcloud's — which is how a
# failed deploy was first mistaken for a clean one.
echo
echo "verifying $URL"
HEALTH="$(curl -fsS --max-time 90 "$URL/api/health" || true)"
case "$HEALTH" in
  *'"ok":true'*)
    echo "  health OK: $HEALTH"
    ;;
  *)
    echo "  HEALTH CHECK FAILED: ${HEALTH:-no response}" >&2
    echo "  the live revision may still be an older one — check:" >&2
    echo "    gcloud run revisions list --service $SERVICE --region $REGION --project $PROJECT" >&2
    exit 1
    ;;
esac

LIVE="$(gcloud run services describe "$SERVICE" --project "$PROJECT" \
  --region "$REGION" --format='value(status.latestReadyRevisionName)')"
echo "  serving revision: $LIVE"
echo "$URL"
