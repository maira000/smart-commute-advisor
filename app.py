"""
Smart Commute & Outdoor Activity Advisor
------------------------------------------
FortyGuard Hackathon '26 — Track 01: Resilient Cities & Infrastructure

This app tells everyday people the safest time to walk, run, or commute
based on hyperlocal heat exposure data.

IMPORTANT: This version does NOT call the live FortyGuard API. It uses
locally-generated sample data shaped exactly like a real Heatmap response
(same fields: polygon tiles, temperature, exceedance hours) so it's safe to
run unlimited times without burning API credits.

To switch to real data later:
  1. Run notebooks/01_create_heatmap.ipynb once, save the result as JSON
     (e.g. data/phoenix_heatmap_cache.json)
  2. Replace `generate_sample_heatmap()` below with a function that loads
     that saved JSON file instead.
"""

import random
import zlib
from datetime import datetime

import altair as alt
import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from fortyguard_client import (
    extract_hourly_series,
    fetch_and_cache_area,
    has_credentials,
    load_cached,
    summarize_result,
)

# ----------------------------------------------------------------------
# PAGE CONFIG
# ----------------------------------------------------------------------
st.set_page_config(
    page_title="Smart Commute Advisor",
    page_icon=":material/thermostat:",
    layout="wide",
)

# ----------------------------------------------------------------------
# SAMPLE / CACHED DATA GENERATION
# (Replace this section with real cached FortyGuard data when ready)
# ----------------------------------------------------------------------

PHOENIX_AREAS = {
    "Downtown Phoenix":      {"lat": 33.4484, "lon": -112.0740, "shade_factor": 0.15},
    "Scottsdale Old Town":   {"lat": 33.4942, "lon": -111.9261, "shade_factor": 0.25},
    "Tempe (ASU area)":      {"lat": 33.4255, "lon": -111.9400, "shade_factor": 0.30},
    "South Mountain Park":   {"lat": 33.3350, "lon": -112.0530, "shade_factor": 0.45},
    "Encanto Park":          {"lat": 33.4270, "lon": -112.0850, "shade_factor": 0.55},
    "Camelback East":        {"lat": 33.5120, "lon": -111.9800, "shade_factor": 0.35},
}

HOURS = list(range(5, 22))  # 5 AM to 9 PM — realistic activity window

DATA_SOURCES = {
    "Demo (simulated)": "demo",
    "FortyGuard live": "live",
}

LIVE_ANALYTICS = {
    "tcm": "Temperature snapshot (°C per tile)",
    "exceedance": "Hours above threshold",
    "persistence": "Longest run past threshold",
}

ACTIVITIES = {
    "Walking": ":material/directions_walk:",
    "Running": ":material/directions_run:",
    "Commuting": ":material/commute:",
    "Outdoor exercise": ":material/fitness_center:",
}

SAFE_COLOR = "#16A34A"
HOT_COLOR = "#DC2626"
THRESHOLD_LINE_COLOR = "#C2410C"

# Map marker fill per risk level (matches badge colors)
RISK_HEX = {
    "green": "#16A34A",
    "yellow": "#EAB308",
    "orange": "#EA580C",
    "red": "#DC2626",
}

PHOENIX_CENTER = {"lat": 33.4350, "lon": -111.9950}


def fmt_hour(hour: int) -> str:
    """Formats an hour (0-23) as a friendly 12-hour label, e.g. '5 AM'."""
    hour = hour % 24
    suffix = "AM" if hour < 12 else "PM"
    display = ((hour + 11) % 12) + 1
    return f"{display} {suffix}"


def hourly_df_from_series(series: list[dict]) -> pd.DataFrame:
    """Converts [{'hour': h, 'temp_c': t}, ...] into the app's hourly °F frame."""
    df_live = pd.DataFrame(series)
    df_live["temp_f"] = (df_live["temp_c"] * 9.0 / 5.0 + 32.0).round(1)
    return df_live[["hour", "temp_f"]]


