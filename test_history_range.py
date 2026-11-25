"""History range bar and the forecast-models guide.

    python3 test_history_range.py                                   # model guide data
    python3 test_history_range.py --url http://127.0.0.1:5050       # browser checks
"""

import unittest

import metar


class ModelGuideData(unittest.TestCase):
    def test_every_model_says_when_to_use_it(self):
        ids = {t["id"]: t.get("use") for t in metar.FORECAST_TOOL_INFO}
        self.assertEqual(set(ids), {"taf", "lamp", "nws_grid", "hrrr", "nbm", "nam", "gfs"})
        self.assertTrue(all(ids.values()), ids)


async def run_browser(base):
    import asyncio
    import json
    from datetime import date, timedelta
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for width, height in [(320, 640), (390, 844), (1440, 900)]:
                ctx = await browser.new_context(viewport={"width": width, "height": height},
                                                timezone_id="America/Los_Angeles")
                try:
                    page = await ctx.new_page()
                    errors, stats = [], []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.on("request", lambda r: stats.append(r.url) if "/api/stats" in r.url else None)
                    await page.route("**/api/fetch_stream*", lambda r: r.fulfill(
                        content_type="application/x-ndjson", body=json.dumps({"type": "done", "summary": {}}) + "\n"))
                    await page.goto(base)
                    # The range is not in the input panel any more.
                    assert await page.locator(".panel #date_from").count() == 0
                    await page.fill("#airport", "KMYF")
                    await page.wait_for_selector(".historical-section .history-range", timeout=90_000)
                    await page.wait_for_function("!document.getElementById('run').disabled", timeout=90_000)
                    today = date.today().isoformat()
                    frm, to = await page.input_value("#date_from"), await page.input_value("#date_to")
                    assert to == today and frm == (date.today() - timedelta(days=30)).isoformat(), (frm, to)
                    assert await page.get_attribute("#dr-30", "aria-pressed") == "true"
                    assert await page.is_disabled("#dr-next"), "can't step past today"

                    # Step back one window: same length, ends the day before, re-queries.
                    back_to = (date.today() - timedelta(days=31)).isoformat()
                    async with page.expect_request(lambda r: "/api/stats" in r.url, timeout=60_000) as req:
                        await page.click("#dr-prev")
                    assert "date_to=" + back_to in (await req.value).url
                    await page.wait_for_function(f"document.querySelector('#date_to')?.value === '{back_to}'")
                    await page.wait_for_function("!document.getElementById('run').disabled", timeout=90_000)
                    assert await page.get_attribute("#dr-30", "aria-pressed") == "false"
                    assert not await page.is_disabled("#dr-next")
                    async with page.expect_response(lambda r: "/api/stats" in r.url, timeout=60_000):
                        await page.click("#dr-next")
                    await page.wait_for_function(f"document.querySelector('#date_to')?.value === '{today}'")
                    # Back on exactly the 30-day window: 30D lights again.
                    assert await page.get_attribute("#dr-30", "aria-pressed") == "true"

                    # Picking a date by hand re-queries once.
                    await page.wait_for_function("!document.getElementById('run').disabled", timeout=90_000)
                    stats.clear()
                    await page.fill("#date_from", (date.today() - timedelta(days=10)).isoformat())
                    await page.wait_for_timeout(2500)
                    assert len([u for u in stats if "date_from=" in u]) == 1, stats

                    # ‹ stops at the first cached day and keeps the window's length.
                    first = await page.evaluate("historyFirstDay()")
                    if first:
                        d0 = date.fromisoformat(first)
                        await page.fill("#date_to", (d0 + timedelta(days=38)).isoformat())
                        await page.fill("#date_from", (d0 + timedelta(days=9)).isoformat())
                        await page.wait_for_timeout(1200)
                        await page.wait_for_function("!document.getElementById('run').disabled", timeout=90_000)
                        async with page.expect_response(lambda r: "/api/stats" in r.url, timeout=60_000):
                            await page.click("#dr-prev")
                        await page.wait_for_function("!document.getElementById('run').disabled", timeout=90_000)
                        got = (await page.input_value("#date_from"), await page.input_value("#date_to"))
                        assert got == (first, (d0 + timedelta(days=29)).isoformat()), got
                        assert await page.is_disabled("#dr-prev"), "nothing before the first cached day"
                        await page.click("#dr-30")
                        await page.wait_for_function("!document.getElementById('run').disabled", timeout=90_000)

                    # The same bar shows in All historical.
                    await page.get_by_role("button", name="All historical").first.click()
                    await page.wait_for_selector(".all-history-section .history-range", timeout=60_000)
                    await page.get_by_role("button", name="Overview").first.click()
                    await page.wait_for_selector(".historical-section .history-range", timeout=60_000)

                    # Forecast "which model to trust" tray.
                    await page.wait_for_selector("#models-info-button", timeout=90_000)
                    btn, tray = page.locator("#models-info-button"), page.locator("#models-info")
                    box = await btn.bounding_box()
                    assert box["width"] <= 20, box       # small dot; its hit area is padded
                    await btn.click()
                    await page.wait_for_timeout(400)
                    assert await btn.get_attribute("aria-expanded") == "true"
                    assert (await tray.bounding_box())["height"] > 80
                    text = await tray.inner_text()
                    assert "WHICH FORECAST TO TRUST" in text.upper() and "HRRR" in text, text[:200]
                    uses = await tray.locator("dl").nth(1).locator("dd").count()  # first list is the horizons
                    assert uses == 7, f"one line per model expected, got {uses}"
                    await btn.click()
                    await page.wait_for_timeout(400)
                    assert (await tray.bounding_box())["height"] < 1
                    # The tray's own Close button, at its top, does the same.
                    await btn.click()
                    await page.wait_for_timeout(400)
                    await page.click("#models-info-close")
                    await page.wait_for_timeout(400)
                    assert (await tray.bounding_box())["height"] < 1
                    assert await btn.get_attribute("aria-expanded") == "false"

                    over = await page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    assert over <= 0, over
                    assert not errors, errors
                    print(f"ok {width}x{height}")
                finally:
                    await ctx.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    import sys
    if "--url" in sys.argv:
        import asyncio
        asyncio.run(run_browser(sys.argv[sys.argv.index("--url") + 1].rstrip("/")))
    else:
        unittest.main()
