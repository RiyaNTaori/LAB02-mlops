import os

import functions_framework
import joblib
import numpy as np
from flask import jsonify

MODEL_PATH = os.path.join(os.path.dirname(__file__), "model.joblib")
_bundle = None

FEATURE_RANGES = {
    "median_income": (0, 20),
    "house_age": (0, 100),
    "avg_rooms": (0, 200),
    "avg_bedrooms": (0, 50),
    "population": (0, 50000),
    "avg_occupancy": (0, 2000),
    "latitude": (32, 42.5),
    "longitude": (-125, -114),
}


def load_model():
    """Loads the bundled model once per instance."""
    global _bundle
    if _bundle is None:
        _bundle = joblib.load(MODEL_PATH)
    return _bundle


def parse_features(payload, feature_names):
    """Accepts {"features": [8 numbers]} or {"features": {name: number, ...}}."""
    features = payload.get("features")
    if isinstance(features, dict):
        missing = [f for f in feature_names if f not in features]
        if missing:
            raise ValueError(f"Missing features: {', '.join(missing)}")
        features = [features[f] for f in feature_names]
    if not isinstance(features, list) or len(features) != len(feature_names):
        raise ValueError(f"'features' must be a list of {len(feature_names)} numbers or an object with keys {feature_names}")
    values = [float(x) for x in features]
    for name, value in zip(feature_names, values):
        lo, hi = FEATURE_RANGES[name]
        if not lo <= value <= hi:
            raise ValueError(f"{name}={value} is outside the expected range [{lo}, {hi}]")
    return values


@functions_framework.http
def predict(request):
    """Predicts California median house value (USD) from 8 block-group features."""
    headers = {"Access-Control-Allow-Origin": "*"}
    if request.method == "OPTIONS":
        return ("", 204, {**headers, "Access-Control-Allow-Methods": "POST, GET",
                          "Access-Control-Allow-Headers": "Content-Type"})

    bundle = load_model()
    if request.method == "GET":
        return jsonify({"model_version": bundle["version"], "features": bundle["features"],
                        "metrics": bundle["metrics"]}), 200, headers

    try:
        if not request.is_json:
            return jsonify({"error": "Request content-type must be application/json"}), 400, headers
        payload = request.get_json(silent=True) or {}
        if "features" not in payload:
            return jsonify({"error": "Missing 'features' in request body"}), 400, headers
        values = parse_features(payload, bundle["features"])
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400, headers

    try:
        prediction = float(bundle["model"].predict(np.array(values).reshape(1, -1))[0])
        return jsonify({
            "predicted_median_house_value": round(max(prediction, 0.0), 2),
            "model_version": bundle["version"],
            "features": dict(zip(bundle["features"], values)),
        }), 200, headers
    except Exception as e:
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500, headers
