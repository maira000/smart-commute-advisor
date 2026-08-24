# Smart Commute & Outdoor Activity Advisor

A Streamlit app that recommends the safest time to walk, run, or commute in Phoenix, AZ based on heat exposure — built for FortyGuard Hackathon '26.

## How to Run It

```bash
pip install -r requirements.txt
streamlit run app.py
```

It'll open automatically in your browser at `http://localhost:8501`.

## Data Sources

The app has a **Data source** toggle in the sidebar:

### 1. Demo mode (default — zero API credits)
Runs entirely on locally simulated data shaped like a real FortyGuard Heatmap
response (same hourly structure, same exceedance/persistence logic). Test it
unlimited times. The sample-data functions are always kept as a fallback so the
app works instantly even with no API key configured.

### 2. Live FortyGuard data (explicit, cached, credit-safe)
Switch the toggle to *FortyGuard live*. Nothing is fetched automatically:

1. The app first checks the local cache at `data/<area>_<date>_<analytic>.json`
2. If there's no cache yet, a **Fetch live data** button appears — clicking it
   is the ONLY way a real API call ever happens (one credit-billed call per
   area/date/analytic, saved to `data/` and reused forever after)
3. Streamlit reruns the whole script on every UI interaction, which is why the
   file-based cache exists — reruns never trigger live calls

**Setup:** copy `.env.example` to `.env` and set `FORTYGUARD_API_KEY` (never
hardcode keys or commit `.env`).

## How It Works
1. **`generate_sample_heatmap()`** — creates a realistic hourly temperature curve for a chosen area, peaking mid-afternoon (mimics real Phoenix heat patterns). Each area has a "shade factor" so parks/tree-lined areas run cooler than downtown.
2. **`compute_exceedance()`** — counts how many hours exceed your chosen threshold, and finds the longest unbroken hot streak — this mirrors FortyGuard's real `analytic_type='exceedance'` and `'persistence'` options.
3. **`risk_label()`** — simple rule-based scoring (no ML needed) that turns exceedance hours into a plain risk label (green/yellow/orange/red).
4. **`best_time_window()`** — finds the largest safe (non-hot) contiguous block of hours and recommends it.
5. **`fortyguard_client.py`** — wraps the real API following the official quickstart pattern:
   - `POST /v1/heatmap` with `polygon_aoi` (GeoJSON), `date_time`
     (`filter_type=3` = full day), `analytic_type`, `threshold` (°C, converted
     from °F), `direction='above'`, `granularity=100`
   - receives an `activity_id`, then polls `GET /v1/status/<activity_id>` every
     5 s until status is `Completed` (or `Failed`)
   - auth is a single header (`api-key: <key>`), loaded from `.env` via
     python-dotenv
   - tolerant result parsing: hourly tile series → full recommendations;
     aggregate-only payloads → stats summary + raw JSON view

## Project Structure
```
smart_commute_advisor/
├── app.py                  # Main Streamlit app
├── fortyguard_client.py    # Real API wrapper (submit → poll → cache)
├── requirements.txt        # Python dependencies
├── .env.example            # Template for FORTYGUARD_API_KEY
├── .streamlit/config.toml  # Theme (heat-safety palette)
└── data/                   # Cached live API responses (gitignored)
```

## Notes / Next Steps
- Currently covers 6 sample Phoenix-area locations (~1.5 km square AOI each)
- Threshold is adjustable via slider (90–115°F); converted to °C automatically for live calls
- Could extend with: polygon drawing for custom AOIs, tree-cover/shade data from Satellite Segmentation, or an LLM-generated natural-language summary layer on top of the existing logic

## Live Demo
https://smart-commute-advisor-8eeztc72ervmwezyp8b2vh.streamlit.app/
