"""METAR historical analysis tool.

Pulls METAR history from ogimet into a local SQLite store, then serves a
small Flask web app that aggregates per-hour-of-day statistics in
Pacific Time for any airport, runway heading, and date window.

CLI:
    python metar.py fetch KL08 --start 2024-01-01 [--end 2026-04-30]
    python metar.py serve [--host 127.0.0.1 --port 5050]
    python metar.py info  KL08
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev, stdev
from typing import Iterable, Iterator, Optional
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, redirect, render_template, request, stream_with_context


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "metar.db"
VERSION_PATH = ROOT / "VERSION"
GIT_HASH_PATH = ROOT / "BUILD_GIT_HASH"
RECAPTCHA_PATH = ROOT / "recaptcha"
SITE_URL = "https://windcheck.net"
PACIFIC = ZoneInfo("America/Los_Angeles")
# Zone used to bucket hours, anchor date windows and label times for the
# current request (?tz=IANA name). Pacific when absent or unknown.
_LOCAL_TZ: ContextVar[ZoneInfo] = ContextVar("local_tz", default=PACIFIC)


def local_tz() -> ZoneInfo:
    return _LOCAL_TZ.get()


@lru_cache(maxsize=1)
def _tz_names() -> frozenset:
    return frozenset(available_timezones())


def parse_tz(name: Optional[str]) -> ZoneInfo:
    return ZoneInfo(name) if name in _tz_names() else PACIFIC
UTC = timezone.utc

OGIMET_URL = "https://www.ogimet.com/display_metars2.php"
OGIMET_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.ogimet.com/metars.phtml.en",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
}

# Be polite to ogimet between chunked requests.
OGIMET_REQUEST_GAP_SEC = 1.5
OGIMET_CHUNK_DAYS = 30

IASTATE_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
IASTATE_HEADERS = {"User-Agent": "metar-history-tool (personal/local-use)"}
IASTATE_REQUEST_GAP_SEC = 6.0  # iastate aggressively rate-limits

DENSITY_ALTITUDE_WARN_FT = 8000

AWC_TAF_URL = "https://aviationweather.gov/api/data/taf"
NWS_POINTS_URL = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
LAMP_TEXT_URL = "https://lamp.mdl.nws.noaa.gov/lamp/Data/bull/lavlamp38h.txt"
FORECAST_HEADERS = {
    "User-Agent": "windcheck/forecast-research (personal flight-planning tool)",
    "Accept": "application/json,text/plain,*/*",
}
RECAPTCHA_VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
RECAPTCHA_MIN_SCORE = float(os.environ.get(
    "WINDCHECK_RECAPTCHA_MIN_SCORE", "0.1"))

MPS_TO_KT = 1.9438444924
KMH_TO_KT = 0.5399568035
M_TO_FT = 3.280839895
M_TO_SM = 0.000621371192
HPA_TO_INHG = 0.0295299830714

OURAIRPORTS_RUNWAYS_URL = (
    "https://davidmegginson.github.io/ourairports-data/runways.csv"
)
OURAIRPORTS_AIRPORTS_URL = (
    "https://davidmegginson.github.io/ourairports-data/airports.csv"
)


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS metar (
    airport         TEXT    NOT NULL,
    obs_time_utc    TEXT    NOT NULL,
    raw             TEXT,
    report_type     TEXT,
    wind_dir        INTEGER,
    is_vrb          INTEGER NOT NULL DEFAULT 0,
    wind_speed_kt   REAL,
    gust_kt         REAL,
    vis_sm          REAL,
    ceiling_ft      REAL,
    cloud_layers    TEXT,
    temp_c          INTEGER,
    dew_c           INTEGER,
    altimeter_inhg  REAL,
    PRIMARY KEY (airport, obs_time_utc)
);

CREATE INDEX IF NOT EXISTS idx_metar_airport      ON metar(airport);
CREATE INDEX IF NOT EXISTS idx_metar_airport_time ON metar(airport, obs_time_utc);

CREATE TABLE IF NOT EXISTS fetched_months (
    airport     TEXT    NOT NULL,
    year        INTEGER NOT NULL,
    month       INTEGER NOT NULL,
    fetched_at  TEXT    NOT NULL,
    source      TEXT,
    PRIMARY KEY (airport, year, month)
);

CREATE TABLE IF NOT EXISTS runway_ends (
    airport     TEXT    NOT NULL,
    ident       TEXT    NOT NULL,
    heading_deg REAL,
    length_ft   INTEGER,
    surface     TEXT,
    closed      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (airport, ident)
);

CREATE INDEX IF NOT EXISTS idx_runway_ends_airport ON runway_ends(airport);

CREATE TABLE IF NOT EXISTS airports (
    ident             TEXT    PRIMARY KEY,
    name              TEXT,
    type              TEXT,
    lat               REAL,
    lon               REAL,
    elev_ft           INTEGER,
    iata_code         TEXT,
    municipality      TEXT,
    iso_country       TEXT,
    iso_region        TEXT,
    scheduled_service TEXT,
    gps_code          TEXT,
    local_code        TEXT
);
CREATE INDEX IF NOT EXISTS idx_airports_lat ON airports(lat);
CREATE INDEX IF NOT EXISTS idx_airports_lon ON airports(lon);
CREATE INDEX IF NOT EXISTS idx_airports_local ON airports(local_code);

CREATE TABLE IF NOT EXISTS meta_kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def db_init() -> None:
    with closing(db_connect()) as conn, conn:
        conn.executescript(SCHEMA)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(fetched_months)")}
        if "source" not in existing:
            conn.execute("ALTER TABLE fetched_months ADD COLUMN source TEXT")
        # One-time: migrate legacy 3-char airport keys (e.g. "L22" → "KL22")
        # in fetched_months and metar so they don't show as orphaned cache.
        if not conn.execute(
            "SELECT 1 FROM meta_kv WHERE k='legacy_3char_migrated'"
        ).fetchone():
            for tbl in ("fetched_months", "metar"):
                rows = conn.execute(
                    f"SELECT DISTINCT airport FROM {tbl}"
                ).fetchall()
                for (code,) in rows:
                    if code and len(code) == 3 and code.isalnum():
                        normalized = "K" + code.upper()
                        conn.execute(
                            f"UPDATE OR IGNORE {tbl} SET airport=? WHERE airport=?",
                            (normalized, code),
                        )
                        conn.execute(
                            f"DELETE FROM {tbl} WHERE airport=?", (code,)
                        )
            conn.execute(
                "INSERT OR REPLACE INTO meta_kv VALUES ('legacy_3char_migrated', ?)",
                (datetime.now(UTC).isoformat(),),
            )


# ----------------------------------------------------------------------
# METAR parsing
# ----------------------------------------------------------------------

@dataclass
class Observation:
    airport: str
    obs_time_utc: datetime
    raw: str
    report_type: str
    wind_dir: Optional[int]
    is_vrb: bool
    wind_speed_kt: Optional[float]
    gust_kt: Optional[float]
    vis_sm: Optional[float]
    ceiling_ft: Optional[float]
    cloud_layers: list[tuple[str, int]]
    temp_c: Optional[int]
    dew_c: Optional[int]
    altimeter_inhg: Optional[float]


WIND_RE = re.compile(r"^(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT$")
CLOUD_RE = re.compile(r"^(FEW|SCT|BKN|OVC|VV)(\d{3})$")
VIS_TOKEN_RE = re.compile(r"^(M|P)?(\d+(?:/\d+)?)SM$")
TEMP_RE = re.compile(r"^(M?\d{2})/(M?\d{2})$")
ALT_RE = re.compile(r"^A(\d{4})$")


def _parse_int_temp(s: str) -> int:
    return -int(s[1:]) if s.startswith("M") else int(s)


def _parse_vis_value(prefix: Optional[str], num: str) -> float:
    """Resolve an SM token's numeric portion (handles fractions)."""
    if "/" in num:
        a, b = num.split("/", 1)
        v = int(a) / int(b)
    else:
        v = float(num)
    if prefix == "M":
        v = v / 2.0  # "M" = less than; conservative midpoint estimate
    elif prefix == "P":
        v = v + 0.5  # "P" = greater than; small bump above the floor
    return v


def parse_metar_line(line: str) -> Optional[Observation]:
    """Parse one ogimet text line of the form '<YYYYMMDDHHMM> METAR/SPECI ...'."""
    line = line.strip()
    if not line or not line[:12].isdigit():
        return None

    date_str = line[:12]
    try:
        obs_time = datetime.strptime(date_str, "%Y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError:
        return None

    body = line[12:].strip().rstrip("=").strip()
    parts = body.split()
    if not parts:
        return None

    report_type = "METAR"
    if parts[0] in ("METAR", "SPECI"):
        report_type = parts[0]
        parts = parts[1:]
    if not parts:
        return None

    airport = parts[0]
    parts = parts[1:]

    # Drop noise tokens we never care about for stats.
    cleaned: list[str] = []
    for p in parts:
        if p in ("AUTO", "COR", "NIL"):
            continue
        cleaned.append(p)
    parts = cleaned

    wind_dir: Optional[int] = None
    is_vrb = False
    wind_speed: Optional[float] = None
    gust: Optional[float] = None
    vis: Optional[float] = None
    cloud_layers: list[tuple[str, int]] = []
    ceiling: Optional[float] = None
    temp: Optional[int] = None
    dew: Optional[int] = None
    altimeter: Optional[float] = None

    i = 0
    while i < len(parts):
        tok = parts[i]

        m = WIND_RE.match(tok)
        if m and wind_speed is None:
            d, sp, g = m.groups()
            if d == "VRB":
                is_vrb = True
                wind_dir = None
            else:
                wind_dir = int(d) % 360
            wind_speed = float(int(sp))
            gust = float(int(g)) if g else None
            i += 1
            continue

        if tok in ("CAVOK",):
            vis = 10.0
            i += 1
            continue

        # Visibility may be one token "10SM" / "1/2SM" or two tokens "1 1/2SM"
        if vis is None:
            if tok.isdigit() and i + 1 < len(parts):
                nxt = parts[i + 1]
                m2 = VIS_TOKEN_RE.match(nxt)
                if m2 and "/" in m2.group(2):
                    whole = int(tok)
                    vis = whole + _parse_vis_value(m2.group(1), m2.group(2))
                    i += 2
                    continue
            mv = VIS_TOKEN_RE.match(tok)
            if mv:
                vis = _parse_vis_value(mv.group(1), mv.group(2))
                i += 1
                continue

        mc = CLOUD_RE.match(tok)
        if mc:
            cover, hh = mc.group(1), int(mc.group(2)) * 100
            cloud_layers.append((cover, hh))
            if cover in ("BKN", "OVC", "VV"):
                if ceiling is None or hh < ceiling:
                    ceiling = float(hh)
            i += 1
            continue

        if tok in ("CLR", "SKC", "NSC", "NCD"):
            i += 1
            continue

        mt = TEMP_RE.match(tok)
        if mt and temp is None:
            try:
                temp = _parse_int_temp(mt.group(1))
                dew = _parse_int_temp(mt.group(2))
            except ValueError:
                pass
            i += 1
            continue

        ma = ALT_RE.match(tok)
        if ma and altimeter is None:
            altimeter = int(ma.group(1)) / 100.0
            i += 1
            continue

        i += 1

    return Observation(
        airport=airport,
        obs_time_utc=obs_time,
        raw=line,
        report_type=report_type,
        wind_dir=wind_dir,
        is_vrb=is_vrb,
        wind_speed_kt=wind_speed,
        gust_kt=gust,
        vis_sm=vis,
        ceiling_ft=ceiling,
        cloud_layers=cloud_layers,
        temp_c=temp,
        dew_c=dew,
        altimeter_inhg=altimeter,
    )


def parse_metar_text(text: str) -> list[Observation]:
    out: list[Observation] = []
    for line in text.splitlines():
        obs = parse_metar_line(line)
        if obs is not None:
            out.append(obs)
    return out


# ----------------------------------------------------------------------
# Ogimet fetcher
# ----------------------------------------------------------------------

def _ogimet_request(airport: str, start: datetime, end: datetime) -> Optional[str]:
    params = {
        "lang": "en", "lugar": airport, "tipo": "ALL",
        "ord": "REV", "nil": "SI", "fmt": "txt",
        "ano": start.year, "mes": start.month, "day": start.day, "hora": start.hour,
        "anof": end.year, "mesf": end.month, "dayf": end.day, "horaf": end.hour,
        "minf": "59", "send": "send",
    }
    r = requests.get(OGIMET_URL, headers=OGIMET_HEADERS, params=params, timeout=60)
    if r.status_code != 200:
        print(f"  ogimet status {r.status_code}", file=sys.stderr)
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    pre = soup.find("pre")
    if not pre:
        return None
    return pre.get_text()


def _month_iter(start: date, end: date) -> Iterator[tuple[int, int]]:
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, 0, 0, tzinfo=UTC)
    if month == 12:
        next_start = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        next_start = datetime(year, month + 1, 1, tzinfo=UTC)
    end = next_start - timedelta(minutes=1)
    return start, end


