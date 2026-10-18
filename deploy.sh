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
# Built from the project number: on a brand-new project these agents may not be
# provisioned yet, and `gcloud storage service-agent` can return nothing useful.
GCS_SA="service-${PROJECT_NUMBER}@gs-project-accounts.iam.gserviceaccount.com"
EVENTARC_SA="service-${PROJECT_NUMBER}@gcp-sa-eventarc.iam.gserviceaccount.com"

# Retries an IAM grant while newly created service agents propagate.
grant_role() {
  local member="$1" role="$2"
  for attempt in 1 2 3 4 5; do
    if gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="$member" --role="$role" --condition=None >/dev/null 2>&1; then
      echo "  granted ${role} to ${member}"
      return 0
    fi
    echo "  grant ${role} to ${member} failed (attempt ${attempt}), retrying in 20s..."
    sleep 20
  done
  echo "ERROR: could not grant ${role} to ${member}" >&2
  return 1
}

echo "==> Step 1: enable APIs"
gcloud services enable \
  cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com eventarc.googleapis.com pubsub.googleapis.com \
  storage.googleapis.com bigquery.googleapis.com logging.googleapis.com

echo "==> Step 2: bucket gs://${BUCKET}"
if gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  echo "  bucket already exists, skipping"
else
  gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" --uniform-bucket-level-access
fi

echo "==> Step 3: IAM roles"
for role in roles/bigquery.admin roles/eventarc.eventReceiver roles/run.invoker \
            roles/storage.objectViewer roles/cloudbuild.builds.builder \
            roles/logging.logWriter roles/artifactregistry.writer; do
  grant_role "serviceAccount:${COMPUTE_SA}" "$role"
done
# Cloud Storage must be able to publish events for the Eventarc trigger.
grant_role "serviceAccount:${GCS_SA}" "roles/pubsub.publisher"

echo "==> Step 6 (before deploy): BigQuery dataset ${DATASET}"
if bq --location=US show "${PROJECT_ID}:${DATASET}" >/dev/null 2>&1; then
  echo "  dataset already exists, skipping"
else
  bq --location=US mk --dataset "${PROJECT_ID}:${DATASET}"
fi

echo "==> Eventarc service agent"
gcloud beta services identity create --service=eventarc.googleapis.com --project="$PROJECT_ID" >/dev/null
grant_role "serviceAccount:${EVENTARC_SA}" "roles/eventarc.serviceAgent"

echo "==> Steps 4-5: deploy ETL function (Cloud Storage trigger)"
# Right after the grants above, Eventarc often fails with 403 "storage.buckets.get denied"
# until permissions propagate; retry only on that error.
DEPLOY_LOG="$(mktemp)"
for attempt in 1 2 3 4 5; do
  if gcloud functions deploy "$ETL_FUNCTION" \
      --gen2 --region="$REGION" --runtime=python311 \
      --source="${ROOT}/etl_function" --entry-point=hello_gcs \
      --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
      --trigger-event-filters="bucket=${BUCKET}" \
      --trigger-location="$REGION" \
      --service-account="$COMPUTE_SA" \
      --set-env-vars="BQ_DATASET=${DATASET}" \
      --memory=512Mi --timeout=300s 2>&1 | tee "$DEPLOY_LOG"; [ "${PIPESTATUS[0]}" -eq 0 ]; then
    break
  fi
  if ! grep -q "storage.buckets.get" "$DEPLOY_LOG" || [ "$attempt" -eq 5 ]; then
    echo "ERROR: ETL function deploy failed." >&2
    exit 1
  fi
  echo "Eventarc permissions still propagating (attempt ${attempt}), retrying in 60s..."
  sleep 60
done
rm -f "$DEPLOY_LOG"

echo "==> ML example: deploy HTTP prediction function"
gcloud functions deploy "$ML_FUNCTION" \
  --gen2 --region="$REGION" --runtime=python311 \
  --source="${ROOT}/ml_function" --entry-point=predict \
  --trigger-http --allow-unauthenticated \
  --memory=1Gi --timeout=60s

ML_URL="$(gcloud functions describe "$ML_FUNCTION" --gen2 --region="$REGION" --format='value(serviceConfig.uri)')"

mkdir -p "${ROOT}/streamlit_app/.streamlit"
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