@st.cache_data(ttl="1h", max_entries=64)
def generate_sample_heatmap(area_name: str, date: str) -> pd.DataFrame:
    """
    Simulates a FortyGuard Heatmap response (analytic_type='tcm' across a day)
    for one area. Shaped like real output: one temperature reading per hour.

    Real equivalent: client.create_heatmap(polygon_aoi=..., filter_type=3,
                                            analytic_type='tcm', ...)
    """
    area = PHOENIX_AREAS[area_name]
    seed = zlib.crc32(f"{area_name}|{date}".encode())  # deterministic per area+date
    rng = random.Random(seed)

    base_peak_f = 108 - (area["shade_factor"] * 20)  # shadier areas run cooler
    rows = []
    for hour in HOURS:
        # Simple bell-curve-ish daily temperature pattern peaking ~3-4 PM
        distance_from_peak = abs(hour - 15.5)
        temp_f = base_peak_f - (distance_from_peak ** 1.6) * 1.3
        temp_f += rng.uniform(-1.5, 1.5)  # small natural noise
        rows.append({"hour": hour, "temp_f": round(temp_f, 1)})

    return pd.DataFrame(rows)


@st.cache_data(ttl="1h", max_entries=256)
def compute_exceedance(df: pd.DataFrame, threshold_f: float) -> dict:
    """
    Simulates FortyGuard's analytic_type='exceedance':
    counts hours above a threshold, and finds the longest unbroken hot streak
    (that part simulates analytic_type='persistence').

    Real equivalent: client.create_heatmap(..., analytic_type='exceedance',
                                            threshold=<C>, direction='above')
    """
    hot_hours = df[df["temp_f"] > threshold_f]["hour"].tolist()
    exceedance_count = len(hot_hours)

    # Longest consecutive streak (persistence)
    longest_streak = 0
    current_streak = 0
    prev_hour = None
    for h in sorted(hot_hours):
        if prev_hour is not None and h == prev_hour + 1:
            current_streak += 1
        else:
            current_streak = 1
        longest_streak = max(longest_streak, current_streak)
        prev_hour = h

    return {
        "exceedance_hours": exceedance_count,
        "longest_streak": longest_streak,
        "hot_hours": hot_hours,
    }


def risk_label(exceedance_hours: int) -> tuple:
    """Rule-based risk scoring — returns (plain-language label, badge color)."""
    if exceedance_hours == 0:
        return "Low risk", "green"
    elif exceedance_hours <= 3:
        return "Moderate risk", "yellow"
    elif exceedance_hours <= 6:
        return "High risk", "orange"
    else:
        return "Extreme risk", "red"


def best_time_window(df: pd.DataFrame, hot_hours: list):
    """
    Finds the largest contiguous safe (non-hot) block of hours.
    Returns (start_hour, end_hour_exclusive), or None if no fully safe window.
    """
    safe_df = df[~df["hour"].isin(hot_hours)]
    if safe_df.empty:
        return None

    safe_hours = sorted(safe_df["hour"].tolist())
    blocks, block = [], [safe_hours[0]]
    for h in safe_hours[1:]:
        if h == block[-1] + 1:
            block.append(h)
        else:
            blocks.append(block)
            block = [h]
    blocks.append(block)
    best_block = max(blocks, key=len)

    return best_block[0], best_block[-1] + 1


def hot_window_range(hot_hours: list):
    """Returns the overall span of risky hours as (start, end_exclusive)."""
    if not hot_hours:
        return None
    return min(hot_hours), max(hot_hours) + 1


