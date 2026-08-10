"""
Flask web server for Asset Allocation Model portfolio optimization API.
"""

import os
import hashlib
import hmac
import json
import math
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta, timezone

import flask
from flask import Flask, request, jsonify
try:
    from google.cloud import storage
except Exception:  # pragma: no cover - optional in local JSON-only engine mode
    storage = None
import requests

# Import the optimization pipeline
# Add parent directory to path to access SAA Model
import sys

# Get the project root (parent of api directory)
# This works whether running from api/ or from parent directory
_current_file = Path(__file__).resolve()
if _current_file.parent.name == "api":
    # Running from api directory - go up one level
    BASE_DIR = _current_file.parent.parent
else:
    # Running as module - already at correct level
    BASE_DIR = _current_file.parent.parent
SAA_MODEL_DIR = BASE_DIR / "SAA Model"

# Change to SAA Model directory for relative imports to work
_original_cwd = os.getcwd()
os.chdir(str(SAA_MODEL_DIR))
sys.path.insert(0, str(SAA_MODEL_DIR))

from layers.L2.layer2_active_risk import (
    run_layered_optimization,
    build_layer1_config,
    build_layer2_config,
    build_layer3_config,
    ActiveRiskAllocator,
)
from layers.L1.layer1_saa import apply_hard_exclusions, run_layer1
from layers.L3.layer3_manager_selection import ManagerSelectionEngine
from layers.L3.portfolio_metrics import (
    compute_portfolio_expected_return_and_volatility,
)
import argparse

_apply_hard_exclusions = apply_hard_exclusions

TARGET_CALIBRATION_MAX_ATTEMPTS = 6
TARGET_CALIBRATION_TOLERANCE = 0.0001
MIN_INTERNAL_TARGET_VOLATILITY = 0.01
# The internal solver target may need to sit above the signed boundary because
# Layers 2/3 can reduce final portfolio volatility. This is calibration only;
# the external signed target remains capped at 20%.
MAX_INTERNAL_TARGET_VOLATILITY = 0.25

# Restore original working directory
os.chdir(_original_cwd)

app = Flask(__name__)

ASSET_ALLOCATION_CONSTRAINT_CONTRACT_VERSION = "asset_allocation_constraints.v1"
SUPPORTED_ASSET_CLASSES = {
    "Cash",
    "US Treasury",
    "Global Investment Grade Corporate Bond",
    "Global High Yield Bond BB-B",
    "Emerging Market Local Currency Government Bonds",
    "Emerging Market Hard Currency Debt",
    "US Equity",
    "Dev. Europe ex UK Equity",
    "Japan Equity",
    "China Equity",
    "India Equity",
    "Commodities",
    "Gold",
    "Hedge Funds",
    "Bitcoin",
}

# Load environment variables
# Priority: 1) Process environment variables (Docker --env-file, GitHub Actions, etc.)
#           2) .env file (for local development)
# This allows GitHub Actions to pass env vars directly, while local dev uses .env file
try:
    from dotenv import load_dotenv

    # Only load .env if it exists and variables aren't already set
    # override=False ensures environment variables take precedence
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass  # dotenv is optional

# Read from environment (works with both Docker env vars and .env file)
API_SECRET = os.getenv("API_SECRET")
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
GCS_PROJECT_ID = os.getenv("GCS_PROJECT_ID")
GCS_CREDENTIALS_PATH = os.getenv("GCS_CREDENTIALS_PATH")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

# Debug: Print environment variables (flush immediately for visibility)
import sys

# Validate required environment variables
if not API_SECRET and os.getenv("LOCAL_DEV", "").lower() == "true":
    API_SECRET = "local-dev-secret"
if not API_SECRET:
    raise ValueError("API_SECRET environment variable is required")

storage_client = None
bucket = None
local_dev = os.getenv("LOCAL_DEV", "").lower() == "true"

# Initialize GCS client when configured. Local JSON optimize does not require GCS.
if storage is None:
    print(
        "GCS disabled: google-cloud-storage is not installed.",
        file=sys.stderr,
        flush=True,
    )
elif not GCS_BUCKET_NAME or not GCS_PROJECT_ID:
    if not local_dev:
        print(
            "GCS disabled: GCS_BUCKET_NAME and GCS_PROJECT_ID are not configured.",
            file=sys.stderr,
            flush=True,
        )
