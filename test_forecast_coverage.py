"""Forecast coverage must not depend on an airport having METAR reports."""

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import metar


class AirportCoverageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windcheck-coverage-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        db = patch.object(metar, "DB_PATH", Path(tmp.name) / "metar.db")
        db.start()
        self.addCleanup(db.stop)
        metar.db_init()
        with closing(metar.db_connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO meta_kv (k, v) VALUES (?, ?)",
                [("airports_loaded_at", "fixture"),
                 ("runways_loaded_at", "fixture")],
            )
            conn.executemany(
                "INSERT INTO airports "
                "(ident,name,type,lat,lon,elev_ft,iso_country,local_code,gps_code) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [("L78", "Jacumba Airport", "small_airport", 32.616001,
                  -116.165001, 2844, "US", "L78", "L78"),
                 ("KL22", "Yucca Valley Airport", "small_airport", 34.1297,
                  -116.407, 3224, "US", "L22", "KL22"),
                 ("KMYF", "Montgomery Field", "medium_airport", 32.8157,
                  -117.14, 427, "US", "MYF", "KMYF")],
            )
            conn.executemany(
                "INSERT INTO runway_ends "
                "(airport,ident,heading_deg,length_ft,surface,closed) "
                "VALUES (?,?,?,?,?,0)",
                [("L78", "07", None, 2562, "ASPH"),
                 ("L78", "25", None, 2562, "ASPH"),
                 ("KL22", "06", 71.3, 4363, "ASPH")],
            )

    def test_local_identifier_survives_internal_normalization(self):
        for code in ("L78", "l78", "KL78"):
            with self.subTest(code=code):
                record = metar.get_airport_record(code)
                self.assertIsNotNone(record)
                self.assertEqual(record["ident"], "L78")
                self.assertAlmostEqual(record["lat"], 32.616001)

    def test_existing_icao_and_local_aliases_still_resolve(self):
        for code, ident in (("MYF", "KMYF"), ("KMYF", "KMYF"),
                            ("L22", "KL22"), ("KL22", "KL22")):
            with self.subTest(code=code):
                self.assertEqual(metar.get_airport_record(code)["ident"], ident)
        self.assertIsNone(metar.get_airport_record("EGZZ"))

    def test_recovered_local_alias_does_not_match_foreign_airport(self):
        with closing(metar.db_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO airports (ident,name,type,iso_country,local_code) "
                "VALUES ('X99','Foreign fixture','small_airport','CA','X99')"
            )
        self.assertIsNone(metar.get_airport_record("KX99"))

    def test_runways_use_metadata_identity_without_inventing_headings(self):
        for code in ("L78", "KL78"):
            with self.subTest(code=code):
                runways = metar.get_runways(code)
                self.assertEqual([r["ident"] for r in runways], ["07", "25"])
                self.assertTrue(all(r["heading_deg"] is None for r in runways))

    def test_info_has_coordinates_even_with_no_observations(self):
        with metar.app.test_client() as client:
            response = client.get("/api/info?airport=L78")
        self.assertEqual(response.status_code, 200)
        info = response.get_json()
        self.assertEqual(info["name"], "Jacumba Airport")
        self.assertEqual(info["obs_count"], 0)
        self.assertAlmostEqual(info["lon"], -116.165001)
        self.assertEqual(len(info["runways"]), 2)

    def test_forecast_api_does_not_require_metar_history(self):
        model = metar._forecast_model(
            "nws_grid", "NWS gridpoint", source="fixture", horizon="0-7d",
            spatial="airport coordinates", accuracy="fixture",
            points=[metar._forecast_point(
                metar._parse_target_pt(None), wind_speed_kt=9,
                wind_dir_deg=270, runways=[], elev_ft=2844)],
        )
        with patch.object(metar, "_taf_forecast", return_value=model), \
                patch.object(metar, "_lamp_forecast", return_value=model), \
                patch.object(metar, "_nws_grid_forecast", return_value=model) as grid, \
                patch.object(metar, "_open_meteo_forecasts", return_value=[]), \
                metar.app.test_client() as client:
            response = client.get("/api/forecast?airport=L78")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["name"], "Jacumba Airport")
        self.assertEqual(data["models"][0]["points"][0]["wind_speed_kt"], 9)
        self.assertAlmostEqual(grid.call_args.args[0]["lat"], 32.616001)

    def test_empty_taf_array_explains_missing_product(self):
        with patch.object(metar, "_safe_json", return_value=([], None, 200)):
            model = metar._taf_forecast("KL78", metar._parse_target_pt(None), [])
        self.assertEqual(model["status"], "missing")
        self.assertEqual(model["missing_reason"], "no same-airport TAF available")


