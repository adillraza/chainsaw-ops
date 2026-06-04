"""Fetch Ballarat weather + Victorian emergency alerts into BigQuery.

Two free public sources, no API key:
  * Open-Meteo            -> operations.weather_current + operations.weather_forecast
  * VicEmergency GeoJSON  -> operations.weather_alerts

Append-only snapshots (each run stamps ``fetched_at``). Alerts accumulate so a
30-day history builds up over time even though the live feed only carries
currently-active incidents. Run by ``flask fetch-weather`` on a 4-hourly
systemd timer (deploy/systemd/chainsaw-ops-weather.*) to catch sudden weather
or alert changes between the daily ordering runs.

Reuses the BigQuery client already initialised by purchase_orders_service.
"""
from __future__ import annotations

import math
from datetime import datetime

import requests

from app.services.purchase_orders_service import purchase_orders_service

PROJECT = "chainsawspares-385722"
BALLARAT_LAT, BALLARAT_LON = -37.5622, 143.8503

_OPEN_METEO = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,is_day"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
    "precipitation_probability_max,wind_speed_10m_max,weather_code"
    "&timezone=Australia/Sydney&forecast_days=7"
)
_VICEMERGENCY = "https://emergency.vic.gov.au/public/osom-geojson.json"

# WMO weather codes -> short label (for display later)
WEATHER_CODE_LABEL = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 80: "Light showers", 81: "Showers",
    82: "Violent showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm + hail", 99: "Thunderstorm + hail",
}

_DDL = {
    "weather_current": """
        CREATE TABLE IF NOT EXISTS `{p}.operations.weather_current` (
          fetched_at TIMESTAMP, location STRING,
          temp_c FLOAT64, apparent_c FLOAT64, precip_mm FLOAT64,
          wind_kmh FLOAT64, weather_code INT64, weather_label STRING, is_day BOOL
        ) PARTITION BY DATE(fetched_at)
    """,
    "weather_forecast": """
        CREATE TABLE IF NOT EXISTS `{p}.operations.weather_forecast` (
          fetched_at TIMESTAMP, location STRING, forecast_date DATE, day_offset INT64,
          temp_min FLOAT64, temp_max FLOAT64, precip_mm FLOAT64,
          precip_prob_max INT64, wind_max_kmh FLOAT64,
          weather_code INT64, weather_label STRING
        ) PARTITION BY DATE(fetched_at)
    """,
    "weather_alerts": """
        CREATE TABLE IF NOT EXISTS `{p}.operations.weather_alerts` (
          fetched_at TIMESTAMP, source_id STRING, feed_type STRING, source_org STRING,
          category1 STRING, category2 STRING, status STRING,
          headline STRING, action STRING, location STRING, text STRING,
          created TIMESTAMP, updated TIMESTAMP,
          latitude FLOAT64, longitude FLOAT64, distance_km FLOAT64, url STRING
        ) PARTITION BY DATE(fetched_at)
    """,
}


def _haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def _first_point(geom):
    """Recursively find the first [lon, lat] pair in any GeoJSON geometry."""
    if not isinstance(geom, dict):
        return None
    t = geom.get("type")
    if t == "Point":
        c = geom.get("coordinates")
        if isinstance(c, list) and len(c) >= 2:
            return c[1], c[0]  # lat, lon
        return None
    if t == "GeometryCollection":
        for g in geom.get("geometries", []):
            pt = _first_point(g)
            if pt:
                return pt
        return None
    # LineString / Polygon / Multi*: dig for the first numeric pair
    coords = geom.get("coordinates")

    def dig(x):
        if isinstance(x, list) and len(x) >= 2 and all(isinstance(v, (int, float)) for v in x[:2]):
            return x[1], x[0]
        if isinstance(x, list):
            for item in x:
                r = dig(item)
                if r:
                    return r
        return None

    return dig(coords)


def _alert_url(feed_type, source_id):
    if not source_id:
        return "https://emergency.vic.gov.au/respond/"
    if feed_type == "warning":
        return f"https://emergency.vic.gov.au/respond/#!/warning/{source_id}"
    if feed_type == "incident":
        return f"https://emergency.vic.gov.au/respond/#!/incident/{source_id}"
    return f"https://emergency.vic.gov.au/respond/#!/{source_id}"