def build_nl_summary(
    area_name: str,
    activity: str,
    threshold_f: float,
    result: dict,
    df: pd.DataFrame,
    safe_window,
    comparison_df: pd.DataFrame,
) -> str:
    """
    Composes a short plain-English recommendation from the analysis results.
    Purely rule-based — no external service, works offline, zero cost.
    """
    activity = activity.lower()
    exceedance = result["exceedance_hours"]
    peak_row = df.loc[df["temp_f"].idxmax()]
    peak_temp = float(peak_row["temp_f"])
    peak_time = fmt_hour(int(peak_row["hour"]))
    coolest_row = df.loc[df["temp_f"].idxmin()]

    # Suggest a cooler alternative if a safer area exists for this date/threshold
    alt_clause = ""
    try:
        best_row = comparison_df.iloc[0]
        if best_row["Area"] != area_name and int(best_row["Hours above threshold"]) < exceedance:
            alt_clause = f" If you have flexibility, {best_row['Area']} runs cooler today."
    except (KeyError, IndexError):
        pass

    if exceedance == 0:
        return (
            f"It's a clear day in {area_name}: temperatures stay below "
            f"{threshold_f:.0f}°F from open to close, so {activity} is comfortable "
            f"whenever it suits you. The most pleasant stretch sits around "
            f"{fmt_hour(int(coolest_row['hour']))} at roughly "
            f"{float(coolest_row['temp_f']):.0f}°F."
        )

    streak = result["longest_streak"]
    streak_note = (
        f" That heat holds unbroken for up to {streak} hrs."
        if streak >= 4
        else ""
    )

    if safe_window:
        start_h, end_h = safe_window
        return (
            f"For {activity} in {area_name}, aim for {fmt_hour(start_h)} – "
            f"{fmt_hour(end_h)}, when conditions hold under {threshold_f:.0f}°F. "
            f"From {fmt_hour(hot_window_range(result['hot_hours'])[0])} onward, "
            f"expect {exceedance} hr{'s' if exceedance != 1 else ''} above "
            f"{threshold_f:.0f}°F, peaking near {peak_temp:.0f}°F around "
            f"{peak_time}.{streak_note}{alt_clause}"
        )

    return (
        f"There's no fully safe window for {activity} in {area_name} today — "
        f"{exceedance} tracked hours run above {threshold_f:.0f}°F, topping out "
        f"near {peak_temp:.0f}°F around {peak_time}.{streak_note} Move it to "
        f"early morning, shift it indoors, or pick a cooler area.{alt_clause}"
    )


@st.cache_data(ttl="1h", max_entries=32)
def demo_comparison_table(date: str, threshold_f: float) -> pd.DataFrame:
    """Simulated-data risk summary across every area (demo mode)."""
    rows = []
    for name in PHOENIX_AREAS:
        area_df = generate_sample_heatmap(name, date)
        area_result = compute_exceedance(area_df, threshold_f)
        area_label, area_color = risk_label(area_result["exceedance_hours"])
        window = best_time_window(area_df, area_result["hot_hours"])
        window_text = f"{fmt_hour(window[0])} – {fmt_hour(window[1])}" if window else "None"
        rows.append({
            "Area": name,
            "Data": "demo",
            "Risk level": area_label,
            "_color": area_color,
            "Hours above threshold": area_result["exceedance_hours"],
            "Longest hot streak": area_result["longest_streak"],
            "Peak temp (°F)": float(area_df["temp_f"].max()),
            "Best window": window_text,
        })
    return pd.DataFrame(rows).sort_values("Hours above threshold").reset_index(drop=True)


def _area_summary_row(name: str, area_df: pd.DataFrame, provenance: str, threshold_f: float) -> dict:
    area_result = compute_exceedance(area_df, threshold_f)
    area_label, area_color = risk_label(area_result["exceedance_hours"])
    window = best_time_window(area_df, area_result["hot_hours"])
    window_text = f"{fmt_hour(window[0])} – {fmt_hour(window[1])}" if window else "None"
    return {
        "Area": name,
        "Data": provenance,
        "Risk level": area_label,
        "_color": area_color,
        "Hours above threshold": area_result["exceedance_hours"],
        "Longest hot streak": area_result["longest_streak"],
        "Peak temp (°F)": float(area_df["temp_f"].max()),
        "Best window": window_text,
    }


def build_area_summaries(date: str, threshold_f: float, use_live: bool) -> pd.DataFrame:
    """
    Per-area risk summary for the map + table.

    Demo mode: simulated curves for everything.
    Live mode: uses each area's cached FortyGuard hourly series when available;
    areas without a cache fall back to simulated values and are explicitly
    labeled 'demo*' so nothing is ever misrepresented as real data.
    """
    if not use_live:
        return demo_comparison_table(date, threshold_f)

    rows = []
    for name in PHOENIX_AREAS:
        payload = load_cached(name, date, "tcm")
        series = extract_hourly_series(payload) if payload else None
        if series is not None:
            rows.append(_area_summary_row(name, hourly_df_from_series(series), "live", threshold_f))
        else:
            rows.append(_area_summary_row(
                name, generate_sample_heatmap(name, date), "demo*", threshold_f
            ))
    return pd.DataFrame(rows).sort_values("Hours above threshold").reset_index(drop=True)


