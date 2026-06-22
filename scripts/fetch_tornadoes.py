#!/usr/bin/env python3
"""
fetch_tornadoes.py
==================
Fetches the NOAA Storm Prediction Center (SPC) tornado database CSV,
converts each record into a GeoJSON LineString feature, and writes the
result to tornadoes.geojson in the repo root.

Run by GitHub Actions on a weekly schedule (see .github/workflows/fetch-tornado-data.yml).
Can also be run locally:
    python scripts/fetch_tornadoes.py

Data source:
    https://www.spc.noaa.gov/wcm/#data  (SPC Severe Weather Database Files)

The CSV includes every tornado from 1950 to the previous complete year.
NOAA typically releases the updated CSV in the spring following each calendar year.
"""

import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from io import StringIO

# ---------------------------------------------------------------------------
# CONFIGURATION — adjust these as needed
# ---------------------------------------------------------------------------

# Primary and fallback CSV URLs (NOAA updates the filename each year)
# The script tries each URL in order until one succeeds.
CSV_URLS = [
    "https://www.spc.noaa.gov/wcm/data/1950-2025_actual_tornadoes.csv",
    "https://www.spc.noaa.gov/wcm/data/1950-2024_actual_tornadoes.csv",
    "https://www.spc.noaa.gov/wcm/data/1950-2023_actual_tornadoes.csv",
    "https://www.spc.noaa.gov/wcm/data/1950-2022_actual_tornadoes.csv",
]

# Only include tornadoes on or after this year to keep the GeoJSON manageable.
# Set to 0 to include ALL records since 1950 (warning: very large file).
START_YEAR = 2000

# Output path (relative to repo root, which is the script's working directory)
OUTPUT_PATH = "tornadoes.geojson"

# State FIPS → abbreviation lookup (for records that only have stf, not st)
FIPS_TO_STATE = {
    "1":"AL","2":"AK","4":"AZ","5":"AR","6":"CA","8":"CO","9":"CT",
    "10":"DE","12":"FL","13":"GA","15":"HI","16":"ID","17":"IL","18":"IN",
    "19":"IA","20":"KS","21":"KY","22":"LA","23":"ME","24":"MD","25":"MA",
    "26":"MI","27":"MN","28":"MS","29":"MO","30":"MT","31":"NE","32":"NV",
    "33":"NH","34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND","39":"OH",
    "40":"OK","41":"OR","42":"PA","44":"RI","45":"SC","46":"SD","47":"TN",
    "48":"TX","49":"UT","50":"VT","51":"VA","53":"WA","54":"WV","55":"WI",
    "56":"WY",
}


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def fetch_csv(urls):
    """Try each URL in order; return raw text of the first that succeeds."""
    last_err = None
    for url in urls:
        try:
            print(f"  Trying: {url}")
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "TornadoTracker/1.0 (yorkdevelops.com)"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read().decode("utf-8", errors="replace")
            print(f"  OK — {len(data):,} bytes downloaded.")
            return data, url
        except Exception as e:
            print(f"  Failed ({e})")
            last_err = e
    raise RuntimeError(f"All CSV URLs failed. Last error: {last_err}")


def safe_float(val, default=0.0):
    try:
        f = float(val)
        return f if f == f else default  # NaN check
    except (ValueError, TypeError):
        return default


def safe_int(val, default=0):
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def build_linestring(slat, slon, elat, elon):
    """
    Return a GeoJSON LineString coordinate array.
    If the end point is missing (0,0) or identical to start, we synthesise a
    tiny second point so Leaflet renders it as a visible dot-line.
    """
    start = [round(slon, 5), round(slat, 5)]

    # SPC uses 0,0 for "unknown end location"
    if elat == 0.0 and elon == 0.0:
        # Offset ~0.01° east (~0.6 mi) as a stub
        end = [round(slon + 0.01, 5), round(slat, 5)]
    else:
        end = [round(elon, 5), round(elat, 5)]

    if start == end:
        end = [round(slon + 0.01, 5), round(slat, 5)]

    return [start, end]