def _ensure_tables(client):
    for ddl in _DDL.values():
        client.query(ddl.format(p=PROJECT)).result()


def _fetch_weather_rows(now_iso):
    r = requests.get(_OPEN_METEO.format(lat=BALLARAT_LAT, lon=BALLARAT_LON), timeout=20)
    r.raise_for_status()
    d = r.json()

    cur = d.get("current", {}) or {}
    code = cur.get("weather_code")
    current_row = {
        "fetched_at": now_iso, "location": "Ballarat",
        "temp_c": cur.get("temperature_2m"), "apparent_c": cur.get("apparent_temperature"),
        "precip_mm": cur.get("precipitation"), "wind_kmh": cur.get("wind_speed_10m"),
        "weather_code": code, "weather_label": WEATHER_CODE_LABEL.get(code),
        "is_day": bool(cur.get("is_day")),
    }

    dl = d.get("daily", {}) or {}
    dates = dl.get("time", [])
    forecast_rows = []
    for i, day in enumerate(dates):
        c = (dl.get("weather_code") or [None] * len(dates))[i]
        forecast_rows.append({
            "fetched_at": now_iso, "location": "Ballarat",
            "forecast_date": day, "day_offset": i,
            "temp_min": dl.get("temperature_2m_min", [None] * len(dates))[i],
            "temp_max": dl.get("temperature_2m_max", [None] * len(dates))[i],
            "precip_mm": dl.get("precipitation_sum", [None] * len(dates))[i],
            "precip_prob_max": dl.get("precipitation_probability_max", [None] * len(dates))[i],
            "wind_max_kmh": dl.get("wind_speed_10m_max", [None] * len(dates))[i],
            "weather_code": c, "weather_label": WEATHER_CODE_LABEL.get(c),
        })
    return current_row, forecast_rows


def _fetch_alert_rows(now_iso):
    r = requests.get(_VICEMERGENCY, timeout=30)
    r.raise_for_status()
    feats = r.json().get("features", [])
    rows = []
    for f in feats:
        p = f.get("properties", {}) or {}
        pt = _first_point(f.get("geometry", {}))
        lat, lon = (pt if pt else (None, None))
        dist = _haversine(BALLARAT_LAT, BALLARAT_LON, lat, lon) if pt else None
        sid = str(p.get("sourceId") or p.get("id") or "")
        rows.append({
            "fetched_at": now_iso,
            "source_id": sid,
            "feed_type": p.get("feedType"),
            "source_org": p.get("sourceOrg"),
            "category1": p.get("category1"),
            "category2": p.get("category2"),
            "status": p.get("status"),
            "headline": (p.get("webHeadline") or p.get("sourceTitle") or p.get("name")),
            "action": p.get("action"),
            "location": p.get("location"),
            "text": (p.get("text") or "")[:1000] or None,
            "created": p.get("created"),
            "updated": p.get("updated"),
            "latitude": lat, "longitude": lon, "distance_km": dist,
            "url": _alert_url(p.get("feedType"), sid),
        })
    return rows


def ingest_weather_and_alerts():
    """Fetch + append a fresh snapshot. Returns (success, message)."""
    client = getattr(purchase_orders_service, "client", None)
    if client is None:
        return False, "BigQuery client not initialised"
    try:
        _ensure_tables(client)
        now_iso = datetime.utcnow().isoformat()

        current_row, forecast_rows = _fetch_weather_rows(now_iso)
        alert_rows = _fetch_alert_rows(now_iso)

        errs = []
        errs += client.insert_rows_json(f"{PROJECT}.operations.weather_current", [current_row]) or []
        errs += client.insert_rows_json(f"{PROJECT}.operations.weather_forecast", forecast_rows) or []
        if alert_rows:
            errs += client.insert_rows_json(f"{PROJECT}.operations.weather_alerts", alert_rows) or []
        if errs:
            return False, f"insert errors: {errs[:3]}"

        return True, f"weather + {len(forecast_rows)}d forecast + {len(alert_rows)} alerts"
    except Exception as exc:  # noqa: BLE001
        return False, f"weather ingest failed: {exc}"
