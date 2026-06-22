#!/usr/bin/env python3
"""
TornadoTracker — fetch_tornadoes.py
=====================================
Dual-source GeoJSON pipeline:

  Source 1 — NOAA SPC Historical CSV  (primary, high quality)
    URL:      https://www.spc.noaa.gov/wcm/data/1950-{YEAR}_actual_tornadoes.csv
    Coverage: 1950 → prior calendar year (field-verified tracks)
    Refresh:  Annually, typically released each April by NOAA

  Source 2 — NOAA NCEI Storm Events Database  (current-year supplement)
    URL:      https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/
    Coverage: Current calendar year, updated monthly (~2-month lag)
    Refresh:  Weekly GitHub Action picks up new NCEI monthly drops

Both sources use Python stdlib only (csv, gzip, io, json, urllib.request).
No pip dependencies.

Usage:
    python scripts/fetch_tornadoes.py          # from repo root
    START_YEAR=1990 python scripts/fetch_tornadoes.py
"""

import csv
import gzip
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

# Only include tornadoes on or after this year (reduces output file size).
# Override via the START_YEAR environment variable or the workflow_dispatch input.
START_YEAR = int(os.environ.get("START_YEAR", "2000"))

# SPC: newest URL tried first — script falls back automatically each year until
# NOAA publishes the latest file (typically each April).
SPC_CSV_URLS = [
    "https://www.spc.noaa.gov/wcm/data/1950-2026_actual_tornadoes.csv",
    "https://www.spc.noaa.gov/wcm/data/1950-2025_actual_tornadoes.csv",
    "https://www.spc.noaa.gov/wcm/data/1950-2024_actual_tornadoes.csv",
    "https://www.spc.noaa.gov/wcm/data/1950-2023_actual_tornadoes.csv",
]

# NCEI Storm Events index page — script parses HTML to discover the latest
# .csv.gz file for each year it needs to supplement.
NCEI_INDEX_URL = "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"

# Regex to match NCEI detail filenames and extract year + created-date
NCEI_FILE_RE = re.compile(
    r"StormEvents_details-ftp_v1\.0_d(\d{4})_c(\d{8})\.csv\.gz"
)

# Output path — repo root
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tornadoes.geojson",
)

REQUEST_HEADERS = {
    "User-Agent": (
        "TornadoTracker/2.0 "
        "(https://tornadotracker.yorkdevelops.com; "
        "github.com/cyork95/TornadoTracker)"
    )
}

# State FIPS code → abbreviation (SPC uses integer FIPS in some records)
FIPS_TO_STATE = {
    "1":"AL",  "2":"AK",  "4":"AZ",  "5":"AR",  "6":"CA",  "8":"CO",
    "9":"CT",  "10":"DE", "12":"FL", "13":"GA", "15":"HI", "16":"ID",
    "17":"IL", "18":"IN", "19":"IA", "20":"KS", "21":"KY", "22":"LA",
    "23":"ME", "24":"MD", "25":"MA", "26":"MI", "27":"MN", "28":"MS",
    "29":"MO", "30":"MT", "31":"NE", "32":"NV", "33":"NH", "34":"NJ",
    "35":"NM", "36":"NY", "37":"NC", "38":"ND", "39":"OH", "40":"OK",
    "41":"OR", "42":"PA", "44":"RI", "45":"SC", "46":"SD", "47":"TN",
    "48":"TX", "49":"UT", "50":"VT", "51":"VA", "53":"WA", "54":"WV",
    "55":"WI", "56":"WY",
}

