"""
api/routes/battery_model.py
----------------------------
GET /api/battery-model/info

Returns metadata about the predictive battery model:
  - model_type          : scikit-learn class name
  - feature_names       : ordered list of features used
  - feature_importances : per-feature importance (tree models only)
  - training_samples    : synthetic dataset size
  - use_predictive_battery_flag : whether ML model is active
  - caveat              : plain-text disclaimer for judge transparency

The ML model is loaded into app.state.ml_battery_model at startup.
If not present (no pickle), the response still returns cleanly with
model_type="StaticBatteryModel" and a note.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Request

from api.models import BatteryModelInfoResponse, FeatureImportanceOut
from ml.battery_model import FEATURE_NAMES

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Battery Model"])

_CAVEAT = (
    "⚠️  This model was trained on synthetic data generated from domain "
    "constants (drive energy ~50 Wh/km, heater surcharge in dark zones, "
    "Peukert efficiency loss). It has NOT been validated against real "
    "Chandrayaan-2 or any flight hardware data. Use for demonstration "
    "purposes only. Accuracy will improve when actual rover telemetry "
    "is available."
)

_TRAINING_SAMPLES = 5_000   # must match ml/train_battery_model.py N_SAMPLES


@router.get(
    "/api/battery-model/info",
    response_model=BatteryModelInfoResponse,
    summary="Predictive Battery Model — Metadata",
    description=(
        "Return metadata about the scikit-learn battery drain model used when "
        "`use_predictive_battery=true`.  If the model pickle is missing, the "
        "API transparently falls back to the static constant model and this "
        "endpoint documents that fact."
    ),
)
async def battery_model_info(request: Request) -> BatteryModelInfoResponse:
    ml_model = getattr(request.app.state, "ml_battery_model", None)

    if ml_model is None:
        # Pickle not loaded — report static fallback
        return BatteryModelInfoResponse(
            model_type="StaticBatteryModel",
            feature_names=FEATURE_NAMES,
            feature_importances=None,
            training_samples=0,
            use_predictive_battery_flag=False,
            caveat=(
                "ML model pickle not found. Run `python -m ml.train_battery_model` "
                "to generate it, then restart the API server. "
                "Currently using StaticBatteryModel (hand-tuned constants). "
                + _CAVEAT
            ),
        )

    # ML model is loaded
    raw_fi = ml_model.feature_importances()
    fi_out: Optional[list[FeatureImportanceOut]] = None
    if raw_fi is not None:
        fi_out = [
            FeatureImportanceOut(feature=d["feature"], importance=d["importance"])
            for d in raw_fi
        ]

    return BatteryModelInfoResponse(
        model_type=ml_model.model_type,
        feature_names=FEATURE_NAMES,
        feature_importances=fi_out,
        training_samples=_TRAINING_SAMPLES,
        use_predictive_battery_flag=True,
        caveat=_CAVEAT,
    )