def build_risk_map(comparison_df: pd.DataFrame, selected_area: str) -> folium.Map:
    """
    Interactive Phoenix map (OpenStreetMap tiles) with one risk-colored marker
    per area. The selected area gets a highlight ring.
    """
    m = folium.Map(
        location=[PHOENIX_CENTER["lat"], PHOENIX_CENTER["lon"]],
        zoom_start=10,
        tiles="OpenStreetMap",
        control_scale=True,
    )

    points = []
    for _, row in comparison_df.iterrows():
        name = row["Area"]
        area_meta = PHOENIX_AREAS[name]
        lat, lon = area_meta["lat"], area_meta["lon"]
        points.append((lat, lon))

        hex_color = RISK_HEX.get(row["_color"], "#71717A")
        is_selected = name == selected_area

        popup_html = f"""
        <div style="font-family: Inter, sans-serif; min-width: 190px;">
          <div style="font-weight: 700; font-size: 14px; margin-bottom: 4px;">{name}</div>
          <div style="color: {hex_color}; font-weight: 600;">&#9679; {row['Risk level']}</div>
          <div style="margin-top: 6px; font-size: 12px; color: #333;">
            Hours above threshold: <b>{int(row['Hours above threshold'])}</b><br>
            Peak temp: <b>{row['Peak temp (°F)']:.0f}&deg;F</b><br>
            Best window: <b>{row['Best window']}</b><br>
            Data source: <b>{row['Data']}</b>
          </div>
        </div>
        """

        if is_selected:
            # Highlight ring around the selected area
            folium.CircleMarker(
                location=[lat, lon],
                radius=float(18 + int(row["Hours above threshold"]) * 2),
                color="#27272A",
                weight=2,
                fill=False,
                opacity=0.8,
            ).add_to(m)

        folium.CircleMarker(
            location=[lat, lon],
            radius=float(11 + int(row["Hours above threshold"]) * 1.6),
            color="#FFFFFF",
            weight=2,
            fill=True,
            fill_color=hex_color,
            fill_opacity=0.85,
            tooltip=f"{name} — {row['Risk level']}",
            popup=folium.Popup(popup_html, max_width=260),
        ).add_to(m)

    if points:
        m.fit_bounds(points, padding=(24, 24))

    return m


def daily_temperature_chart(df: pd.DataFrame, result: dict, threshold_f: float):
    """Hourly temperature bars colored by safe/hot, plus a dashed threshold line."""
    chart_df = df.copy()
    hour_order = sorted(int(h) for h in chart_df["hour"].tolist())
    chart_df["Time"] = chart_df["hour"].apply(fmt_hour)
    chart_df["Status"] = chart_df["hour"].apply(
        lambda h: "Above threshold" if h in result["hot_hours"] else "Safe"
    )

    bars = (
        alt.Chart(chart_df)
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X(
                "hour:O",
                sort=hour_order,
                title=None,
                axis=alt.Axis(
                    labelExpr=(
                        "datum.label < 12 ? datum.label + ' AM' "
                        ": datum.label == 12 ? '12 PM' "
                        ": (datum.label - 12) + ' PM'"
                    ),
                ),
            ),
            y=alt.Y("temp_f:Q", title="Temp (°F)", scale=alt.Scale(zero=False, padding=8)),
            color=alt.Color(
                "Status:N",
                scale=alt.Scale(domain=["Safe", "Above threshold"], range=[SAFE_COLOR, HOT_COLOR]),
                legend=alt.Legend(orient="top", title=None),
            ),
            tooltip=[
                alt.Tooltip("Time:N", title="Time"),
                alt.Tooltip("temp_f:Q", title="Temp (°F)", format=".1f"),
                alt.Tooltip("Status:N", title=None),
            ],
        )
    )

    rule = (
        alt.Chart(pd.DataFrame({"threshold_f": [threshold_f]}))
        .mark_rule(strokeDash=[5, 4], color=THRESHOLD_LINE_COLOR, strokeWidth=2)
        .encode(y=alt.Y("threshold_f:Q", title="Temp (°F)"))
    )

    st.altair_chart((bars + rule).resolve_scale(y="shared"))


