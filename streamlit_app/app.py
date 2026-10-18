"""Streamlit front end for the GCS -> Cloud Function -> BigQuery housing ETL pipeline."""
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from google.api_core.exceptions import NotFound
from google.cloud import bigquery, storage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from housing import FEATURES, new_batch_id, normalize_columns, to_ndjson, to_records

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

st.set_page_config(page_title="Housing ETL Pipeline", page_icon="🏠", layout="wide")


def setting(key, default=""):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except FileNotFoundError:
        pass
    return os.getenv(key, default)


# ---------------------------------------------------------------- sidebar config
with st.sidebar:
    st.header("☁️ GCP settings")
    project = st.text_input("Project ID", setting("GCP_PROJECT"))
    bucket_name = st.text_input("Upload bucket", setting("GCS_BUCKET"))
    dataset = st.text_input("BigQuery dataset", setting("BQ_DATASET", "staging"))
    ml_url = st.text_input("ML function URL", setting("ML_FUNCTION_URL"))
    st.caption("Values come from `.streamlit/secrets.toml` or environment variables. "
               "Auth uses `gcloud auth application-default login`.")

if not project or not bucket_name:
    st.title("🏠 California Housing ETL Pipeline")
    st.warning("Set the **Project ID** and **Upload bucket** in the sidebar to get started.")
    st.stop()


@st.cache_resource
def clients(project_id):
    return storage.Client(project=project_id), bigquery.Client(project=project_id)


gcs, bq = clients(project)
LISTINGS = f"`{project}.{dataset}.housing_listings`"
PREDICTIONS = f"`{project}.{dataset}.price_predictions`"


def run_query(sql, params=None):
    job_config = bigquery.QueryJobConfig(
        query_parameters=params or [],
        maximum_bytes_billed=1_000_000_000,  # 1 GB guardrail
    )
    return bq.query(sql, job_config=job_config).to_dataframe()


@st.cache_data(ttl=30, show_spinner=False)
def cached_query(sql):
    try:
        return run_query(sql)
    except NotFound:
        return None


def upload_to_bucket(blob_name, payload):
    blob = gcs.bucket(bucket_name).blob(blob_name)
    blob.upload_from_string(payload, content_type="application/x-ndjson")
    return f"gs://{bucket_name}/{blob_name}"


def wait_for_rows(table, id_column, id_value, expected, timeout=180):
    """Polls BigQuery until the Cloud Function has loaded the uploaded rows."""
    sql = f"SELECT COUNT(*) AS n FROM {table} WHERE {id_column} = @v"
    params = [bigquery.ScalarQueryParameter("v", "STRING", id_value)]
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            n = int(run_query(sql, params)["n"][0])
            if n >= expected:
                return n
        except NotFound:
            pass  # table is created by the function on first upload
        time.sleep(4)
    return None


# ---------------------------------------------------------------- header
st.title("🏠 California Housing ETL Pipeline")
st.markdown(
    f"**Streamlit** ➜ `gs://{bucket_name}` ➜ **Cloud Function** (`hello_gcs`) ➜ "
    f"**BigQuery** `{project}.{dataset}`"
)

tab_ingest, tab_explore, tab_predict, tab_sql, tab_bucket = st.tabs(
    ["📤 Ingest", "📊 Explore", "🔮 Predict", "🧪 SQL", "🗂️ Bucket"]
)

