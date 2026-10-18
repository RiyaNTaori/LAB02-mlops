"""Helpers for turning California Housing rows into the NDJSON records the ETL function loads."""
import json
import uuid
from datetime import datetime, timezone

import pandas as pd

# Column names produced by sklearn.datasets.fetch_california_housing
SKLEARN_TO_SCHEMA = {
    "MedInc": "median_income",
    "HouseAge": "house_age",
    "AveRooms": "avg_rooms",
    "AveBedrms": "avg_bedrooms",
    "Population": "population",
    "AveOccup": "avg_occupancy",
    "Latitude": "latitude",
    "Longitude": "longitude",
    "MedHouseVal": "median_house_value",
}

FEATURES = [
    "median_income", "house_age", "avg_rooms", "avg_bedrooms",
    "population", "avg_occupancy", "latitude", "longitude",
]
TARGET = "median_house_value"
REQUIRED_COLUMNS = FEATURES + [TARGET]

# Rough bounding boxes, checked in order.
REGIONS = [
    ("Bay Area", 37.1, 38.4, -123.1, -121.5),
    ("Los Angeles", 33.6, 34.5, -118.9, -117.6),
    ("San Diego", 32.5, 33.4, -117.6, -116.8),
    ("Sacramento", 38.3, 38.9, -121.7, -121.0),
    ("Central Valley", 35.0, 40.0, -121.5, -118.5),
]


def region_for(lat, lon):
    for name, lat_lo, lat_hi, lon_lo, lon_hi in REGIONS:
        if lat_lo <= lat <= lat_hi and lon_lo <= lon <= lon_hi:
            return name
    return "Other"


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Accept either sklearn column names or schema column names."""
    df = df.rename(columns=SKLEARN_TO_SCHEMA)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {', '.join(missing)}")
    return df


def to_records(df: pd.DataFrame, batch_id: str) -> list[dict]:
    """Convert a housing dataframe into nested records matching staging.housing_listings."""
    df = normalize_columns(df)
    uploaded_at = datetime.now(timezone.utc).isoformat()
    records = []
    for row in df.itertuples(index=False):
        r = row._asdict()
        records.append({
            "listing_id": str(uuid.uuid4()),
            "batch_id": batch_id,
            "uploaded_at": uploaded_at,
            "median_income": float(r["median_income"]),
            "house_age": float(r["house_age"]),
            "avg_rooms": float(r["avg_rooms"]),
            "avg_bedrooms": float(r["avg_bedrooms"]),
            "population": float(r["population"]),
            "avg_occupancy": float(r["avg_occupancy"]),
            # sklearn reports value in units of $100,000; store dollars.
            "median_house_value": round(float(r[TARGET]) * 100_000, 2),
            "location": {
                "latitude": float(r["latitude"]),
                "longitude": float(r["longitude"]),
                "region": region_for(float(r["latitude"]), float(r["longitude"])),
            },
        })
    return records


def to_ndjson(records: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in records) + "\n"


def new_batch_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