def _save_observations(conn: sqlite3.Connection, observations: Iterable[Observation]) -> int:
    rows = [
        (
            o.airport,
            o.obs_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            o.raw,
            o.report_type,
            o.wind_dir,
            1 if o.is_vrb else 0,
            o.wind_speed_kt,
            o.gust_kt,
            o.vis_sm,
            o.ceiling_ft,
            json.dumps(o.cloud_layers),
            o.temp_c,
            o.dew_c,
            o.altimeter_inhg,
        )
        for o in observations
    ]
    if not rows:
        return 0
    with conn:
        conn.executemany(
            """
            INSERT INTO metar (
                airport, obs_time_utc, raw, report_type, wind_dir, is_vrb,
                wind_speed_kt, gust_kt, vis_sm, ceiling_ft, cloud_layers,
                temp_c, dew_c, altimeter_inhg
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(airport, obs_time_utc) DO UPDATE SET
                raw=excluded.raw,
                report_type=excluded.report_type,
                wind_dir=excluded.wind_dir,
                is_vrb=excluded.is_vrb,
                wind_speed_kt=excluded.wind_speed_kt,
                gust_kt=excluded.gust_kt,
                vis_sm=excluded.vis_sm,
                ceiling_ft=excluded.ceiling_ft,
                cloud_layers=excluded.cloud_layers,
                temp_c=excluded.temp_c,
                dew_c=excluded.dew_c,
                altimeter_inhg=excluded.altimeter_inhg
            """,
            rows,
        )
    return len(rows)


def fetch_airport_iter(
    airport: str,
    start: date,
    end: date,
    *,
    refetch: bool = False,
) -> Iterator[dict]:
    """Generator yielding fetch progress events. Each event is a dict with a
    `type` key. Event shapes:

      {"type": "start",        "airport", "month_count", "start", "end"}
      {"type": "month_skip",   "year", "month", "index", "total", "reason"}
      {"type": "month_start",  "year", "month", "index", "total"}
      {"type": "source_try",   "year", "month", "source"}
      {"type": "source_result","year", "month", "source", "rows", "ok"}
      {"type": "month_done",   "year", "month", "rows", "source", "index", "total"}
      {"type": "done",         "summary", "info"}
    """
    airport = normalize_airport(airport)
    db_init()
    today = datetime.now(UTC).date()
    end = min(end, today)

    months = list(_month_iter(start, end))
    yield {
        "type": "start", "airport": airport,
        "month_count": len(months),
        "start": start.isoformat(), "end": end.isoformat(),
    }

    summary = {
        "airport": airport,
        "months_fetched": 0,
        "months_skipped": 0,
        "months_empty": 0,
        "months_with_data": 0,
        "rows_upserted": 0,
        "sources_yielded": {},   # which source produced data, by month count
        "sources_attempted": {}, # which source was tried, total attempts
    }

    with closing(db_connect()) as conn:
        already = {
            (r["year"], r["month"])
            for r in conn.execute(
                "SELECT year, month FROM fetched_months WHERE airport=?",
                (airport,),
            ).fetchall()
        }

        for i, (y, m) in enumerate(months):
            is_current = (y == today.year and m == today.month)
            if (y, m) in already and not is_current and not refetch:
                summary["months_skipped"] += 1
                yield {"type": "month_skip", "year": y, "month": m,
                       "index": i, "total": len(months), "reason": "cached"}
                continue

            ms, me = _month_bounds(y, m)
            ms = max(ms, datetime(start.year, start.month, start.day, tzinfo=UTC))
            me = min(me, datetime(end.year, end.month, end.day, 23, 59, tzinfo=UTC))

            yield {"type": "month_start", "year": y, "month": m,
                   "index": i, "total": len(months)}

            obs_list: list[Observation] = []
            source = "none"

            # Try ogimet first.
            yield {"type": "source_try", "year": y, "month": m, "source": "ogimet"}
            summary["sources_attempted"]["ogimet"] = (
                summary["sources_attempted"].get("ogimet", 0) + 1)
            text = _ogimet_request(airport, ms, me)
            if text:
                obs_list = [o for o in parse_metar_text(text)
                            if o.airport.upper() == airport]
            yield {"type": "source_result", "year": y, "month": m,
                   "source": "ogimet", "rows": len(obs_list), "ok": bool(obs_list)}
            if obs_list:
                source = "ogimet"

            # Fallback to Iowa Mesonet.
            if not obs_list:
                time.sleep(IASTATE_REQUEST_GAP_SEC)
                yield {"type": "source_try", "year": y, "month": m, "source": "iastate"}
                summary["sources_attempted"]["iastate"] = (
                    summary["sources_attempted"].get("iastate", 0) + 1)
                text2 = _iastate_request(airport, ms, me)
                if text2:
                    obs_list = [o for o in parse_metar_text(text2)
                                if o.airport.upper() == airport]
                yield {"type": "source_result", "year": y, "month": m,
                       "source": "iastate",
                       "rows": len(obs_list), "ok": bool(obs_list)}
                if obs_list:
                    source = "iastate"

            n = _save_observations(conn, obs_list) if obs_list else 0
            summary["sources_yielded"][source] = (
                summary["sources_yielded"].get(source, 0) + 1)
            summary["rows_upserted"] += n
            summary["months_fetched"] += 1
            if n == 0:
                summary["months_empty"] += 1
            else:
                summary["months_with_data"] += 1

            if not is_current:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO fetched_months "
                        "(airport, year, month, fetched_at, source) VALUES (?,?,?,?,?)",
                        (airport, y, m, datetime.now(UTC).isoformat(), source),
                    )

            yield {"type": "month_done", "year": y, "month": m,
                   "rows": n, "source": source,
                   "index": i, "total": len(months)}
            time.sleep(OGIMET_REQUEST_GAP_SEC)

    yield {"type": "done", "summary": summary, "info": airport_info(airport)}


def fetch_airport(
    airport: str,
    start: date,
    end: date,
    *,
    refetch: bool = False,
    verbose: bool = True,
) -> dict:
    """Synchronous wrapper around fetch_airport_iter (for CLI/tests)."""
    summary: dict = {}
    for ev in fetch_airport_iter(airport, start, end, refetch=refetch):
        t = ev["type"]
        if not verbose:
            pass
        elif t == "month_start":
            print(f"  fetch {ev['airport'] if 'airport' in ev else airport} "
                  f"{ev['year']}-{ev['month']:02d}")
        elif t == "month_skip":
            print(f"  skip {airport} {ev['year']}-{ev['month']:02d} ({ev['reason']})")
        elif t == "source_try":
            print(f"    trying {ev['source']} ...")
        elif t == "source_result":
            mark = "✓" if ev["ok"] else "·"
            print(f"      {mark} {ev['source']}: {ev['rows']} reports")
        elif t == "month_done":
            print(f"    saved {ev['rows']} observations [source={ev['source']}]")
        elif t == "done":
            summary = ev["summary"]
    return summary


def fetch_recent(airport: str, days: int = 60, *, verbose: bool = True) -> dict:
    """Convenience: fetch the last `days` of history for `airport`."""
    today = datetime.now(UTC).date()
    return fetch_airport(airport, today - timedelta(days=days), today,
                         refetch=False, verbose=verbose)


def normalize_airport(code: str) -> str:
    """Uppercase and prepend `K` for 3-char US-style FAA identifiers."""
    code = (code or "").strip().upper()
    if len(code) == 3 and code.isalnum():
        return "K" + code
    return code


