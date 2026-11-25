"""Time zone setting: hour buckets, date windows and labels follow ?tz=.

    python3 test_timezone.py                              # backend checks
    python3 test_timezone.py --url http://127.0.0.1:5050 --out /tmp/tz   # UI
"""

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import metar

# 2026-04-05 17:53 PDT = 20:53 EDT; 23:53 PDT = Apr 6 02:53 EDT;
# 03:53 PDT = 06:53 EDT.
OBS = ["2026-04-06T00:53:00Z", "2026-04-06T06:53:00Z", "2026-04-05T10:53:00Z"]


class TimezoneApiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windcheck-tz-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        db = patch.object(metar, "DB_PATH", Path(tmp.name) / "metar.db")
        db.start()
        self.addCleanup(db.stop)
        metar.db_init()
        with closing(metar.db_connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO meta_kv (k, v) VALUES (?, ?)",
                [("airports_loaded_at", "fixture"), ("runways_loaded_at", "fixture")])
            conn.executemany(
                "INSERT INTO metar (airport, obs_time_utc, raw, wind_dir, "
                "wind_speed_kt, vis_sm) VALUES ('KMYF', ?, 'fixture', 270, 8, 10)",
                [(t,) for t in OBS])
        self.client = metar.app.test_client()

    def hours(self, tz=None):
        q = "/api/stats?airport=KMYF&date_from=2026-04-05&date_to=2026-04-05"
        data = self.client.get(q + (f"&tz={tz}" if tz else "")).get_json()
        return data["tz"], sorted(r["hour_pt"] for r in data["rows"] if r["obs_count"])

    def test_default_is_pacific(self):
        self.assertEqual(self.hours(), ("America/Los_Angeles", [3, 17, 23]))

    def test_zone_moves_buckets_and_day_edges(self):
        # 02:53 EDT falls on Apr 6, outside the one-day window.
        self.assertEqual(self.hours("America/New_York"), ("America/New_York", [6, 20]))
        self.assertEqual(self.hours("UTC"), ("UTC", [10]))

    def test_unknown_zone_falls_back_to_pacific(self):
        for bad in ("Bogus/Zone", "../../etc/passwd", ""):
            self.assertEqual(self.hours(bad)[0], "America/Los_Angeles")

    def test_raw_and_info_label_in_zone(self):
        raw = self.client.get("/api/raw?airport=KMYF&date_from=2026-04-05"
                              "&date_to=2026-04-05&tz=UTC").get_json()
        self.assertEqual([r["time_pt"] for r in raw["rows"]], ["2026-04-05 10:53"])
        info = self.client.get("/api/info?airport=KMYF&tz=America/New_York").get_json()
        self.assertTrue(info["first_obs_pt"].endswith("EDT"), info["first_obs_pt"])

    def test_forecast_default_target_is_11am_local(self):
        with metar.app.test_request_context("/?tz=Asia/Kolkata"):
            metar.app.preprocess_request()
            target = metar._parse_target_pt(None)
        self.assertEqual((target.hour, target.tzinfo.key), (11, "Asia/Kolkata"))


