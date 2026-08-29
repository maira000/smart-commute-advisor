<img src="assets/cover_banner.svg" alt="Smart Commute & Outdoor Activity Advisor" width="100%">

# Smart Commute & Outdoor Activity Advisor

A Streamlit app that recommends the safest times to walk, run, or commute in Phoenix, AZ based on heat exposure — built for the FortyGuard Hackathon '26.

## How to Run It
```bash
pip install -r requirements.txt
streamlit run app.py
```
It will open automatically in your browser at `http://localhost:8501`.

## Automated Testing Suite
The app includes a headless UI test suite built on Streamlit's `AppTest` framework — requiring no browser, no network, and zero API credits.
```bash
pip install -r requirements-dev.txt
pytest tests/
```
The suite drives the real `app.py` script and verifies that it renders with **zero exceptions** across these scenarios:
1. The app loads with its default state (demo mode, Downtown Phoenix, and Fahrenheit display).
2. Toggling the temperature units between °F and °C works correctly.
3. Switching between all six sample Phoenix locations runs smoothly.
4. Toggling analytic types (TCM snapshot vs. Exceedance hours vs. Persistence continuous runs) dynamically updates the map layers and swaps the KPI card metrics to their correct formats.
5. Toggling the Data source to 'FortyGuard live' with no API key loaded displays a graceful, user-friendly instruction banner instead of throwing an error.
6. A simulated API connection failure or an HTTP 429 rate limit is caught defensively, dropping the user back to the cached offline demo database with an active warning banner.

*Result:* **31 passed tests with zero exceptions** across all edge cases.

## Data Sources & Caching Pipeline
The app features an explicit **Data source** toggle in the sidebar:

### 1. Demo mode (Default — Zero API Credits)
Runs entirely on locally simulated data shaped like a real FortyGuard Heatmap response (matching the hourly structure and the exceedance/persistence mathematical models). The sample-data functions serve as a persistent fallback so the app remains instantly functional for anyone, even without an active API key configured.

### 2. Live FortyGuard data (Explicit, Cached, Credit-Safe)
Switching the toggle to *FortyGuard live* opens premium integration options:
1. **File-Based Cache First:** Before calling any endpoints, the application checks your local cache directory at `data/<area>_<date>_<analytic>.json`.
2. **Explicit "Opt-In" Fetch Button:** If no cache exists, a **Fetch live data** button appears. Clicking this button is the ONLY way a real, credit-billing API transaction ever fires.
3. **Preventing Reruns:** Because Streamlit natively reruns the entire script on every user interaction, our client caching logic prevents costly duplicate API requests.
4. **Git Protection:** The `.env` variables and generated `data/` caches are strictly `.gitignored` to prevent credential leaks or API credit hijacking.

*Setup:* Copy `.env.example` to `.env` and set `FORTYGUARD_API_KEY`.

## How It Works
1. **`generate_sample_heatmap()`** — Generates realistic Phoenix diurnal temperature curves peaking in the mid-afternoon, adjusting for localized "shade factors" (e.g., cooling down vegetated parks relative to dense asphalt centers).
2. **`compute_exceedance()`** — Tallies cumulative exposure hours above user-defined threshold limits and calculates the longest unbroken "hot streak" (Persistence).
3. **`risk_label()`** — Applies a rule-based heat hazard index, sorting current risks into Green (Safe), Yellow (Moderate), Orange (High), or Red (Extreme) categories.
4. **`best_time_window()`** — Evaluates the diurnal curve to locate the longest contiguous block of safe travel hours and recommends it to the user.
5. **`fortyguard_client.py`** — Wraps the premium asynchronous endpoints:
   * Submits a `POST /v1/heatmap` containing the GeoJSON polygon boundary, date, and threshold target.
   * Securely polls `GET /v1/status/<activity_id>` every 5 seconds until the job returns a status of `Completed`.
   * Integrates defensive try/except loops to catch network anomalies and switch seamlessly to offline datasets.

## 📡 Example FortyGuard API Interaction

Below is a request/response payload showing how the Smart Commute Advisor communicates with FortyGuard's Large Temperature Models (LTMs) to pull precise 2-meter ambient temperature data.

