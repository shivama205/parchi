#!/usr/bin/env bash
# Deploy Parchi to Cloud Run in asia-south1.
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
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION,GOOGLE_GENAI_USE_VERTEXAI=true,PARCHI_TODAY=$TODAY,PARCHI_SWEEP_TOKEN=$TOKEN"

echo
gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format="value(status.url)"
