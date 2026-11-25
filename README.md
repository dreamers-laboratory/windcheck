# Windcheck

Created by [Dreamers Inc.](https://thedreamers.us/).
The hosted app is at [windcheck.net](https://windcheck.net/).
Windcheck is a planning aid, not a source for operational flight decisions.

This project is a local METAR history analysis tool. It fetches historical
aviation weather observations into a SQLite database, resolves runway geometry
from OurAirports data, and serves a small Flask web app for analyzing weather
by local hour of day in a chosen time zone (the device's zone, Pacific when it
can't be detected).

The core use case is practical flight planning: pick an airport, date window,
hours measured, runway mode, and personal minimums, then compare historical
patterns with near-term model guidance for wind, ceiling, visibility, crosswind,
and density altitude.

## License

Windcheck's original source code is licensed under the
[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for original-author
attribution. Third-party code and data retain their own terms; see
[Third-party notices](THIRD_PARTY_NOTICES.md) and [Data Sources](#data-sources).

## Project Files

`metar.py` is the backend, database layer, data fetcher, METAR parser, stats
engine, Flask API, and CLI entry point.

`templates/index.html` is the whole web UI: controls, tables, charts, responsive
CSS, hover readouts, timeline filtering, raw observation rendering, and API
client code.

`test_date_window.py` is the main regression suite. It probes backend API
correctness and drives the frontend with Playwright.

`test_timezone.py` covers the time zone setting: backend hour buckets and
day edges per zone (`python3 test_timezone.py`) and the settings cog in
Playwright (`--url ... --out ...`).

`test_history_range.py` covers the Historical range bar (‹ 30D 90D ALL CACHED ›
plus dates, which moved out of the input panel) and the forecast "which
forecast to trust" tray. `test_phone_web.py` pins phone-browser behaviour found
on iOS Safari and Android Chrome: no input zoom, keyboard Go, swipes over charts
scroll the page, readable chart text, table headers and sticky row labels, touch
target sizes, and the table-time picker's Back and landscape scrolling. Both take
`--url`.

`responsive_test.py` is the screenshot-based responsive design harness. It
captures the app across the 10 standard viewports in Overview and All Historical
modes.

`requirements.txt` lists the Python dependencies:

```text
flask>=3.0
requests>=2.31
beautifulsoup4>=4.12
gunicorn>=22.0
```

`metar.db` is the local SQLite database. It is generated and updated by the app.

`VERSION` contains the app's semantic version. The UI is currently `1.4.0`; click
the `WINDCHECK` mark in the header to reveal the version and git hash.

Runway labels use **magnetic degrees (`°M`)** from the packaged FAA NASR
2026-10-29 snapshot (`runway_headings.json`). The generator subtracts the
airport's published east-positive magnetic variation from each runway end's
FAA true alignment. KSMO runway 21 therefore displays **212°M**. If the FAA
fields are missing or the airport is outside this dataset, the label shows
only the runway ID, rather than guessing a magnetic bearing. Older cached
responses without the new field also show only the runway ID until refreshed.

API runway objects keep `heading_deg` as the existing **true** bearing for
METAR/forecast crosswind calculations and add nullable `heading_magnetic_deg`
for display only. Custom heading inputs remain explicitly **true (`°T`)**.
Web and native clients must never send the magnetic display value as a
crosswind input. Native source is updated too; an already-installed native
build requires a new app release to receive the label change.

To refresh the display snapshot, download the applicable published FAA NASR
APT CSV ZIP, then run (the FAA URL uses abbreviated month names):

```bash
curl --fail --output /tmp/windcheck-APT.zip \
  https://nfdc.faa.gov/webContent/28DaySub/extra/29_Oct_2026_APT_CSV.zip
python3 scripts/update_runway_headings.py --zip /tmp/windcheck-APT.zip \
  --cycle 2026-10-29 --output runway_headings.json
python3 -m unittest test_runway_headings -v
```

Review the cycle-specific fixture assertions when changing the FAA snapshot.
No runtime FAA request, database migration, or calculation change is needed.

`screenshots/` contains responsive and scenario screenshots created by the test
and verification workflow.

`archive/` contains older one-off exports and earlier experimental scripts.
The active app does not depend on these files.

## Quick Start

Install dependencies:

```bash
cd windcheck
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m pip install playwright
python3 -m playwright install chromium
```

Start the local app:

```bash
python3 metar.py serve --port 5050
```

Open:

```text
http://127.0.0.1:5050/
```

Fetch history for an airport:

```bash
python3 metar.py fetch KMYF --days 60
```

Fetch a specific date range:

```bash
python3 metar.py fetch KMYF --start 2026-01-01 --end 2026-05-05
```

Inspect cached airport coverage:

```bash
python3 metar.py info KMYF
```

Show runway data:

```bash
python3 metar.py runways KMYF
```

Run the main regression suite against a running server:

```bash
python3 test_date_window.py --url http://127.0.0.1:5050
```

Run the responsive screenshot sweep:

```bash
python3 responsive_test.py --url http://127.0.0.1:5050/ --out screenshots/after --view both --hover
```

The [Flutter client](mobile/README.md) runs on iOS and Android and can
point to your local server. There is no bundled observation cache; startup
creates an empty database.

## Data Sources

The software and upstream datasets have separate terms. Runway headings are
derived from the [FAA NASR subscription](https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/),
and [OurAirports airport and runway data](https://ourairports.com/data/) are
public domain. Forecast model data via [Open-Meteo](https://open-meteo.com/en/license)
are CC BY 4.0 and are normalized for display in Windcheck. Open-Meteo's
[free hosted API](https://open-meteo.com/en/terms) is for non-commercial use;
the software does not grant access rights to third-party services.

The app fetches METAR history month-by-month. It tries Ogimet first, then falls
back to Iowa State Mesonet when Ogimet does not yield observations for a month.

Ogimet endpoint:

```text
https://www.ogimet.com/display_metars2.php
```

Iowa State Mesonet endpoint:

```text
https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
```

Runway and airport metadata come from OurAirports:

```text
https://davidmegginson.github.io/ourairports-data/runways.csv
https://davidmegginson.github.io/ourairports-data/airports.csv
```

The fetcher is intentionally polite. Ogimet requests are separated by
`OGIMET_REQUEST_GAP_SEC = 1.5`. Iowa State requests are separated by
`IASTATE_REQUEST_GAP_SEC = 6.0` because that source rate-limits more
aggressively.

## Database

The SQLite database path is:

```text
metar.db
```

`metar.py` initializes the database automatically through `db_init()`.

The primary tables are:

`metar`

Stores parsed observations. The primary key is `(airport, obs_time_utc)`.
Important columns include raw report text, report type, wind direction, VRB
flag, wind speed, gust, visibility, ceiling, cloud layers, temperature, dew
point, and altimeter.

`fetched_months`

Tracks which airport/month combinations have already been fetched and which
source produced the data. This avoids repeatedly pulling the same month unless
`--refetch` is used. The current month is allowed to refresh because new METARs
may still arrive.

`runway_ends`

Stores runway-end identifiers, headings, lengths, surfaces, and closed flags
from OurAirports.

`airports`

Stores airport identity, name, coordinates, elevation, country/region, local
code, GPS code, and related metadata from OurAirports.

`meta_kv`

Stores lightweight migration and metadata flags. One current use is recording
the one-time migration of old 3-character U.S. airport keys to normalized
4-character ICAO-style keys.

## Airport Code Normalization

The app normalizes airport codes with `normalize_airport()`.

For U.S. three-character codes like `L22`, the app uses `KL22` internally.
This keeps METAR cache keys consistent with common ICAO-style station IDs while
still letting the user type FAA-style local identifiers.

The UI tries to preserve what the user typed in visible labels when appropriate,
especially during nearest-station substitution flows.

Metadata and runway lookups resolve the original OurAirports identifier, which
may omit the synthetic `K` prefix (for example, `L78` / Jacumba). Forecasts use
that airport's coordinates independently of METAR history availability. An
empty historical result must not hide the forecast section. Station LAMP and
standalone GFS MOS are different products; Windcheck currently ingests station
LAMP, not a separate GFS MOS feed or ForeFlight's MOS product.
Forecast guidance starts independently while historical observations load.

## Fetching Flow

The central fetch function is `fetch_airport_iter()`.

It:

1. Normalizes the airport code.
2. Initializes the database.
3. Clamps the requested end date to today.
4. Breaks the range into calendar months.
5. Skips already-fetched non-current months unless `refetch=True`.
6. Tries Ogimet for each month.
7. Falls back to Iowa State Mesonet if Ogimet produces no usable observations.
8. Parses reports with `parse_metar_text()` and `parse_metar_line()`.
9. Upserts observations into `metar`.
10. Records the month result in `fetched_months`.
11. Yields structured progress events for the UI and CLI.

`fetch_airport()` is the synchronous CLI wrapper around the generator.

`fetch_recent()` is a convenience wrapper that fetches the last N days.

The UI uses `/api/fetch_stream` for refresh/get-more-history so it can show
month-by-month progress while the backend works.

## METAR Parsing

The parser is intentionally focused on the fields this app needs:

`wind_dir`

An integer compass direction from `DDDSSKT` tokens. VRB winds set `is_vrb=True`
and leave `wind_dir=None`.

`wind_speed_kt`

Wind speed in knots.

`gust_kt`

Optional gust speed from `Gxx` wind tokens.

`vis_sm`

Visibility in statute miles. The parser supports values like `10SM`, `1/2SM`,
and split whole-plus-fraction forms such as `1 1/2SM`.

`ceiling_ft`

The lowest BKN, OVC, or VV layer, converted from hundreds of feet to feet AGL.
FEW and SCT layers are stored in `cloud_layers` but do not count as ceilings.

`temp_c`, `dew_c`, `altimeter_inhg`

Parsed when present.

The parser skips tokens such as `AUTO`, `COR`, and `NIL` because they are not
used in the statistics.

## Runway Data And Active Runway Logic

Runway data comes from `ensure_runway_data()` and `get_runways()`.

In auto mode, each observation with a usable wind direction is assigned to the
runway end whose heading is closest to the wind direction. This is handled by
`_pick_end_for_obs()`.

The app tracks runway use in two ways:

`runways_used`

A ranked list of runway ends used across the selected window. The first item is
the primary runway end for that window.

`active_end_ident`

The active runway end for a specific hour bucket or raw observation.

`is_mixed`

For Overview rows, this means more than one runway end was active among the
observations in that hour bucket.

Manual runway mode is also supported. If the user selects a specific runway
heading, all crosswind calculations use that heading, and the active runway
readout reflects the selected runway end when the heading matches known runway
data.

If an airport has no usable runway data, crosswind is undefined. The app does
not invent a runway heading from the wind, because that would make crosswind
look artificially safe.

## Crosswind Logic

Crosswind is computed by:

```text
abs(sin(wind_direction - runway_heading)) * wind_speed
```

The implementation is `crosswind(wind_dir, wind_speed, runway_heading)`.

Important rules:

VRB winds are excluded from crosswind calculations because they have no defined
direction.

Missing wind direction is excluded from crosswind calculations.

Missing runway data means crosswind is `None`, not zero.

Missing crosswind is not treated as a personal-minimums breach.

In auto runway mode, each observation is computed against the runway end chosen
for that observation.

For two ends of the same runway that are 180 degrees apart, the crosswind
magnitude is the same either way, so the tie goes to the end facing into the
wind (`test_runway_end.py`). The active-end tracking matters because it
explains which runway end the wind favored.

## Personal Minimums

The UI exposes three thresholds:

`vis_min_sm`

Default `5`. An observation breaches visibility minimums when reported
visibility is below this value.

`ceiling_min_ft`

Default `8000`. An observation breaches ceiling minimums when the lowest
BKN/OVC/VV layer is below this value.

`xwind_max_kt`

Default `12`. An observation breaches crosswind minimums when absolute
crosswind exceeds this value.

The combined `beneath minimums` count increments when any of these conditions
is true for an observation:

```text
visibility < vis_min_sm
ceiling < ceiling_min_ft
abs(crosswind) > xwind_max_kt
```

If crosswind is undefined because of VRB, missing wind direction, or missing
runway data, that missing value is not a crosswind breach.

## Statistics API

`/api/stats`

Returns per-local-hour aggregate rows for the selected hour set and date
window. This powers the Overview view.

Accepted query parameters:

`airport`

Required. FAA or ICAO-style airport code.

`runway_heading`

`auto`, empty, `none`, or an integer heading.

`runways_airport`

Optional. Uses weather observations from `airport` but runway geometry from
this airport. This is important for small airports without METAR feeds where
the app substitutes the nearest reporting station for weather but keeps the
requested airport's runway geometry.

`hours`

Comma-separated local hours from `0` through `23`, in the `tz` zone.

`date_from`, `date_to`

Local-date window boundaries in `YYYY-MM-DD` format, in the `tz` zone.

`tz`

Optional IANA time zone name (for example `America/New_York` or `UTC`).
Hour buckets, date-window edges and every `*_pt` time string use this zone.
Unknown or absent values fall back to `America/Los_Angeles`. Field names keep
the historical `_pt` suffix for compatibility; every response echoes the zone
used as `tz`. `/api/info`, `/api/raw`, `/api/forecast` and
`/api/fetch_stream` accept the same parameter.

`vis_min_sm`, `ceiling_min_ft`, `xwind_max_kt`

Personal-minimum thresholds.

Each stats row includes:

`hour_pt`

Local hour bucket (in the `tz` zone).

`obs_count`

Number of matching observations for that hour.

`avg_dir_deg`

Wind-speed-weighted circular mean direction. VRB and zero-wind samples are
skipped.

`xwind`, `wind_speed`, `gust`, `ceiling`

Stats blocks containing count, min, max, mean, and population standard
deviation.

`ct_min_breached`, `vis_breached`, `ceiling_breached`, `xwind_breached`

Counts of observations that breached combined or individual minimums.

`active_end_ident`, `is_mixed`, `end_counts`

Runway-end attribution information for the hour bucket.

Top-level stats response fields include selected runway heading, selected
runway identifier, runway-auto status, primary runway identifier, runway usage,
personal minimums, date window, total observations in the window, total cached
observations, and runway metadata.

## Raw Observation API

`/api/raw`

Returns individual observations for the All Historical view.

It uses the same query parser as `/api/stats`, so the two endpoints stay
aligned on airport, runway mode, hours, date window, runway-frame airport, and
personal minimums.

The raw response includes:

`rows`

Observation rows matching the selected hour filter.

`timeline`

Chronological observations for all 24 hours, capped to the most recent
`TIMELINE_MAX_DAYS = 90` days. This powers the All Historical time-series charts.

`timeline_from`, `timeline_to`, `timeline_max_days`

The range and cap metadata for the timeline charts.

`returned`, `total_obs`, `truncated`, `limit`

Raw table payload size metadata. `RAW_OBS_LIMIT = 5000`.

The All Historical table respects the selected hour filter. The timeline charts
intentionally use all 24 hours so diurnal patterns remain visible.

## Forecast API

`/api/forecast?airport=KAVX`

Returns the available model guidance horizons for the requested airport. The
route also returns one comparable table row per model at the backend's default
next-11a local checkpoint, or at `target_pt` if the caller supplies one. The
route uses exact airport metadata from OurAirports. It does not use a nearest
METAR station trick for density altitude.

Forecast sources:

- Same-airport TAF from NOAA Aviation Weather Center, when a TAF exists.
- Station LAMP from NOAA/MDL, when the airport appears in the LAMP bulletin.
- NWS gridpoint guidance from `api.weather.gov`.
- HRRR, NBM, NAM, and GFS fields through Open-Meteo's free NOAA model wrapper.

The response contains:

`models`

One entry per forecast source, including status, missing reason when unavailable,
source/range/spatial/accuracy metadata, and chartable wind, crosswind, ceiling,
visibility, and density-altitude time-series points when those fields exist.

`target_rows`

One table row per model for the default next-11a local checkpoint, or for the
selected target time when `target_pt` is supplied by an API caller. If a model is
outside its horizon or lacks a field, the row keeps the model visible and
explains why the point is missing.

`tool_info`

The expandable model guide rendered under the forecast table.

In the Overview UI, forecast charts always show each source's full available
horizon. The forecast table has its own custom monochrome local date-time
picker and samples the nearest model point to that selected time without
refetching or trimming the charts. The table-time picker lives in the forecast
table header and re-samples automatically when the selected time changes; the
custom table-time picker is a compact anchored popover and does not scroll the
page when opened. Recent forecast payloads are cached per airport, so returning
to a previously loaded airport paints the last charts/table immediately while
the status cell shows the live refresh. The
compact refresh icon beside the status only reloads model guidance. Forecast
charts use denser responsive rows on larger viewports, support horizontal
drag-to-zoom with a compact zoom-out reset icon; hover readouts wrap inside the
SVG so they do not clip at narrower chart widths. Model
legend keys use distinct line patterns while keeping the plotted chart lines
minimal. Forecast chart legends are interactive: all plottable series are on
by default, clicking a legend item hides that source for that chart, and the
muted legend item can be clicked again to restore it. Legends only list sources
with values available for that chart and selected range; a model may appear in
wind/crosswind but not ceiling if the feed returns no numeric ceiling or
cloud-base field. Forecast table cells use `not returned` for missing values so
numeric `0` remains unambiguous. Model confidence and caveats live in the
expandable forecast model guide instead of the compact table. The forecast
control strip uses a shared label/control grid so the status readout and
refresh icon stay flush on desktop and tablet widths, then stack on phone
widths.

Airport runs render cached history first whenever a usable cache exists,
including cached nearest-station substitutes for non-METAR fields such as
`L22`, then refresh the history stream and redraw when new rows arrive.

## Other API Endpoints

`GET /`

Serves the app shell. The response disables browser caching with `no-store` so
local UI edits are not hidden by stale HTML/JS.

`GET /api/info?airport=KMYF`

Returns database coverage, airport metadata, runway metadata, cached date range,
and nearest-station suggestions when no METAR data exists.

It also returns `recent_contiguous_from` and `recent_contiguous_to`, which power
the All cached date preset. This range is the most recent contiguous monthly
data block ending at the latest cached observation, so an older orphan month
separated by a data gap does not expand the query window misleadingly.

`GET /api/runways?airport=KMYF`

Returns known runway ends for the airport.

`POST /api/fetch`

Fetches history synchronously and returns JSON.

`POST /api/fetch_stream`

Fetches history and streams newline-delimited JSON progress events.

## Web UI: Input Info Panel

The left-side Input info panel controls the analysis. The settings cog at the
right of its header slides open a tray with the time zone picker: Auto (this
device's zone, Pacific when it can't be detected), common US zones and UTC, then
every IANA zone. The choice is stored in `localStorage` (`metar.timeZone`);
changing it re-runs the query and rebuilds the forecast panel in the new zone.

Airport input accepts FAA or ICAO-style airport codes.

Runway selector defaults to Auto by wind. It is populated from runway metadata,
shows runway ID plus heading, and also includes Custom heading. Changing the
selector after a query refreshes the xwind-dependent stats in place.

Hours measured allows selecting any subset of hours. The default is `10` through
`18`, with buttons for All, `10–18`, and Clear.

Date window defaults to the last 30 days in the chosen zone. Presets:

`30d`

Sets from/to to the last 30 days.

`90d`

Sets from/to to the last 90 days.

`All cached`

Uses the longest contiguous cached date range ending at the most recent
observation. It intentionally skips older data separated by monthly gaps.

Personal minimums lets the user set visibility, ceiling, and max crosswind.

Available data shows cached observation count and cached range for the selected
airport.

The primary action is a single button. It reads `Run query` before cache is
known, then changes to `Refresh / get more history` when cached observations
exist for the selected airport. In the cached state, clicking it refreshes the
streamed history cache first, then redraws historical stats and forecast
guidance. On page load, the UI restores the last selected airport and runs that
same refresh/query flow automatically.

History-fetch endpoints are protected with invisible reCAPTCHA on public hosts.
The local `recaptcha` file is intentionally not committed; it contains the
private secret key on the first non-empty line and the public site key on the
second non-empty line.

## Public Crawler Pages

`/privacy/` serves the Windcheck website/mobile privacy policy, with `/privacy`
redirecting to it. It is a direct-URL-only page: no homepage/about navigation,
sitemap entry or llms.txt listing, and both HTML and HTTP `noindex` directives.
Its provider/logging disclosures are separate from Apple's App Privacy answers.

The root page includes canonical, Open Graph, Twitter card, and JSON-LD
metadata. `/about` is a crawler-readable explanation page linked from the app
header, with a back arrow to the app. `/robots.txt`, `/sitemap.xml`, and
`/llms.txt` are served directly by Flask.

## Web UI: Overview View

Overview shows available forecast guidance first, followed by one historical row
per selected hour.

The table includes:

Hour PT, count, average wind direction, crosswind stats, wind speed stats, gust
stats, gust occurrence percentage, ceiling stats, and beneath-minimums counts.

Per-measure `ct` columns were intentionally removed because the leftmost Count
column already gives the hour-bucket observation count. For crosswind, VRB and
missing-direction observations are excluded from xwind stats, but still count
toward the hour's overall observation count.

Gust averages use only observations that report a gust value; missing gusts are
not treated as `0 kt`. The Gust `%` column shows reported-gust observations
divided by total observations for that hour bucket.

The crosswind group includes an `active` sub-column. A `+` suffix means the hour
bucket was mixed across more than one active runway end.

Responsive behavior:

On wide screens, all table columns are shown.

Below `900px`, verbose min/max sub-columns are hidden so the table focuses on
mean, standard deviation, active runway, and beneath-minimums counts.

Below `600px`, Avg dir is also hidden because the phone layout cannot fit that
many columns without excessive horizontal scrolling.

The Overview charts show:

Wind speed by hour with gust as a dashed overlay. The hover readout includes the
gust occurrence percentage.

Crosswind by hour, with dot color indicating runway attribution.

Ceiling by hour.

Beneath-minimums percentage by cause.

Line charts show a top-of-chart hover readout instead of a floating tooltip, so
the readout does not obscure data.

## Web UI: All Historical View

All Historical shows raw observations plus chronological charts.

The raw table includes time, wind, gust, visibility, ceiling, crosswind, and
beneath-minimums flag.

The timeline filter above the charts lets the user narrow the visible chart
range within the already-loaded timeline payload. This filtering is client-side
and does not issue another API request. The filter includes monochrome date
fields plus `7d`, `30d`, and `90d` presets that reuse the loaded raw payload.

Timeline charts:

Wind speed over time, with gust as a dashed overlay. Hover also shows wind
direction, including `VRB` when applicable.

Crosswind over time, with non-primary runway observations highlighted in blue.
Hover shows active runway and labels non-primary active ends as `non-primary`.

Ceiling over time, with compact k-suffix y-axis labels when values reach the
thousands.

On wide desktop viewports, All Historical charts use a two-column scan layout so
users can see complete chart rows instead of one oversized chart plus part of
the next. On narrow viewports under `760px`, each timeline chart splits into two
stacked date panels. This avoids compressing a long horizontal time series into
an unreadable strip on phones.

All Historical charts support horizontal drag-to-zoom across the shared timeline
range. A compact zoom-out icon appears in the timeline filter while zoom is
active and resets the charts without changing scroll position.

## Chart Rendering

The app uses custom SVG rendering instead of a charting library.

`renderLineChart()`

Renders the Overview hour-bucket line charts. It supports mean lines, optional
SEM error bars, optional dashed overlays, x/y axes, compact y-axis labels, and
hover readouts.

`renderBarChart()`

Renders the beneath-minimums percentage-by-cause bars.

`renderTimeSeriesChart()`

Renders All Historical chronological charts. It supports dashed overlays, point dots,
secondary runway dot classes, pinned x-axis ranges for split panels, compact
y-axis labels, and hover readouts.

`_attachChartHover()`

Shared hover framework for both Overview and All Historical SVG charts. It creates a
crosshair and a pinned top readout row.

`attachTsChartHover()`

Time-series hover resolver. It interpolates the y-value at the mouse time,
looks up the nearest actual observation for metadata, and displays wind
direction or active runway when requested.

`attachCategoryLineHover()`

Hour-category hover resolver for Overview line charts.

`_appendTimeSeriesPanels()`

Splits a long timeline into multiple stacked SVG panels at narrow viewports.

## Dot Color Semantics

Crosswind dot color is intentionally asymmetric:

Primary runway dots use the default foreground color.

Non-primary runway dots use the blue accent color.

The reason is visual priority. Primary runway is the common case. Blue is
reserved for the less frequent event that should draw attention: observations
or hour buckets attributed to a non-primary runway end, or mixed buckets in the
Overview chart.

The connecting crosswind line remains the default foreground color. Only the
dots carry runway attribution color.

## Nearest Reporting Station Substitution

Some small airports do not have their own METAR feed but do have runway data.
When an airport has no cached METAR observations, `find_nearest_metar()` can
identify nearby stations with cached observations.

The important design split:

Weather observations can come from the substitute reporting station.

Runway geometry can remain tied to the originally requested airport through
`runways_airport`.

This allows a user to analyze, for example, wind from a nearby METAR station
against the runways of a small non-reporting field.

## Time Zones And Date Windows

The app presents and filters in one chosen time zone: the `tz` request
parameter, which the UI sets from its settings tray. Pacific remains the
default for API callers that omit it.

METAR observations are stored in UTC.

`_obs_query()` converts UTC observation times into local date/hour in that zone
for filtering and grouping. Tests specifically cover the local-midnight edge
case where naive UTC filtering would incorrectly drop late-afternoon
observations from single-day windows, and `test_timezone.py` checks the same
fixture bucketed in Pacific, Eastern and UTC.

In the browser, `tzParts`, `tzWallDate` and `tzMs` do all date math in the
chosen zone, so results are correct even when the device sits in another zone.

## All Cached Date Preset

The All cached preset is not simply first cached observation through last cached
observation.

Instead, `/api/info` computes the longest recent contiguous monthly block that
includes the most recent observation. If the cache has older isolated months
separated by gaps, those older months are ignored for this preset.

This prevents a click on All cached from selecting a range that visually looks
continuous but actually contains a missing season or missing month gap.

For KMYF during recent verification, the database had older cached data back to
`2024-11-01`, but gaps in the cache meant the recent contiguous block was
`2026-01-01` through `2026-05-04`.

## Responsive Design

The layout is tested across these 10 viewports:

```text
phone-narrow           320x568
phone-common           360x800
phone-modern           390x844
phone-large-tall       430x932
phone-landscape-short  844x390
tablet-portrait        768x1024
laptop-common          1366x768
laptop-modern          1512x982
desktop-common         1920x1080
desktop-ultrawide      2560x1080
```

The responsive harness checks document overflow, chart widths, SVG overflow,
and table scroll state, then captures screenshots.

Important responsive choices:

Below `1100px`, the left Query panel stacks above results so charts and tables
can use the full viewport width.

Below `900px`, Overview hides min/max sub-columns and tightens table spacing.

Below `600px`, Overview also hides Avg dir.

Above `1500px`, All Historical timeline charts switch to a two-column layout for
better scan density. Below `760px`, All Historical timeline charts split into two
stacked panels.

Y-axis labels use compact suffixes for large values to avoid text overlapping
the plot area.

Phone-width Overview tables may still require horizontal table scrolling
because even the reduced numeric table is wider than a 320-430px viewport. This
is intentional; the alternative would be removing too much information or
turning each hour into a much taller card layout.

## Testing

`test_date_window.py` currently covers backend and frontend behavior.

Backend coverage includes:

Date-window filtering.

Local-date boundary correctness in any zone.

Whole-cache vs windowed observation counts.

Ceiling stats block shape and consistency.

Runway attribution and runway-frame substitution.

Airports with no runway data.

Recent contiguous cached range.

Frontend coverage includes:

Initial UI state.

Date presets.

All cached preset behavior.

Header bar values.

View toggle behavior.

Overview table column structure.

Removal of redundant `ct` columns.

Active runway cells.

Chart presence and SVG structure.

SEM bars and legend copy.

Gust overlays.

All Historical raw table and timeline charts.

Timeline client-side filtering.

Hover readouts.

Crosswind color semantics.

Compact y-axis labels.

Narrow-viewport split panels.

Nearest-station substitution.

No-runway-data handling.

Run it with:

```bash
python3 test_date_window.py --url http://127.0.0.1:5050
```

`responsive_test.py` captures screenshot evidence and checks overflow:

```bash
python3 responsive_test.py --url http://127.0.0.1:5050/ --out screenshots/after --view both --hover
```

## Operational Notes

Run only one local app server for this project at a time unless intentionally
comparing versions. The usual dev server is:

```text
http://127.0.0.1:5050/
```

Check whether port 5050 is already in use:

```bash
lsof -i :5050 -sTCP:LISTEN
```

If you need to stop the current server:

```bash
lsof -i :5050 -sTCP:LISTEN -t | xargs -I{} kill {}
```

If tests need a browser:

```bash
python3 -m playwright install chromium
```

The Flask app began as a local development tool. The current public deployment
is a lightweight personal app behind nginx, not an authenticated or generally
hardened internet-facing product.

## Known Limits And Design Tradeoffs

METAR parsing is intentionally focused. It is not a complete implementation of
every possible METAR token.

The timeline chart is capped to 90 days to keep SVG rendering fast and readable.
The raw table is capped at 5000 returned observations. The All Historical
timeline presets and drag zoom are client-side refinements of the already-loaded
timeline payload, not additional history fetches.

Ceiling stats exclude clear-sky observations because they do not report a
ceiling. The hour's overall observation count still includes those observations.

Crosswind stats exclude VRB and missing-direction observations. The hour's
overall observation count still includes them.

Missing runway data means crosswind is undefined, not zero.

The app uses sample standard deviation in `_stats_block()` (`stdev`) for
per-bucket descriptive statistics when at least two observations exist; single
observation buckets report `0.0`.

SEM error bars are shown on Overview charts as subtle uncertainty markers, but
the legend intentionally does not mention SEM because the UI should stay pilot
readable rather than statistically verbose.

## Common Workflows

Analyze KMYF with defaults:

1. Start the server.
2. Open `http://127.0.0.1:5050/`.
3. Enter `KMYF`.
4. Leave runway as Auto by wind.
5. Leave hours measured at `10–18`.
6. Leave date window at the last 30 days.
7. Leave personal minimums at `vis < 5`, `ceiling < 8000`, `xwind max 12`.
8. Click Run query.

Use a small airport's runways with nearby METAR weather:

1. Enter the small airport code.
2. If it has no METAR observations, use the nearest reporting station suggested
   by the UI.
3. Keep runway geometry tied to the requested airport via the runway-frame
   substitution flow.
4. Compare crosswind by active runway end.

Investigate a long cached period:

1. Click All cached.
2. Run query.
3. Switch to All Historical.
4. Use the timeline filter, `7d` / `30d` / `90d` presets, or drag-to-zoom to
   narrow within the loaded 90-day chart payload.

Validate changes:

1. Run `python3 test_date_window.py --url http://127.0.0.1:5050`.
2. Run `python3 responsive_test.py --url http://127.0.0.1:5050/ --out screenshots/after --view both --hover`.
3. Inspect screenshots under `screenshots/after`.

## Mental Model

The app answers this question:

```text
For this airport, runway context, date window, and set of local hours,
how often do the observations look flyable under my minimums, and why?
```

The backend is responsible for factual transformations:

Fetch history.

Parse METARs.

Store observations.

Resolve airport and runway metadata.

Compute hour-bucketed statistics.

Emit raw observation rows and chart timelines.

The frontend is responsible for interaction and explanation:

Choose inputs.

Render Overview aggregates.

Render All Historical observations.

Show runway attribution clearly.

Keep hover readouts out of the data path.

Keep long time series readable on small screens.

Keep implementation details out of user-facing copy.