# ----------------------------------------------------------------------
# UI — SIDEBAR CONTROLS
# ----------------------------------------------------------------------
with st.sidebar:
    st.header("Plan your outing")

    area_name = st.selectbox("Area in Phoenix, AZ", list(PHOENIX_AREAS.keys()))

    date = st.date_input("Date", value=datetime(2026, 7, 15)).isoformat()

    activity = st.segmented_control(
        "Activity",
        options=list(ACTIVITIES.keys()),
        format_func=lambda a: f"{ACTIVITIES[a]}  {a}",
        default="Walking",
    )
    activity = activity or "Walking"

    threshold_f = st.slider(
        "Heat risk threshold (°F)",
        min_value=90,
        max_value=115,
        value=100,
        step=1,
        help="Hours above this temperature count as risky exposure.",
    )

    data_source_label = st.segmented_control(
        "Data source",
        options=list(DATA_SOURCES.keys()),
        default="Demo (simulated)",
    )
    live_mode = DATA_SOURCES.get(data_source_label) == "live"

    if live_mode:
        with st.expander("Live fetch options", icon=":material/tune:"):
            live_analytic = st.selectbox(
                "Analytic type",
                options=list(LIVE_ANALYTICS.keys()),
                format_func=lambda a: LIVE_ANALYTICS[a],
                help=(
                    "tcm returns temperature tiles (best for the hourly curve). "
                    "exceedance/persistence return hour-based analytics per tile. "
                    "One live call = one cached file, only on button click."
                ),
            )
        st.caption(
            ":material/key: Reads FORTYGUARD_API_KEY from your local .env. "
            "Responses are cached under data/ and reused — a live call happens "
            "**only** when you click *Fetch live data*."
        )
        creds_ok = has_credentials()
        if not creds_ok:
            st.warning(
                "No API key found. Copy `.env.example` to `.env` and add your key "
                "(demo mode keeps working without one).",
                icon=":material/key_off:",
            )
    else:
        st.caption(
            ":material/science: Demo mode — locally simulated data shaped like a real "
            "FortyGuard Heatmap response. No live API calls, no credits used."
        )


# ----------------------------------------------------------------------
# LIVE FETCH PANEL (the ONLY place a live API call can ever start)
# ----------------------------------------------------------------------

def render_fetch_panel(area_name: str, date: str, analytic_type: str, threshold_f: float):
    """Shown in live mode when no cached file exists yet for this area/date."""
    area_meta = PHOENIX_AREAS[area_name]

    st.warning(
        f"No cached FortyGuard data for **{area_name}** on **{date}** "
        f"(analytic: `{analytic_type}`). Nothing has been fetched — your credits are safe.",
        icon=":material/cloud_off:",
    )

    if not has_credentials():
        st.error(
            "No API key configured. Copy `.env.example` to `.env`, set "
            "`FORTYGUARD_API_KEY`, then reload this page.",
            icon=":material/key_off:",
        )
        return

    with st.container(border=True):
        st.markdown(f"#### Fetch live data for {area_name}")
        st.markdown(
            f"`POST /v1/heatmap` · single-day filter (`filter_type=3`) · "
            f"`granularity=100` · analytic=`{analytic_type}`"
            + (f" · threshold={int(threshold_f)}°F ({round((threshold_f - 32) * 5 / 9, 1)}°C)" if analytic_type != "tcm" else "")
        )
        st.caption(
            "This performs exactly one credit-billed API call, saves the response to "
            "`data/`, and reuses the cache on every future rerun. The FortyGuard API "
            "supports dates from 2019-01-01 up to 12 hours into the future."
        )
        if st.button(
            "Fetch live data",
            type="primary",
            icon=":material/cloud_download:",
        ):
            with st.status("Submitting task to FortyGuard…", expanded=True) as status:
                try:
                    payload, from_cache = fetch_and_cache_area(
                        area_name=area_name,
                        lat=area_meta["lat"],
                        lon=area_meta["lon"],
                        date=date,
                        analytic_type=analytic_type,
                        threshold_f=threshold_f if analytic_type != "tcm" else None,
                        progress_callback=lambda msg: st.write(msg),
                    )
                    status.update(
                        label="Loaded from cache" if from_cache else "Fetched and cached locally",
                        state="complete",
                        expanded=False,
                    )
                    st.rerun()
                except Exception as exc:  # noqa: BLE001 — surface any failure cleanly
                    status.update(label="Fetch failed — no data saved", state="error")
                    st.error(str(exc), icon=":material/error:")