> **Submission proof:** This is a real captured response from `data/downtown_phoenix_2026-07-15_exceedance_thr100F.json`, included here (with the AOI coordinates and stats intact) as required submission proof.

### 1. Request (POST /v1/heatmap)
Sent to generate 100-meter exceedance analytics over our Downtown Phoenix Area of Interest (AOI). This is the exact body produced by `build_heatmap_payload()` for the cached area/date/analytic (100°F threshold converted to 37.78°C):

```http
POST https://api.fortyguard.com/v1/heatmap
api-key: <FORTYGUARD_API_KEY>
Content-Type: application/json
```

```json
{
  "polygon_aoi": {
    "type": "FeatureCollection",
    "features": [
      {
        "type": "Feature",
        "properties": {},
        "geometry": {
          "type": "Polygon",
          "coordinates": [
            [
              [-112.0821, 33.4417],
              [-112.0821, 33.4551],
              [-112.0659, 33.4551],
              [-112.0659, 33.4417],
              [-112.0821, 33.4417]
            ]
          ]
        }
      }
    ]
  },
  "date_time": {
    "start_date": "2026-07-15",
    "filter_type": 3
  },
  "granularity": 100,
  "analytic_type": "exceedance",
  "threshold": 37.78,
  "direction": "above"
}
```

### 2. Polling Sequence (GET /v1/status/{activity_id})
The API returns an instant tracking ticket, polled by our client every 5 seconds until processed:

```http
GET https://api.fortyguard.com/v1/status/activity_heatmap_exceedance_phoenix_999a8b7c
api-key: <FORTYGUARD_API_KEY>
```

### 3. Response (200 OK — Completed)
The completed result is saved verbatim to `data/` by the caching layer. This is the genuine stored payload:
```json
{
  "map_data": {
    "type": "FeatureCollection",
    "features": [
      {
        "properties": {
          "value": 1.2
        },
        "geometry": {
          "type": "Polygon",
          "coordinates": [
            [
              [-112.08, 33.44],
              [-112.07, 33.44],
              [-112.07, 33.45],
              [-112.08, 33.44]
            ]
          ]
        }
      },
      {
        "properties": {
          "value": 2.46
        },
        "geometry": {
          "type": "Polygon",
          "coordinates": [
            [
              [-112.07, 33.44],
              [-112.06, 33.44],
              [-112.06, 33.45],
              [-112.07, 33.44]
            ]
          ]
        }
      }
    ]
  },
  "stats_data": {
    "Exposure_stats": {
      "minimum": 1.2,
      "mean": 1.83,
      "maximum": 2.46
    }
  }
}
```

### Bonus: TCM (raw temperature) example

This is a real captured response from `data/downtown_phoenix_2026-07-15_tcm.json`, showing the `tcm` analytic type returning raw 2-meter temperature grid data instead of exposure-hour aggregates.

#### Request (POST /v1/heatmap)
The same Downtown Phoenix AOI and date requested with `analytic_type` set to `"tcm"` — note there are no `threshold`/`direction` fields, since a raw temperature snapshot doesn't use a threshold:

```http
POST https://api.fortyguard.com/v1/heatmap
api-key: <FORTYGUARD_API_KEY>
Content-Type: application/json
```

```json
{
  "polygon_aoi": {
    "type": "FeatureCollection",
    "features": [
      {
        "type": "Feature",
        "properties": {},
        "geometry": {
          "type": "Polygon",
          "coordinates": [
            [
              [-112.0821, 33.4417],
              [-112.0821, 33.4551],
              [-112.0659, 33.4551],
              [-112.0659, 33.4417],
              [-112.0821, 33.4417]
            ]
          ]
        }
      }
    ]
  },
  "date_time": {
    "start_date": "2026-07-15",
    "filter_type": 3
  },
  "granularity": 100,
  "analytic_type": "tcm"
}
```

#### Response (200 OK — Completed)
The `tcm` response is a raw temperature grid: 165 tile features, each carrying per-tile `average_temperature`, `min_temperature`, and `max_temperature`. Below are the first 2 tiles as a representative sample — the rest are truncated for brevity:

```json
{
  "map_data": {
    "type": "FeatureCollection",
    "features": [
      {
        "id": "0",
        "type": "Feature",
        "properties": {
          "tile_id": 0,
          "average_temperature": 37.3158,
          "min_temperature": 32.8884,
          "max_temperature": 40.7798
        },
        "geometry": {
          "type": "Polygon",
          "coordinates": [
            [
              [-112.07758086419294, 33.44165867544603],
              [-112.07652196865251, 33.44166787298159],
              [-112.07653291739332, 33.442554241246675],
              [-112.07759182369813, 33.44254504340345],
              [-112.07758086419294, 33.44165867544603]
            ]
          ]
        }
      },
      {
        "id": "1",
        "type": "Feature",
        "properties": {
          "tile_id": 1,
          "average_temperature": 37.3088,
          "min_temperature": 32.8635,
          "max_temperature": 40.7884
        },
        "geometry": {
          "type": "Polygon",
          "coordinates": [
            [
              [-112.07652196865251, 33.44166787298159],
              [-112.07546307263149, 33.441677061477925],
              [-112.07547401060786, 33.44256343005037],
              [-112.07653291739332, 33.442554241246675],
              [-112.07652196865251, 33.44166787298159]
            ]
          ]
        }
      }
      // ... 163 more tiles (full response has 165 tile features)
    ]
  },
  "stats_data": {
    "temperature_stats": {
      "minimum": 37.2439,
      "maximum": 37.3173,
      "mean": 37.278861212121214,
      "standard_deviation": 0.023653934308649392
    },
    "overall_temperature_distribution": [
      37.2439,
      37.2559,
      37.2778,
      37.3016,
      37.3173
    ]
    // the full response also includes "normal_temperature_distribution" and
    // "temperature_frequency" arrays (omitted here for brevity)
  }
}
```

Note: `temperature_stats` is included verbatim above. The full response's `stats_data` also includes `normal_temperature_distribution` and `temperature_frequency` arrays (not pasted here) — the app's histogram visualization uses those to plot the temperature spread across the AOI.

Together these two examples show FortyGuard's two data shapes: instantaneous temperature snapshots (`tcm`) versus cumulative exposure-over-time metrics (`exceedance`/`persistence`) — the mathematical distinction the app's Innovation angle relies on.

## Known Limitations
Some honest caveats about what doesn't fully work yet:

* **U.S.-only API coverage:** FortyGuard API coverage is currently U.S.-only. A custom location outside the US has no real live data, so it will gracefully fall back to simulated demo values (labeled `demo*` in the app) rather than returning an error.
* **Live fetch requires a valid API key:** The *FortyGuard live* feature only returns real data when `FORTYGUARD_API_KEY` is configured in `.env`. Without a key, the app stays fully functional in demo mode but cannot fetch live analytics.
* **Six preset Phoenix locations only:** The built-in area comparison covers the 6 included Phoenix sites. Custom locations are analyzed individually, but there is no side-by-side comparison table for them.
* **Histogram-of-hourly-curve vs. tile exposure:** Best-window recommendations rely on an hourly temperature series (`tcm` analytics). Exceedance/persistence live payloads contain hour-based tile aggregates, so time-window guidance is based on tile exposure rather than a full diurnal curve.
* **Custom place names are labels only:** In "Custom location" mode, entering a place name/address is used purely as a display label — coordinates define the actual location (no offline address geocoding).

## Future Roadmap
Genuinely future items (not yet built):
* **Multi-city support:** Expand coverage beyond Phoenix to additional metros as FortyGuard API coverage grows, letting users compare safe commuting windows across cities.
* **Eye-Level Shade Analysis:** Correlate hot spots with physical tree canopies and building structures by integrating FortyGuard's premium **Satellite Segmentation** and **Street View Segmentation** APIs.
* **Historical & seasonal context:** Add comparison to historical norms or seasonal averages so recommendations are relative to what's typical, not just absolute temperature.
* **Mobile / shareable recommendations:** Generate a shareable summary card or notification so users can quickly check their safest window before leaving home.

## Live Demo
[https://smart-commute-advisor.streamlit.app/](https://smart-commute-advisor.streamlit.app/)

#### Authors:
* **Maira Naveed**
* **Khadeeja Ansari**