# ---------------------------------------------------------------- ingest
with tab_ingest:
    st.subheader("Send housing data through the pipeline")
    source = st.radio(
        "Data source",
        ["Random sample from California Housing", "Bundled sample batch", "Upload my own CSV"],
        horizontal=True,
    )

    df = None
    if source == "Random sample from California Housing":
        n = st.slider("Rows", 50, 5000, 500, step=50)
        full_csv = DATA_DIR / "california_housing.csv"
        if full_csv.exists():
            df = pd.read_csv(full_csv).sample(n=n)
        else:
            st.error("Run `scripts/prepare_data_and_model.py` first to download the dataset.")
    elif source == "Bundled sample batch":
        files = sorted(DATA_DIR.glob("housing_sample_*.csv"))
        if not files:
            st.error("Run `scripts/prepare_data_and_model.py` first to generate the sample batches.")
        choice = st.selectbox("Batch file", files, format_func=lambda p: p.name)
        if choice:
            df = pd.read_csv(choice)
    else:
        uploaded = st.file_uploader(
            "CSV with columns: " + ", ".join(FEATURES + ["median_house_value"]) +
            " (sklearn names like MedInc also work; values in $100k like the original dataset)",
            type="csv",
        )
        if uploaded:
            df = pd.read_csv(uploaded)

    if df is not None:
        try:
            df = normalize_columns(df).dropna()
        except ValueError as e:
            st.error(str(e))
            df = None

    if df is not None:
        c1, c2, c3 = st.columns(3)
        c1.metric("Rows", f"{len(df):,}")
        c2.metric("Median value", f"${df['median_house_value'].median() * 100_000:,.0f}")
        c3.metric("Median income", f"${df['median_income'].median() * 10_000:,.0f}")
        st.dataframe(df.head(20), use_container_width=True)

        if st.button("🚀 Send to pipeline", type="primary"):
            batch_id = new_batch_id()
            records = to_records(df, batch_id)
            blob_name = f"housing_listings_{batch_id}.json"
            with st.status("Running pipeline…", expanded=True) as status:
                st.write(f"1️⃣ Converted {len(records):,} rows to newline-delimited JSON")
                uri = upload_to_bucket(blob_name, to_ndjson(records))
                st.write(f"2️⃣ Uploaded to `{uri}` — this fires the Cloud Function")
                st.write("3️⃣ Waiting for the function to load the rows into BigQuery…")
                t0 = time.time()
                loaded = wait_for_rows(LISTINGS, "batch_id", batch_id, len(records))
                if loaded:
                    status.update(label=f"✅ Loaded {loaded:,} rows in {time.time() - t0:.0f}s",
                                  state="complete")
                    st.cache_data.clear()
                else:
                    status.update(label="⏱️ Timed out waiting for BigQuery", state="error")
                    st.write("Check the function logs: "
                             "`gcloud functions logs read housing-etl --region <region> --gen2`")
            st.success(f"Batch `{batch_id}`")

