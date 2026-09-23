#!/bin/bash
# Which model IDs can this project actually call?
#
# Vertex and AI Studio use different names and the difference is not
# guessable - "gemini-flash" works on AI Studio and 404s on Vertex. This calls
# each candidate directly, so the question is settled in seconds rather than by
# a redeploy. Add candidates as Google ships them.
#
#   bash scripts/try-models.sh [project] [location]

set -u
PROJECT="${1:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}}"
LOCATION="${2:-global}"
TOKEN=$(gcloud auth print-access-token)

echo "project=$PROJECT location=$LOCATION"
echo

for model in \
  gemini-flash-latest \
  gemini-flash \
  gemini-3.8-flash \
  gemini-3.7-flash \
  gemini-3.6-flash \
  gemini-3.5-flash \
  gemini-2.5-flash
do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    "https://aiplatform.googleapis.com/v1/projects/$PROJECT/locations/$LOCATION/publishers/google/models/${model}:generateContent" \
    -d '{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}')
  if [ "$code" = "200" ]; then
    printf 'works    %s\n' "$model"
  else
    printf '%-8s %s\n' "$code" "$model"
  fi
done