async def run_browser(base, out):
    """Settings cog, zone persistence and zone-aware requests/labels."""
    import asyncio
    import json
    import urllib.request
    from playwright.async_api import async_playwright

    def api(path):
        with urllib.request.urlopen(base + path, timeout=120) as response:
            return json.load(response)

    stats_utc = api("/api/stats?airport=KMYF&hours=10,11,12&tz=UTC")
    forecast = {"airport": "KMYF", "models": [], "target_rows": [],
                "tool_info": [], "tz": "UTC", "target_pt": "2026-10-26T11:00"}
    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for width, height in [(320, 568), (390, 844), (1366, 768), (1920, 1080)]:
                context = await browser.new_context(
                    viewport={"width": width, "height": height},
                    timezone_id="America/New_York")
                try:
                    page = await context.new_page()
                    errors, seen, slow = [], [], {"on": False}
                    release = asyncio.Event()   # history refresh held open, like a slow Ogimet pull
                    page.on("pageerror", lambda e: errors.append(str(e)))

                    async def route_api(route):
                        url = route.request.url
                        seen.append(url)
                        if "/api/fetch_stream" in url:   # never write history
                            if slow["on"]:
                                await release.wait()
                            await route.fulfill(content_type="application/x-ndjson",
                                                body=json.dumps({"type": "done"}) + "\n")
                        elif "/api/stats" in url and slow["on"]:
                            await asyncio.sleep(1.5)  # keep the first query in flight
                            await route.continue_()
                        elif "/api/forecast" in url:
                            tz = "UTC" if "tz=UTC" in url else "America/New_York"
                            await route.fulfill(content_type="application/json",
                                                body=json.dumps({**forecast, "tz": tz}))
                        else:
                            await route.continue_()

                    await page.route("**/api/**", route_api)
                    await page.goto(base, wait_until="domcontentloaded")
                    head = await page.inner_text(".panel h2")
                    assert head.strip().upper() == "INPUT INFO", head
                    labels = await page.locator(".panel .body > label").all_inner_texts()
                    assert "HOURS MEASURED" in [t.upper() for t in labels], labels
                    geo = await page.evaluate("""() => {
                      const h = document.querySelector('.panel h2').getBoundingClientRect();
                      const b = document.querySelector('#settings-button').getBoundingClientRect();
                      const p = document.querySelector('.panel').getBoundingClientRect();
                      return {hh: h.height, mid: Math.abs((b.top + b.bottom) / 2 - (h.top + h.bottom) / 2),
                              right: p.right - b.right};
                    }""")
                    assert geo["hh"] < 33 and geo["mid"] <= 1.5 and geo["right"] <= 8, geo

                    btn = page.locator("#settings-button")
                    pop = page.locator("#settings-pop")
                    label_y = (await page.locator("label[for=airport]").bounding_box())["y"]
                    await btn.click()
                    await page.wait_for_timeout(400)   # tray transition
                    tray = await pop.bounding_box()
                    assert tray["height"] > 60, tray
                    # Tray pushes the form down instead of covering it.
                    moved = (await page.locator("label[for=airport]").bounding_box())["y"] - label_y
                    assert abs(moved - tray["height"]) < 2, (moved, tray)
                    assert await btn.get_attribute("aria-expanded") == "true"
                    assert await page.input_value("#tz-select") == "auto"
                    auto_text = await page.locator("#tz-select option[value=auto]").inner_text()
                    assert "America/New_York" in auto_text, auto_text
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1")
                    # Nothing may stick out of the input panel (date row at 320 did).
                    escaped = await page.evaluate("""() => {
                      const b = document.querySelector('.panel').getBoundingClientRect();
                      return [...document.querySelectorAll('.panel input, .panel button, .panel .field-pair')]
                        .filter(e => e.getBoundingClientRect().width)
                        .filter(e => e.getBoundingClientRect().right > b.right + 0.5)
                        .map(e => e.id || e.className);
                    }""")
                    assert not escaped, escaped
                    await page.screenshot(path=str(out / f"{width}x{height}-settings.png"))
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(400)
                    assert (await pop.bounding_box())["height"] < 1
                    assert await page.evaluate("document.querySelector('#settings-pop').inert")
                    assert await page.evaluate("document.activeElement.id") == "settings-button"

                    # Switch zones while the first (Eastern) query is still in flight.
                    slow["on"] = True
                    await page.fill("#airport", "KMYF")
                    for _ in range(300):
                        if any("/api/stats" in u and "tz=America%2FNew_York" in u for u in seen):
                            break
                        await asyncio.sleep(0.05)
                    assert any("/api/stats" in u and "tz=America%2FNew_York" in u for u in seen), seen
                    seen.clear()
                    await btn.click()
                    async with page.expect_response(
                            lambda r: "/api/stats" in r.url and "tz=UTC" in r.url, timeout=15000) as resp:
                        await page.select_option("#tz-select", "UTC")
                    utc_url = (await resp.value).url
                    await page.wait_for_function(
                        "[...document.querySelectorAll('th')].some(t => t.textContent.trim() === 'Hour UTC')",
                        timeout=30000)
                    await page.wait_for_timeout(2500)  # the stale Eastern reply lands here
                    want = api(utc_url.split(base, 1)[1])["rows"][0]["obs_count"]
                    cells = await page.locator(".historical-section tbody tr").first.locator("td").all_inner_texts()
                    assert cells[1].strip() == str(want), (cells, want)
                    slow["on"] = False
                    release.set()
                    assert any("/api/forecast" in u and "tz=UTC" in u for u in seen), seen
                    assert "(UTC)" in await page.inner_text("#forecast-at-label")
                    assert await page.evaluate("localStorage.getItem('metar.timeZone')") == "UTC"
                    await page.screenshot(path=str(out / f"{width}x{height}-utc.png"), full_page=True)

                    seen.clear()
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_function(
                        "[...document.querySelectorAll('th')].some(t => t.textContent.trim() === 'Hour UTC')",
                        timeout=30000)
                    assert await page.input_value("#tz-select") == "UTC"
                    assert all("tz=UTC" in u for u in seen if "/api/stats" in u), seen
                    first = (await page.locator(".historical-section tbody td").first.inner_text()).strip()
                    hour = stats_utc["rows"][0]["hour_pt"]
                    assert first.startswith(f"{hour:02d}"), (first, hour)
                    assert not errors, errors
                    print(f"ok {width}x{height}")
                finally:
                    await context.close()
        finally:
            await browser.close()


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