else:
    try:
        print(f"Initializing GCS client...", file=sys.stderr, flush=True)

        # Resolve credentials path - try multiple locations
        credentials_path = None
        if GCS_CREDENTIALS_PATH:
            # Try as-is first
            if os.path.exists(GCS_CREDENTIALS_PATH):
                credentials_path = GCS_CREDENTIALS_PATH
            else:
                # Try relative to api directory
                api_dir = Path(__file__).parent
                potential_path = api_dir / GCS_CREDENTIALS_PATH
                if potential_path.exists():
                    credentials_path = str(potential_path)
                else:
                    # Try relative to app root
                    app_root = Path(__file__).parent.parent
                    potential_path = app_root / GCS_CREDENTIALS_PATH
                    if potential_path.exists():
                        credentials_path = str(potential_path)
        if credentials_path and os.path.exists(credentials_path):
            print(
                f"Using service account credentials from: {credentials_path}",
                file=sys.stderr,
                flush=True,
            )
            storage_client = storage.Client.from_service_account_json(
                credentials_path, project=GCS_PROJECT_ID
            )
        else:
            print(
                "Using default credentials (for Cloud Run or Application Default Credentials)",
                file=sys.stderr,
                flush=True,
            )
            if GCS_CREDENTIALS_PATH:
                print(
                    f"WARNING: Credentials file not found at: {GCS_CREDENTIALS_PATH}",
                    file=sys.stderr,
                    flush=True,
                )
            # Try to use default credentials (for Cloud Run)
            storage_client = storage.Client(project=GCS_PROJECT_ID)

        print(
            f"Creating bucket reference for: {GCS_BUCKET_NAME}",
            file=sys.stderr,
            flush=True,
        )
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        print("GCS client initialized successfully!", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"ERROR: Could not initialize GCS client: {e}", file=sys.stderr, flush=True)
        print(
            "GCS operations will fail. Make sure credentials are properly configured.",
            file=sys.stderr,
            flush=True,
        )