# NCEI uses full-uppercase state names (e.g. "TEXAS") — map to abbreviation
STATE_NAME_TO_ABBREV = {
    "ALABAMA":"AL","ALASKA":"AK","ARIZONA":"AZ","ARKANSAS":"AR",
    "CALIFORNIA":"CA","COLORADO":"CO","CONNECTICUT":"CT","DELAWARE":"DE",
    "FLORIDA":"FL","GEORGIA":"GA","HAWAII":"HI","IDAHO":"ID",
    "ILLINOIS":"IL","INDIANA":"IN","IOWA":"IA","KANSAS":"KS",
    "KENTUCKY":"KY","LOUISIANA":"LA","MAINE":"ME","MARYLAND":"MD",
    "MASSACHUSETTS":"MA","MICHIGAN":"MI","MINNESOTA":"MN","MISSISSIPPI":"MS",
    "MISSOURI":"MO","MONTANA":"MT","NEBRASKA":"NE","NEVADA":"NV",
    "NEW HAMPSHIRE":"NH","NEW JERSEY":"NJ","NEW MEXICO":"NM","NEW YORK":"NY",
    "NORTH CAROLINA":"NC","NORTH DAKOTA":"ND","OHIO":"OH","OKLAHOMA":"OK",
    "OREGON":"OR","PENNSYLVANIA":"PA","RHODE ISLAND":"RI","SOUTH CAROLINA":"SC",
    "SOUTH DAKOTA":"SD","TENNESSEE":"TN","TEXAS":"TX","UTAH":"UT",
    "VERMONT":"VT","VIRGINIA":"VA","WASHINGTON":"WA","WEST VIRGINIA":"WV",
    "WISCONSIN":"WI","WYOMING":"WY",
}


# ---------------------------------------------------------------------------
# SHARED HELPERS
# ---------------------------------------------------------------------------

def fetch_url(url, binary=False):
    """Fetch a URL; return bytes if binary=True, else decoded str."""
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", errors="replace")


def safe_float(val, default=0.0):
    """Parse val as float, returning default on failure or NaN."""
    try:
        f = float(val)
        return f if f == f else default  # NaN guard
    except (ValueError, TypeError):
        return default


def safe_int(val, default=0):
    """Parse val as int via float (handles '3.0'), returning default on failure."""
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def format_time(raw):
    """Convert HHMM integer/string → 'HH:MM'."""
    try:
        t = str(int(float(raw or "0"))).zfill(4)
        return f"{t[:2]}:{t[2:]}"
    except Exception:
        return "00:00"


def build_coords(slat, slon, elat, elon):
    """
    Return [[slon, slat], [elon, elat]] GeoJSON coordinate array.
    If the end point is 0,0 (unknown) a tiny stub offset is applied so
    Leaflet renders the track as a visible segment, not a point.
    """
    start = [round(slon, 5), round(slat, 5)]
    if elat == 0.0 and elon == 0.0:
        end = [round(slon + 0.01, 5), round(slat, 5)]
    else:
        end = [round(elon, 5), round(elat, 5)]
    if start == end:
        end = [round(slon + 0.01, 5), round(slat, 5)]
    return [start, end]


# ---------------------------------------------------------------------------
# SOURCE 1 — NOAA SPC
# ---------------------------------------------------------------------------

def clamp_ef(mag_raw):
    """Map SPC mag field to 0-5 EF rating. -9 (unknown) → 0."""
    m = safe_int(mag_raw, -9)
    return max(0, min(5, m)) if m >= 0 else 0


def parse_spc_row(row):
    """Convert one SPC CSV row dict → GeoJSON Feature dict, or None to skip."""
    yr = safe_int(row.get("yr") or row.get("year"), 0)
    if yr < START_YEAR or yr == 0:
        return None

    mo = safe_int(row.get("mo") or row.get("month"), 1)
    dy = safe_int(row.get("dy") or row.get("day"),   1)

    slat = safe_float(row.get("slat"))
    slon = safe_float(row.get("slon"))
    elat = safe_float(row.get("elat"))
    elon = safe_float(row.get("elon"))

    if slat == 0.0 and slon == 0.0:
        return None  # No usable start coordinate — skip

    # Correct wrongly-positive longitudes (CONUS must be negative/west)
    if slon > 0:
        slon = -slon
    if elon > 0:
        elon = -elon

    state = (
        row.get("st")
        or FIPS_TO_STATE.get(str(safe_int(row.get("stf"), 0)))
        or "??"
    ).strip().upper()

    mag = clamp_ef(row.get("mag") or row.get("f") or row.get("ef") or "-9")
    inj  = safe_int(row.get("inj"))
    fat  = safe_int(row.get("fat"))
    wid  = safe_int(row.get("wid"))
    leng = round(safe_float(row.get("len")), 1)
    time = format_time(row.get("time") or "0")

    stn  = safe_int(row.get("stn") or row.get("om"), 0)
    loc  = f"{state} Tornado #{stn}" if stn else f"{state} Tornado"

    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": build_coords(slat, slon, elat, elon),
        },
        "properties": {
            "ef":       mag,
            "date":     f"{yr:04d}-{mo:02d}-{dy:02d}",
            "time":     time,
            "state":    state,
            "location": loc,
            "width_yd": wid,
            "len_mi":   leng,
            "injuries": inj,
            "fat":      fat,
            "source":   "SPC",
        },
    }


