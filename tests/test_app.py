"""AppTest-based headless test suite for app.py.

Every test drives the real Streamlit app script through
``streamlit.testing.v1.AppTest`` and asserts it renders with zero exceptions.
Live-mode tests never touch the network: the FortyGuard cache directory is
redirected to a pytest ``tmp_path`` and ``requests.post`` is mocked.
"""

import pytest
import requests

import fortyguard_client as fg
from helpers import (
    AREAS,
    DEFAULT_AREA,
    DEFAULT_DATE,
    DEFAULT_THRESHOLD_F,
    seg_control,
    selectbox_by_label,
    set_data_source,
    set_temp_unit,
)


def assert_no_exceptions(at) -> None:
    assert not at.exception, f"App raised exceptions: {[e.value for e in at.exception]}"


def _fetch_button(at):
    for b in at.button:
        if b.label.startswith("Fetch live data"):
            return b
    raise AssertionError("'Fetch live data' button not found")


def _status_labels(at):
    return [s.label for s in at.main if getattr(s, "type", "") == "status"]


# ----------------------------------------------------------------------
# 1) Default load — demo mode renders with zero exceptions
# ----------------------------------------------------------------------
def test_default_load_no_exceptions(app):
    app.run()

    assert_no_exceptions(app)
    assert any("Smart Commute" in (t.value or "") for t in app.title)
    assert seg_control(app, key="temp_unit").value == "\u00b0F"
    assert seg_control(app, has_option="FortyGuard live").value == "Demo (simulated)"
    assert selectbox_by_label(app, "Area in Phoenix, AZ").value == DEFAULT_AREA
    assert "Peak temperature" in {m.label for m in app.metric}


def test_injected_polygon_state_safely_heals_to_square_fallback(app):
    """With no real drawing in the browser, any stale drawn-polygon state is
    cleared on the next run and the app falls back to the square AOI."""
    ring = [[-112.08, 33.44], [-112.04, 33.44], [-112.04, 33.47], [-112.08, 33.47], [-112.08, 33.44]]
    aoi = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}
        ],
    }
    app.run()
    app.session_state["drawn_polygon_aoi"] = aoi
    app.run()

    assert_no_exceptions(app)
    assert app.session_state["drawn_polygon_aoi"] is None
    assert "Peak temperature" in {m.label for m in app.metric}


# ----------------------------------------------------------------------
# 2) Temperature unit toggle (°F <-> °C) renders without breaking
# ----------------------------------------------------------------------
def test_temperature_unit_toggle_renders(app):
    app.run()

    peak = lambda: next(m for m in app.metric if m.label == "Peak temperature")
    assert peak().value.endswith("\u00b0F")

    set_temp_unit(app, "\u00b0C")
    assert_no_exceptions(app)
    assert peak().value.endswith("\u00b0C")

    set_temp_unit(app, "\u00b0F")
    assert_no_exceptions(app)
    assert peak().value.endswith("\u00b0F")


# ----------------------------------------------------------------------
# 3) All six sample areas switch without exceptions
# ----------------------------------------------------------------------
@pytest.mark.parametrize("area_name", AREAS)
def test_each_sample_area_renders(app, area_name):
    app.run()
    selectbox_by_label(app, "Area in Phoenix, AZ").set_value(area_name).run()

    assert_no_exceptions(app)
    assert any(f"in {area_name}" in (m.value or "") for m in app.markdown)


# ----------------------------------------------------------------------
# 4) Analytic types (tcm / exceedance / persistence)
# ----------------------------------------------------------------------
def test_demo_mode_renders_tcm_tile_legend_and_temp_metrics(app):
    app.run()

    assert_no_exceptions(app)
    html_text = "".join(
        getattr(getattr(e, "proto", None), "body", "") or ""
        for e in app.main
        if getattr(e, "type", "") == "html"
    )
    assert "Temperature (tiles)" in html_text

    labels = {m.label for m in app.metric}
    assert "Peak temperature" in labels
    assert not any("Exposure" in l for l in labels)


def test_demo_payload_and_tile_layer_for_each_analytic():
    """Demo-mode data for all three analytics builds renderable tile layers."""
    import folium

    import app as app_mod

    for analytic in ("tcm", "exceedance", "persistence"):
        payload = app_mod.generate_demo_payload(
            "South Mountain Park", DEFAULT_DATE, analytic, DEFAULT_THRESHOLD_F
        )
        assert payload["map_data"]["features"]

        layer = app_mod.build_tile_layer(payload, analytic)
        assert isinstance(layer, folium.FeatureGroup)
        assert len(layer._children) >= 1


# ----------------------------------------------------------------------
# 4b) Optional polygon AOI drawing (folium.plugins.Draw toolbar)
#      Additive: no polygon drawn -> square AOI fallback is untouched.
# ----------------------------------------------------------------------
def _polygon_ring(lat=33.45, lon=-112.05, half=0.02):
    return [
        [lon - half, lat - half],
        [lon + half, lat - half],
        [lon + half, lat + half],
        [lon - half, lat + half],
        [lon - half, lat - half],
    ]