def verify_api_key(api_key: str) -> bool:
    """Verify API key using HMAC."""
    if not api_key:
        return False

    # API keys are stored as: HMAC(secret, "api_key")
    # We need to check if the provided key matches any valid key
    # For simplicity, we'll check against a single expected key
    # In production, you might want to store valid keys in a database

    # Generate expected key from secret
    expected_key = hmac.new(
        API_SECRET.encode("utf-8"), b"api_key", hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(api_key, expected_key)


def require_api_key(f):
    """Decorator to require API key authentication."""

    def decorated_function(*args, **kwargs):
        api_key = request.headers.get("X-Api-Key")
        if not api_key or not verify_api_key(api_key):
            return (
                jsonify(
                    {"error": "Unauthorized", "message": "Invalid or missing API key"}
                ),
                401,
            )
        return f(*args, **kwargs)

    decorated_function.__name__ = f.__name__
    return decorated_function


def upload_file_to_gcs(local_path: Path, gcs_path: str) -> str:
    """Upload a file to Google Cloud Storage and return presigned URL."""
    if not bucket or not storage_client:
        raise RuntimeError("GCS bucket not initialized. Check your GCS configuration.")

    blob = bucket.blob(gcs_path)
    blob.upload_from_filename(str(local_path))

    # Generate presigned URL (valid for 1 hour)
    # The client will automatically use the service account credentials
    url = blob.generate_signed_url(
        version="v4", expiration=timedelta(hours=1), method="GET"
    )
    return url


def call_webhook(webhook_config: Dict[str, Any], data: Dict[str, Any]) -> None:
    """Call webhook with provided configuration."""
    url = webhook_config.get("url")
    method = webhook_config.get("method", "POST")
    headers = webhook_config.get("headers", {})

    if method != "POST":
        # This should have been validated earlier, but double-check
        return

    try:
        response = requests.post(url, json=data, headers=headers, timeout=30)
        response.raise_for_status()
    except Exception as e:
        # Log error but don't fail the request
        print(f"Webhook call failed: {e}")


def run_optimization(
    risk_profile: str = "RP1",
    weight_type: str = "dynamic",
    target_volatility: float = None,
) -> Tuple[Path, Path]:
    """Run the portfolio optimization pipeline and return output file paths."""
    # Create a namespace for temporary outputs
    temp_output_dir = BASE_DIR.parent / ".tmp" / "asset_allocation_outputs"
    temp_output_dir.mkdir(parents=True, exist_ok=True)

    saa_output = temp_output_dir / "SAA_Results.xlsx"
    portfolio_output = temp_output_dir / "Portfolio_Construction_Results.xlsx"

    # Build arguments
    args = argparse.Namespace(
        risk_profile=risk_profile,
        weight_type=weight_type,
        target_volatility=target_volatility,
    )

    # Temporarily modify the output paths in the config
    # We need to patch the config building functions
    import layers.L2.layer2_active_risk as layer2_module

    original_build_layer1_config = layer2_module.build_layer1_config
    original_build_layer2_config = layer2_module.build_layer2_config

    # Change to SAA Model directory for relative paths to work
    original_cwd = os.getcwd()
    saa_model_dir = BASE_DIR / "SAA Model"

    try:
        os.chdir(str(saa_model_dir))

        # Monkey patch to use temp outputs
        def patched_layer1_config(args):
            config = original_build_layer1_config(args)
            config.output_file = saa_output
            return config

        def patched_layer2_config(args, layer1_target_vol):
            config = original_build_layer2_config(args, layer1_target_vol)
            config.output_file = portfolio_output
            return config

        # Replace the functions temporarily
        layer2_module.build_layer1_config = patched_layer1_config
        layer2_module.build_layer2_config = patched_layer2_config

        # Run optimization
        result = run_layered_optimization(args)

        return saa_output, portfolio_output
    finally:
        # Restore original functions and working directory
        layer2_module.build_layer1_config = original_build_layer1_config
        layer2_module.build_layer2_config = original_build_layer2_config
        os.chdir(original_cwd)


def run_optimization_json(
    risk_profile: str = "RP3",
    target_volatility: float = 0.12,
    weight_type: str = "dynamic",
    active_risk_percentage: Optional[float] = None,
    investment_amount: Optional[float] = None,
    excluded_asset_classes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Run the portfolio optimization pipeline and return results as JSON.

    This is a JSON-friendly version of run_layered_optimization that returns
    structured data instead of writing to Excel files.

    Args:
        risk_profile: Risk profile (RP1-RP5)
        target_volatility: Target portfolio volatility (e.g., 0.12 for 12%)
        weight_type: 'dynamic' or 'equilibrium'
        active_risk_percentage: Signed active-risk implementation budget.
        investment_amount: Signed mandate notional used for dollar outputs.
        excluded_asset_classes: Hard exclusions removed from the optimizer
            universe before Layer 1.

    Returns:
        Dictionary with all three layers' results and portfolio summary
    """
    # Build arguments
    args = argparse.Namespace(
        risk_profile=risk_profile,
        weight_type=weight_type,
        target_volatility=target_volatility,
        excluded_asset_classes=list(excluded_asset_classes or []),
    )

    # Change to SAA Model directory for relative paths to work
    original_cwd = os.getcwd()
    saa_model_dir = BASE_DIR / "SAA Model"

    try:
        os.chdir(str(saa_model_dir))

        temp_output_dir = BASE_DIR.parent / ".tmp" / "asset_allocation_outputs"
        temp_output_dir.mkdir(parents=True, exist_ok=True)
        layer3_config = build_layer3_config()

        def execute_layers(internal_target_volatility: Optional[float]):
            layer1_config = build_layer1_config(args)
            layer1_config.target_volatility = internal_target_volatility
            layer1_config.output_file = temp_output_dir / "SAA_Results.xlsx"
            layer1_value = run_layer1(layer1_config)
            layer2_config = build_layer2_config(args, layer1_value.target_vol)
            layer2_engine = ActiveRiskAllocator(
                layer2_config,
                active_risk_percentage=active_risk_percentage,
            )
            layer2_values = layer2_engine.run(layer1_value)
            (
                target_active_risks_value,
                active_alloc_value,
                achieved_vol_value,
                risk_budget_shares_value,
                layer2_info_value,
            ) = layer2_values
            manager_value = ManagerSelectionEngine(layer3_config).run(
                target_tes=target_active_risks_value
            )
            expected_return_value, expected_volatility_value = (
                compute_portfolio_expected_return_and_volatility(
                    layer1_result=layer1_value,
                    active_allocations=active_alloc_value,
                    manager_result=manager_value,
                    passive_vols=layer2_info_value.get("passive_vols", {}),
                )
            )
            return (
                layer1_value,
                target_active_risks_value,
                active_alloc_value,
                achieved_vol_value,
                risk_budget_shares_value,
                layer2_info_value,
                manager_value,
                expected_return_value,
                expected_volatility_value,
            )

        calibration_attempts: List[Dict[str, float]] = []
        if target_volatility is None:
            layer_values = execute_layers(None)
            signed_target = float(layer_values[0].target_vol)
            observed_volatility = float(layer_values[-1])
            difference = observed_volatility - signed_target
            calibration_attempts.append(
                {
                    "attempt": 1,
                    "internal_target_volatility": signed_target,
                    "observed_portfolio_volatility": observed_volatility,
                    "difference_bps": difference * 10_000.0,
                    "converged": (
                        abs(difference) <= TARGET_CALIBRATION_TOLERANCE
                    ),
                }
            )
        else:
            signed_target = float(target_volatility)
            layer_values, calibration_attempts = _calibrate_target_volatility(
                execute_layers,
                signed_target=signed_target,
            )

        (
            layer1_result,
            target_active_risks,
            active_alloc,
            achieved_vol,
            risk_budget_shares,
            layer2_info,
            manager_result,
            portfolio_expected_return,
            portfolio_expected_volatility,
        ) = layer_values

        # Serialize results to JSON-compatible format
        # Layer 1 results
        layer1_json = {
            "profile_name": layer1_result.profile_name,
            "target_vol": float(layer1_result.target_vol),
            "equilibrium_weights": (
                layer1_result.equilibrium_weights.to_dict()
                if layer1_result.equilibrium_weights is not None
                else {}
            ),
            "dynamic_weights": (
                layer1_result.dynamic_weights.to_dict()
                if layer1_result.dynamic_weights is not None
                else None
            ),
            "selected_weights": (
                layer1_result.selected_weights.to_dict()
                if layer1_result.selected_weights is not None
                else {}
            ),
            "asset_clusters": layer1_result.asset_clusters,
        }

        # Layer 2 results
        layer2_json = {
            "target_active_risks": {
                k: float(v) for k, v in target_active_risks.items()
            },
            "active_allocations": {k: float(v) for k, v in active_alloc.items()},
            "achieved_volatility": float(achieved_vol),
            "risk_budget_shares": {k: float(v) for k, v in risk_budget_shares.items()},
            "active_risk_budget": float(layer2_info.get("active_risk_budget", 0.0)),
            "passive_risk_pct": float(layer2_info.get("passive_risk_pct", 0.0)),
            "active_risk_pct": float(layer2_info.get("active_risk_pct", 0.0)),
            "active_risk_source": layer2_info.get("active_risk_source"),
            "passive_tickers": layer2_info.get("passive_tickers", {}),
            "passive_names": layer2_info.get("passive_names", {}),
        }

        # Layer 3 results
        layer3_json = {
            "allocations_by_asset_class": {
                asset_class: {
                    manager: float(weight) for manager, weight in managers.items()
                }
                for asset_class, managers in manager_result.allocations.items()
            },
            "active_volatilities": {
                k: float(v) for k, v in manager_result.active_vols.items()
            },
            "active_tracking_errors": {
                k: float(v) for k, v in manager_result.active_tes.items()
            },
        }

        layer3_json["portfolio_expected_return"] = float(portfolio_expected_return)
        layer3_json["portfolio_expected_volatility"] = float(portfolio_expected_volatility)

        # Calculate portfolio summary
        total_managers = sum(
            len(managers) for managers in manager_result.allocations.values()
        )
        total_tracking_error = (
            sum(manager_result.active_tes.values())
            if manager_result.active_tes
            else 0.0
        )

        # Calculate expected return (weighted average of asset class returns)
        # Note: This is a simplified calculation - you may want to enhance this
        expected_return = (
            0.0  # Placeholder - would need access to expected returns data
        )

        portfolio_summary = {
            "total_volatility": float(achieved_vol),
            "total_tracking_error": float(total_tracking_error),
            "expected_return": float(expected_return),
            "portfolio_expected_return": float(portfolio_expected_return),
            "portfolio_expected_volatility": float(portfolio_expected_volatility),
            "manager_count": total_managers,
            "asset_class_count": len(layer1_result.selected_weights),
        }

        # Return complete JSON structure
        return {
            "success": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_investment": investment_amount,
            "excluded_asset_classes": list(excluded_asset_classes or []),
            "constraint_contract": {
                "version": ASSET_ALLOCATION_CONSTRAINT_CONTRACT_VERSION,
                "acknowledgements": {
                    "active_risk_percentage": {
                        "supported": active_risk_percentage is not None,
                        "applied_decimal": active_risk_percentage,
                        "source": layer2_info.get("active_risk_source"),
                    },
                    "investment_amount": {
                        "supported": investment_amount is not None,
                        "applied_usd": investment_amount,
                        "application": "security_notional_scaling",
                    },
                    "excluded_asset_classes": {
                        "supported": True,
                        "applied": list(excluded_asset_classes or []),
                        "application": "optimizer_universe_filter",
                    },
                },
            },
            "optimization_calibration": {
                "signed_target_volatility": signed_target,
                "final_internal_target_volatility": calibration_attempts[-1][
                    "internal_target_volatility"
                ],
                "final_observed_portfolio_volatility": calibration_attempts[-1][
                    "observed_portfolio_volatility"
                ],
                "final_difference_bps": calibration_attempts[-1][
                    "difference_bps"
                ],
                "convergence_tolerance_bps": (
                    TARGET_CALIBRATION_TOLERANCE * 10_000.0
                ),
                "converged": calibration_attempts[-1]["converged"],
                "attempts": calibration_attempts,
            },
            "layers": {
                "layer1": layer1_json,
                "layer2": layer2_json,
                "layer3": layer3_json,
            },
            "portfolio_summary": portfolio_summary,
        }

    except Exception as e:
        # Return error in JSON format
        return {
            "success": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": str(e),
            "error_type": type(e).__name__,
        }
    finally:
        os.chdir(original_cwd)


def _calibrate_target_volatility(
    execute_layers,
    *,
    signed_target: float,
    max_attempts: int = TARGET_CALIBRATION_MAX_ATTEMPTS,
    convergence_tolerance: float = TARGET_CALIBRATION_TOLERANCE,
):
    """Calibrate the model's internal target to the signed portfolio target.

    Layer 1's post-processing and Layers 2/3 can move final portfolio
    volatility away from the number supplied to Layer 1. The signed target
    remains immutable; only the internal solver target is adjusted. Returning
    every attempt makes this numerical implementation detail auditable.
    """

    internal_target = float(signed_target)
    layer_values = None
    attempts: List[Dict[str, Any]] = []
    for attempt_number in range(1, max(1, int(max_attempts)) + 1):
        layer_values = execute_layers(internal_target)
        observed_volatility = float(layer_values[-1])
        difference = observed_volatility - signed_target
        converged = abs(difference) <= convergence_tolerance
        attempts.append(
            {
                "attempt": attempt_number,
                "internal_target_volatility": internal_target,
                "observed_portfolio_volatility": observed_volatility,
                "difference_bps": difference * 10_000.0,
                "converged": converged,
            }
        )
        if converged:
            break
        internal_target = max(
            MIN_INTERNAL_TARGET_VOLATILITY,
            min(
                MAX_INTERNAL_TARGET_VOLATILITY,
                internal_target - difference,
            ),
        )
    return layer_values, attempts


@app.route("/asset-allocation/api/v1/generate", methods=["POST"])
@require_api_key
def generate():
    """Generate portfolio optimization results."""
    try:
        data = request.get_json() or {}

        # Validate required parameters
        storage_id = data.get("storageId")
        file_name = data.get("fileName")

        errors = {}
        if not storage_id:
            errors["storageId"] = [
                {"message": "storageId is required", "code": "REQUIRED"}
            ]
        if not file_name:
            errors["fileName"] = [
                {"message": "fileName is required", "code": "REQUIRED"}
            ]

        if errors:
            return jsonify(errors), 422

        # Validate webhook if provided
        webhook = data.get("webhook")
        if webhook:
            if not isinstance(webhook, dict):
                return jsonify({"error": "webhook must be an object"}), 400

            webhook_method = webhook.get("method", "POST")
            if webhook_method != "POST":
                return (
                    jsonify(
                        {
                            "error": "webhook method must be POST",
                            "code": "INVALID_METHOD",
                        }
                    ),
                    400,
                )

            if not webhook.get("url"):
                return (
                    jsonify({"error": "webhook url is required", "code": "REQUIRED"}),
                    400,
                )

        # Get optional optimization parameters
        risk_profile = data.get("riskProfile", "RP1")
        weight_type = data.get("weightType", "dynamic")

        # If webhook is provided, run asynchronously
        if webhook:

            def async_task():
                try:
                    # Run optimization
                    saa_path, portfolio_path = run_optimization(
                        risk_profile=risk_profile,
                        weight_type=weight_type,
                    )

                    # Upload to GCS
                    saa_gcs_path = f"{storage_id}/{file_name}/SAA_Results.xlsx"
                    portfolio_gcs_path = (
                        f"{storage_id}/{file_name}/Portfolio_Construction_Results.xlsx"
                    )

                    saa_url = upload_file_to_gcs(saa_path, saa_gcs_path)
                    portfolio_url = upload_file_to_gcs(
                        portfolio_path, portfolio_gcs_path
                    )

                    # Call webhook
                    webhook_data = {
                        "storageId": storage_id,
                        "fileName": file_name,
                        "status": "completed",
                        "files": {
                            "saaResults": saa_url,
                            "portfolioResults": portfolio_url,
                        },
                    }
                    call_webhook(webhook, webhook_data)

                    # Cleanup temp files
                    if saa_path.exists():
                        saa_path.unlink()
                    if portfolio_path.exists():
                        portfolio_path.unlink()
                except Exception as e:
                    # Call webhook with error
                    if webhook:
                        error_data = {
                            "storageId": storage_id,
                            "fileName": file_name,
                            "status": "error",
                            "error": str(e),
                        }
                        call_webhook(webhook, error_data)

            # Start async task
            thread = threading.Thread(target=async_task)
            thread.daemon = True
            thread.start()

            # Return immediately
            return (
                jsonify(
                    {
                        "status": "processing",
                        "message": "Request accepted and processing started",
                    }
                ),
                202,
            )

        else:
            # Synchronous execution
            saa_path, portfolio_path = run_optimization(
                risk_profile=risk_profile,
                weight_type=weight_type,
            )

            # Upload to GCS
            saa_gcs_path = f"{storage_id}/{file_name}/SAA_Results.xlsx"
            portfolio_gcs_path = (
                f"{storage_id}/{file_name}/Portfolio_Construction_Results.xlsx"
            )

            saa_url = upload_file_to_gcs(saa_path, saa_gcs_path)
            portfolio_url = upload_file_to_gcs(portfolio_path, portfolio_gcs_path)

            # Cleanup temp files
            if saa_path.exists():
                saa_path.unlink()
            if portfolio_path.exists():
                portfolio_path.unlink()

            return (
                jsonify(
                    {
                        "status": "completed",
                        "files": {
                            "saaResults": saa_url,
                            "portfolioResults": portfolio_url,
                        },
                    }
                ),
                200,
            )

    except Exception as e:
        return jsonify({"error": "Internal server error", "message": str(e)}), 500


@app.route("/asset-allocation/api/v1/optimize", methods=["POST"])
@require_api_key
def optimize():
    """
    Run portfolio optimization and return JSON results.

    This endpoint is designed for AWM advisor tool integration.
    Unlike /generate, this returns JSON directly without uploading to GCS.

    Request body:
    {
        "risk_profile": "RP1" | "RP2" | "RP3" | "RP4" | "RP5",
        "target_volatility": 0.12,  // Optional, derived from risk profile if not provided
        "weight_type": "dynamic" | "equilibrium"  // Optional, default: "dynamic"
    }

    Response:
    {
        "success": true,
        "timestamp": "2024-01-15T10:30:00Z",
        "layers": {
            "layer1": { ... },
            "layer2": { ... },
            "layer3": { ... }
        },
        "portfolio_summary": { ... }
    }
    """
    try:
        data = request.get_json() or {}

        # Get parameters with defaults
        risk_profile = data.get("risk_profile", "RP3")
        target_volatility = data.get(
            "target_volatility"
        )  # Optional - derived from risk profile
        weight_type = data.get("weight_type", "dynamic")
        active_risk_percentage = data.get("active_risk_percentage")
        investment_amount = data.get("investment_amount")
        excluded_asset_classes = data.get("excluded_asset_classes", [])

        # Validate risk_profile
        valid_profiles = ["RP1", "RP2", "RP3", "RP4", "RP5"]
        if risk_profile not in valid_profiles:
            return (
                jsonify(
                    {
                        "error": "Invalid risk_profile",
                        "message": f'risk_profile must be one of: {", ".join(valid_profiles)}',
                        "code": "INVALID_RISK_PROFILE",
                    }
                ),
                400,
            )

        # Validate weight_type
        valid_weight_types = ["dynamic", "equilibrium"]
        if weight_type not in valid_weight_types:
            return (
                jsonify(
                    {
                        "error": "Invalid weight_type",
                        "message": f'weight_type must be one of: {", ".join(valid_weight_types)}',
                        "code": "INVALID_WEIGHT_TYPE",
                    }
                ),
                400,
            )

        if active_risk_percentage is None:
            return (
                jsonify(
                    {
                        "error": "Missing active_risk_percentage",
                        "message": "active_risk_percentage is required for the optimize contract",
                        "code": "MISSING_REQUIRED_CONSTRAINT",
                    }
                ),
                422,
            )
        try:
            active_risk_percentage = float(active_risk_percentage)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid active_risk_percentage", "code": "INVALID_ACTIVE_RISK"}), 400
        if not math.isfinite(active_risk_percentage) or not 0.0 <= active_risk_percentage <= 1.0:
            return jsonify({"error": "Invalid active_risk_percentage", "code": "INVALID_ACTIVE_RISK"}), 400

        try:
            investment_amount = float(investment_amount)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid investment_amount", "code": "INVALID_INVESTMENT_AMOUNT"}), 400
        if not math.isfinite(investment_amount) or investment_amount <= 0:
            return jsonify({"error": "Invalid investment_amount", "code": "INVALID_INVESTMENT_AMOUNT"}), 400

        if not isinstance(excluded_asset_classes, list) or any(
            not isinstance(value, str) for value in excluded_asset_classes
        ):
            return jsonify({"error": "Invalid excluded_asset_classes", "code": "INVALID_EXCLUSIONS"}), 400
        unknown_exclusions = sorted(set(excluded_asset_classes) - SUPPORTED_ASSET_CLASSES)
        if unknown_exclusions:
            return (
                jsonify(
                    {
                        "error": "Unknown excluded asset class",
                        "code": "INVALID_EXCLUSIONS",
                        "unknown": unknown_exclusions,
                    }
                ),
                400,
            )
        # Validate target_volatility if provided
        if target_volatility is not None:
            try:
                target_volatility = float(target_volatility)
                if not math.isfinite(target_volatility) or not (0.05 <= target_volatility <= 0.20):
                    return (
                        jsonify(
                            {
                                "error": "Invalid target_volatility",
                                "message": "target_volatility must be between 0.05 and 0.20 (5% to 20%)",
                                "code": "INVALID_TARGET_VOLATILITY",
                            }
                        ),
                        400,
                    )
            except (TypeError, ValueError):
                return (
                    jsonify(
                        {
                            "error": "Invalid target_volatility",
                            "message": "target_volatility must be a number",
                            "code": "INVALID_TARGET_VOLATILITY",
                        }
                    ),
                    400,
                )

        # Run optimization
        result = run_optimization_json(
            risk_profile=risk_profile,
            target_volatility=target_volatility,
            weight_type=weight_type,
            active_risk_percentage=active_risk_percentage,
            investment_amount=investment_amount,
            excluded_asset_classes=excluded_asset_classes,
        )

        # Check if optimization was successful
        if not result.get("success", False):
            return jsonify(result), 500

        return jsonify(result), 200

    except Exception as e:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Internal server error",
                    "message": str(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ),
            500,
        )


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