def render_live_summary(payload: dict):
    """Shown when a cached live payload has aggregates but no hourly series."""
    st.info(
        "The cached live result contains aggregate analytics but no hourly temperature "
        "series, so time-window recommendations aren't available for it. Fetch with "
        "analytic type **tcm** (single day) to get tile temperatures instead.",
        icon=":material/analytics:",
    )
    summary = summarize_result(payload)
    cols = st.columns(4)
    cols[0].metric("Tiles in AOI", summary.get("tile_count", "—"))
    cols[1].metric("Min (°C)", summary.get("min", "—"))
    cols[2].metric("Mean (°C)", summary.get("mean", "—"))
    cols[3].metric("Max (°C)", summary.get("max", "—"))
    if "median_tile_hours_above_threshold" in summary:
        st.metric(
            "Median hours above threshold (per tile)",
            f"{summary['median_tile_hours_above_threshold']:g} hrs",
        )
    with st.expander("Raw cached API response", icon=":material/data_object:"):
        st.json(payload)


# ----------------------------------------------------------------------
# DATA RESOLUTION (demo vs cached-live) + ANALYSIS
# ----------------------------------------------------------------------
if not live_mode:
    df = generate_sample_heatmap(area_name, date)
    selected_is_live = False
else:
    cached_payload = load_cached(
        area_name,
        date,
        live_analytic,
        threshold_f if live_analytic != "tcm" else None,
    )
    if cached_payload is None:
        render_fetch_panel(area_name, date, live_analytic, threshold_f)
        st.stop()

    live_series = extract_hourly_series(cached_payload)
    if live_series is None:
        render_live_summary(cached_payload)
        st.stop()

    df = hourly_df_from_series(live_series)
    selected_is_live = True

result = compute_exceedance(df, threshold_f)
risk_text, risk_color = risk_label(result["exceedance_hours"])
safe_window = best_time_window(df, result["hot_hours"])
hot_span = hot_window_range(result["hot_hours"])
comparison_df = build_area_summaries(date, threshold_f, live_mode)
day_hour_count = len(df)

# ----------------------------------------------------------------------
# UI — HEADER / BRANDING
# ----------------------------------------------------------------------
header_left, header_right = st.columns([3, 1], vertical_alignment="center")
with header_left:
    st.title(":material/thermostat: Smart Commute & Outdoor Activity Advisor")
    st.caption("Hyperlocal heat-exposure guidance for walking, running, and commuting · Powered by FortyGuard Temperature Intelligence")
with header_right:
    with st.container(horizontal=True, horizontal_alignment="right"):
        if live_mode and selected_is_live:
            st.badge("Live · cached", icon=":material/cloud_done:", color="green")
        elif live_mode:
            st.badge("Live fallback", icon=":material/science:", color="orange")
        else:
            st.badge("Demo data", icon=":material/science:", color="gray")
        st.badge("Phoenix, AZ", icon=":material/location_on:", color="orange")

# ----------------------------------------------------------------------
# UI — HERO RECOMMENDATION CARD
# ----------------------------------------------------------------------
with st.container(border=True):
    st.markdown(f"#### When to go {activity.lower()} in {area_name}")
    st.badge(risk_text, color=risk_color, icon=":material/thermostat:")

    rec_left, rec_right = st.columns(2)
    with rec_left:
        if safe_window:
            start_h, end_h = safe_window
            st.markdown(f"## :green[{fmt_hour(start_h)} – {fmt_hour(end_h)}]")
            st.caption(":material/check_circle: Safest window for outdoor activity")
        else:
            coolest = df.loc[df["temp_f"].idxmin()]
            st.markdown(f"## :red[{fmt_hour(int(coolest['hour']))}]")
            st.caption(
                f":material/warning: No fully safe window today — coolest hour shown "
                f"({coolest['temp_f']}°F)"
            )
    with rec_right:
        if hot_span:
            hot_start, hot_end = hot_span
            st.markdown(f"## :red[{fmt_hour(hot_start)} – {fmt_hour(hot_end)}]")
            st.caption(
                f":material/local_fire_department: Avoid — {len(result['hot_hours'])} hrs above {threshold_f}°F"
            )
        else:
            st.markdown(f"## :green[All day]")
            st.caption(":material/check_circle: Temperatures stay below the threshold")

if result["exceedance_hours"] > 0:
    st.warning(
        f"Heat advisory for **{area_name}**: temperatures exceed **{threshold_f}°F** between "
        f"**{fmt_hour(hot_span[0])}** and **{fmt_hour(hot_span[1])}**. Shift your "
        f"{activity.lower()} earlier or later, or pick a shadier area below.",
        icon=":material/warning:",
    )
