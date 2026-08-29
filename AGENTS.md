# Agent Guide — Smart Commute & Outdoor Activity Advisor

Reference for AI agents (and humans) working on this codebase. Read this file
before making changes so you preserve the app's credit-safety model, API
shapes, and testing conventions.

## Repository

```
smart-commute-advisor/
├── app.py                # Streamlit app: UI + analysis + rendering (single file)
├── fortyguard_client.py  # FortyGuard API wrapper: submit/poll, caching, parsing
├── tests/                # Headless pytest suite driving the real app.py via AppTest
│   ├── conftest.py       # Fixtures: api_key, no_api_key, isolated_cache, demo_caches
│   ├── helpers.py        # Shared builders (demo_payload, write_cache, widget finders)
│   └── test_app.py       # 31 tests covering demo, live, custom-location, error paths
├── data/                 # Cached live API payloads (gitignored, re-fetchable)
├── assets/               # cover_banner.svg etc.
├── .streamlit/config.toml  # Theme + fonts
├── .devcontainer/        # VS Code/Codespaces devcontainer
├── .env.example          # Copy to .env: FORTYGUARD_API_KEY
├── requirements.txt      # App runtime deps
├── requirements-dev.txt  # Adds pytest for the test suite
└── README.md             # User-facing docs (includes real captured API responses)
```

> The sibling folder `temperature-api-quickstart/` is the official FortyGuard
> quickstart repo — a reference only, not part of this app. Ignore it when
> working here.

## Commands

```bash
# Run the app
pip install -r requirements.txt
streamlit run app.py          # opens at http://localhost:8501

# Run the test suite (no browser, no network, zero API credits)
pip install -r requirements-dev.txt
pytest tests/
```

## Stack

- **Streamlit** (`>=1.35`) — UI; import aliased as `st`.
- **pandas**, **numpy** — tabular processing.
- **altair** — hourly heat-curve charts.
- **folium** + **streamlit-folium** — interactive map, polygon AOI drawing
  toolbar (`folium.plugins.Draw`).
- **requests** — live API calls.
- **python-dotenv** — loads `.env` at `fortyguard_client` import.

Internal temperature representation is always **°F**; °C conversion happens
only at display time (`f_to_c`, `fmt_temp` in `app.py`).

## Architecture & Data Flow

The app has two data modes controlled by the **Data source** segmented control:

1. **Demo (simulated)** — default. Locally-generated curves shaped like real
   FortyGuard payloads. `generate_sample_heatmap()` (hourly °F series) and
   `generate_demo_payload()` (tile GeoJSON with `map_data`/`stats_data`).
   No network, no credits.
2. **FortyGuard live** — explicit, cache-first, credit-safe:
   - Check the local JSON cache `data/<area>_<date>_<analytic>[_poly<sig>][_thrNNNF].json`.
   - A real API call happens **only** when the user clicks *Fetch live data*.
   - Responses are saved to the cache before rendering (reused on every rerun).
   - Areas without a cache fall back to simulated values labeled `demo*`.

### Main flow per rerun (`app.py`)

1. Sidebar controls build request params: area (preset/custom), date, activity,
   threshold (°F base + per-activity offset), temp unit, data source.
2. **Data resolution** (`# DATA RESOLUTION` section): pick `df` (hourly curve)
   and `tile_payload` (GeoJSON tiles) from demo or cache. No cache in live mode
   → `render_fetch_panel()` and `st.stop()`.
3. Analysis: `compute_exceedance()` (hours above threshold + longest hot
   streak), `risk_label()` (Low/Moderate/High/Extreme), `best_time_window()`,
   `hot_window_range()`.
4. Render: header/badges, hero recommendation card, natural-language summary,
   KPI metrics row, risk map with tile overlay, hourly chart, comparison table,
   footer.

### Key functions

`app.py`:
- `generate_sample_heatmap(area, date, shade_factor)` — deterministic °F hourly
  curve per area+date (seeded by `zlib.crc32`).
- `generate_demo_payload(...)` — simulated tile payload matching cache shape.
- `compute_exceedance()`, `risk_label()`, `best_time_window()`, `hot_window_range()`.
- `build_nl_summary()` — rule-based plain-English recommendation (offline, free).
- `extract_tile_value()` / `tile_value_scale()` / `tile_color()` /
  `build_tile_layer()` — render FortyGuard `map_data` tiles on the folium map.
- `build_area_summaries()` — per-area risk rows (live if cached, else `demo*`).
- `build_risk_map()` — map with risk markers, selected-area ring, Draw toolbar.
- `daily_temperature_chart()` — altair hourly bars + threshold rule.
- `render_fetch_panel()` — the ONLY place a live call can start; shows warning,
  credentials check, and the *Fetch live data* button.
- `render_live_summary()` — KPI cards when a live cache has tiles but no hourly
  series.

`fortyguard_client.py`:
- `get_api_key()` / `get_base_url()` / `has_credentials()` — env-driven creds.
- `create_heatmap(polygon_aoi, date, analytic_type, threshold_f, ..., api_key)`
  — POST `/v1/heatmap`, poll GET `/v1/status/<id>` every 5 s until Completed.