# ---------------------------------------------------------------- explore
with tab_explore:
    top = st.columns([4, 1])
    top[0].subheader("What's in BigQuery")
    if top[1].button("🔄 Refresh"):
        st.cache_data.clear()

    summary = cached_query(f"""
        SELECT COUNT(*) AS `rows`, COUNT(DISTINCT batch_id) AS batches,
               AVG(median_house_value) AS avg_value, MAX(uploaded_at) AS last_upload
        FROM {LISTINGS}""")
    if summary is None or summary["rows"][0] == 0:
        st.info("No data yet — send a batch from the **Ingest** tab.")
    else:
        s = summary.iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Rows", f"{int(s['rows']):,}")
        c2.metric("Batches", int(s["batches"]))
        c3.metric("Avg value", f"${s['avg_value']:,.0f}")
        c4.metric("Last upload", pd.Timestamp(s["last_upload"]).strftime("%b %d, %H:%M UTC"))

        by_region = cached_query(f"""
            SELECT location.region AS region, COUNT(*) AS listings,
                   ROUND(AVG(median_house_value)) AS avg_value,
                   ROUND(AVG(median_income) * 10000) AS avg_income
            FROM {LISTINGS} GROUP BY region ORDER BY avg_value DESC""")
        left, right = st.columns(2)
        with left:
            st.markdown("**Average house value by region**")
            st.bar_chart(by_region, x="region", y="avg_value")
        with right:
            st.markdown("**Region summary**")
            st.dataframe(by_region, use_container_width=True, hide_index=True)

        st.markdown("**Locations (sample of 5,000)**")
        points = cached_query(f"""
            SELECT location.latitude AS lat, location.longitude AS lon
            FROM {LISTINGS} ORDER BY RAND() LIMIT 5000""")
        st.map(points, size=20)

        st.markdown("**Income vs. house value**")
        scatter = cached_query(f"""
            SELECT median_income * 10000 AS income, median_house_value AS value, location.region AS region
            FROM {LISTINGS} ORDER BY RAND() LIMIT 2000""")
        st.scatter_chart(scatter, x="income", y="value", color="region")

        st.markdown("**Batches**")
        st.dataframe(cached_query(f"""
            SELECT batch_id, COUNT(*) AS `rows`, MIN(uploaded_at) AS uploaded_at,
                   ROUND(AVG(median_house_value)) AS avg_value
            FROM {LISTINGS} GROUP BY batch_id ORDER BY uploaded_at DESC"""),
            use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- predict
with tab_predict:
    st.subheader("Predict a median house value")
    if not ml_url:
        st.info("Set the **ML function URL** in the sidebar to enable predictions.")
    else:
        defaults = dict(median_income=3.87, house_age=29.0, avg_rooms=5.4, avg_bedrooms=1.1,
                        population=1166.0, avg_occupancy=3.0, latitude=34.05, longitude=-118.24)
        with st.form("predict"):
            c1, c2 = st.columns(2)
            values = {
                "median_income": c1.number_input("Median income ($10k units)", 0.5, 15.0, defaults["median_income"]),
                "house_age": c1.number_input("Median house age (years)", 1.0, 52.0, defaults["house_age"]),
                "avg_rooms": c1.number_input("Avg rooms per household", 1.0, 20.0, defaults["avg_rooms"]),
                "avg_bedrooms": c1.number_input("Avg bedrooms per household", 0.5, 5.0, defaults["avg_bedrooms"]),
                "population": c2.number_input("Block population", 3.0, 35000.0, defaults["population"]),
                "avg_occupancy": c2.number_input("Avg occupants per household", 0.7, 10.0, defaults["avg_occupancy"]),
                "latitude": c2.number_input("Latitude", 32.5, 42.0, defaults["latitude"], format="%.4f"),
                "longitude": c2.number_input("Longitude", -124.4, -114.3, defaults["longitude"], format="%.4f"),
            }
            log_it = st.checkbox("Also log this prediction to BigQuery through the ETL pipeline", value=True)
            submitted = st.form_submit_button("Predict", type="primary")

        if submitted:
            try:
                resp = requests.post(ml_url, json={"features": values}, timeout=60)
                body = resp.json()
            except Exception as e:
                st.error(f"Request failed: {e}")
                body = None
            if body and resp.ok:
                value = body["predicted_median_house_value"]
                st.metric("Predicted median house value", f"${value:,.0f}",
                          help=f"Model {body['model_version']}")
                st.map(pd.DataFrame({"lat": [values["latitude"]], "lon": [values["longitude"]]}), zoom=8)
                if log_it:
                    prediction_id = str(uuid.uuid4())
                    record = {
                        "prediction_id": prediction_id,
                        "predicted_at": datetime.now(timezone.utc).isoformat(),
                        "predicted_value": value,
                        "model_version": body["model_version"],
                        "features": values,
                    }
                    uri = upload_to_bucket(f"price_predictions_{prediction_id}.json", to_ndjson([record]))
                    with st.spinner(f"Uploaded to `{uri}`, waiting for BigQuery…"):
                        ok = wait_for_rows(PREDICTIONS, "prediction_id", prediction_id, 1, timeout=120)
                    if ok:
                        st.success("Prediction logged to BigQuery.")
                        st.cache_data.clear()
                    else:
                        st.warning("Uploaded, but the row has not shown up in BigQuery yet.")
            elif body:
                st.error(body.get("error", resp.text))

        st.markdown("**Recent predictions (from BigQuery)**")
        recent = cached_query(f"""
            SELECT predicted_at, ROUND(predicted_value) AS predicted_value, model_version,
                   features.median_income, features.house_age, features.latitude, features.longitude
            FROM {PREDICTIONS} ORDER BY predicted_at DESC LIMIT 20""")
        if recent is None or recent.empty:
            st.caption("None yet.")
        else:
            st.dataframe(recent, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- sql
with tab_sql:
    st.subheader("Query BigQuery")
    sql = st.text_area("SQL", f"""SELECT location.region, COUNT(*) AS n,
       ROUND(AVG(median_house_value)) AS avg_value
FROM {LISTINGS}
GROUP BY 1
ORDER BY avg_value DESC""", height=180)
    if st.button("Run query"):
        try:
            with st.spinner("Running…"):
                result = run_query(sql)
            st.dataframe(result, use_container_width=True, hide_index=True)
            st.caption(f"{len(result):,} rows")
        except Exception as e:
            st.error(str(e))

# ---------------------------------------------------------------- bucket
with tab_bucket:
    st.subheader(f"Files in gs://{bucket_name}")
    try:
        blobs = sorted(gcs.list_blobs(bucket_name), key=lambda b: b.time_created, reverse=True)[:100]
        st.dataframe(pd.DataFrame([{
            "name": b.name, "size_kb": round(b.size / 1024, 1), "created": b.time_created,
            "routed_to": ("housing_listings" if b.name.startswith("housing_listings")
                          else "price_predictions" if b.name.startswith("price_predictions") else "—"),
        } for b in blobs]), use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"Could not list bucket: {e}")