def _polygon_aoi(ring=None):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [ring or _polygon_ring()]},
            }
        ],
    }


def test_drawings_to_polygon_aoi_returns_none_without_a_polygon():
    assert fg.drawings_to_polygon_aoi(None) is None
    assert fg.drawings_to_polygon_aoi([]) is None
    assert fg.drawings_to_polygon_aoi(
        [{"type": "Feature", "geometry": {"type": "Marker", "coordinates": [0, 0]}}]
    ) is None
    assert fg.drawings_to_polygon_aoi(
        [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}}]
    ) is None


def test_drawings_to_polygon_aoi_normalizes_drawn_polygon():
    ring = _polygon_ring()
    drawing = {
        "type": "Feature",
        "properties": {"_leaflet_id": 7},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }
    aoi = fg.drawings_to_polygon_aoi([drawing])

    assert aoi["type"] == "FeatureCollection"
    assert len(aoi["features"]) == 1
    assert aoi["features"][0]["properties"] == {}
    assert aoi["features"][0]["geometry"]["coordinates"] == [ring]


def test_demo_payload_keeps_square_aoi_by_default():
    import app as app_mod

    square = app_mod.generate_demo_payload(
        "Downtown Phoenix", DEFAULT_DATE, "tcm", DEFAULT_THRESHOLD_F
    )
    assert len(square["map_data"]["features"]) == 6


@pytest.mark.parametrize("analytic", ["tcm", "exceedance", "persistence"])
def test_demo_payload_clips_tiles_to_drawn_polygon(analytic):
    import app as app_mod

    ring = _polygon_ring()
    payload = app_mod.generate_demo_payload(
        "Downtown Phoenix", DEFAULT_DATE, analytic, DEFAULT_THRESHOLD_F,
        polygon_aoi=_polygon_aoi(ring),
    )
    features = payload["map_data"]["features"]
    assert features, f"expected polygon-clipped tiles for {analytic}"

    for feature in features:
        tile_ring = feature["geometry"]["coordinates"][0]
        c_lat = sum(p[1] for p in tile_ring) / len(tile_ring)
        c_lon = sum(p[0] for p in tile_ring) / len(tile_ring)
        assert fg.point_in_ring(c_lon, c_lat, ring)


def test_demo_payload_polygon_aoi_is_deterministic():
    import app as app_mod

    aoi = _polygon_aoi()
    a = app_mod.generate_demo_payload(
        "Downtown Phoenix", DEFAULT_DATE, "tcm", DEFAULT_THRESHOLD_F, polygon_aoi=aoi
    )
    b = app_mod.generate_demo_payload(
        "Downtown Phoenix", DEFAULT_DATE, "tcm", DEFAULT_THRESHOLD_F, polygon_aoi=aoi
    )
    assert a == b


def test_cache_path_distinguishes_square_and_drawn_polygon(isolated_cache):
    square = fg.cache_path("Downtown Phoenix", DEFAULT_DATE, "tcm")
    poly = fg.cache_path("Downtown Phoenix", DEFAULT_DATE, "tcm", polygon_aoi=_polygon_aoi())
    assert square != poly
    assert "poly_" in poly.name


def test_fetch_and_cache_area_uses_drawn_polygon_aoi(isolated_cache, monkeypatch):
    import app as app_mod

    aoi = _polygon_aoi()
    captured = {}

    def fake_create(polygon_aoi=None, **kwargs):
        captured["polygon"] = polygon_aoi
        return {"map_data": {"type": "FeatureCollection", "features": []}, "stats_data": {}}

    monkeypatch.setattr(fg, "create_heatmap", fake_create)

    result, from_cache = fg.fetch_and_cache_area(
        "Downtown Phoenix", 33.4484, -112.0740, DEFAULT_DATE, polygon_aoi=aoi
    )
    assert not from_cache
    assert captured["polygon"] == aoi

    payload, from_cache = fg.fetch_and_cache_area(
        "Downtown Phoenix", 33.4484, -112.0740, DEFAULT_DATE, polygon_aoi=aoi
    )
    assert from_cache  # polygon-scoped cache hit, no second create_heatmap call


def test_risk_map_includes_draw_control():
    import folium
    from folium.plugins import Draw as DrawControl

    import app as app_mod

    comparison = app_mod.demo_comparison_table(DEFAULT_DATE, DEFAULT_THRESHOLD_F)
    m = app_mod.build_risk_map(comparison, DEFAULT_AREA)
    draw_controls = [c for _, c in m._children.items() if isinstance(c, DrawControl)]
    assert len(draw_controls) == 1

    m2 = app_mod.build_risk_map(comparison, DEFAULT_AREA, draw_control=False)
    assert not any(isinstance(c, DrawControl) for _, c in m2._children.items())