else:
    st.success(
        f"{area_name} stays below **{threshold_f}°F** all day — any time works for your {activity.lower()}.",
        icon=":material/check_circle:",
    )

# ----------------------------------------------------------------------
# UI — NATURAL-LANGUAGE SUMMARY (rule-based, offline, zero cost)
# ----------------------------------------------------------------------
nl_summary = build_nl_summary(
    area_name, activity, threshold_f, result, df, safe_window, comparison_df
)
with st.container(border=True):
    st.markdown("**:material/quick_reference_all: In plain english**")
    st.write(nl_summary)

# ----------------------------------------------------------------------
# UI — KPI ROW
# ----------------------------------------------------------------------
peak_temp = float(df["temp_f"].max())
coolest_row = df.loc[df["temp_f"].idxmin()]

with st.container(horizontal=True):
    st.metric(
        "Peak temperature",
        f"{peak_temp:.0f}°F",
        delta=f"{peak_temp - threshold_f:+.0f}°F vs threshold",
        delta_color="inverse" if peak_temp > threshold_f else "off",
        border=True,
        chart_data=df["temp_f"].tolist(),
        chart_type="area",
    )
    st.metric(
        "Hours above threshold",
        f"{result['exceedance_hours']} hrs",
        delta=f"{day_hour_count} hr window",
        delta_color="off",
        border=True,
    )
    st.metric(
        "Longest hot streak",
        f"{result['longest_streak']} hrs",
        delta="unbroken" if result["longest_streak"] > 0 else None,
        delta_color="off",
        border=True,
    )
    st.metric(
        "Coolest hour",
        fmt_hour(int(coolest_row["hour"])),
        delta=f"{coolest_row['temp_f']:.0f}°F",
        delta_color="off",
        border=True,
    )

# ----------------------------------------------------------------------
# UI — MAP + HOURLY CHART (side by side)
# ----------------------------------------------------------------------
col_map, col_chart = st.columns([3, 2])

with col_map:
    st.subheader("Heat-risk map")
    risk_map = build_risk_map(comparison_df, area_name)
    st_folium(risk_map, height=430, use_container_width=True)
    st.markdown(
        ":green[● Low] &nbsp; :yellow[● Moderate] &nbsp; "
        ":orange[● High] &nbsp; :red[● Extreme] — larger circles = more hours above threshold. "
        "Click any marker for details."
    )

with col_chart:
    st.subheader(f"Hourly heat curve — {date}")
    daily_temperature_chart(df, result, threshold_f)
    st.caption(f"Dashed line marks your {threshold_f}°F threshold. Red bars are hours of risky exposure.")

# ----------------------------------------------------------------------
# UI — COMPARE ALL AREAS
# ----------------------------------------------------------------------
st.subheader("Compare all Phoenix areas")
if live_mode and (comparison_df["Data"] == "demo*").any():
    st.caption(
        "Sorted by safest first. Areas marked **demo*** have no cached live data yet "
        "for this date — switch to them in the sidebar and click *Fetch live data*. "
        "Their values below are simulated placeholders."
    )
else:
    st.caption("Sorted by safest first — fewer hours above your threshold means a cooler, safer area.")

st.dataframe(
    comparison_df,
    column_config={
        "_color": None,
        "Risk level": st.column_config.TextColumn(width="medium"),
        "Hours above threshold": st.column_config.ProgressColumn(
            min_value=0,
            max_value=max(1, int(comparison_df["Hours above threshold"].max())),
            format="%d hrs",
        ),
        "Longest hot streak": st.column_config.NumberColumn(format="%d hrs"),
        "Peak temp (°F)": st.column_config.NumberColumn(format="%.1f °F"),
        "Best window": st.column_config.TextColumn(width="medium"),
    },
    hide_index=True,
)

# ----------------------------------------------------------------------
# UI — FOOTER
# ----------------------------------------------------------------------
st.divider()
footer_left, footer_right = st.columns([3, 1])
with footer_left:
    st.caption(
        "Built for FortyGuard Hackathon '26 — Building the World's Temperature AI. "
        "Demo data only; not live FortyGuard API output."
    )
with footer_right:
    with st.container(horizontal=True, horizontal_alignment="right"):
        st.badge("Track 01 · Resilient Cities", color="primary")
