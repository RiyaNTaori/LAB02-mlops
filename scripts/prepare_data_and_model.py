"""Downloads California Housing, writes sample upload files, and trains the model for ml_function.

Run from the project root:  .venv/bin/python scripts/prepare_data_and_model.py
"""
import os
import sys

import joblib
import numpy as np
from sklearn.datasets import fetch_california_housing
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "streamlit_app"))
from housing import FEATURES, TARGET, normalize_columns, to_ndjson, to_records  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
MODEL_PATH = os.path.join(ROOT, "ml_function", "model.joblib")
MODEL_VERSION = "hgb-v1"


def main():
    df = normalize_columns(fetch_california_housing(as_frame=True).frame)
    os.makedirs(DATA_DIR, exist_ok=True)

    # Full CSV for the Streamlit app, plus small CSV/NDJSON batches for manual uploads.
    df.to_csv(os.path.join(DATA_DIR, "california_housing.csv"), index=False)
    sample = df.sample(n=1500, random_state=42).reset_index(drop=True)
    for i, chunk in enumerate(np.array_split(sample, 3), start=1):
        batch_id = f"sample-{i:03d}"
        chunk.to_csv(os.path.join(DATA_DIR, f"housing_sample_{i:03d}.csv"), index=False)
        with open(os.path.join(DATA_DIR, f"housing_listings_batch_{i:03d}.json"), "w") as f:
            f.write(to_ndjson(to_records(chunk, batch_id)))
    print(f"Wrote sample files to {DATA_DIR}")

    X, y = df[FEATURES], df[TARGET] * 100_000
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.1, random_state=42)
    model.fit(X_train.to_numpy(), y_train.to_numpy())
    pred = model.predict(X_test.to_numpy())
    metrics = {"r2": round(r2_score(y_test, pred), 4), "mae": round(mean_absolute_error(y_test, pred), 2)}
    print(f"Test metrics: {metrics}")

    joblib.dump({"model": model, "features": FEATURES, "version": MODEL_VERSION, "metrics": metrics}, MODEL_PATH)
    print(f"Saved model to {MODEL_PATH} ({os.path.getsize(MODEL_PATH) / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
