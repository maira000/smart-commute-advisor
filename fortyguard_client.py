"""
FortyGuard Temperature API client
---------------------------------
Thin wrapper around the real FortyGuard Enterprise API following their
official quickstart pattern (https://docs-api.fortyguard.com/docs/quickstart):

    1. POST /v1/heatmap          -> submit task, receive data.activity_id
    2. GET  /v1/status/<id>      -> poll every ~5 s until status == "Completed"
    3. data.result               -> {"map_data": <GeoJSON>, "stats_data": {...}}

Auth is a single header:  api-key: <key>
Credentials come from environment variables loaded from `.env` (via
python-dotenv):

    FORTYGUARD_API_KEY=...
    FORTYGUard_BASE_URL=https://api.fortyguard.com   # optional override

CREDIT-SAFETY DESIGN
--------------------
Streamlit reruns the whole script on every widget interaction, so this module
is deliberately *never* called automatically during normal rendering. The app:

    1. Checks the local JSON cache first (data/<area>_<date>_<analytic>.json)
    2. Only performs a live call when the user explicitly clicks
       "Fetch live data" in the UI
    3. Saves every live response to the cache before rendering

Never hardcode your API key anywhere in source code.
"""

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "https://api.fortyguard.com"
POLL_INTERVAL_SECONDS = 5
MAX_POLLS = 120  # 120 * 5 s = 10 minutes upper bound per task
REQUEST_TIMEOUT_SECONDS = 30

CACHE_DIR = Path(__file__).parent / "data"


class FortyGuardError(RuntimeError):
    """Base error for FortyGuard API interactions."""


class MissingCredentialsError(FortyGuardError):
    """Raised when FORTYGUARD_API_KEY is not configured."""


class TaskFailedError(FortyGuardError):
    """Raised when a submitted task ends in the terminal 'Failed' state."""


# ----------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------

def get_api_key() -> Optional[str]:
    return os.getenv("FORTYGUARD_API_KEY") or None


def get_base_url() -> str:
    return (os.getenv("FORTYGUARD_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def has_credentials() -> bool:
    return bool(get_api_key())


def _headers(api_key: str) -> dict:
    # NOTE: never log these headers — they contain the secret key.
    return {"api-key": api_key, "Content-Type": "application/json"}


# ----------------------------------------------------------------------
# Request building helpers
# ----------------------------------------------------------------------

def f_to_c(temp_f: float) -> float:
    return round((temp_f - 32.0) * 5.0 / 9.0, 2)


def square_polygon_geojson(lat: float, lon: float, side_km: float = 1.5) -> dict:
    """
    Builds a small square GeoJSON FeatureCollection polygon centered on
    (lat, lon) — used as polygon_aoi when we only have a point of interest.
    """
    half_lat = (side_km / 2.0) / 111.32  # degrees latitude per km (~constant)
    half_lon = (side_km / 2.0) / (111.32 * max(0.2, math.cos(math.radians(lat))))
    ring = [
        [lon - half_lon, lat - half_lat],
        [lon + half_lon, lat - half_lat],
        [lon + half_lon, lat + half_lat],
        [lon - half_lon, lat + half_lat],
        [lon - half_lon, lat - half_lat],  # closed ring
    ]
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        ],
    }


def build_heatmap_payload(
    polygon_aoi: dict,
    date: str,
    analytic_type: str = "tcm",
    threshold_f: Optional[float] = None,
    granularity: int = 100,
) -> dict:
    """
    Assembles the POST /v1/heatmap body.

    filter_type=3 means "Single Day" (covers 00:00-23:59 for start_date).
    threshold must be Celsius for exceedance/persistence analyses.
    """
    payload: dict[str, Any] = {
        "polygon_aoi": polygon_aoi,
        "date_time": {
            "start_date": date,
            "filter_type": 3,
        },
        "granularity": granularity,
        "analytic_type": analytic_type,
    }
    if analytic_type in ("exceedance", "persistence"):
        if threshold_f is None:
            raise ValueError(f"{analytic_type} requires a threshold")
        payload["threshold"] = f_to_c(threshold_f)
        payload["direction"] = "above"
    return payload


# ----------------------------------------------------------------------
# Local JSON cache (checked BEFORE any live call)
# ----------------------------------------------------------------------

