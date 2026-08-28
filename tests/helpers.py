"""Shared helpers for the AppTest suite (no pytest fixtures here)."""

import fortyguard_client as fg

AREAS = (
    "Downtown Phoenix",
    "Scottsdale Old Town",
    "Tempe (ASU area)",
    "South Mountain Park",
    "Encanto Park",
    "Camelback East",
)

DEFAULT_AREA = "Downtown Phoenix"
DEFAULT_DATE = "2026-07-15"
DEFAULT_THRESHOLD_F = 100.0


def seg_control(at, key=None, has_option=None):
    for s in at.sidebar.segmented_control:
        if key is not None and s.key == key:
            return s
        if has_option is not None and s.options and has_option in s.options:
            return s
    raise AssertionError(
        f"segmented control not found (key={key!r}, has_option={has_option!r})"
    )


def selectbox_by_label(at, label):
    for s in at.sidebar.selectbox:
        if s.label == label:
            return s
    raise AssertionError(f"selectbox not found: {label!r}")


def set_temp_unit(at, unit):
    seg_control(at, key="temp_unit").set_value(unit).run()


def set_data_source(at, label):
    seg_control(at, has_option=label).set_value(label).run()


def demo_payload(analytic_type="tcm", lat=33.4484, lon=-112.0740):
    """
    Builds a small FortyGuard-shaped cache payload (mirrors the shape the app
    generates for demo tiles) so live-mode tests can render without a network.
    """
    half_lat, half_lon = 0.012, 0.014
    features, values = [], []
    cells = ((0, 0), (1, 0), (0, 1), (1, 1), (2, 1), (1, 2))
    for i, j in cells:
        value = (
            float(5 + i + j)
            if analytic_type != "tcm"
            else float(36.0 + i * 0.5 + j * 0.3)
        )
        values.append(value)
        lat0 = lat + i * half_lat
        lon0 = lon + j * half_lon
        ring = [
            [lon0, lat0],
            [lon0 + half_lon, lat0],
            [lon0 + half_lon, lat0 + half_lat],
            [lon0, lat0 + half_lat],
            [lon0, lat0],
        ]
        props = (
            {"value": value}
            if analytic_type != "tcm"
            else {"temperature": round(value, 2)}
        )
        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )

    stats_key = "Exposure_stats" if analytic_type != "tcm" else "Temperature_stats"
    stats = {
        stats_key: {
            "Minimum": min(values),
            "Maximum": max(values),
            "Mean": round(sum(values) / len(values), 2),
        }
    }
    return {
        "map_data": {"type": "FeatureCollection", "features": features},
        "stats_data": stats,
    }


def write_cache(payload, area_name, date, analytic_type, threshold_f=None):
    fg.save_cache(payload, area_name, date, analytic_type, threshold_f)