@pytest.mark.parametrize(
    ("analytic", "is_exposure"),
    [("tcm", False), ("exceedance", True), ("persistence", True)],
)
def test_live_mode_analytic_switch_renders_correct_units(app, demo_caches, analytic, is_exposure):
    """Analytic switching is exposed in the UI only in live mode; each type
    must render its own metric-card units without exceptions."""
    app.run()
    set_data_source(app, "FortyGuard live")
    assert_no_exceptions(app)

    selectbox_by_label(app, "Analytic type").set_value(analytic).run()
    assert_no_exceptions(app)

    labels = {m.label for m in app.metric}
    if is_exposure:
        assert "Min Exposure" in labels
        assert "Max Exposure" in labels
    else:
        assert "Min Temp" in labels
        assert "Max Temp" in labels


# ----------------------------------------------------------------------
# 5) Live mode without an API key shows a graceful error, no crash
# ----------------------------------------------------------------------
def test_live_mode_without_api_key_is_graceful(app, no_api_key, isolated_cache):
    app.run()
    set_data_source(app, "FortyGuard live")

    assert_no_exceptions(app)
    assert any("No API key" in e.value for e in app.error)
    assert any(
        t.label == "FortyGuard API key (optional)" for t in app.sidebar.text_input
    )


# ----------------------------------------------------------------------
# 6) API failures fall back gracefully (no crash, cached data preferred)
# ----------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text
        self.ok = status_code == 200

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def test_fetch_rate_limited_429_is_graceful(app, api_key, isolated_cache, monkeypatch):
    monkeypatch.setattr(
        fg.requests, "post", lambda *a, **k: _FakeResponse(429, "rate limited")
    )

    app.run()
    set_data_source(app, "FortyGuard live")
    assert_no_exceptions(app)

    _fetch_button(app).click()
    app.run()
    assert_no_exceptions(app)
    assert any("429" in e.value for e in app.error)
    assert any("Fetch failed" in s for s in _status_labels(app))


def test_fetch_connection_error_is_graceful(app, api_key, isolated_cache, monkeypatch):
    def _boom(*a, **k):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(fg.requests, "post", _boom)

    app.run()
    set_data_source(app, "FortyGuard live")
    assert_no_exceptions(app)

    _fetch_button(app).click()
    app.run()
    assert_no_exceptions(app)
    assert any("Could not reach" in e.value for e in app.error)
    assert any("Fetch failed" in s for s in _status_labels(app))


# ----------------------------------------------------------------------
# 7) Custom location (coordinates + place name) renders without breaking,
#    and the preset-area comparison table is gracefully skipped
# ----------------------------------------------------------------------
def _radio_by_label(elements, label):
    for r in elements:
        if r.label == label:
            return r
    raise AssertionError(f"radio not found: {label!r}")


def test_live_mode_falls_back_to_cached_data_without_api_call(app, demo_caches, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network must not be touched when a cache exists")

    monkeypatch.setattr(fg.requests, "post", _boom)

    app.run()
    set_data_source(app, "FortyGuard live")

    assert_no_exceptions(app)
    assert all(b.label != "Fetch live data" for b in app.button)  # cache found
    labels = {m.label for m in app.metric}
    assert "Tiles in AOI" in labels       # rendered from the cached payload
    assert "Peak temperature" in labels   # demo fallback curve still renders


# ----------------------------------------------------------------------
# 7) Custom location (coordinates + place name) renders without breaking,
#    and the preset-area comparison table is gracefully skipped
# ----------------------------------------------------------------------
def _radio_by_label(elements, label):
    for r in elements:
        if r.label == label:
            return r
    raise AssertionError(f"radio not found: {label!r}")


def test_custom_location_coordinates_renders(app):
    app.run()
    _radio_by_label(app.sidebar.radio, "Location").set_value("Custom location").run()
    _radio_by_label(app.sidebar.radio, "Specify custom location by").set_value(
        "Coordinates"
    ).run()

    assert_no_exceptions(app)
    assert any("Custom (" in (m.value or "") for m in app.markdown)
    assert any("Area comparison is only shown" in (i.value or "") for i in app.info)
    assert not app.dataframe


def test_custom_location_place_name_renders(app):
    app.run()
    _radio_by_label(app.sidebar.radio, "Location").set_value("Custom location").run()
    _radio_by_label(app.sidebar.radio, "Specify custom location by").set_value(
        "Place name or address"
    ).run()

    text_input = next(
        t for t in app.sidebar.text_input if t.label == "Place name or address"
    )
    text_input.set_value("My Cool Spot").run()

    assert_no_exceptions(app)
    assert any("in My Cool Spot" in (m.value or "") for m in app.markdown)


def test_toggle_back_to_preset_restores_comparison(app):
    app.run()
    _radio_by_label(app.sidebar.radio, "Location").set_value("Custom location").run()
    assert not app.dataframe

    _radio_by_label(app.sidebar.radio, "Location").set_value("Preset area").run()
    assert_no_exceptions(app)
    assert any("Area in Phoenix, AZ" in (s.label or "") for s in app.sidebar.selectbox)
    assert app.dataframe  # comparison table is back for presets
