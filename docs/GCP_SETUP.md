# GCP Setup Guide: Housing ETL Pipeline, ML Function, and Streamlit App

Everything below happens in your browser at **https://console.cloud.google.com**. There are two ways to do it:

- **Path A — Cloud Shell (recommended, ~15 min):** paste command blocks into the terminal built into the console.
- **Path B — Console clicks:** the same steps as the original lab, done through the UI.

Both end at **Part 3**, where you run the Streamlit app from Cloud Shell.

Files used below (all paths are relative to the repository root):

| File | Used for |
|---|---|
| `etl_function/` | ETL function code (`main.py`, `schemas.yaml`, `requirements.txt`) |
| `ml_function/` | ML function code including the trained `model.joblib` |
| `streamlit_app/` | Streamlit app |
| `data/housing_listings_batch_001.json` | Test file for the pipeline |

For Path B you need two zip files for the function editors. Create them from the repository root:

```bash
(cd etl_function && zip -r ../etl_function.zip main.py schemas.yaml requirements.txt)
(cd ml_function && zip -r ../ml_function.zip main.py model.joblib requirements.txt)
```

---

## Part 0: Before you start (both paths)

1. Open https://console.cloud.google.com and sign in.
2. **Select or create a project:** click the project picker at the top-left → **New Project** → give it a name (e.g. `housing-etl-lab`) → **Create**. Make sure it is selected in the picker.
3. **Billing must be enabled** (Cloud Functions requires it): ☰ menu → **Billing** → link a billing account (the free trial / education credits work).
4. Note your **Project ID** (shown in the project picker, e.g. `housing-etl-lab-123456`).

---

## Path A: Cloud Shell (paste and go)

### A1. Open Cloud Shell and get the code