def _iastate_request(airport: str, start: datetime, end: datetime) -> Optional[str]:
    """Fetch raw METAR text from Iowa State Mesonet. Returns ogimet-style text
    (each line: `YYYYMMDDHHMM <metar>=`) so the existing parser handles it."""
    params = {
        "station": airport, "data": "metar", "tz": "Etc/UTC",
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "format": "onlytmpf", "latlon": "no", "missing": "M", "trace": "T",
        "direct": "no", "report_type": "3", "report_type": "4",
    }
    try:
        r = requests.get(IASTATE_URL, params=params,
                         headers=IASTATE_HEADERS, timeout=60)
    except requests.RequestException as e:
        print(f"  iastate error: {e}", file=sys.stderr)
        return None
    if r.status_code == 429:
        print("  iastate 429 (rate limited) - waiting 30s and retrying once",
              file=sys.stderr)
        time.sleep(30)
        try:
            r = requests.get(IASTATE_URL, params=params,
                             headers=IASTATE_HEADERS, timeout=60)
        except requests.RequestException:
            return None
    if r.status_code != 200:
        print(f"  iastate status {r.status_code}", file=sys.stderr)
        return None

    out_lines: list[str] = []
    for ln in r.text.splitlines():
        if ln.startswith("#") or ln.startswith("station,"):
            continue
        # CSV: station,valid,metar
        # `valid` is "YYYY-MM-DD HH:MM" UTC; `metar` is the raw report.
        parts = ln.split(",", 2)
        if len(parts) != 3:
            continue
        station, valid, raw = parts
        try:
            ts = datetime.strptime(valid, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        prefix = ts.strftime("%Y%m%d%H%M")
        # raw already contains the METAR body (often starts with "METAR " or station id)
        body = raw.strip()
        if not body:
            continue
        # Make sure ogimet-style "<prefix> METAR <body>" structure holds so the parser
        # can find the report-type token. If raw lacks a "METAR"/"SPECI" tag, prepend.
        first_tok = body.split(" ", 1)[0]
        if first_tok not in ("METAR", "SPECI"):
            body = "METAR " + body
        out_lines.append(f"{prefix} {body}")
    return "\n".join(out_lines) if out_lines else ""


def ensure_runway_data(force: bool = False) -> int:
    """Download OurAirports runways.csv into `runway_ends` if not yet cached."""
    db_init()
    with closing(db_connect()) as conn:
        if not force:
            row = conn.execute("SELECT v FROM meta_kv WHERE k='runways_loaded_at'").fetchone()
            if row:
                return 0
            row = conn.execute("SELECT COUNT(*) AS n FROM runway_ends").fetchone()
            if row["n"] > 0:
                conn.execute("INSERT OR REPLACE INTO meta_kv VALUES ('runways_loaded_at', ?)",
                             (datetime.now(UTC).isoformat(),))
                conn.commit()
                return 0
    print("downloading OurAirports runways.csv ...", file=sys.stderr)
    r = requests.get(OURAIRPORTS_RUNWAYS_URL,
                     headers={"User-Agent": "metar-history-tool"}, timeout=60)
    r.raise_for_status()
    import csv as _csv
    import io as _io
    reader = _csv.DictReader(_io.StringIO(r.text))
    rows: list[tuple] = []
    for rec in reader:
        airport = (rec.get("airport_ident") or "").upper().strip()
        if not airport:
            continue
        closed = 1 if (rec.get("closed") or "0").strip() in ("1", "true") else 0
        length_ft = None
        try:
            length_ft = int(rec.get("length_ft") or "0") or None
        except ValueError:
            length_ft = None
        surface = (rec.get("surface") or "").strip() or None
        for prefix in ("le", "he"):
            ident = (rec.get(f"{prefix}_ident") or "").strip().upper()
            hdg_raw = (rec.get(f"{prefix}_heading_degT") or "").strip()
            if not ident:
                continue
            hdg: Optional[float]
            try:
                hdg = float(hdg_raw) if hdg_raw else None
            except ValueError:
                hdg = None
            rows.append((airport, ident, hdg, length_ft, surface, closed))
    with closing(db_connect()) as conn, conn:
        conn.execute("DELETE FROM runway_ends")
        conn.executemany(
            "INSERT OR REPLACE INTO runway_ends "
            "(airport, ident, heading_deg, length_ft, surface, closed) "
            "VALUES (?,?,?,?,?,?)", rows,
        )
        conn.execute("INSERT OR REPLACE INTO meta_kv VALUES ('runways_loaded_at', ?)",
                     (datetime.now(UTC).isoformat(),))
    print(f"  loaded {len(rows)} runway-end rows", file=sys.stderr)
    return len(rows)


@lru_cache(maxsize=1)
def _runway_display_data() -> dict:
    try:
        return json.loads((ROOT / "runway_headings.json").read_text())
    except (OSError, ValueError):
        return {}


def _magnetic_runway_headings(record: Optional[dict], airport: str) -> dict:
    data = _runway_display_data().get("airports", {})
    keys = [airport]
    if record and record.get("iso_country") in {"US", "AS", "GU", "MP", "PR", "VI"}:
        keys.extend([record.get("gps_code"), record.get("local_code")])
    for key in keys:
        if key in data:
            return data[key].get("runways", {})
    return {}


def get_runways(airport: str) -> list[dict]:
    """Return open runway ends for `airport` (auto-loads OurAirports data)."""
    record = get_airport_record(airport)
    airport = record["ident"] if record else normalize_airport(airport)
    magnetic = _magnetic_runway_headings(record, airport)
    ensure_runway_data()
    with closing(db_connect()) as conn:
        rows = conn.execute(
            "SELECT ident, heading_deg, length_ft, surface FROM runway_ends "
            "WHERE airport=? AND closed=0 ORDER BY heading_deg",
            (airport,),
        ).fetchall()
    return [
        {
            "ident": r["ident"],
            "heading_deg": (round(r["heading_deg"]) if r["heading_deg"] is not None else None),
            "heading_magnetic_deg": magnetic.get(r["ident"]),
            "length_ft": r["length_ft"],
            "surface": r["surface"],
        }
        for r in rows
    ]


def ensure_airport_data(force: bool = False) -> int:
    """Download OurAirports airports.csv into `airports` table."""
    db_init()
    with closing(db_connect()) as conn:
        if not force:
            row = conn.execute(
                "SELECT v FROM meta_kv WHERE k='airports_loaded_at'"
            ).fetchone()
            if row:
                return 0
            row = conn.execute("SELECT COUNT(*) AS n FROM airports").fetchone()
            if row["n"] > 0:
                conn.execute(
                    "INSERT OR REPLACE INTO meta_kv VALUES "
                    "('airports_loaded_at', ?)",
                    (datetime.now(UTC).isoformat(),),
                )
                conn.commit()
                return 0
    print("downloading OurAirports airports.csv ...", file=sys.stderr)
    r = requests.get(OURAIRPORTS_AIRPORTS_URL,
                     headers={"User-Agent": "metar-history-tool"}, timeout=60)
    r.raise_for_status()
    import csv as _csv
    import io as _io
    reader = _csv.DictReader(_io.StringIO(r.text))
    rows: list[tuple] = []

    def _maybe_float(s: str) -> Optional[float]:
        try:
            return float(s) if s else None
        except ValueError:
            return None

    def _maybe_int(s: str) -> Optional[int]:
        try:
            return int(s) if s else None
        except ValueError:
            return None

    for rec in reader:
        ident = (rec.get("ident") or "").strip().upper()
        if not ident:
            continue
        rows.append((
            ident,
            (rec.get("name") or "").strip(),
            (rec.get("type") or "").strip(),
            _maybe_float(rec.get("latitude_deg") or ""),
            _maybe_float(rec.get("longitude_deg") or ""),
            _maybe_int(rec.get("elevation_ft") or ""),
            (rec.get("iata_code") or "").strip() or None,
            (rec.get("municipality") or "").strip() or None,
            (rec.get("iso_country") or "").strip() or None,
            (rec.get("iso_region") or "").strip() or None,
            (rec.get("scheduled_service") or "").strip() or None,
            (rec.get("gps_code") or "").strip().upper() or None,
            (rec.get("local_code") or "").strip().upper() or None,
        ))
    with closing(db_connect()) as conn, conn:
        conn.execute("DELETE FROM airports")
        conn.executemany(
            "INSERT OR REPLACE INTO airports "
            "(ident, name, type, lat, lon, elev_ft, iata_code, municipality,"
            " iso_country, iso_region, scheduled_service, gps_code, local_code) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta_kv VALUES ('airports_loaded_at', ?)",
            (datetime.now(UTC).isoformat(),),
        )
    print(f"  loaded {len(rows)} airport rows", file=sys.stderr)
    return len(rows)


def get_airport_record(code: str) -> Optional[dict]:
    """Look up an airport by ident, gps_code, or local_code (any case).
    Tries the normalized key first, then falls back to the raw input."""
    ensure_airport_data()
    code_up = (code or "").strip().upper()
    if not code_up:
        return None
    candidates = [normalize_airport(code_up), code_up]
    # Cache keys prepend K to FAA identifiers, but OurAirports may retain
    # the local ident (L78, not KL78). Recover only US alphanumeric aliases.
    local_alias = (code_up[1:] if re.fullmatch(r"K[A-Z0-9]{3}", code_up)
                   and any(c.isdigit() for c in code_up[1:]) else None)
    if local_alias:
        candidates.append(local_alias)
    seen = set()
    with closing(db_connect()) as conn:
        for c in candidates:
            if c in seen:
                continue
            seen.add(c)
            country_filter = " AND iso_country='US'" if c == local_alias else ""
            for col in ("ident", "gps_code", "local_code"):
                row = conn.execute(
                    "SELECT ident, name, type, lat, lon, elev_ft, iata_code, "
                    "       municipality, iso_country, iso_region, gps_code, "
                    "       local_code "
                    f"FROM airports WHERE {col}=?{country_filter} "
                    "ORDER BY CASE type "
                    "  WHEN 'large_airport' THEN 0 "
                    "  WHEN 'medium_airport' THEN 1 "
                    "  WHEN 'small_airport' THEN 2 "
                    "  ELSE 3 END, ident",
                    (c,),
                ).fetchone()
                if row:
                    return dict(row)
    return None


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    R_NM = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return 2 * R_NM * math.asin(math.sqrt(a))


def initial_bearing_deg(lat1: float, lon1: float,
                        lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def compass_octant(deg: float) -> str:
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][
        int(((deg + 22.5) % 360) // 45)]


def find_nearest_metar(airport: str, *, max_results: int = 6,
                       max_distance_nm: float = 250.0) -> list[dict]:
    """Find the nearest airports likely to have a METAR feed.

    Heuristic for METAR-likely: 4-char ICAO starting with 'K' (US) AND
    (large/medium airport OR small_airport with scheduled service or IATA).
    Filters to a coarse lat/lon bounding box first for speed."""
    record = get_airport_record(airport)
    if not record or record["lat"] is None or record["lon"] is None:
        return []
    lat0, lon0 = record["lat"], record["lon"]
    # ~1deg lat ≈ 60 nm. Pad lon by 1/cos(lat).
    dlat = max_distance_nm / 60.0
    dlon = dlat / max(0.2, math.cos(math.radians(lat0)))
    src_ident = record["ident"]
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            SELECT ident, name, type, lat, lon, iata_code, municipality,
                   scheduled_service
            FROM airports
            WHERE ident != ?
              AND lat BETWEEN ? AND ?
              AND lon BETWEEN ? AND ?
              AND length(ident) = 4
              AND ident GLOB 'K[A-Z][A-Z0-9][A-Z0-9]'
              AND (type IN ('large_airport', 'medium_airport')
                   OR scheduled_service = 'yes'
                   OR iata_code IS NOT NULL)
            """,
            (src_ident, lat0 - dlat, lat0 + dlat, lon0 - dlon, lon0 + dlon),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = haversine_nm(lat0, lon0, r["lat"], r["lon"])
        if d > max_distance_nm:
            continue
        b = initial_bearing_deg(lat0, lon0, r["lat"], r["lon"])
        out.append({
            "ident": r["ident"],
            "name": r["name"],
            "type": r["type"],
            "iata_code": r["iata_code"],
            "municipality": r["municipality"],
            "distance_nm": round(d, 1),
            "bearing_deg": round(b, 0),
            "compass": compass_octant(b),
        })
    # Tier sort: large > medium > everything else, then distance.
    type_rank = {"large_airport": 0, "medium_airport": 1}
    out.sort(key=lambda r: (type_rank.get(r["type"], 2), r["distance_nm"]))
    return out[:max_results]


def _ang_diff(a: float, b: float) -> float:
    """Smallest absolute angle between two compass bearings, in degrees."""
    d = abs((float(a) - float(b)) % 360.0)
    return 360.0 - d if d > 180.0 else d


def pick_active_runway(runways: list[dict], wind_dir_deg: Optional[float]
                       ) -> Optional[dict]:
    """Pick the runway end whose heading is closest to the wind direction."""
    if wind_dir_deg is None:
        return None
    best, best_diff = None, 999.0
    for rw in runways:
        h = rw.get("heading_deg")
        if h is None:
            continue
        diff = _ang_diff(h, wind_dir_deg)
        if diff < best_diff:
            best, best_diff = rw, diff
    return best


def _pick_end_for_obs(usable_runways: list[dict], wind_dir: int
                      ) -> Optional[dict]:
    """For one observation, pick the runway end with the least absolute
    crosswind component. The two ends of a runway tie on crosswind, so the
    tie goes to the end facing into the wind (headwind, not tailwind).
    Caller must pre-filter `usable_runways` to entries with non-null
    heading_deg.
    """
    if not usable_runways:
        return None
    return min(usable_runways, key=lambda r: (
        round(crosswind(wind_dir, 1.0, r["heading_deg"]), 9),   # drop float noise
        -math.cos(math.radians(wind_dir - r["heading_deg"])),   # headwind first
    ))


def _utc_to_pacific_label(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        t_utc = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return s
    return t_utc.astimezone(local_tz()).strftime("%Y-%m-%d %H:%M %Z")


SOURCES_TRIED = ("ogimet", "iastate")


def airport_info(airport: str) -> dict:
    airport = normalize_airport(airport)
    db_init()
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(obs_time_utc) AS first, MAX(obs_time_utc) AS last "
            "FROM metar WHERE airport=?",
            (airport,),
        ).fetchone()
        months = conn.execute(
            "SELECT year, month, source FROM fetched_months WHERE airport=? "
            "ORDER BY year, month",
            (airport,),
        ).fetchall()
        # Months that actually contain at least one observation row.
        months_with_data_rows = conn.execute(
            "SELECT DISTINCT strftime('%Y-%m', obs_time_utc) AS ym "
            "FROM metar WHERE airport=?",
            (airport,),
        ).fetchall()
    months_with_data = {r["ym"] for r in months_with_data_rows}
    months_attempted = [(r["year"], r["month"]) for r in months]
    months_empty = [
        (r["year"], r["month"]) for r in months
        if r["source"] == "none"
        or f"{r['year']:04d}-{r['month']:02d}" not in months_with_data
    ]
    # Longest contiguous monthly block that includes the most recent
    # cached observation. Used by the "All cached" preset so it skips
    # any older orphan month that's separated from "today" by a gap.
    sorted_months = sorted(months_with_data)
    cov_from_iso = None
    cov_to_iso = None
    last_obs_pt_full = _utc_to_pacific_label(row["last"])
    if sorted_months and last_obs_pt_full:
        last_ym = last_obs_pt_full[:7]   # YYYY-MM in PT
        if last_ym not in months_with_data:
            last_ym = sorted_months[-1]
        # Walk backwards from last_ym while the previous month is
        # also present (no gap).
        def _prev_ym(ym: str) -> str:
            y, m = int(ym[:4]), int(ym[5:7])
            m -= 1
            if m == 0:
                m = 12
                y -= 1
            return f"{y:04d}-{m:02d}"
        cur = last_ym
        first_in_block = cur
        while True:
            prev = _prev_ym(first_in_block)
            if prev in months_with_data:
                first_in_block = prev
            else:
                break
        cov_from_iso = f"{first_in_block}-01"
        cov_to_iso = last_obs_pt_full[:10]
    runways = get_runways(airport)
    obs_count = row["n"] or 0
    record = get_airport_record(airport)
    info = {
        "airport": airport,
        "name": record["name"] if record else None,
        "municipality": record["municipality"] if record else None,
        "lat": record["lat"] if record else None,
        "lon": record["lon"] if record else None,
        "obs_count": obs_count,
        "first_obs_utc": row["first"],
        "last_obs_utc": row["last"],
        "first_obs_pt": _utc_to_pacific_label(row["first"]),
        "last_obs_pt": _utc_to_pacific_label(row["last"]),
        "recent_contiguous_from": cov_from_iso,
        "recent_contiguous_to": cov_to_iso,
        "months_attempted": months_attempted,
        "months_with_data": sorted_months,
        "months_empty": months_empty,
        "sources_tried": list(SOURCES_TRIED),
        "runways": runways,
        "tz": local_tz().key,
    }
    if obs_count == 0:
        info["nearest"] = find_nearest_metar(airport)
    return info


# ----------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------

def crosswind(wind_dir: int, wind_speed: float, runway_heading: int) -> float:
    """Crosswind component magnitude in knots. Caller must exclude VRB and
    missing-direction observations — those have no defined crosswind."""
    angle = abs((wind_dir - runway_heading) % 360)
    if angle > 180:
        angle = 360 - angle
    return float(wind_speed) * abs(math.sin(math.radians(angle)))


def _stats_block(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None, "stdev": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": mean(values),
        "stdev": stdev(values) if len(values) >= 2 else 0.0,
    }


def _circular_mean_deg(samples: list[tuple[int, float]]) -> Optional[float]:
    """Vector mean wind direction. samples: list of (dir_deg, weight)."""
    if not samples:
        return None
    sx = sum(w * math.sin(math.radians(d)) for d, w in samples)
    cx = sum(w * math.cos(math.radians(d)) for d, w in samples)
    if sx == 0 and cx == 0:
        return None
    ang = math.degrees(math.atan2(sx, cx))
    return ang % 360.0


def density_altitude_ft(elev_ft: Optional[int],
                        temp_c: Optional[float],
                        altimeter_inhg: Optional[float]) -> Optional[float]:
    """Approximate density altitude from field elevation, OAT, and altimeter."""
    if elev_ft is None or temp_c is None or altimeter_inhg is None:
        return None
    pressure_alt_ft = float(elev_ft) + (29.92 - float(altimeter_inhg)) * 1000.0
    isa_temp_c = 15.0 - 1.98 * (pressure_alt_ft / 1000.0)
    return pressure_alt_ft + 120.0 * (float(temp_c) - isa_temp_c)


def _obs_query(airport: str, pacific_hours: list[int],
               date_from: Optional[date], date_to: Optional[date]):
    """Yield observation dicts in the [date_from, date_to] Pacific window
    whose Pacific hour matches `pacific_hours`. Each yielded dict has:
      time_utc, time_pt (str), hour_pt, wind_dir, is_vrb, wind_speed,
      gust, vis, ceiling, temp_c, dew_c, altimeter_inhg.
    """
    airport = normalize_airport(airport)
    db_init()
    where = ["airport = ?"]
    params: list = [airport]
    # Date inputs are Pacific-local calendar days. Anchor each edge at
    # Pacific midnight so a "from 2026-04-05" includes the full Pacific day
    # (no leak from Apr 4 PT) and "to 2026-04-05" includes everything
    # through 23:59:59 PT on Apr 5 (no truncation 7 hours early).
    if date_from is not None:
        where.append("obs_time_utc >= ?")
        params.append(
            datetime.combine(date_from, dt_time(0, 0, 0), local_tz())
                    .astimezone(UTC)
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    if date_to is not None:
        where.append("obs_time_utc <= ?")
        params.append(
            datetime.combine(date_to, dt_time(23, 59, 59), local_tz())
                    .astimezone(UTC)
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    valid_hours = set(pacific_hours)
    tz = local_tz()
    sql = (
        "SELECT obs_time_utc, wind_dir, is_vrb, wind_speed_kt, gust_kt, "
        "vis_sm, ceiling_ft, temp_c, dew_c, altimeter_inhg "
        "FROM metar WHERE " + " AND ".join(where)
        + " ORDER BY obs_time_utc"
    )
    with closing(db_connect()) as conn:
        for r in conn.execute(sql, params):
            t_utc = datetime.strptime(r["obs_time_utc"],
                                      "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            t_local = t_utc.astimezone(tz)
            if t_local.hour not in valid_hours:
                continue
            yield {
                "time_utc": r["obs_time_utc"],
                "time_pt":  t_local.strftime("%Y-%m-%d %H:%M"),
                "hour_pt":  t_local.hour,
                "wind_dir": r["wind_dir"],
                "is_vrb":   bool(r["is_vrb"]),
                "wind_speed": r["wind_speed_kt"],
                "gust":     r["gust_kt"],
                "vis":      r["vis_sm"],
                "ceiling":  r["ceiling_ft"],
                "temp_c":   r["temp_c"],
                "dew_c":    r["dew_c"],
                "altimeter_inhg": r["altimeter_inhg"],
            }


def compute_stats(
    airport: str,
    runway_heading,                 # int | "auto"
    pacific_hours: list[int],
    *,
    runways_airport: Optional[str] = None,
    runway_ident: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    vis_min_sm: float = 5.0,
    ceiling_min_ft: float = 8000.0,
    xwind_max_kt: float = 12.0,
) -> dict:
    """Return per-Pacific-hour aggregates for `airport`.

    Wind/vis/ceiling data comes from `airport`. The runway frame of
    reference (used for crosswind and active-runway selection) is taken
    from `runways_airport` if supplied, else from `airport`. This split
    matters when a substituted reporting station feeds data for a small
    field that has no METAR of its own — e.g. wind from KPSP, runways
    from L22.

    If `runway_heading == "auto"` (or None), each observation is
    individually attributed to the runway END with the least absolute
    crosswind component; the per-window "primary" end is the most-used
    one. Mixed-end usage is exposed in `runways_used` and per-row
    `active_end_ident` / `is_mixed`.
    """
    airport = normalize_airport(airport)
    rwy_airport = normalize_airport(runways_airport or airport)

    buckets: dict[int, dict] = {h: {
        "obs_count": 0,
        "dir_samples": [],          # (deg, weight) for circular mean
        # raw wind tuples, used for xwind once we know which end to use:
        "wind_obs": [],             # (wind_dir, wind_speed_kt, is_vrb)
        "speed": [],
        "gust": [],
        # Ceiling height (ft AGL) of the lowest BKN/OVC/VV layer, when
        # one is reported. None / clear-sky obs are excluded from these
        # stats (count reflects how many obs actually had a ceiling).
        "ceiling_vals": [],
        "ct_min_breached": 0,
        "vis_obs": 0,
        "ceiling_obs": 0,
        "vis_breached": 0,
        "ceiling_breached": 0,
        "xwind_breached": 0,
    } for h in pacific_hours}
    overall_dir_samples: list[tuple[int, float]] = []

    for r in _obs_query(airport, pacific_hours, date_from, date_to):
        h = r["hour_pt"]
        b = buckets[h]
        b["obs_count"] += 1

        ws = r["wind_speed"]
        wd = r["wind_dir"]
        is_vrb = r["is_vrb"]

        if ws is not None:
            b["speed"].append(float(ws))
            b["wind_obs"].append((wd, float(ws), is_vrb))
            if wd is not None and not is_vrb and ws > 0:
                sample = (int(wd), float(ws))
                b["dir_samples"].append(sample)
                overall_dir_samples.append(sample)

        if r["gust"] is not None:
            b["gust"].append(float(r["gust"]))

        vis = r["vis"]
        ceil = r["ceiling"]
        vis_breach = (vis is not None and vis < vis_min_sm)
        ceil_breach = (ceil is not None and ceil < ceiling_min_ft)
        if vis is not None:
            b["vis_obs"] += 1
            if vis_breach:
                b["vis_breached"] += 1
        if ceil is not None:
            b["ceiling_obs"] += 1
            b["ceiling_vals"].append(float(ceil))
            if ceil_breach:
                b["ceiling_breached"] += 1

    # Resolve runway frame.
    requested_ident = runway_ident.strip().upper() if runway_ident else None
    auto_requested = (
        not requested_ident
        and (
            runway_heading is None
            or (isinstance(runway_heading, str)
                and runway_heading.lower() == "auto")
        )
    )
    runways = get_runways(rwy_airport)
    usable = [rw for rw in runways if rw.get("heading_deg") is not None]
    overall_wind_deg = _circular_mean_deg(overall_dir_samples)

    # Per-obs end attribution for AUTO mode (skipped if no usable runways
    # or if user pinned an explicit heading).
    explicit_heading: Optional[int] = None
    explicit_chosen: Optional[dict] = None
    if requested_ident:
        for rw in usable:
            if str(rw.get("ident", "")).upper() == requested_ident:
                explicit_chosen = rw
                explicit_heading = int(rw["heading_deg"])
                break
        if explicit_chosen is None and (
            runway_heading is None
            or (isinstance(runway_heading, str)
                and runway_heading.lower() == "auto")
        ):
            auto_requested = True
    if not auto_requested and explicit_chosen is None:
        explicit_heading = int(runway_heading)
        for rw in runways:
            if rw.get("heading_deg") == explicit_heading:
                explicit_chosen = rw
                break

    end_counts_total: dict[str, dict] = {}     # ident -> {count, runway}
    end_counts_per_h: dict[int, dict[str, int]] = {h: {} for h in pacific_hours}

    if usable and auto_requested:
        for h in pacific_hours:
            for (wd, _ws, vrb) in buckets[h]["wind_obs"]:
                if vrb or wd is None:
                    continue
                end = _pick_end_for_obs(usable, int(wd))
                if not end:
                    continue
                ident = end["ident"]
                end_counts_total.setdefault(
                    ident, {"count": 0, "runway": end})
                end_counts_total[ident]["count"] += 1
                end_counts_per_h[h][ident] = end_counts_per_h[h].get(ident, 0) + 1

    runways_used: list[dict] = []
    if usable and auto_requested and end_counts_total:
        ranked = sorted(
            end_counts_total.values(),
            key=lambda x: (-x["count"], x["runway"]["ident"])
        )
        primary_ident = ranked[0]["runway"]["ident"]
        for entry in ranked:
            rw = entry["runway"]
            runways_used.append({
                "ident":       rw["ident"],
                "heading_deg": rw["heading_deg"],
                "obs_count":   entry["count"],
                "primary":     rw["ident"] == primary_ident,
            })
    elif explicit_chosen is not None:
        runways_used.append({
            "ident":       explicit_chosen["ident"],
            "heading_deg": explicit_chosen["heading_deg"],
            "obs_count":   sum(b["obs_count"] for b in buckets.values()),
            "primary":     True,
        })

    # Resolve the single representative runway/heading we report up front.
    chosen: Optional[dict] = None
    heading_for_xwind: Optional[int] = None
    if explicit_chosen is not None:
        chosen = explicit_chosen
        heading_for_xwind = explicit_heading
    elif runways_used:
        primary = runways_used[0]
        for rw in runways:
            if rw["ident"] == primary["ident"]:
                chosen = rw
                break
        heading_for_xwind = int(primary["heading_deg"])
    elif not usable:
        # No-runway-data airport (e.g. KSAS): xwind is undefined. We do
        # not invent a heading from wind direction here — that gives a
        # meaningless "0 kt always" xwind. Leave heading None so the
        # frontend can render "no runway".
        chosen = None
        heading_for_xwind = None
    else:
        # Usable runways exist but no obs had a usable wind dir
        # (everything VRB / missing). Fall back to the closest end to
        # the vector mean if we have one, else first usable end.
        chosen = pick_active_runway(usable, overall_wind_deg) or usable[0]
        heading_for_xwind = int(chosen["heading_deg"])

    primary_ident = (runways_used[0]["ident"]
                     if runways_used else None)

    # Beneath-minimums total requires crosswind, which depends on the runway
    # frame above. Second pass: same observations, mark vis / ceil / xwind
    # breaches and roll up ct_min_breached + xwind_breached per hour.
    for r in _obs_query(airport, pacific_hours, date_from, date_to):
        h = r["hour_pt"]
        b = buckets[h]
        vis = r["vis"]
        ceil = r["ceiling"]
        vis_breach = (vis is not None and vis < vis_min_sm)
        ceil_breach = (ceil is not None and ceil < ceiling_min_ft)
        xw, _ = _row_xwind(
            r, usable=usable,
            explicit_heading=explicit_heading,
            heading_for_xwind=heading_for_xwind)
        xw_breach = (
            xw is not None
            and abs(float(xw)) > float(xwind_max_kt))
        if xw_breach:
            b["xwind_breached"] += 1
        if vis_breach or ceil_breach or xw_breach:
            b["ct_min_breached"] += 1

    rows = []
    for h in pacific_hours:
        b = buckets[h]
        # Crosswind: skip VRB and missing-dir obs (no defined component).
        # If we have no runway frame at all, xwind is empty for the row.
        if heading_for_xwind is None:
            xs = []
        elif explicit_heading is not None:
            xs = [crosswind(wd, ws, explicit_heading)
                  for (wd, ws, vrb) in b["wind_obs"]
                  if not vrb and wd is not None]
        else:
            # Auto mode with at least one usable runway: xwind for each
            # obs is computed against that obs's chosen end. (For two
            # ends 180° apart this is identical to using either end.)
            xs = []
            for (wd, ws, vrb) in b["wind_obs"]:
                if vrb or wd is None:
                    continue
                end = _pick_end_for_obs(usable, int(wd)) if usable else None
                hdg = end["heading_deg"] if end else heading_for_xwind
                xs.append(crosswind(wd, ws, hdg))

        vrb_count = sum(1 for (_w, _s, vrb) in b["wind_obs"] if vrb)
        ec = end_counts_per_h.get(h, {})
        if explicit_chosen is not None:
            active_end_ident = explicit_chosen["ident"]
            is_mixed = False
        elif ec:
            active_end_ident = max(ec.items(), key=lambda kv: kv[1])[0]
            is_mixed = len(ec) > 1
        else:
            active_end_ident = None
            is_mixed = False

        rows.append({
            "hour_pt": h,
            "obs_count": b["obs_count"],
            "avg_dir_deg": _circular_mean_deg(b["dir_samples"]),
            "xwind": _stats_block(xs),
            "vrb_count": vrb_count,
            "wind_speed": _stats_block(b["speed"]),
            "gust": _stats_block(b["gust"]),
            "ceiling": _stats_block(b["ceiling_vals"]),
            "ct_min_breached": b["ct_min_breached"],
            "vis_breached": b["vis_breached"],
            "ceiling_breached": b["ceiling_breached"],
            "xwind_breached": b["xwind_breached"],
            "active_end_ident": active_end_ident,
            "is_mixed": is_mixed,
            "end_counts": dict(ec),
        })

    info = airport_info(airport)
    total_obs_in_window = sum(r["obs_count"] for r in rows)
    no_runway_data = (not usable)
    return {
        "airport": airport,
        "runways_airport": rwy_airport,
        "runway_heading": heading_for_xwind,
        "runway_ident":   chosen["ident"] if chosen else None,
        "runway_auto":    auto_requested,
        "runway_overall_wind_deg": (round(overall_wind_deg, 1)
                                    if overall_wind_deg is not None else None),
        "runways_used":   runways_used,
        "primary_ident":  primary_ident,
        "no_runway_data": no_runway_data,
        "vis_min_sm": vis_min_sm,
        "ceiling_min_ft": ceiling_min_ft,
        "xwind_max_kt": xwind_max_kt,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "first_obs_utc": info["first_obs_utc"],
        "last_obs_utc": info["last_obs_utc"],
        "first_obs_pt": info["first_obs_pt"],
        "last_obs_pt": info["last_obs_pt"],
        "total_obs": total_obs_in_window,
        "total_obs_cached": info["obs_count"],
        "tz": local_tz().key,
        "runways": runways,
        "rows": rows,
    }


# Soft cap on raw-obs payload to keep frontend rendering snappy. If a
# query exceeds this, we return the most recent N obs and flag the cap.
RAW_OBS_LIMIT = 5000

# Time-series chart in the See-all view. We always include every hour
# (no hour filter) so diurnal oscillation is visible, but we cap the
# horizontal extent to the most recent N days from date_to (or today)
# so the line stays readable. ~90 days × 24 obs ≈ 2.2k points, which
# renders smoothly as SVG.
TIMELINE_MAX_DAYS = 90


def _row_xwind(r: dict, *, usable: list[dict],
               explicit_heading: Optional[int],
               heading_for_xwind: Optional[int]
               ) -> tuple[Optional[float], Optional[str]]:
    """Compute (xwind_kt, active_end_ident) for a single observation,
    given the resolved runway frame. Mirrors the per-obs logic used by
    compute_stats and compute_raw_obs. Returns (None, None) when xwind
    is undefined for the obs (VRB / missing wind, no runway frame)."""
    wd, ws = r["wind_dir"], r["wind_speed"]
    if ws is None or wd is None or r["is_vrb"]:
        return None, None
    if explicit_heading is not None:
        return round(crosswind(int(wd), float(ws), explicit_heading), 2), None
    if usable:
        end = _pick_end_for_obs(usable, int(wd))
        if end:
            return (round(crosswind(int(wd), float(ws),
                                    end["heading_deg"]), 2),
                    end["ident"])
    if heading_for_xwind is not None:
        return round(crosswind(int(wd), float(ws), heading_for_xwind), 2), None
    return None, None


def compute_raw_obs(
    airport: str,
    runway_heading,                 # int | "auto"
    pacific_hours: list[int],
    *,
    runways_airport: Optional[str] = None,
    runway_ident: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    vis_min_sm: float = 5.0,
    ceiling_min_ft: float = 8000.0,
    xwind_max_kt: float = 12.0,
) -> dict:
    """Return every observation in the window/hours as raw rows.

    Crosswind is computed against the runway frame defined by
    `runways_airport` (defaults to `airport`). In auto mode each obs is
    attributed to the runway END with the least absolute crosswind
    component; in manual mode every obs uses the supplied heading.
    If the runway-frame airport has no usable headings (e.g.
    KSAS), `xwind` is None for every row.
    """
    airport = normalize_airport(airport)
    rwy_airport = normalize_airport(runways_airport or airport)
    da_airport = get_airport_record(airport) if airport == rwy_airport else None
    da_elev_ft = da_airport.get("elev_ft") if da_airport else None
    da_available = airport == rwy_airport and da_elev_ft is not None

    # Two passes: pass 1 collects dir samples for auto-runway selection
    # and the raw rows; pass 2 emits rows with xwind/active_end_ident.
    overall_dir_samples: list[tuple[int, float]] = []
    raw: list[dict] = []
    for r in _obs_query(airport, pacific_hours, date_from, date_to):
        wd = r["wind_dir"]
        ws = r["wind_speed"]
        if (ws is not None and wd is not None
                and not r["is_vrb"] and ws > 0):
            overall_dir_samples.append((int(wd), float(ws)))
        raw.append(r)

    runways = get_runways(rwy_airport)
    usable = [rw for rw in runways if rw.get("heading_deg") is not None]
    overall_wind_deg = _circular_mean_deg(overall_dir_samples)
    requested_ident = runway_ident.strip().upper() if runway_ident else None
    auto_requested = (
        not requested_ident
        and (
            runway_heading is None
            or (isinstance(runway_heading, str)
                and runway_heading.lower() == "auto")
        )
    )

    explicit_chosen: Optional[dict] = None
    explicit_heading: Optional[int] = None
    if requested_ident:
        for rw in usable:
            if str(rw.get("ident", "")).upper() == requested_ident:
                explicit_chosen = rw
                explicit_heading = int(rw["heading_deg"])
                break
        if explicit_chosen is None and (
            runway_heading is None
            or (isinstance(runway_heading, str)
                and runway_heading.lower() == "auto")
        ):
            auto_requested = True
    if not auto_requested and explicit_chosen is None:
        explicit_heading = int(runway_heading)
        for rw in runways:
            if rw.get("heading_deg") == explicit_heading:
                explicit_chosen = rw
                break

    end_counts: dict[str, dict] = {}
    if usable and auto_requested:
        for r in raw:
            wd = r["wind_dir"]
            if r["is_vrb"] or wd is None:
                continue
            end = _pick_end_for_obs(usable, int(wd))
            if not end:
                continue
            ident = end["ident"]
            end_counts.setdefault(
                ident, {"count": 0, "runway": end})
            end_counts[ident]["count"] += 1

    runways_used: list[dict] = []
    primary_ident: Optional[str] = None
    if usable and auto_requested and end_counts:
        ranked = sorted(end_counts.values(),
                        key=lambda x: (-x["count"], x["runway"]["ident"]))
        primary_ident = ranked[0]["runway"]["ident"]
        for entry in ranked:
            rw = entry["runway"]
            runways_used.append({
                "ident":       rw["ident"],
                "heading_deg": rw["heading_deg"],
                "obs_count":   entry["count"],
                "primary":     rw["ident"] == primary_ident,
            })
    elif explicit_chosen is not None:
        primary_ident = explicit_chosen["ident"]
        runways_used.append({
            "ident":       explicit_chosen["ident"],
            "heading_deg": explicit_chosen["heading_deg"],
            "obs_count":   len(raw),
            "primary":     True,
        })

    chosen: Optional[dict] = None
    heading_for_xwind: Optional[int] = None
    if explicit_chosen is not None:
        chosen = explicit_chosen
        heading_for_xwind = explicit_heading
    elif runways_used:
        for rw in runways:
            if rw["ident"] == primary_ident:
                chosen = rw
                break
        heading_for_xwind = int(runways_used[0]["heading_deg"])
    elif not usable:
        chosen = None
        heading_for_xwind = None
    else:
        chosen = pick_active_runway(usable, overall_wind_deg) or usable[0]
        heading_for_xwind = int(chosen["heading_deg"])

    def _emit(r):
        xw, active_end_ident = _row_xwind(
            r, usable=usable,
            explicit_heading=explicit_heading,
            heading_for_xwind=heading_for_xwind)
        # When the user pinned a heading we still want active_end_ident
        # to surface as the explicit choice (helps the see-all view).
        if explicit_heading is not None and explicit_chosen is not None:
            active_end_ident = explicit_chosen["ident"]
        vis = r["vis"]
        ceil = r["ceiling"]
        xw_breach = (
            xw is not None
            and abs(float(xw)) > float(xwind_max_kt))
        da = (density_altitude_ft(da_elev_ft, r.get("temp_c"),
                                  r.get("altimeter_inhg"))
              if da_available else None)
        da_round = int(round(da)) if da is not None else None
        return {
            "time_utc":   r["time_utc"],
            "time_pt":    r["time_pt"],
            "hour_pt":    r["hour_pt"],
            "wind_dir":   r["wind_dir"],
            "is_vrb":     r["is_vrb"],
            "wind_speed": r["wind_speed"],
            "gust":       r["gust"],
            "vis":        vis,
            "ceiling":    ceil,
            "xwind":      xw,
            "active_end_ident": active_end_ident,
            "density_altitude_ft": da_round,
            "density_altitude_high": bool(
                da is not None and da > DENSITY_ALTITUDE_WARN_FT),
            "breached":   bool(
                (vis  is not None and vis  < vis_min_sm) or
                (ceil is not None and ceil < ceiling_min_ft) or
                xw_breach
            ),
        }

    out_rows = [_emit(r) for r in raw]

    truncated = False
    total = len(out_rows)
    if total > RAW_OBS_LIMIT:
        out_rows = out_rows[-RAW_OBS_LIMIT:]   # keep most recent
        truncated = True

    # Time-series payload for the See-all chronological line chart.
    # All 24 hours, in chronological order, capped to the most recent
    # TIMELINE_MAX_DAYS days. The same xwind frame applies, so dot
    # coloring (primary vs off-primary end) lines up with the table.
    today = date.today()
    cap_from = (date_to or today) - timedelta(days=TIMELINE_MAX_DAYS - 1)
    timeline_from = (max(date_from, cap_from)
                     if date_from is not None else cap_from)
    timeline_rows = [_emit(r)
                     for r in _obs_query(airport, list(range(24)),
                                         timeline_from, date_to)]
    timeline_truncated = False
    if len(timeline_rows) > RAW_OBS_LIMIT:
        timeline_rows = timeline_rows[-RAW_OBS_LIMIT:]
        timeline_truncated = True

    return {
        "airport":          airport,
        "runways_airport":  rwy_airport,
        "runway_heading":   heading_for_xwind,
        "runway_ident":     chosen["ident"] if chosen else None,
        "runway_auto":      auto_requested,
        "runways_used":     runways_used,
        "primary_ident":    primary_ident,
        "no_runway_data":   (not usable),
        "vis_min_sm":       vis_min_sm,
        "ceiling_min_ft":   ceiling_min_ft,
        "xwind_max_kt":     xwind_max_kt,
        "density_altitude_available": da_available,
        "density_altitude_elev_ft": da_elev_ft if da_available else None,
        "density_altitude_threshold_ft": DENSITY_ALTITUDE_WARN_FT,
        "date_from":        date_from.isoformat() if date_from else None,
        "date_to":          date_to.isoformat() if date_to else None,
        "total_obs":        total,
        "returned":         len(out_rows),
        "truncated":        truncated,
        "limit":            RAW_OBS_LIMIT,
        "rows":             out_rows,
        "timeline":         timeline_rows,
        "timeline_from":    timeline_from.isoformat(),
        "timeline_to":      (date_to or today).isoformat(),
        "timeline_max_days": TIMELINE_MAX_DAYS,
        "timeline_truncated": timeline_truncated,
        "tz": local_tz().key,
    }


# ----------------------------------------------------------------------
# Forecast data
# ----------------------------------------------------------------------

FORECAST_TOOL_INFO = [
    {
        "id": "taf",
        "use": "Official forecast written for this airport, 24-30 h. Trust it first when the airport has one.",
        "label": "TAF",
        "full_name": "Terminal Aerodrome Forecast",
        "what": "Official human-edited aviation forecast for a terminal area.",
        "range": "0-24/30h",
        "spatial": "same-airport terminal forecast, about 5 sm",
        "resolution": "change groups, not a fixed hourly grid",
        "accuracy": "best aviation-context source when same-airport TAF exists; no numeric CI",
        "limits": "use only for matching airport/terminal area; not every field has a TAF",
        "da": "no, TAF generally lacks temp/pressure",
    },
    {
        "id": "lamp",
        "use": "Hourly statistical forecast built from the latest observations, to 38 h. Strong for ceiling and visibility in the next few hours.",
        "label": "Station LAMP",
        "full_name": "Localized Aviation MOS Program",
        "what": "MOS (Model Output Statistics) station forecast: model output statistically corrected with observations.",
        "range": "0-38h",
        "spatial": "station guidance where the airport exists in LAMP",
        "resolution": "hourly station guidance",
        "accuracy": "strong free near-term aviation guidance; categorical probabilities exist in products",
        "limits": "coverage is station-dependent; guidance is not human-edited like a TAF",
        "da": "partial: temp exists, pressure usually absent",
    },
    {
        "id": "nws_grid",
        "use": "Forecaster-edited NWS grid for the airport's location, 7 days. Good for day-by-day planning.",
        "label": "NWS gridpoint",
        "full_name": "National Weather Service grid forecast",
        "what": "Official NWS point/grid forecast sampled at the airport lat/lon.",
        "range": "0-7d",
        "spatial": "official NWS gridpoint at airport lat/lon",
        "resolution": "gridded forecast periods; easy API access",
        "accuracy": "good official baseline; less aviation-specific than TAF/LAMP",
        "limits": "not a direct ceiling forecast and may smooth airport-scale effects",
        "da": "partial when temp and pressure are populated",
    },
    {
        "id": "hrrr",
        "use": "3 km model rerun every hour, to 18-48 h. The most detail for today and tomorrow.",
        "label": "HRRR",
        "full_name": "High-Resolution Rapid Refresh",
        "what": "Hourly updated convection-allowing U.S. model.",
        "range": "0-18h; 48h on major cycles",
        "spatial": "3 km CONUS model grid",
        "resolution": "hourly, 3 km grid",
        "accuracy": "good short-range wind/detail; deterministic, no native CI in this pull",
        "limits": "single model run; confidence comes from agreement/verification, not built-in spread",
        "da": "yes when model temp + pressure are present",
    },
    {
        "id": "nbm",
        "use": "Blend of many models, to about 11 days. The steadiest single guess for days 2-7.",
        "label": "NBM",
        "full_name": "National Blend of Models",
        "what": "NOAA blend of many model and ensemble inputs.",
        "range": "0-11d",
        "spatial": "blended CONUS/grid guidance",
        "resolution": "gridded blend; hourly/period values vary by variable",
        "accuracy": "best free longer-range blend; native probabilistic products exist, wrapper is deterministic",
        "limits": "blend can smooth sharp local wind or marine-layer ceiling changes",
        "da": "partial; wrapper often lacks pressure",
    },
    {
        "id": "nam",
        "use": "3 km model, to 60 h. A second opinion next to HRRR.",
        "label": "NAM 3 km",
        "full_name": "North American Mesoscale 3 km nest",
        "what": "Regional U.S. model used here as a high-res comparison member.",
        "range": "0-60h",
        "spatial": "3 km CONUS model grid",
        "resolution": "3 km grid, forecast-cycle guidance",
        "accuracy": "useful deterministic comparison member; no native CI",
        "limits": "single model run; weaker than a verified blend when used alone",
        "da": "yes when model temp + pressure are present",
    },
    {
        "id": "gfs",
        "use": "Global, coarser model, to 16 days. Use it for the trend beyond a week.",
        "label": "GFS",
        "full_name": "Global Forecast System",
        "what": "Global numerical weather model for broad pattern context.",
        "range": "0-16d",
        "spatial": "global, coarse grid",
        "resolution": "coarser global grid, longer horizon",
        "accuracy": "broad-pattern context; weak for airport marine-layer ceiling",
        "limits": "too coarse for many airport-scale wind/ceiling details",
        "da": "yes when model temp + pressure are present",
    },
]

OPEN_METEO_MODELS = [
    ("hrrr", "HRRR", "gfs_hrrr"),
    ("nbm", "NBM", "ncep_nbm_conus"),
    ("nam", "NAM 3 km", "ncep_nam_conus"),
    ("gfs", "GFS", "gfs_global"),
]

CIG_CATEGORY_FT = {
    "1": 100,
    "2": 300,
    "3": 700,
    "4": 1500,
    "5": 2500,
    "6": 4500,
    "7": 8000,
    "8": 12000,
}

CIG_CATEGORY_LABEL = {
    "1": "< 200 ft",
    "2": "200-400 ft",
    "3": "500-900 ft",
    "4": "1000-1900 ft",
    "5": "2000-3000 ft",
    "6": "3100-6500 ft",
    "7": "6500-12000 ft",
    "8": "no ceiling / > 12000 ft",
}


def _forecast_model(
    model_id: str,
    label: str,
    *,
    source: str,
    horizon: str,
    spatial: str,
    accuracy: str,
    points: Optional[list[dict]] = None,
    status: str = "ok",
    missing_reason: Optional[str] = None,
) -> dict:
    return {
        "id": model_id,
        "label": label,
        "source": source,
        "horizon": horizon,
        "spatial": spatial,
        "accuracy": accuracy,
        "status": status,
        "missing_reason": missing_reason,
        "points": points or [],
    }


def _pt_iso(dt: datetime) -> str:
    return dt.astimezone(local_tz()).strftime("%Y-%m-%dT%H:%M")


def _pt_label(dt: datetime) -> str:
    return dt.astimezone(local_tz()).strftime("%Y-%m-%d %H:%M %Z")


def _parse_target_pt(s: Optional[str]) -> datetime:
    if not s:
        now = datetime.now(local_tz())
        target = (now + timedelta(days=1)).replace(
            hour=11, minute=0, second=0, microsecond=0)
        return target
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(local_tz())


def _safe_json(url: str, *, params: Optional[dict] = None,
               timeout: int = 12) -> tuple[Optional[object], Optional[str], int]:
    try:
        r = requests.get(url, params=params, headers=FORECAST_HEADERS,
                         timeout=timeout)
    except requests.RequestException as exc:
        return None, str(exc), 0
    if r.status_code == 204:
        return None, "no product returned", r.status_code
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}", r.status_code
    try:
        return r.json(), None, r.status_code
    except ValueError as exc:
        return None, f"bad JSON: {exc}", r.status_code


def _safe_text(url: str, *, timeout: int = 15) -> tuple[Optional[str], Optional[str], int]:
    try:
        r = requests.get(url, headers=FORECAST_HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        return None, str(exc), 0
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}", r.status_code
    return r.text, None, r.status_code


def _ceiling_from_clouds(clouds: Optional[list[dict]]) -> tuple[Optional[int], Optional[str]]:
    vals = []
    has_sky_report = clouds is not None
    for c in clouds or []:
        cover = str(c.get("cover") or "").upper()
        base = c.get("base")
        if cover in ("BKN", "OVC", "VV") and base is not None:
            try:
                vals.append(int(base))
            except (TypeError, ValueError):
                pass
    if vals:
        return min(vals), None
    if has_sky_report:
        return 12000, "no ceiling"
    return None, None


def _forecast_xwind(runways: list[dict],
                    wind_dir: Optional[float],
                    wind_speed: Optional[float]) -> tuple[Optional[float], Optional[str]]:
    if wind_dir is None or wind_speed is None:
        return None, None
    usable = [rw for rw in runways if rw.get("heading_deg") is not None]
    end = _pick_end_for_obs(usable, int(round(float(wind_dir)))) if usable else None
    if not end:
        return None, None
    return round(crosswind(int(round(float(wind_dir))),
                           float(wind_speed), int(end["heading_deg"])), 1), end["ident"]


def _forecast_point(
    dt: datetime,
    *,
    wind_speed_kt: Optional[float] = None,
    wind_dir_deg: Optional[float] = None,
    gust_kt: Optional[float] = None,
    ceiling_ft: Optional[float] = None,
    ceiling_label: Optional[str] = None,
    visibility_sm: Optional[float] = None,
    temp_f: Optional[float] = None,
    dew_f: Optional[float] = None,
    pressure_msl_hpa: Optional[float] = None,
    surface_pressure_hpa: Optional[float] = None,
    low_cloud_pct: Optional[float] = None,
    runways: Optional[list[dict]] = None,
    elev_ft: Optional[int] = None,
) -> dict:
    xwind_kt, active_end = _forecast_xwind(runways or [], wind_dir_deg, wind_speed_kt)
    temp_c = ((float(temp_f) - 32.0) * 5.0 / 9.0
              if temp_f is not None else None)
    pressure_hpa = surface_pressure_hpa or pressure_msl_hpa
    altimeter = (pressure_hpa * HPA_TO_INHG
                 if pressure_hpa is not None else None)
    da = density_altitude_ft(elev_ft, temp_c, altimeter)
    return {
        "time_pt": _pt_iso(dt),
        "time_label": _pt_label(dt),
        "time_utc": dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wind_speed_kt": (round(float(wind_speed_kt), 1)
                          if wind_speed_kt is not None else None),
        "wind_dir_deg": (int(round(float(wind_dir_deg))) % 360
                         if wind_dir_deg is not None else None),
        "gust_kt": (round(float(gust_kt), 1)
                    if gust_kt is not None else None),
        "xwind_kt": xwind_kt,
        "active_end_ident": active_end,
        "ceiling_ft": (int(round(float(ceiling_ft)))
                       if ceiling_ft is not None else None),
        "ceiling_label": ceiling_label,
        "visibility_sm": (round(float(visibility_sm), 1)
                          if visibility_sm is not None else None),
        "temp_f": (round(float(temp_f), 1) if temp_f is not None else None),
        "dew_f": (round(float(dew_f), 1) if dew_f is not None else None),
        "low_cloud_pct": (round(float(low_cloud_pct), 0)
                          if low_cloud_pct is not None else None),
        "density_altitude_ft": int(round(da)) if da is not None else None,
        "density_altitude_high": bool(da is not None and da > DENSITY_ALTITUDE_WARN_FT),
    }


def _nearest_forecast_point(points: list[dict],
                            target: datetime) -> tuple[Optional[dict], Optional[float]]:
    if not points:
        return None, None
    target_ts = target.timestamp()
    best = min(points, key=lambda p: abs(
        datetime.fromisoformat(p["time_pt"]).replace(tzinfo=local_tz()).timestamp()
        - target_ts))
    best_dt = datetime.fromisoformat(best["time_pt"]).replace(tzinfo=local_tz())
    delta_h = abs(best_dt.timestamp() - target_ts) / 3600.0
    return best, delta_h


def _taf_forecast(airport: str, target: datetime,
                  runways: list[dict]) -> dict:
    data, err, _status = _safe_json(
        AWC_TAF_URL, params={"ids": airport, "format": "json"}, timeout=18)
    if err or not data:
        return _forecast_model(
            "taf", "TAF", source="NOAA AWC",
            horizon="0-24/30h", spatial="same-airport terminal area, 5 sm",
            accuracy="official aviation forecast; no numeric confidence intervals",
            status="missing",
            missing_reason=("no same-airport TAF available"
                            if err in (None, "no product returned") else err),
        )
    taf = data[0] if isinstance(data, list) and data else data
    points: list[dict] = []
    for fc in taf.get("fcsts") or []:
        try:
            start = datetime.fromtimestamp(int(fc["timeFrom"]), UTC).astimezone(PACIFIC)
            end = datetime.fromtimestamp(int(fc["timeTo"]), UTC).astimezone(PACIFIC)
        except (KeyError, TypeError, ValueError):
            continue
        t = start.replace(minute=0, second=0, microsecond=0)
        if t < start:
            t += timedelta(hours=1)
        while t < end:
            wdir = fc.get("wdir")
            if isinstance(wdir, str) and wdir.upper() == "VRB":
                wdir = None
            vis = fc.get("visib")
            if isinstance(vis, str):
                vis = 6.0 if vis.endswith("+") else None
            ceiling_ft, ceiling_label = _ceiling_from_clouds(fc.get("clouds"))
            points.append(_forecast_point(
                t,
                wind_speed_kt=fc.get("wspd"),
                wind_dir_deg=wdir,
                gust_kt=fc.get("wgst"),
                ceiling_ft=ceiling_ft,
                ceiling_label=ceiling_label,
                visibility_sm=vis,
                runways=runways,
            ))
            t += timedelta(hours=1)
    return _forecast_model(
        "taf", "TAF", source="NOAA AWC",
        horizon="0-24/30h", spatial="same-airport terminal area, 5 sm",
        accuracy="official aviation forecast; no numeric confidence intervals",
        points=points,
        status="ok" if points else "missing",
        missing_reason=None if points else "TAF returned but no decoded forecast periods",
    )


def _parse_lamp_block(text: str, airport: str) -> Optional[str]:
    lines = text.splitlines()
    start = None
    station_line = re.compile(r"^\s*[A-Z0-9]{4}\s{3,}")
    for i, line in enumerate(lines):
        if line.lstrip().startswith(airport + " "):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if station_line.match(lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def _lamp_forecast(airport: str, target: datetime,
                   runways: list[dict], elev_ft: Optional[int]) -> dict:
    text, err, _status = _safe_text(LAMP_TEXT_URL, timeout=18)
    if err or not text:
        return _forecast_model(
            "lamp", "Station LAMP", source="NOAA/MDL LAMP",
            horizon="0-38h", spatial="station guidance",
            accuracy="short-range aviation MOS guidance with categorical probabilities",
            status="missing", missing_reason=err or "LAMP bulletin unavailable")
    block = _parse_lamp_block(text, airport)
    if not block:
        return _forecast_model(
            "lamp", "Station LAMP", source="NOAA/MDL LAMP",
            horizon="0-38h", spatial="station guidance",
            accuracy="short-range aviation MOS guidance with categorical probabilities",
            status="missing", missing_reason="airport not present in station LAMP bulletin")

    header = block.splitlines()[0]
    mh = re.search(r"(\d{4}) UTC\s+(\d{1,2})/(\d{1,2})/(\d{4})", header)
    if not mh:
        return _forecast_model(
            "lamp", "Station LAMP", source="NOAA/MDL LAMP",
            horizon="0-38h", spatial="station guidance",
            accuracy="short-range aviation MOS guidance with categorical probabilities",
            status="missing", missing_reason="could not parse LAMP cycle time")
    hhmm, mon, day, year = mh.groups()
    cycle = datetime(int(year), int(mon), int(day),
                     int(hhmm[:2]), 0, tzinfo=UTC)

    rows: dict[str, list[str]] = {}
    for line in block.splitlines():
        label = line[:4].strip()
        if label in {"HR", "TMP", "DPT", "WDR", "WSP", "WGS", "CIG", "VIS", "CLD"}:
            rows[label] = line[4:].split()
    leads = rows.get("HR") or []
    points: list[dict] = []
    for i, lead_s in enumerate(leads):
        try:
            lead = int(lead_s)
        except ValueError:
            continue

        def val(row: str) -> Optional[str]:
            vals = rows.get(row) or []
            return vals[i] if i < len(vals) else None

        def num(row: str) -> Optional[float]:
            v = val(row)
            if v in (None, "NG", "X", "N"):
                return None
            try:
                return float(v)
            except ValueError:
                return None

        wsp = num("WSP")
        wdr_raw = val("WDR")
        wdir = None
        if wdr_raw not in (None, "NG", "X"):
            try:
                code = int(wdr_raw)
                if code != 0 or (wsp is not None and wsp > 0):
                    wdir = 360 if code == 36 else code * 10
            except ValueError:
                wdir = None
        cig_cat = val("CIG")
        cig_ft = CIG_CATEGORY_FT.get(cig_cat or "")
        cig_label = CIG_CATEGORY_LABEL.get(cig_cat or "")
        vis_cat = val("VIS")
        vis_sm = 6.0 if vis_cat == "7" else None
        dt = (cycle + timedelta(hours=lead)).astimezone(PACIFIC)
        points.append(_forecast_point(
            dt,
            wind_speed_kt=wsp,
            wind_dir_deg=wdir,
            gust_kt=num("WGS"),
            ceiling_ft=cig_ft,
            ceiling_label=cig_label,
            visibility_sm=vis_sm,
            temp_f=num("TMP"),
            dew_f=num("DPT"),
            runways=runways,
            elev_ft=elev_ft,
        ))
    return _forecast_model(
        "lamp", "Station LAMP", source="NOAA/MDL LAMP",
        horizon="0-38h", spatial="airport station guidance",
        accuracy="near-term aviation MOS; categorical ceiling/visibility probabilities are native to LAMP",
        points=points,
        status="ok" if points else "missing",
        missing_reason=None if points else "LAMP station block had no usable rows",
    )


def _parse_iso_duration(s: str) -> timedelta:
    m = re.fullmatch(
        r"P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?)?", s)
    if not m:
        return timedelta(hours=1)
    return timedelta(days=int(m.group("d") or 0),
                     hours=int(m.group("h") or 0),
                     minutes=int(m.group("m") or 0))


def _grid_intervals(prop: Optional[dict]) -> list[tuple[datetime, datetime, object]]:
    out = []
    for row in (prop or {}).get("values") or []:
        vt = row.get("validTime") or ""
        if "/" not in vt:
            continue
        start_s, dur_s = vt.split("/", 1)
        try:
            start = datetime.fromisoformat(start_s).astimezone(PACIFIC)
        except ValueError:
            continue
        out.append((start, start + _parse_iso_duration(dur_s), row.get("value")))
    return out


def _grid_value(intervals: list[tuple[datetime, datetime, object]],
                t: datetime) -> Optional[object]:
    for start, end, value in intervals:
        if start <= t < end:
            return value
    return None


def _nws_grid_forecast(record: dict, runways: list[dict],
                       elev_ft: Optional[int]) -> dict:
    lat, lon = record.get("lat"), record.get("lon")
    data, err, _status = _safe_json(
        NWS_POINTS_URL.format(lat=lat, lon=lon), timeout=12)
    if err or not data:
        return _forecast_model(
            "nws_grid", "NWS gridpoint", source="api.weather.gov",
            horizon="0-7d", spatial="official gridpoint at airport lat/lon",
            accuracy="official baseline; less aviation-specific than TAF/LAMP",
            status="missing", missing_reason=err or "NWS point lookup failed")
    grid_url = ((data.get("properties") or {}).get("forecastGridData"))
    grid, err, _status = _safe_json(grid_url, timeout=15) if grid_url else (None, "no grid URL", 0)
    if err or not grid:
        return _forecast_model(
            "nws_grid", "NWS gridpoint", source="api.weather.gov",
            horizon="0-7d", spatial="official gridpoint at airport lat/lon",
            accuracy="official baseline; less aviation-specific than TAF/LAMP",
            status="missing", missing_reason=err or "NWS grid lookup failed")
    props = grid.get("properties") or {}
    intervals = {
        k: _grid_intervals(props.get(k))
        for k in ("temperature", "dewpoint", "windSpeed", "windDirection",
                  "windGust", "ceilingHeight", "visibility", "pressure")
    }
    now = datetime.now(PACIFIC).replace(minute=0, second=0, microsecond=0)
    end = now + timedelta(days=7)
    points = []
    t = now
    while t <= end:
        ws = _grid_value(intervals["windSpeed"], t)
        wd = _grid_value(intervals["windDirection"], t)
        gust = _grid_value(intervals["windGust"], t)
        temp_c = _grid_value(intervals["temperature"], t)
        dew_c = _grid_value(intervals["dewpoint"], t)
        ceil_m = _grid_value(intervals["ceilingHeight"], t)
        vis_m = _grid_value(intervals["visibility"], t)
        pressure = _grid_value(intervals["pressure"], t)
        if any(v is not None for v in (ws, wd, gust, temp_c, dew_c, ceil_m, vis_m)):
            points.append(_forecast_point(
                t,
                wind_speed_kt=float(ws) * KMH_TO_KT if ws is not None else None,
                wind_dir_deg=wd,
                gust_kt=float(gust) * KMH_TO_KT if gust is not None else None,
                ceiling_ft=float(ceil_m) * M_TO_FT if ceil_m is not None else None,
                visibility_sm=float(vis_m) * M_TO_SM if vis_m is not None else None,
                temp_f=float(temp_c) * 9.0 / 5.0 + 32.0 if temp_c is not None else None,
                dew_f=float(dew_c) * 9.0 / 5.0 + 32.0 if dew_c is not None else None,
                pressure_msl_hpa=float(pressure) / 100.0 if pressure is not None else None,
                runways=runways,
                elev_ft=elev_ft,
            ))
        t += timedelta(hours=1)
    return _forecast_model(
        "nws_grid", "NWS gridpoint", source="api.weather.gov",
        horizon="0-7d", spatial="official gridpoint at airport lat/lon",
        accuracy="official baseline; less aviation-specific than TAF/LAMP",
        points=points,
        status="ok" if points else "missing",
        missing_reason=None if points else "NWS grid had no usable values",
    )


def _om_value(hourly: dict, name: str, suffix: str, i: int) -> Optional[float]:
    vals = hourly.get(f"{name}_{suffix}")
    if not vals or i >= len(vals):
        return None
    v = vals[i]
    return float(v) if v is not None else None


def _open_meteo_forecasts(record: dict, runways: list[dict],
                          elev_ft: Optional[int]) -> list[dict]:
    params = {
        "latitude": record["lat"],
        "longitude": record["lon"],
        "hourly": ",".join([
            "temperature_2m", "dew_point_2m",
            "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
            "cloud_cover_low", "cloud_base", "visibility",
            "surface_pressure", "pressure_msl",
        ]),
        "models": ",".join(suffix for _mid, _label, suffix in OPEN_METEO_MODELS),
        "timezone": "America/Los_Angeles",
        "wind_speed_unit": "kn",
        "temperature_unit": "fahrenheit",
        "forecast_days": 16,
    }
    data, err, _status = _safe_json(OPEN_METEO_URL, params=params, timeout=18)
    if err or not data:
        return [
            _forecast_model(
                mid, label, source="Open-Meteo NOAA wrapper",
                horizon="varies by model", spatial="model grid at airport lat/lon",
                accuracy="deterministic model feed; no native CI in this wrapper",
                status="missing", missing_reason=err or "Open-Meteo unavailable")
            for mid, label, _suffix in OPEN_METEO_MODELS
        ]
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    out = []
    meta = {
        "hrrr": ("0-18/48h", "3 km CONUS", "short-range deterministic detail"),
        "nbm": ("0-11d", "blended CONUS/grid guidance", "best free longer-range blend; wrapper deterministic"),
        "nam": ("0-60h", "3 km CONUS", "extra deterministic comparison member"),
        "gfs": ("0-16d", "global coarse grid", "broad-pattern context"),
    }
    for model_id, label, suffix in OPEN_METEO_MODELS:
        points = []
        for i, ts in enumerate(times):
            try:
                t = datetime.fromisoformat(ts).replace(tzinfo=PACIFIC)
            except ValueError:
                continue
            ws = _om_value(hourly, "wind_speed_10m", suffix, i)
            wd = _om_value(hourly, "wind_direction_10m", suffix, i)
            gust = _om_value(hourly, "wind_gusts_10m", suffix, i)
            temp = _om_value(hourly, "temperature_2m", suffix, i)
            dew = _om_value(hourly, "dew_point_2m", suffix, i)
            low_cloud = _om_value(hourly, "cloud_cover_low", suffix, i)
            cloud_base = _om_value(hourly, "cloud_base", suffix, i)
            vis_m = _om_value(hourly, "visibility", suffix, i)
            surf = _om_value(hourly, "surface_pressure", suffix, i)
            mslp = _om_value(hourly, "pressure_msl", suffix, i)
            if not any(v is not None for v in (
                ws, wd, gust, temp, dew, low_cloud, cloud_base, vis_m, surf, mslp
            )):
                continue
            points.append(_forecast_point(
                t,
                wind_speed_kt=ws,
                wind_dir_deg=wd,
                gust_kt=gust,
                ceiling_ft=cloud_base * M_TO_FT if cloud_base is not None else None,
                visibility_sm=vis_m * M_TO_SM if vis_m is not None else None,
                temp_f=temp,
                dew_f=dew,
                pressure_msl_hpa=mslp,
                surface_pressure_hpa=surf,
                low_cloud_pct=low_cloud,
                runways=runways,
                elev_ft=elev_ft,
            ))
        horizon, spatial, accuracy = meta[model_id]
        out.append(_forecast_model(
            model_id, label, source="Open-Meteo NOAA wrapper",
            horizon=horizon, spatial=spatial, accuracy=accuracy,
            points=points,
            status="ok" if points else "missing",
            missing_reason=None if points else "model out of range or fields unavailable",
        ))
    return out


def compute_forecast(airport: str, target_pt: Optional[str] = None) -> dict:
    airport = normalize_airport(airport)
    record = get_airport_record(airport)
    if not record or record.get("lat") is None or record.get("lon") is None:
        return {
            "airport": airport,
            "error": "airport not found in airport metadata",
            "models": [],
            "target_rows": [],
            "tool_info": FORECAST_TOOL_INFO,
        }
    target = _parse_target_pt(target_pt)
    runways = get_runways(airport)
    elev_ft = record.get("elev_ft")
    models = [
        _taf_forecast(airport, target, runways),
        _lamp_forecast(airport, target, runways, elev_ft),
        _nws_grid_forecast(record, runways, elev_ft),
    ]
    models.extend(_open_meteo_forecasts(record, runways, elev_ft))

    target_rows = []
    for model in models:
        point, delta_h = _nearest_forecast_point(model.get("points") or [], target)
        in_range = point is not None and delta_h is not None and delta_h <= 1.51
        target_rows.append({
            "model_id": model["id"],
            "model": model["label"],
            "status": model["status"] if in_range else "missing",
            "missing_reason": (
                None if in_range
                else (model.get("missing_reason")
                      or "target time outside this model's available horizon")
            ),
            "nearest_delta_h": round(delta_h, 2) if delta_h is not None else None,
            "point": point if in_range else None,
        })

    return {
        "airport": airport,
        "name": record.get("name"),
        "municipality": record.get("municipality"),
        "lat": record.get("lat"),
        "lon": record.get("lon"),
        "elev_ft": elev_ft,
        "target_pt": _pt_iso(target),
        "target_label": _pt_label(target),
        "tz": local_tz().key,
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "density_altitude_threshold_ft": DENSITY_ALTITUDE_WARN_FT,
        "models": models,
        "target_rows": target_rows,
        "tool_info": FORECAST_TOOL_INFO,
    }


# ----------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------

app = Flask(__name__, template_folder=str(ROOT / "templates"),
            static_folder=str(ROOT / "static"))
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


@app.before_request
def _set_request_tz():
    _LOCAL_TZ.set(parse_tz(request.args.get("tz")))


def _read_app_version() -> str:
    try:
        version = VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "1.0.1"
    return version or "1.0.1"


def _read_git_hash() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc:
        git_hash = proc.stdout.strip()
        if git_hash:
            return git_hash
    try:
        git_hash = GIT_HASH_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return git_hash or "unknown"


def _read_recaptcha_keys() -> tuple[str, str]:
    try:
        lines = [
            line.strip()
            for line in RECAPTCHA_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return "", ""
    if len(lines) < 2:
        return "", ""
    # Local secret file format: secret key first, public site key second.
    return lines[0], lines[1]


APP_VERSION = _read_app_version()
GIT_HASH = _read_git_hash()
RECAPTCHA_SECRET, RECAPTCHA_SITE_KEY = _read_recaptcha_keys()


@app.context_processor
def inject_build_info():
    return {
        "app_version": APP_VERSION,
        "git_hash": GIT_HASH,
        "site_url": SITE_URL,
        "recaptcha_site_key": RECAPTCHA_SITE_KEY,
        "recaptcha_enabled": bool(RECAPTCHA_SECRET and RECAPTCHA_SITE_KEY),
    }


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return date.fromisoformat(s)


@app.route("/")
def index():
    # Local dev tool: never serve a stale shell. The page is small and
    # the JS evolves frequently — caching just causes "why isn't my
    # change showing up?" confusion.
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/about")
def about():
    resp = app.make_response(render_template("about.html"))
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/about/")
def about_trailing_slash():
    return redirect("/about", code=301)


@app.route("/privacy/")
def privacy():
    resp = app.make_response(render_template("privacy.html"))
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["X-Robots-Tag"] = "noindex, follow"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    return resp


@app.route("/privacy")
def privacy_without_slash():
    return redirect("/privacy/", code=301)


@app.route("/robots.txt")
def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    lastmod = datetime.now(UTC).date().isoformat()
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{SITE_URL}/</loc>
    <lastmod>{lastmod}</lastmod>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>{SITE_URL}/about</loc>
    <lastmod>{lastmod}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.6</priority>
  </url>
</urlset>
"""
    return Response(body, mimetype="application/xml")


@app.route("/llms.txt")
def llms_txt():
    body = f"""# Windcheck

Windcheck is a free aviation weather web app for comparing airport wind,
crosswind, ceiling, visibility, density-altitude history, and forecast model
guidance.

Primary URLs:
- {SITE_URL}/: interactive forecast and historical METAR analysis app
- {SITE_URL}/about: crawler-readable explanation of methods, sources, limits,
  and intended use
- {SITE_URL}/health: public version/hash health check

Data and methods:
- Historical observations come from METAR/ASOS/AWOS-style reports cached from
  public archives and bucketed by local hour in the viewer's time zone.
- Runway geometry comes from open runway data and is used to compute absolute
  crosswind by runway end.
- Forecast guidance may include TAF, LAMP, NWS gridpoint, HRRR, NBM, NAM, and
  GFS where available.
- Windcheck is a planning aid, not an official weather briefing, and pilots
  should verify conditions with official aviation sources before flight.
"""
    return Response(body, mimetype="text/plain")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "windcheck",
        "version": APP_VERSION,
        "git_hash": GIT_HASH,
        "database": str(DB_PATH.name),
        "time_utc": datetime.now(UTC).isoformat(),
    })


def _is_local_request() -> bool:
    host = (request.host or "").split(":")[0].strip("[]").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _recaptcha_should_enforce() -> bool:
    if os.environ.get("WINDCHECK_RECAPTCHA_DISABLED") == "1":
        return False
    if not (RECAPTCHA_SECRET and RECAPTCHA_SITE_KEY):
        return False
    return not _is_local_request()


def _verify_recaptcha(payload: dict, expected_action: str) -> Optional[str]:
    if not _recaptcha_should_enforce():
        return None
    token = str(payload.get("recaptcha_token") or "").strip()
    action = str(payload.get("recaptcha_action") or "").strip()
    if action != expected_action:
        return "recaptcha action mismatch"
    if not token:
        return "recaptcha required"
    try:
        resp = requests.post(
            RECAPTCHA_VERIFY_URL,
            data={
                "secret": RECAPTCHA_SECRET,
                "response": token,
                "remoteip": request.headers.get("X-Forwarded-For",
                                                request.remote_addr or ""),
            },
            timeout=5,
        )
        data = resp.json()
    except (requests.RequestException, ValueError):
        return "recaptcha verification unavailable"

    if not data.get("success"):
        return "recaptcha failed"
    if data.get("action") != expected_action:
        return "recaptcha action mismatch"
    try:
        score = float(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    if score < RECAPTCHA_MIN_SCORE:
        return "recaptcha score too low"
    hostname = str(data.get("hostname") or "").lower()
    if hostname and hostname not in {"windcheck.net", "www.windcheck.net"}:
        return "recaptcha host mismatch"
    return None


@app.route("/api/info")
def api_info():
    airport = normalize_airport(request.args.get("airport", ""))
    if not airport:
        return jsonify({"error": "airport required"}), 400
    return jsonify(airport_info(airport))


@app.route("/api/runways")
def api_runways():
    airport = normalize_airport(request.args.get("airport", ""))
    if not airport:
        return jsonify({"error": "airport required"}), 400
    return jsonify({"airport": airport, "runways": get_runways(airport)})


def _resolve_fetch_range(payload: dict) -> tuple[date, date]:
    mode = (payload.get("mode") or "range").lower()
    today = datetime.now(UTC).date()
    if mode == "recent":
        days = int(payload.get("days", 60))
        return today - timedelta(days=days), today
    start = _parse_date(payload.get("start")) or (today - timedelta(days=365))
    end = _parse_date(payload.get("end")) or today
    return start, end


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    payload = request.get_json(force=True, silent=True) or {}
    airport = normalize_airport(payload.get("airport") or "")
    if not airport:
        return jsonify({"error": "airport required"}), 400
    recaptcha_error = _verify_recaptcha(payload, "fetch_history")
    if recaptcha_error:
        return jsonify({"error": recaptcha_error}), 403
    refetch = bool(payload.get("refetch"))
    try:
        start, end = _resolve_fetch_range(payload)
    except ValueError as e:
        return jsonify({"error": f"bad date: {e}"}), 400
    result = fetch_airport(airport, start, end, refetch=refetch, verbose=True)
    result["info"] = airport_info(airport)
    return jsonify(result)


@app.route("/api/fetch_stream", methods=["POST"])
def api_fetch_stream():
    payload = request.get_json(force=True, silent=True) or {}
    airport = normalize_airport(payload.get("airport") or "")
    if not airport:
        return jsonify({"error": "airport required"}), 400
    recaptcha_error = _verify_recaptcha(payload, "fetch_history")
    if recaptcha_error:
        return jsonify({"error": recaptcha_error}), 403
    refetch = bool(payload.get("refetch"))
    try:
        start, end = _resolve_fetch_range(payload)
    except ValueError as e:
        return jsonify({"error": f"bad date: {e}"}), 400

    def gen():
        try:
            for ev in fetch_airport_iter(airport, start, end, refetch=refetch):
                yield json.dumps(ev) + "\n"
        except Exception as exc:                           # pragma: no cover
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return Response(
        stream_with_context(gen()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _parse_stats_args():
    """Shared parser for /api/stats and /api/raw query strings.
    Returns either (kwargs_dict, None) or (None, (json_error, http_status)).
    """
    airport = normalize_airport(request.args.get("airport", ""))
    if not airport:
        return None, ({"error": "airport required"}, 400)
    rwy_airport_raw = request.args.get("runways_airport", "").strip()
    runways_airport = (normalize_airport(rwy_airport_raw)
                       if rwy_airport_raw else None)
    runway_ident_raw = request.args.get("runway_ident", "").strip().upper()
    runway_ident = runway_ident_raw or None
    rh_raw = request.args.get("runway_heading", "auto").strip()
    if rh_raw.lower() in ("auto", "", "none"):
        runway_heading: object = "auto"
    else:
        try:
            runway_heading = int(rh_raw)
        except ValueError:
            return None, ({"error": "runway_heading must be int or 'auto'"}, 400)
    hours_raw = request.args.get("hours", "")
    if hours_raw.strip():
        try:
            hours = sorted({int(x) for x in hours_raw.split(",")
                            if x.strip() != ""})
        except ValueError:
            return None, ({"error": "hours must be comma list of ints"}, 400)
    else:
        hours = list(range(24))
    hours = [h for h in hours if 0 <= h <= 23]
    try:
        date_from = _parse_date(request.args.get("date_from"))
        date_to = _parse_date(request.args.get("date_to"))
    except ValueError as e:
        return None, ({"error": f"bad date: {e}"}, 400)
    try:
        vis_min = float(request.args.get("vis_min_sm", "5"))
        ceiling_min = float(request.args.get("ceiling_min_ft", "8000"))
        xwind_max = float(request.args.get("xwind_max_kt", "12"))
    except ValueError:
        return None, ({"error": "vis_min_sm, ceiling_min_ft, and xwind_max_kt must be numeric"}, 400
                      )
    return {
        "airport": airport,
        "runways_airport": runways_airport,
        "runway_ident": runway_ident,
        "runway_heading": runway_heading,
        "pacific_hours": hours,
        "date_from": date_from,
        "date_to": date_to,
        "vis_min_sm": vis_min,
        "ceiling_min_ft": ceiling_min,
        "xwind_max_kt": xwind_max,
    }, None


@app.route("/api/stats")
def api_stats():
    args, err = _parse_stats_args()
    if err is not None:
        return jsonify(err[0]), err[1]
    return jsonify(compute_stats(
        args["airport"], args["runway_heading"], args["pacific_hours"],
        runways_airport=args["runways_airport"],
        runway_ident=args["runway_ident"],
        date_from=args["date_from"], date_to=args["date_to"],
        vis_min_sm=args["vis_min_sm"], ceiling_min_ft=args["ceiling_min_ft"],
        xwind_max_kt=args["xwind_max_kt"],
    ))


@app.route("/api/raw")
def api_raw():
    """Return every observation in the window/hours as raw rows. Used by
    the See-all view for scatter plots and the per-obs table."""
    args, err = _parse_stats_args()
    if err is not None:
        return jsonify(err[0]), err[1]
    return jsonify(compute_raw_obs(
        args["airport"], args["runway_heading"], args["pacific_hours"],
        runways_airport=args["runways_airport"],
        runway_ident=args["runway_ident"],
        date_from=args["date_from"], date_to=args["date_to"],
        vis_min_sm=args["vis_min_sm"], ceiling_min_ft=args["ceiling_min_ft"],
        xwind_max_kt=args["xwind_max_kt"],
    ))


@app.route("/api/forecast")
def api_forecast():
    airport = normalize_airport(request.args.get("airport", ""))
    if not airport:
        return jsonify({"error": "airport required"}), 400
    target_pt = request.args.get("target_pt") or None
    out = compute_forecast(airport, target_pt)
    if out.get("error"):
        return jsonify(out), 404
    return jsonify(out)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def _cli() -> int:
    p = argparse.ArgumentParser(description="METAR history tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    fp = sub.add_parser("fetch", help="Fetch METAR history into the local DB")
    fp.add_argument("airport")
    fp.add_argument("--start", help="YYYY-MM-DD")
    fp.add_argument("--end", help="YYYY-MM-DD (default: today UTC)")
    fp.add_argument("--days", type=int,
                    help="Convenience: fetch the last N days (overrides --start/--end)")
    fp.add_argument("--refetch", action="store_true",
                    help="Re-fetch even months already cached")

    rp = sub.add_parser("runways", help="Show runway data for an airport")
    rp.add_argument("airport")

    sp = sub.add_parser("serve", help="Run the local web app")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=5050)
    sp.add_argument("--debug", action="store_true")

    ip = sub.add_parser("info", help="Show DB coverage for an airport")
    ip.add_argument("airport")

    sp2 = sub.add_parser("stats", help="Print stats to stdout (debug)")
    sp2.add_argument("airport")
    sp2.add_argument("--runway", type=int, required=True)
    sp2.add_argument("--hours", default="0,6,12,18")

    args = p.parse_args()

    if args.cmd == "fetch":
        if args.days:
            result = fetch_recent(args.airport, days=args.days)
        else:
            if not args.start:
                p.error("either --days or --start is required")
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end) if args.end else datetime.now(UTC).date()
            result = fetch_airport(args.airport, start, end, refetch=args.refetch)
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.cmd == "runways":
        print(json.dumps({"airport": normalize_airport(args.airport),
                          "runways": get_runways(args.airport)}, indent=2))
        return 0

    if args.cmd == "serve":
        db_init()
        app.run(host=args.host, port=args.port, debug=args.debug)
        return 0

    if args.cmd == "info":
        print(json.dumps(airport_info(args.airport), indent=2))
        return 0

    if args.cmd == "stats":
        hours = sorted({int(x) for x in args.hours.split(",")})
        out = compute_stats(args.airport, args.runway, hours)
        print(json.dumps(out, indent=2, default=str))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli())