async def run_browser(base, out):
    """Exercise empty-history UI with a real forecast payload and no feed writes."""
    import asyncio
    import json
    import urllib.parse
    import urllib.request
    from playwright.async_api import async_playwright

    def api(path):
        with urllib.request.urlopen(base + path, timeout=120) as response:
            return json.load(response)

    info = api("/api/info?airport=L78")
    forecast = api("/api/forecast?airport=L78")
    stats = api("/api/stats?airport=L78&hours=10,11,12")
    assert info["name"] == forecast["name"] == "Jacumba Airport"
    assert abs(forecast["lat"] - 32.616001) < 0.001
    assert any(m["status"] == "ok" and m["points"] for m in forecast["models"])
    # Isolate the original failure: zero observations, with no substitute.
    info.update(obs_count=0, nearest=[])
    stats.update(total_obs=0, total_obs_cached=0)
    for row in stats["rows"]:
        row["obs_count"] = 0
    out.mkdir(parents=True, exist_ok=True)
    viewports = [(320, 568), (360, 800), (390, 844), (430, 932),
                 (844, 390), (768, 1024), (1366, 768), (1512, 982),
                 (1920, 1080), (2560, 1080), (2560, 1440)]
    report = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for width, height in viewports:
                context = await browser.new_context(
                    viewport={"width": width, "height": height})
                try:
                    page = await context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    refresh_started = asyncio.Event()
                    refresh_release = asyncio.Event()
                    history_started = asyncio.Event()
                    history_release = asyncio.Event()
                    hold_refresh = False
                    substitute = False

                    async def route_api(route):
                        path = route.request.url.split("?")[0]
                        airport = urllib.parse.parse_qs(urllib.parse.urlsplit(
                            route.request.url).query).get("airport", [""])[0]
                        if path.endswith("/api/info"):
                            if substitute and airport == "KSAN":
                                data = {**info, "airport": "KSAN", "obs_count": 1}
                            elif substitute:
                                data = {**info, "nearest": [{
                                    "ident": "KSAN", "name": "San Diego International",
                                    "municipality": "San Diego", "distance_nm": 52.3,
                                    "compass": "W"}]}
                            else:
                                data = info
                        elif path.endswith("/api/stats"):
                            data = ({**stats, "airport": "KSAN",
                                     "runways_airport": "KL78"}
                                    if substitute else stats)
                        elif path.endswith("/api/forecast"):
                            if hold_refresh:
                                refresh_started.set()
                                await refresh_release.wait()
                            data = forecast
                        elif path.endswith("/api/fetch_stream"):
                            history_started.set()
                            await history_release.wait()
                            await route.fulfill(
                                content_type="application/x-ndjson",
                                body=json.dumps({"type": "done", "info": info,
                                    "summary": {"months_with_data": 0,
                                                "months_empty": 2,
                                                "rows_upserted": 0}}) + "\n")
                            return
                        else:
                            await route.continue_()
                            return
                        await route.fulfill(content_type="application/json",
                                            body=json.dumps(data))

                    await page.route("**/api/**", route_api)
                    await page.goto(base, wait_until="domcontentloaded")
                    await page.fill("#airport", "L78")
                    await asyncio.wait_for(history_started.wait(), timeout=10)
                    await page.wait_for_selector(".forecast-wind svg path.forecast-line")
                    assert "Loading history" in await page.inner_text(".historical-section")
                    history_release.set()
                    await page.wait_for_function(
                        "document.querySelector('.historical-section')?.textContent.includes('No METAR observations')")
                    await page.wait_for_function(
                        "document.querySelector('#forecast-status')?.textContent.startsWith('updated')")
                    assert "No METAR observations available for L78" in await page.inner_text(
                        ".historical-section")
                    assert await page.locator(".forecast-table tbody tr").count() == len(
                        forecast["models"])
                    assert await page.locator(".historical-section svg").count() == 0
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1")
                    await page.locator(".forecast-wind").scroll_into_view_if_needed()
                    await page.screenshot(path=str(out / f"{width}x{height}.png"))
                    hit = page.locator(".forecast-wind .ch-hit").first
                    await hit.hover(position={"x": 20, "y": 40})
                    fit = await page.evaluate("""() => {
                      const svg = document.querySelector('.forecast-wind svg');
                      const label = svg.querySelector('.readout text');
                      const r = label.getBBox(), box = svg.viewBox.baseVal;
                      return {text: label.textContent, fits: r.x >= 0 && r.y >= 0
                        && r.x + r.width <= box.width + 1
                        && r.y + r.height <= box.height + 1};
                    }""")
                    assert fit["text"] and fit["fits"], fit
                    await page.screenshot(path=str(out / f"{width}x{height}-hover.png"))
                    line_count = await page.locator(".forecast-line").count()
                    # Hold the refresh response; populated content must stay mounted.
                    hold_refresh = True
                    await page.get_by_role("button", name="Refresh forecast guidance").click()
                    await asyncio.wait_for(refresh_started.wait(), timeout=10)
                    assert await page.locator(".forecast-line").count() == line_count
                    assert await page.locator(".forecast-table tbody tr").count() == len(
                        forecast["models"])
                    assert (await page.inner_text("#forecast-status")).lower() == "loading..."
                    refresh_release.set()
                    await page.wait_for_function(
                        "document.querySelector('#forecast-status')?.textContent.startsWith('updated')")
                    hold_refresh = False
                    # Reload starts with the last successful snapshot, before new guidance.
                    hold_refresh = True
                    refresh_started.clear()
                    refresh_release.clear()
                    await page.reload(wait_until="domcontentloaded")
                    await asyncio.wait_for(refresh_started.wait(), timeout=15)
                    assert await page.locator(".forecast-line").count() == line_count
                    assert await page.locator(".forecast-table tbody tr").count() == len(
                        forecast["models"])
                    refresh_release.set()
                    await page.wait_for_function(
                        "document.querySelector('#forecast-status')?.textContent.startsWith('updated')")
                    hold_refresh = False
                    if width == 1366:
                        substitute = True
                        await page.fill("#airport", "L78")
                        await page.wait_for_selector(".substitute-banner")
                        banner = await page.inner_text(".substitute-banner")
                        assert "KSAN" in banner and "52.3 nm" in banner.lower(), banner
                        assert "L78" in await page.inner_text("#forecast-section .section-meta")
                    assert not errors, errors
                    report.append({"viewport": f"{width}x{height}", "status": "PASS",
                                   "hover": fit, "lines": line_count})
                    print(f"PASS {width}x{height}: independent forecast, empty history, hover, refresh, cached reload")
                finally:
                    refresh_release.set()
                    history_release.set()
                    await context.close()
        finally:
            await browser.close()
    (out / "results.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    import sys
    if "--url" in sys.argv:
        import argparse
        import asyncio
        parser = argparse.ArgumentParser()
        parser.add_argument("--url", required=True)
        parser.add_argument("--out", type=Path, required=True)
        args = parser.parse_args()
        asyncio.run(run_browser(args.url.rstrip("/"), args.out))
    else:
        unittest.main()