def fetch_spc():
    """Try each SPC URL in order. Returns (features, source_url, max_year)."""
    for url in SPC_CSV_URLS:
        print(f"  [SPC] Trying {url} …")
        try:
            raw = fetch_url(url)
            reader = csv.DictReader(io.StringIO(raw))
            features = []
            max_year = 0
            for row in reader:
                feat = parse_spc_row(row)
                if feat:
                    yr = int(feat["properties"]["date"][:4])
                    if yr > max_year:
                        max_year = yr
                    features.append(feat)
            print(f"  [SPC] ✓ {len(features):,} records (through {max_year})")
            return features, url, max_year
        except Exception as exc:
            print(f"  [SPC] ✗ Failed: {exc}")
    print("  [SPC] ERROR — all URLs failed; no SPC data.")
    return [], "", 0


# ---------------------------------------------------------------------------
# SOURCE 2 — NOAA NCEI Storm Events
# ---------------------------------------------------------------------------

def discover_ncei_file(year):
    """
    Fetch the NCEI CSV directory index and return the URL of the most recent
    StormEvents details file for the requested year, or None if not found.
    """
    try:
        html = fetch_url(NCEI_INDEX_URL)
    except Exception as exc:
        print(f"  [NCEI] ✗ Cannot fetch index: {exc}")
        return None

    best_cdate    = ""
    best_filename = ""
    for m in NCEI_FILE_RE.finditer(html):
        file_year, cdate = m.group(1), m.group(2)
        if file_year == str(year) and cdate > best_cdate:
            best_cdate    = cdate
            best_filename = m.group(0)

    if not best_filename:
        print(f"  [NCEI] No file found for year {year}")
        return None

    url = NCEI_INDEX_URL + best_filename
    print(f"  [NCEI] Found: {best_filename}  (NCEI updated {best_cdate[:4]}-{best_cdate[4:6]}-{best_cdate[6:]})")
    return url


def parse_ncei_ef(raw):
    """Parse NCEI TOR_F_SCALE string (e.g. 'EF2', 'F3', 'EFU') → int 0-5."""
    if not raw:
        return 0
    m = re.search(r"(\d)", raw.strip().upper())
    return max(0, min(5, int(m.group(1)))) if m else 0


def parse_ncei_row(row):
    """Convert one NCEI CSV row dict → GeoJSON Feature dict, or None to skip."""
    if row.get("EVENT_TYPE", "").strip().upper() != "TORNADO":
        return None

    ym     = row.get("BEGIN_YEARMONTH", "")
    dy_raw = row.get("BEGIN_DAY", "1")
    if not ym or len(ym) < 6:
        return None
    yr = int(ym[:4])
    mo = int(ym[4:6])
    dy = safe_int(dy_raw, 1)

    if yr < START_YEAR:
        return None

    slat = safe_float(row.get("BEGIN_LAT"))
    slon = safe_float(row.get("BEGIN_LON"))
    elat = safe_float(row.get("END_LAT"))
    elon = safe_float(row.get("END_LON"))

    if slat == 0.0 and slon == 0.0:
        return None  # No usable coordinate

    # Correct wrongly-positive longitudes
    if slon > 0:
        slon = -slon
    if elon > 0:
        elon = -elon

    state_name = row.get("STATE", "").strip().upper()
    state = STATE_NAME_TO_ABBREV.get(state_name, state_name[:2] if len(state_name) >= 2 else "??")

    ef  = parse_ncei_ef(row.get("TOR_F_SCALE", ""))
    inj = safe_int(row.get("INJURIES_DIRECT")) + safe_int(row.get("INJURIES_INDIRECT"))
    fat = safe_int(row.get("DEATHS_DIRECT"))   + safe_int(row.get("DEATHS_INDIRECT"))
    wid = safe_int(row.get("TOR_WIDTH"))
    leng = round(safe_float(row.get("TOR_LENGTH")), 1)

    time = format_time(row.get("BEGIN_TIME") or "0")

    raw_loc = row.get("BEGIN_LOCATION") or row.get("CZ_NAME") or ""
    loc = raw_loc.strip().title() or state

    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": build_coords(slat, slon, elat, elon),
        },
        "properties": {
            "ef":       ef,
            "date":     f"{yr:04d}-{mo:02d}-{dy:02d}",
            "time":     time,
            "state":    state,
            "location": loc,
            "width_yd": wid,
            "len_mi":   leng,
            "injuries": inj,
            "fat":      fat,
            "source":   "NCEI",
        },
    }


