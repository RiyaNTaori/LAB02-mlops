#!/usr/bin/env bash
# Deploys the housing ETL pipeline + ML prediction function to GCP.
# Usage: ./deploy.sh <PROJECT_ID> [REGION]
set -euo pipefail

PROJECT_ID="${1:?Usage: ./deploy.sh <PROJECT_ID> [REGION]}"
REGION="${2:-us-central1}"
BUCKET="${PROJECT_ID}-housing-etl"
DATASET="staging"
ETL_FUNCTION="housing-etl"
ML_FUNCTION="housing-price-predict"
ROOT="$(cd "$(dirname "$0")" && pwd)"

gcloud config set project "$PROJECT_ID" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "==> Step 1: enable APIs"
gcloud services enable \
  cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com eventarc.googleapis.com pubsub.googleapis.com \
  storage.googleapis.com bigquery.googleapis.com logging.googleapis.com

echo "==> Step 2: create bucket gs://${BUCKET}"
gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" --uniform-bucket-level-access

echo "==> Step 3: IAM roles"
for role in roles/bigquery.admin roles/eventarc.eventReceiver roles/run.invoker roles/storage.objectViewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${COMPUTE_SA}" --role="$role" --condition=None >/dev/null
done
# Cloud Storage must be able to publish events for the Eventarc trigger.
GCS_SA="$(gcloud storage service-agent --project="$PROJECT_ID")"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${GCS_SA}" --role="roles/pubsub.publisher" --condition=None >/dev/null

echo "==> Step 6 (before deploy): BigQuery dataset ${DATASET}"
bq --location=US show "${PROJECT_ID}:${DATASET}" >/dev/null 2>&1 \
  || bq --location=US mk --dataset "${PROJECT_ID}:${DATASET}"

echo "==> Steps 4-5: deploy ETL function (Cloud Storage trigger)"
# Eventarc can take a minute after the IAM grants above; retry a few times.
for attempt in 1 2 3; do
  gcloud functions deploy "$ETL_FUNCTION" \
    --gen2 --region="$REGION" --runtime=python311 \
    --source="${ROOT}/etl_function" --entry-point=hello_gcs \
    --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
    --trigger-event-filters="bucket=${BUCKET}" \
    --trigger-location="$REGION" \
    --service-account="$COMPUTE_SA" \
    --set-env-vars="BQ_DATASET=${DATASET}" \
    --memory=512Mi --timeout=300s && break
  echo "Deploy failed (attempt $attempt), retrying in 60s..."; sleep 60
done

echo "==> ML example: deploy HTTP prediction function"
gcloud functions deploy "$ML_FUNCTION" \
  --gen2 --region="$REGION" --runtime=python311 \
  --source="${ROOT}/ml_function" --entry-point=predict \
  --trigger-http --allow-unauthenticated \
  --memory=1Gi --timeout=60s

ML_URL="$(gcloud functions describe "$ML_FUNCTION" --gen2 --region="$REGION" --format='value(serviceConfig.uri)')"

cat > "${ROOT}/streamlit_app/.streamlit/secrets.toml" <<EOF
GCP_PROJECT = "${PROJECT_ID}"
GCS_BUCKET = "${BUCKET}"
BQ_DATASET = "${DATASET}"
ML_FUNCTION_URL = "${ML_URL}"
EOF

echo
echo "Done."
echo "  Bucket:        gs://${BUCKET}"
echo "  BigQuery:      ${PROJECT_ID}.${DATASET}"
echo "  ML endpoint:   ${ML_URL}"
echo "  Streamlit config written to streamlit_app/.streamlit/secrets.toml"