- `fetch_and_cache_area(...)` — cache-first orchestrator; calls
  `create_heatmap` only on cache miss.
- `build_heatmap_payload(...)` — request body (filter_type=3 single day,
  granularity=100, threshold in °C, direction=above).
- `load_cached` / `save_cache` / `cache_path` — JSON cache under `data/`.
- `extract_hourly_series()` / `summarize_result()` — tolerant payload parsing.
- `square_polygon_geojson()` / `extract_polygon_ring()` / `ring_bounds()` /
  `point_in_ring()` / `drawings_to_polygon_aoi()` — AOI geometry helpers.

## Credentials Model (IMPORTANT)

Two sources, resolved as `ui_field_key or env_key`:

- Environment: `FORTYGUARD_API_KEY` loaded from `.env` via `python-dotenv`.
- UI: a password field (`st.text_input`, session key `"api_key"`) in the
  sidebar shown in live mode. Session-only — never written to `.env`, never
  logged, cleared on browser refresh.

Resolution helper in `app.py`:

```python
def _ui_api_key():
    return (st.session_state.get("api_key") or "").strip()

def _effective_api_key():
    return _ui_api_key() or fg.get_api_key()
```

The app checks `_effective_api_key()` (not raw `fg.has_credentials()`) before
showing the *Fetch live data* button, and passes it explicitly:

```python
result = fetch_and_cache_area(
    ..., api_key=_effective_api_key()
)
```

Never hardcode, log, print, or commit API keys. `.env` and `data/*.json` are
gitignored — keep it that way.

## FortyGuard API Shapes

- **Request** — `POST /v1/heatmap` with header `api-key: <key>`:
  ```json
  {
    "polygon_aoi": {"type": "FeatureCollection", "features": [...]},
    "date_time": {"start_date": "2026-07-15", "filter_type": 3},
    "granularity": 100,
    "analytic_type": "tcm",
    "threshold": 37.78,
    "direction": "above"
  }
  ```
  `threshold`/`direction` only for `exceedance`/`persistence`; `tcm` omits them.
- **Response** — `{"map_data": <GeoJSON FeatureCollection>, "stats_data": {...}}`.
  - `tcm` tiles: `properties.temperature` (or `average_temperature` /
    `min_temperature` / `max_temperature`). Hourly series keys tolerated:
    `hourly_temps`, `temps_by_hour`, `timeseries`, `time_series`, `hourly`,
    `hours`.
  - `exceedance`/`persistence` tiles: `properties.value` / `value_hour` /
    `exceedance_hours` (exposure hours per tile).
  - GeoJSON coordinate order is `[lon, lat]`; folium needs `[lat, lon]`.

## Streamlit Gotchas

- **Script reruns on every interaction.** Cache-first design + `@st.cache_data`
  prevent duplicate API calls and recompute.
- **`session_state`** (JS-persisted): `temp_unit`, `threshold_f`,
  `drawn_polygon_aoi` (user-drawn AOI), `api_key` (optional live key).
- **Drawn polygon healing**: a stale `drawn_polygon_aoi` with no real browser
  drawing is cleared on the next run (see `test_injected_polygon_state_safely_heals_to_square_fallback`).
- **`st.stop()`** halts a rerun early (e.g., live-mode fetch panel) — render
  whatever you need before calling it.
- **`@st.cache_data` TTLs**: sample-generation 1h; don't cache anything tied to
  user-entered secrets (a cached key is a leaked key).

## Testing Conventions

- Suite drives the real `app.py` via `streamlit.testing.v1.AppTest` — no
  browser, no network, no credits. `assert_no_exceptions()` is the central
  invariant.
- **Fixtures** (`tests/conftest.py`):
  - `app` — `AppTest.from_file(app.py)`.
  - `api_key` / `no_api_key` — set/remove `FORTYGUARD_API_KEY` env.
  - `isolated_cache` — redirects `fg.CACHE_DIR` to a pytest `tmp_path` so tests
    never touch `data/*.json`.
  - `demo_caches` — pre-writes demo payload caches for all 3 analytic types.
- **Helpers** (`tests/helpers.py`): `seg_control`, `selectbox_by_label`,
  `set_temp_unit`, `set_data_source`, `demo_payload`, `write_cache`.
- Widget patterns: set a value then `.run()`; buttons via `.click()` then a
  second `app.run()`; find sidebar widgets on `app.sidebar.*`.
- New tests: prefer `isolated_cache` + monkeypatching `fg.create_heatmap` /
  `fg.requests.post` over any real network. When asserting what key was used,
  capture the `api_key` kwarg on `fg.create_heatmap` (app.py binds
  `fetch_and_cache_area` at import, so patch beneath it, e.g. `fg.create_heatmap`).

## Conventions

- No external services for analysis — everything rule-based and offline.
- Tolerate multiple plausible payload shapes (`extract_hourly_series`,
  `summarize_result`); never crash on a missing tile.
- Keep the 6 preset Phoenix areas in `PHOENIX_AREAS` in sync with any UI copy.
- Maintain the gitignore (`.env`, `data/*.json`). Do not commit cache files.
- Follow existing docstring style; this codebase documents function purposes
  and "real equivalent" API endpoints.