1. Click the **Activate Cloud Shell** icon (`>_`) at the top-right of the console. A terminal opens at the bottom.
2. Clone the repository (replace the URL with this repository's URL):

```bash
cd ~ && git clone https://github.com/<your-github-username>/<repo-name>.git housing_lab && cd housing_lab && ls
```

You should see `etl_function  ml_function  streamlit_app  scripts  data  docs  deploy.sh  README.md`.

*(Alternatively, zip the repository locally, upload it with **More (⋮) → Upload** in the Cloud Shell toolbar, and unzip it into `~/housing_lab`.)*

### A2. Set your variables

Replace `YOUR_PROJECT_ID` and paste. **Re-paste this block whenever Cloud Shell reconnects.**

```bash
export PROJECT_ID="YOUR_PROJECT_ID"
export REGION="us-central1"
export BUCKET="${PROJECT_ID}-housing-etl"
gcloud config set project "$PROJECT_ID"
export PROJECT_NUMBER="$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')"
export COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
echo "Project: $PROJECT_ID  Number: $PROJECT_NUMBER  Bucket: $BUCKET"
```

### A3. Deploy everything with one command

```bash
cd ~/housing_lab && chmod +x deploy.sh && ./deploy.sh "$PROJECT_ID" "$REGION"
```

This takes about 5–10 minutes and does lab steps 1–6 (APIs, bucket, IAM, BigQuery dataset, Eventarc service agent, both functions) and writes the Streamlit config. Re-running it is safe: an existing bucket or dataset is skipped. It ends with:

```
Done.
  Bucket:        gs://<project>-housing-etl
  BigQuery:      <project>.staging
  ML endpoint:   https://housing-price-predict-xxxx.us-central1.run.app
```

> On a brand-new project the ETL deploy often fails at first with 403 `storage.buckets.get denied` while the Eventarc service agent's permissions propagate. The script retries automatically on that error. If it still fails after all retries, wait 2 minutes and run the same command again.

<details>
<summary>Prefer to run each step yourself? Expand for the individual commands.</summary>

**Step 1: Enable APIs**
```bash
gcloud services enable cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com eventarc.googleapis.com pubsub.googleapis.com \
  storage.googleapis.com bigquery.googleapis.com logging.googleapis.com
```

**Step 2: Create the bucket** (skipped if it exists)
```bash
gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" --uniform-bucket-level-access
```

**Step 3: IAM roles**
```bash
for role in roles/bigquery.admin roles/eventarc.eventReceiver roles/run.invoker roles/storage.objectViewer \
            roles/cloudbuild.builds.builder roles/logging.logWriter roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${COMPUTE_SA}" --role="$role" --condition=None >/dev/null
done

# Build the Cloud Storage service agent address directly: on a new project it may not exist yet.
# If this grant fails, wait 30 seconds and run it again.
GCS_SA="service-${PROJECT_NUMBER}@gs-project-accounts.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${GCS_SA}" --role="roles/pubsub.publisher" --condition=None >/dev/null
```

**Eventarc service agent** (prevents 403 `storage.buckets.get denied` when deploying the ETL function)
```bash
gcloud beta services identity create --service=eventarc.googleapis.com --project="$PROJECT_ID"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-eventarc.iam.gserviceaccount.com" \
  --role="roles/eventarc.serviceAgent" --condition=None >/dev/null
```

**Step 6: BigQuery dataset** (created before the function so the first upload succeeds; skipped if it exists)
```bash
bq --location=US show "${PROJECT_ID}:staging" >/dev/null 2>&1 || bq --location=US mk --dataset "${PROJECT_ID}:staging"
```

**Steps 4–5: ETL function** (if it fails with 403 `storage.buckets.get denied`, wait a minute and run it again)
```bash
gcloud functions deploy housing-etl --gen2 --region="$REGION" --runtime=python311 \
  --source=./etl_function --entry-point=hello_gcs \
  --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
  --trigger-event-filters="bucket=${BUCKET}" --trigger-location="$REGION" \
  --service-account="$COMPUTE_SA" --set-env-vars="BQ_DATASET=staging" \
  --memory=512Mi --timeout=300s
```

**ML function**
```bash
gcloud functions deploy housing-price-predict --gen2 --region="$REGION" --runtime=python311 \
  --source=./ml_function --entry-point=predict --trigger-http --allow-unauthenticated \
  --memory=1Gi --timeout=60s
```
</details>

### A4. Test the ETL pipeline (lab Step 7)

```bash
cd ~/housing_lab
gcloud storage cp data/housing_listings_batch_001.json "gs://${BUCKET}/"
sleep 45
gcloud functions logs read housing-etl --gen2 --region="$REGION" --limit=20
```

Look for `Job finished. Loaded 500 rows into staging.housing_listings`. Then:

```bash
bq query --use_legacy_sql=false '
SELECT location.region, COUNT(*) AS listings, ROUND(AVG(median_house_value)) AS avg_value
FROM staging.housing_listings GROUP BY 1 ORDER BY avg_value DESC'
```

### A5. Test the ML function

```bash
export ML_URL="$(gcloud functions describe housing-price-predict --gen2 --region=$REGION --format='value(serviceConfig.uri)')"
curl -s "$ML_URL"; echo
curl -s -X POST "$ML_URL" -H 'Content-Type: application/json' \
  -d '{"features": [8.3252, 41, 6.98, 1.02, 322, 2.55, 37.88, -122.23]}'; echo
```

Expected: `{"predicted_median_house_value": ~418000, "model_version": "hgb-v1", ...}`

➡️ Continue to **Part 3**.

---

## Path B: Console clicks (mirrors the original lab)

### B1. Enable APIs (lab Step 1)
☰ → **APIs & Services → Library**. Search and **Enable** each:
Cloud Functions API, Cloud Run Admin API, Cloud Build API, Artifact Registry API, Eventarc API, Cloud Pub/Sub API, BigQuery API.

### B2. Create the bucket (lab Step 2)
☰ → **Cloud Storage → Buckets → + Create**
- Name: `<your-project-id>-housing-etl` (must be globally unique)
- Location type: **Region** → `us-central1`
- Leave the rest as default → **Create** (confirm "Enforce public access prevention" if asked).

### B3. IAM roles (lab Step 3)
☰ → **IAM & Admin → IAM**. Tick **"Include Google-provided role grants"** (top-right) so service accounts show.
1. Find `PROJECT_NUMBER-compute@developer.gserviceaccount.com` → ✏️ **Edit** → **+ Add another role** for each:
   - **BigQuery Admin**
   - **Eventarc Event Receiver**
   - **Cloud Run Invoker**
   - **Storage Object Viewer**
   - **Cloud Build Builder**
   - **Logs Writer**
   - **Artifact Registry Writer**
   → **Save**.
2. Find `service-PROJECT_NUMBER@gs-project-accounts.iam.gserviceaccount.com` (Cloud Storage Service Agent) → ✏️ → add **Pub/Sub Publisher** → **Save**.
   *(If it isn't listed, click **+ Grant access**, paste that address as the principal, and add the role. On a brand-new project the grant can fail for a minute until the agent exists; just retry.)*
3. Open **Cloud Shell** (`>_`) and create the Eventarc service agent, then grant it its role. Without this, creating the ETL function can fail with 403 `storage.buckets.get denied`:
   ```bash
   PROJECT_NUMBER=$(gcloud projects describe $(gcloud config get-value project) --format='value(projectNumber)')
   gcloud beta services identity create --service=eventarc.googleapis.com
   gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
     --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-eventarc.iam.gserviceaccount.com" \
     --role="roles/eventarc.serviceAgent" --condition=None
   ```

### B4. Create the BigQuery dataset (lab Step 6, done now so it exists before the function)
☰ → **BigQuery → Studio**. In the Explorer, click **⋮** next to your project → **Create dataset**
- Dataset ID: `staging`
- Location type: **Multi-region → US**
→ **Create dataset**.

### B5. Create the ETL function (lab Steps 4–5)
☰ → **Cloud Run → Write a function** (older UI: **Cloud Run functions → Create function**).
- Service name: `housing-etl`
- Region: `us-central1`
- Runtime: **Python 3.11**
- **Trigger → Add trigger → Cloud Storage trigger**
  - Event type: `google.cloud.storage.object.v1.finalized`
  - Bucket: **Browse** → pick `<your-project-id>-housing-etl`
  - Service account: the default compute service account → **Save trigger**
- Authentication: **Require authentication**
- Expand **Containers, Volumes, Networking, Security → Variables & Secrets** → add `BQ_DATASET = staging`. Set **Memory** to 512 MiB.
- **Create**.

In the source editor that opens:
- **Function entry point:** `hello_gcs`
- Either click **Upload ZIP** (under the source dropdown) and choose `etl_function.zip` (see the top of this guide),
  **or** replace the editor's `main.py` and `requirements.txt` with the contents of `etl_function/main.py` and `etl_function/requirements.txt`, then **+ add file** `schemas.yaml` and paste `etl_function/schemas.yaml`.
- **Save and redeploy**. Wait for the green check (~2–3 min).

### B6. Create the ML function
☰ → **Cloud Run → Write a function**
- Service name: `housing-price-predict`, Region `us-central1`, Runtime **Python 3.11**
- Trigger: none (HTTPS is the default)
- Authentication: **Allow unauthenticated invocations** (newer UI: **Allow public access**)
- Memory: **1 GiB**
- **Create** → in the editor set **Function entry point** to `predict` → **Upload ZIP** → `ml_function.zip`
  *(a ZIP is required here because `model.joblib` is a binary file that can't be pasted)* → **Save and redeploy**.
- Copy the **URL** shown at the top of the service page.

### B7. Test (lab Step 7)
1. **Cloud Storage → Buckets → your bucket → Upload files** → `data/housing_listings_batch_001.json`.
2. **Cloud Run → housing-etl → Logs**: within a minute you should see `Job finished. Loaded 500 rows into staging.housing_listings`.
3. **BigQuery → Studio → + (new query)**, run:
   ```sql
   SELECT location.region, COUNT(*) AS listings, ROUND(AVG(median_house_value)) AS avg_value
   FROM staging.housing_listings
   GROUP BY 1 ORDER BY avg_value DESC;
   ```
4. Open the ML function URL in your browser — you should see the model info JSON.

➡️ Continue to **Part 3**.

---

## Part 3: Run the Streamlit app (from Cloud Shell)

Cloud Shell is already logged in to your project, so the app can upload to the bucket and query BigQuery without extra setup.

1. If you followed **Path B**, do **A1** and **A2** first (get the code, set variables), then create the config:

   ```bash
   export ML_URL="$(gcloud functions describe housing-price-predict --gen2 --region=$REGION --format='value(serviceConfig.uri)' 2>/dev/null || gcloud run services describe housing-price-predict --region=$REGION --format='value(status.url)')"
   mkdir -p ~/housing_lab/streamlit_app/.streamlit
   cat > ~/housing_lab/streamlit_app/.streamlit/secrets.toml <<EOF
   GCP_PROJECT = "${PROJECT_ID}"
   GCS_BUCKET = "${BUCKET}"
   BQ_DATASET = "staging"
   ML_FUNCTION_URL = "${ML_URL}"
   EOF
   cat ~/housing_lab/streamlit_app/.streamlit/secrets.toml
   ```
   *(Path A's `deploy.sh` already wrote this file.)*

2. Install and start the app:

   ```bash
   cd ~/housing_lab/streamlit_app
   python3 -m venv ~/st-venv && ~/st-venv/bin/pip install -q -r requirements.txt
   ~/st-venv/bin/streamlit run app.py --server.port 8080 \
     --server.enableCORS false --server.enableXsrfProtection false
   ```

3. In the Cloud Shell toolbar click **Web Preview (👁 icon) → Preview on port 8080**. The app opens in a new tab.

4. Try it:
   - **📤 Ingest** → "Random sample", 500 rows → **🚀 Send to pipeline**. Watch it upload to GCS, trigger the function, and confirm the rows in BigQuery (~30–60 s).
   - **📊 Explore** → charts and a map built from BigQuery.
   - **🔮 Predict** → enter features → **Predict**. With the checkbox on, the prediction is also sent through the ETL pipeline into `staging.price_predictions`.
   - **🧪 SQL** and **🗂️ Bucket** → inspect the data and uploaded files.

Stop the app with `Ctrl+C`.

> **Running the app on your own laptop instead:** install the gcloud CLI, run `gcloud auth application-default login` and `gcloud auth application-default set-quota-project <PROJECT_ID>`, copy the `secrets.toml` values above into `streamlit_app/.streamlit/secrets.toml`, then `cd streamlit_app && ../.venv/bin/streamlit run app.py`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Deploy error mentions **Eventarc** / "permission denied while using the Eventarc Service Agent" | IAM still propagating. Wait 2–3 min and redeploy. |
| ETL deploy fails with 403 **`storage.buckets.get denied`** | Create the Eventarc service agent and grant it `roles/eventarc.serviceAgent` (Path A step list / B3 step 3), wait a minute, and redeploy. |
| IAM grant to `service-…@gs-project-accounts.iam.gserviceaccount.com` fails | The Cloud Storage service agent isn't provisioned yet on a new project. Wait 30–60 seconds and retry the grant. |
| Build fails with a **Cloud Build** / **Artifact Registry** permission error | Grant the compute service account Cloud Build Builder, Logs Writer, and Artifact Registry Writer. |
| Explore tab: `Syntax error: Unexpected keyword ROWS` | `rows` is reserved in BigQuery; aliases must be written ``AS `rows` `` (already done in `app.py`). |
| Local Streamlit shows a **quota project** warning | Run `gcloud auth application-default set-quota-project <PROJECT_ID>`. |
| Deploy error: **billing** not enabled | Link a billing account (Part 0, step 3). |
| File uploaded but no rows in BigQuery | Check function logs. File name must **start with** `housing_listings` or `price_predictions`; the `staging` dataset must exist; dataset location (US) is fine with a `us-central1` bucket. |
| `Access Denied: BigQuery` in logs | Compute service account is missing **BigQuery Admin** (Step 3). |
| ML URL returns **403** | The function wasn't made public: Cloud Run → `housing-price-predict` → **Security** → Allow unauthenticated / public access. |
| `--allow-unauthenticated` blocked by an **org policy** (common on university accounts) | Use a personal project, or keep it private and call it with `curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" ...` (the Streamlit Predict tab will then need that header). |
| Streamlit upload returns **403** in Web Preview | Make sure you started it with `--server.enableXsrfProtection false`. |
| Cloud Shell variables are empty | Cloud Shell reconnected — re-paste the **A2** block. |

## Cleanup (avoid charges)

`gcloud functions delete` takes one function name at a time:

```bash
gcloud functions delete housing-etl --gen2 --region="$REGION" --quiet
gcloud functions delete housing-price-predict --gen2 --region="$REGION" --quiet
gcloud storage rm -r "gs://${BUCKET}"
bq rm -r -f "${PROJECT_ID}:staging"
```

If the project was created only for this lab, deleting the whole project removes everything in one step:

```bash
gcloud projects delete "$PROJECT_ID"
```
