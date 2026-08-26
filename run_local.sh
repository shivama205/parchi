#!/usr/bin/env bash
# Local run. In-memory store and a pinned clock, so the demo is reproducible.
set -euo pipefail
cd "$(dirname "$0")"
export PARCHI_STORE=memory
export PARCHI_TODAY=${PARCHI_TODAY:-2026-08-26}
export GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT:-ascend-473804}
export GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION:-asia-south1}
export GOOGLE_GENAI_USE_VERTEXAI=true
export HOST=127.0.0.1
export PORT=${PORT:-8123}
exec ./.venv/bin/python -m parchi.serve