def _safe_name(area_name: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", area_name.lower()).strip("_")
    return name or "area"


def cache_path(
    area_name: str,
    date: str,
    analytic_type: str = "tcm",
    threshold_f: Optional[float] = None,
) -> Path:
    """data/<area>_<date>_<analytic>[_thrNNNF].json"""
    parts = [_safe_name(area_name), date, _safe_name(analytic_type)]
    if analytic_type in ("exceedance", "persistence") and threshold_f is not None:
        parts.append(f"thr{int(round(threshold_f))}F")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / ("_".join(parts) + ".json")


def load_cached(
    area_name: str,
    date: str,
    analytic_type: str = "tcm",
    threshold_f: Optional[float] = None,
) -> Optional[dict]:
    path = cache_path(area_name, date, analytic_type, threshold_f)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_cache(
    payload: dict,
    area_name: str,
    date: str,
    analytic_type: str = "tcm",
    threshold_f: Optional[float] = None,
) -> Path:
    path = cache_path(area_name, date, analytic_type, threshold_f)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# Submit + poll (mirrors the official quickstart pattern)
# ----------------------------------------------------------------------

def create_heatmap(
    polygon_aoi: dict,
    date: str,
    analytic_type: str = "tcm",
    threshold_f: Optional[float] = None,
    granularity: int = 100,
    progress_callback=None,
    api_key: Optional[str] = None,
) -> dict:
    """
    Submits a heatmap task and polls until completion.

    Returns data.result from the completed status response:
        {"map_data": <GeoJSON FeatureCollection>, "stats_data": {...}}

    Raises MissingCredentialsError / TaskFailedError / FortyGuardError.
    """
    key = api_key or get_api_key()
    if not key:
        raise MissingCredentialsError(
            "No API key configured. Copy .env.example to .env and set "
            "FORTYGUARD_API_KEY (never hardcode keys in source code)."
        )
    base = get_base_url()

    payload = build_heatmap_payload(polygon_aoi, date, analytic_type, threshold_f, granularity)

    # --- Step 1: submit --------------------------------------------------
    try:
        submit_resp = requests.post(
            f"{base}/v1/heatmap",
            headers=_headers(key),
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise FortyGuardError(f"Could not reach {base}: {exc}") from exc

    if submit_resp.status_code in (400, 422):
        raise FortyGuardError(f"Invalid request (HTTP {submit_resp.status_code}): {submit_resp.text[:300]}")
    if submit_resp.status_code == 401:
        raise MissingCredentialsError("API key missing or invalid (HTTP 401).")
    if submit_resp.status_code == 403:
        raise FortyGuardError("Insufficient plan access (HTTP 403).")
    if submit_resp.status_code == 429:
        raise FortyGuardError("Rate limit exceeded (HTTP 429). Try again later.")
    submit_resp.raise_for_status()

    try:
        activity_id = submit_resp.json()["data"]["activity_id"]
    except (KeyError, TypeError, ValueError) as exc:
        raise FortyGuardError(
            f"Unexpected submission response shape: {submit_resp.text[:300]}"
        ) from exc

    if progress_callback:
        progress_callback(f"Task submitted (activity {activity_id}). Polling…")

    # --- Step 2: poll GET /v1/status/<activity_id> -----------------------
    for poll_index in range(MAX_POLLS):
        time.sleep(POLL_INTERVAL_SECONDS if poll_index else 0)
        try:
            status_resp = requests.get(
                f"{base}/v1/status/{activity_id}",
                headers={"api-key": key},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            status_resp.raise_for_status()
            data = status_resp.json().get("data") or {}
        except (requests.RequestException, ValueError) as exc:
            # 404 can occur briefly right after submission — keep polling.
            if progress_callback:
                progress_callback(f"Status check hiccup ({exc}); retrying…")
            continue

        status = (data.get("status") or "").lower()
        if status in ("completed", "succeeded"):
            result = data.get("result")
            if result is None:
                raise FortyGuardError(
                    f"Activity {activity_id} completed without a result payload."
                )
            if progress_callback:
                progress_callback("Completed.")
            return result
        if status in ("failed", "error"):
            raise TaskFailedError(
                f"Activity {activity_id} failed on FortyGuard's side. "
                "Record this ID and contact support if it persists."
            )

        if progress_callback:
            elapsed = poll_index * POLL_INTERVAL_SECONDS
            progress_callback(f"Still processing… ({elapsed}s elapsed)")

    raise FortyGuardError(
        f"Activity {activity_id} did not complete within "
        f"{MAX_POLLS * POLL_INTERVAL_SECONDS // 60} minutes. It may still finish — "
        "retry later; credits are only deducted on completion."
    )


# ----------------------------------------------------------------------
# Result parsing (tolerant — handles several plausible payload shapes)
# ----------------------------------------------------------------------

_HOURLY_KEYS = ("hourly_temps", "temps_by_hour", "timeseries", "time_series", "hourly", "hours")


def _coerce_hourly(value: Any) -> Optional[list[tuple[int, float]]]:
    """
    Normalizes something that looks like hourly readings into (hour, °C)
    pairs ordered by hour. Accepts:
      {"5": 40.1, ...} | [{"hour"/"time", "temp_c"/...}, ...] | [40.1, 41.0, ...]
    """
    temps: dict[int, float] = {}

    def _read_item(hour_key: Any, item: Any) -> None:
        try:
            h = int(hour_key)
        except (TypeError, ValueError):
            return
        if not 0 <= h <= 23:
            return
        if isinstance(item, (int, float)):
            temps[h] = float(item)
        elif isinstance(item, dict):
            for tk in ("temp_c", "temperature", "temp", "value", "tcm"):
                v = item.get(tk)
                if isinstance(v, (int, float)):
                    temps[h] = float(v)
                    return

    if isinstance(value, dict):
        for k, v in value.items():
            _read_item(k, v)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, dict):
                hour_field = item.get("hour", item.get("time", i))
                if isinstance(hour_field, str) and ":" in hour_field:
                    try:
                        hour_field = int(str(hour_field).split(":")[0])
                    except ValueError:
                        pass
                _read_item(hour_field, item)
            else:
                _read_item(i, item)

    if len(temps) >= 6:  # enough coverage to be a usable daily curve
        return sorted(temps.items())
    return None


def extract_hourly_series(result: dict) -> Optional[list[dict]]:
    """
    Best-effort extraction of an hourly temperature series (°C) from a
    completed heatmap result. Returns [{"hour": h, "temp_c": t}, ...] sorted
    by hour, or None when the payload has no recognizable time series (e.g.
    pure exceedance/persistence aggregates).
    """
    search_spaces: list[Any] = [result]

    # Some cached payloads may be stored as full status responses with a
    # nested "result" object — search that too.
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        search_spaces.append(result["result"])

    map_data = result.get("map_data") if isinstance(result, dict) else None
    if isinstance(map_data, dict):
        search_spaces.append(map_data)
        for feature in map_data.get("features") or []:
            if isinstance(feature, dict):
                search_spaces.append(feature.get("properties") or {})

    stats = result.get("stats_data") if isinstance(result, dict) else None
    if isinstance(stats, dict):
        search_spaces.append(stats)

    for space in search_spaces:
        if not isinstance(space, dict):
            continue
        for key in _HOURLY_KEYS:
            if key in space:
                pairs = _coerce_hourly(space[key])
                if pairs:
                    return [
                        {"hour": h, "temp_c": round(t, 2)} for h, t in pairs
                    ]
    return None


def summarize_result(result: dict) -> dict:
    """Pulls human-readable aggregate stats out of stats_data, tolerantly."""
    stats = result.get("stats_data") or {}
    temp_stats = stats.get("Temperature_stats") or stats.get("temperature_stats") or {}

    tile_hours: list[float] = []
    map_data = result.get("map_data") or {}
    features = map_data.get("features") or [] if isinstance(map_data, dict) else []
    for feature in features:
        props = (feature or {}).get("properties") or {}
        for key in ("exceedance_hours", "value", "hours"):
            v = props.get(key)
            if isinstance(v, (int, float)):
                tile_hours.append(float(v))
                break

    summary: dict[str, Any] = {
        "stats_present": bool(stats),
        "tile_count": len(features),
    }
    if temp_stats:
        summary["min"] = temp_stats.get("Minimum")
        summary["max"] = temp_stats.get("Maximum")
        summary["mean"] = temp_stats.get("Mean")
        summary["std_dev"] = temp_stats.get("Standard_deviation")
    if tile_hours:
        tile_hours.sort()
        mid = len(tile_hours) // 2
        summary["median_tile_hours_above_threshold"] = (
            tile_hours[mid]
            if len(tile_hours) % 2
            else (tile_hours[mid - 1] + tile_hours[mid]) / 2
        )
    return summary


def fetch_and_cache_area(
    area_name: str,
    lat: float,
    lon: float,
    date: str,
    analytic_type: str = "tcm",
    threshold_f: Optional[float] = None,
    side_km: float = 1.5,
    progress_callback=None,
) -> tuple[dict, bool]:
    """
    Cache-first orchestration used by the app's manual fetch button.

    Returns (payload, from_cache). Never performs a live call unless the
    cache file is missing.
    """
    cached = load_cached(area_name, date, analytic_type, threshold_f)
    if cached is not None:
        return cached, True

    polygon = square_polygon_geojson(lat, lon, side_km=side_km)
    result = create_heatmap(
        polygon_aoi=polygon,
        date=date,
        analytic_type=analytic_type,
        threshold_f=threshold_f,
        progress_callback=progress_callback,
    )
    save_cache(result, area_name, date, analytic_type, threshold_f)
    return result, False