def format_time(raw_time):
    """Convert HHMM integer string → 'HH:MM', gracefully."""
    try:
        t = str(int(raw_time)).zfill(4)
        return f"{t[:2]}:{t[2:]}"
    except Exception:
        return "00:00"


def clamp_ef(mag):
    """Map SPC mag field to 0-5 EF scale. -9 = unknown → treat as 0."""
    m = safe_int(mag, 0)
    if m < 0:
        return 0
    return min(m, 5)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("TornadoTracker — NOAA SPC CSV → GeoJSON converter")
    print(f"Start year filter : {START_YEAR if START_YEAR > 0 else 'all (1950+)'}")
    print(f"Output            : {OUTPUT_PATH}")
    print("=" * 60)

    # 1. Download CSV
    print("\n[1/3] Fetching CSV from NOAA SPC...")
    raw_csv, source_url = fetch_csv(CSV_URLS)

    # 2. Parse CSV → GeoJSON features
    print("\n[2/3] Parsing records...")
    reader = csv.DictReader(StringIO(raw_csv))
    features = []
    skipped  = 0

    for row in reader:
        yr = safe_int(row.get("yr") or row.get("year"), 0)

        # Year filter
        if START_YEAR > 0 and yr < START_YEAR:
            continue

        mo  = safe_int(row.get("mo") or row.get("month"), 1)
        dy  = safe_int(row.get("dy") or row.get("day"), 1)
        mag = clamp_ef(row.get("mag") or row.get("f") or row.get("ef") or "0")

        slat = safe_float(row.get("slat"))
        slon = safe_float(row.get("slon"))
        elat = safe_float(row.get("elat"))
        elon = safe_float(row.get("elon"))

        # Skip records with no valid start coordinate
        if slat == 0.0 and slon == 0.0:
            skipped += 1
            continue

        # SPC longitudes for CONUS are negative (west) — correct if positive
        if slon > 0:
            slon = -slon
        if elon > 0 and elon != 0.0:
            elon = -elon

        # State abbreviation
        state = (
            row.get("st")
            or FIPS_TO_STATE.get(str(safe_int(row.get("stf"), 0)))
            or "??"
        ).strip().upper()

        inj  = safe_int(row.get("inj"))
        fat  = safe_int(row.get("fat"))
        wid  = safe_int(row.get("wid"))         # yards
        leng = round(safe_float(row.get("len")), 1)  # miles
        time = format_time(row.get("time") or "0")

        date_str = f"{yr:04d}-{mo:02d}-{dy:02d}"

        # Build location label from state + tornado number
        stn      = safe_int(row.get("stn") or row.get("om"), 0)
        location = f"{state} Tornado #{stn}" if stn else f"{state} Tornado"

        coords = build_linestring(slat, slon, elat, elon)

        feature = {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coords,
            },
            "properties": {
                "ef":       mag,
                "date":     date_str,
                "time":     time,
                "state":    state,
                "location": location,
                "width_yd": wid,
                "len_mi":   leng,
                "injuries": inj,
                "fat":      fat,
            },
        }
        features.append(feature)

    print(f"  Parsed  : {len(features):,} features")
    print(f"  Skipped : {skipped:,} (no coordinates)")

    # 3. Write GeoJSON
    print(f"\n[3/3] Writing {OUTPUT_PATH}...")
    geojson = {
        "type": "FeatureCollection",
        "metadata": {
            "generated":   datetime.now(timezone.utc).isoformat(),
            "source":      source_url,
            "start_year":  START_YEAR,
            "record_count": len(features),
        },
        "features": features,
    }

    # Write to the repo root (script is called from root by GitHub Actions)
    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), OUTPUT_PATH)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, separators=(",", ":"))   # compact JSON

    size_kb = os.path.getsize(out_path) / 1024
    print(f"  Done — {size_kb:,.0f} KB written to {out_path}")
    print("\nAll done! Commit tornadoes.geojson to deploy updated data.")


if __name__ == "__main__":
    main()