def fetch_ncei_year(year):
    """Fetch + decompress + parse NCEI Storm Events for one year. Returns feature list."""
    url = discover_ncei_file(year)
    if not url:
        return []
    print(f"  [NCEI] Downloading {url} …")
    try:
        raw_bytes = fetch_url(url, binary=True)
        with gzip.open(io.BytesIO(raw_bytes), "rt", encoding="utf-8", errors="replace") as gz:
            reader   = csv.DictReader(gz)
            features = [f for f in (parse_ncei_row(r) for r in reader) if f]
        print(f"  [NCEI] ✓ {len(features):,} tornado records for {year}")
        return features
    except Exception as exc:
        print(f"  [NCEI] ✗ Failed for {year}: {exc}")
        return []


def fetch_ncei(spc_max_year):
    """
    Fetch NCEI data for every year not already covered by SPC.
    spc_max_year: the last full year the SPC file contains.
    Returns (features, ncei_through_year).
    """
    current_year = datetime.now().year
    ncei_years   = list(range(spc_max_year + 1, current_year + 1))

    if not ncei_years:
        print("  [NCEI] SPC already covers current year — no supplement needed.")
        return [], spc_max_year

    print(f"  [NCEI] Supplementing years: {ncei_years}")
    all_feats = []
    for yr in ncei_years:
        all_feats.extend(fetch_ncei_year(yr))
    return all_feats, current_year


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("=" * 64)
    print("TornadoTracker — Dual-Source GeoJSON Pipeline")
    print(f"  Sources   : NOAA SPC (historical) + NOAA NCEI (current year)")
    print(f"  Start year: {START_YEAR}")
    print(f"  Output    : {OUTPUT_PATH}")
    print("=" * 64)

    # 1. Fetch SPC historical (primary source)
    print("\n[1/3] NOAA SPC Historical CSV …")
    spc_features, spc_url, spc_max_year = fetch_spc()

    # 2. Fetch NCEI supplement for years beyond SPC coverage
    print("\n[2/3] NOAA NCEI Storm Events (current-year supplement) …")
    ncei_features, ncei_through = fetch_ncei(spc_max_year)

    # 3. Merge: SPC first (better quality), NCEI appended; sort chronologically
    print("\n[3/3] Merging and writing GeoJSON …")
    all_features = spc_features + ncei_features
    all_features.sort(key=lambda f: f["properties"]["date"])

    if not all_features:
        print("ERROR — No features collected from either source. Aborting.")
        sys.exit(1)

    geojson = {
        "type": "FeatureCollection",
        "metadata": {
            "generated":       datetime.now(timezone.utc).isoformat(),
            "record_count":    len(all_features),
            "spc_source":      spc_url,
            "spc_through":     str(spc_max_year),
            "ncei_records":    len(ncei_features),
            "ncei_through":    str(ncei_through),
            "start_year":      START_YEAR,
            "update_schedule": (
                "SPC historical: refreshed each April when NOAA releases prior-year data. "
                "NCEI current year: updated weekly (NCEI publishes monthly, ~2-month lag)."
            ),
        },
        "features": all_features,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(geojson, fh, separators=(",", ":"))

    size_kb = os.path.getsize(OUTPUT_PATH) / 1024
    print(f"\n{'─' * 64}")
    print(f"  SPC records    : {len(spc_features):>8,}  (1950 – {spc_max_year})")
    print(f"  NCEI records   : {len(ncei_features):>8,}  ({spc_max_year + 1} – {ncei_through})")
    print(f"  Total records  : {len(all_features):>8,}")
    print(f"  File size      : {size_kb:>8,.0f} KB")
    print(f"  Written to     : {OUTPUT_PATH}")
    print("=" * 64)


if __name__ == "__main__":
    main()
