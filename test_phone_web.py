"""Phone-browser behaviour of the web page (touch, keyboard, small screens).

    python3 test_phone_web.py --url http://127.0.0.1:5050

Runs Chromium with phone emulation (touch, coarse pointer). The real-engine
checks (iOS Safari, Android Chrome) are done by hand on simulators; this
pins what they found so it stays fixed.
"""

import asyncio
import json
import sys

from playwright.async_api import async_playwright


# Visible fields under 16px (iOS zooms the page when one gets focus).
SMALL_FIELDS = """[...document.querySelectorAll('input:not([type=hidden]), select')]
    .filter(e => e.getBoundingClientRect().width && parseFloat(getComputedStyle(e).fontSize) < 16)
    .map(e => e.id || e.className)"""


async def phone(browser, width, height):
    ctx = await browser.new_context(viewport={"width": width, "height": height}, has_touch=True,
                                    is_mobile=True, device_scale_factor=2,
                                    timezone_id="America/Los_Angeles")
    page = await ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    await page.route("**/api/fetch_stream*", lambda r: r.fulfill(
        content_type="application/x-ndjson", body=json.dumps({"type": "done", "summary": {}}) + "\n"))
    return ctx, page


async def run(base):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for width, height in [(360, 780), (390, 844)]:
                ctx, page = await phone(browser, width, height)
                try:
                    await page.goto(base)
                    assert await page.evaluate("matchMedia('(pointer: coarse)').matches")
                    air = page.locator("#airport")
                    assert await air.get_attribute("enterkeyhint") == "go"
                    assert await air.get_attribute("autocapitalize") == "characters"
                    assert await page.get_attribute("#vis_min", "inputmode") == "decimal"
                    # No field under 16px, or iOS zooms the page on focus.
                    small = await page.evaluate(SMALL_FIELDS)
                    assert not small, small
                    hour = await page.locator("#hours button").first.bounding_box()
                    assert hour["height"] >= 32, hour

                    # Cog: a tap opens the tray without popping the native zone menu.
                    await page.tap("#settings-button")
                    await page.wait_for_timeout(300)
                    assert await page.evaluate("document.activeElement.id") != "tz-select"
                    # Its hit area reaches past the 24px icon.
                    hit = await page.evaluate("""(() => {
                        const r = document.getElementById('settings-button').getBoundingClientRect();
                        const el = document.elementFromPoint(r.left - 6, r.top + r.height / 2);
                        return !!el?.closest('#settings-button');
                    })()""")
                    assert hit, "cog hit area"
                    await page.tap("#settings-button")

                    # Go on the keyboard runs the query and brings the results up.
                    await air.fill("KMYF")
                    await page.wait_for_timeout(50)
                    await air.press("Enter")
                    await page.wait_for_selector(".historical-section .table-wrap", timeout=90_000)
                    await page.wait_for_timeout(900)
                    top = await page.evaluate("document.getElementById('header-bar').getBoundingClientRect().top")
                    assert top < height * 0.6, f"results not brought into view: header at {top}"

                    # Overview group headers sit over their own sub-columns.
                    misaligned = await page.evaluate("""(() => {
                        const t = document.querySelector('.historical-section table');
                        const [top, sub] = t.tHead.rows;
                        const subs = [...sub.cells].filter(c => c.getBoundingClientRect().width);
                        let i = 0, bad = [];
                        for (const th of [...top.cells].filter(c => c.getBoundingClientRect().width)) {
                          if (th.rowSpan === 2) continue;
                          const group = subs.slice(i, i + th.colSpan); i += th.colSpan;
                          const r = th.getBoundingClientRect();
                          const L = group[0].getBoundingClientRect().left, R = group.at(-1).getBoundingClientRect().right;
                          if (Math.abs(r.left - L) > 1.5 || Math.abs(r.right - R) > 1.5) bad.push(th.textContent);
                        }
                        return i === subs.length ? bad : ['count ' + i + '/' + subs.length];
                    })()""")
                    assert not misaligned, misaligned
                    small = await page.evaluate(SMALL_FIELDS)
                    assert not small, f"fields under 16px after results: {small}"
                    # Row labels stay put when the table scrolls sideways.
                    assert await page.evaluate(
                        "getComputedStyle(document.querySelector('.historical-section tbody td')).position") == "sticky"

                    # Charts: vertical swipes scroll the page, text stays readable.
                    await page.wait_for_selector("#forecast-section .chart svg", timeout=90_000)
                    ta = await page.evaluate("getComputedStyle(document.querySelector('.ch-hit')).touchAction")
                    assert "pan-y" in ta, ta
                    px = await page.evaluate("""[...document.querySelectorAll('#forecast-section .chart svg')].map(s => {
                        const t = s.querySelector('.axis text');
                        return t ? parseFloat(getComputedStyle(t).fontSize) * s.clientWidth / s.viewBox.baseVal.width : 99;
                    })""")
                    assert min(px) >= 7, px

                    # A tap's readout stays after the finger lifts (forecast and hour-of-day charts).
                    for sel in ["#forecast-section .chart", "#charts .chart"]:
                        hit = page.locator(f"{sel} .ch-hit").first
                        await hit.scroll_into_view_if_needed()
                        hb = await hit.bounding_box()
                        await page.touchscreen.tap(hb["x"] + hb["width"] * 0.5, hb["y"] + hb["height"] * 0.5)
                        await page.wait_for_timeout(400)
                        op = await page.evaluate(
                            "(sel) => document.querySelector(sel + ' .crosshair')?.getAttribute('opacity')", sel)
                        assert op == "1", f"readout gone after tap on {sel}: opacity {op}"

                    # Table-time picker: Back closes it and stays on the page.
                    await page.wait_for_function(
                        "document.querySelector('#forecast-status')?.textContent.startsWith('updated')", timeout=90_000)
                    await page.locator("#forecast-at-button").scroll_into_view_if_needed()
                    url = page.url
                    await page.tap("#forecast-at-button")
                    await page.wait_for_selector("#forecast-picker-panel:not([hidden])")
                    await page.go_back()
                    await page.wait_for_timeout(300)
                    assert page.url == url and await page.is_hidden("#forecast-picker-panel")

                    # All historical: the page doesn't jump while it loads.
                    # (The bug: results shrank to a placeholder and the browser clamped the
                    # scroll position. Content growing above, like the progress log, isn't it.)
                    toggle = page.get_by_role("button", name="All historical").first
                    await toggle.scroll_into_view_if_needed()
                    pos = "[scrollY, document.getElementById('header-bar').getBoundingClientRect().top + scrollY]"
                    y0, top0 = await page.evaluate(pos)
                    await toggle.tap()
                    await page.wait_for_selector(".all-history-section .chart svg", timeout=60_000)
                    y1, top1 = await page.evaluate(pos)
                    assert y1 - y0 > -30, f"scroll clamped while loading: scrollY {y0} -> {y1} (bar {top0} -> {top1})"
                    px = await page.evaluate("""[...document.querySelectorAll('.all-history-section .chart svg')].map(s => {
                        const t = s.querySelector('.axis text');
                        return t ? parseFloat(getComputedStyle(t).fontSize) * s.clientWidth / s.viewBox.baseVal.width : 99;
                    })""")
                    assert min(px) >= 7, px
                    small = await page.evaluate(SMALL_FIELDS)
                    assert not small, f"fields under 16px in All historical: {small}"
                    # Timeline presets: never two lit at once, labels stay with their dates.
                    lit = await page.locator(".timeline-filter [data-tf-days].on").count()
                    assert lit <= 1, lit
                    pair = await page.evaluate("""[...document.querySelectorAll('.tf-pair')].map(p => {
                        const [l, i] = p.children; return Math.abs(l.getBoundingClientRect().top - i.getBoundingClientRect().top);
                    })""")
                    assert all(d < 6 for d in pair), pair

                    over = await page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    assert over <= 0, over
                    assert not page.errors, page.errors
                    print(f"ok {width}x{height}")
                finally:
                    await ctx.close()

            # Landscape: the picker panel scrolls instead of snapping back.
            ctx, page = await phone(browser, 844, 390)
            try:
                await page.goto(base)
                await page.fill("#airport", "KMYF")
                await page.wait_for_selector("#forecast-at-button", timeout=90_000)
                # Open it once the fresh forecast has replaced the cached one.
                await page.wait_for_function(
                    "document.querySelector('#forecast-status')?.textContent.startsWith('updated')", timeout=90_000)
                await page.wait_for_timeout(300)
                await page.locator("#forecast-at-button").scroll_into_view_if_needed()
                await page.tap("#forecast-at-button")
                panel = page.locator("#forecast-picker-panel")
                await page.wait_for_selector("#forecast-picker-panel:not([hidden])")
                room = await panel.evaluate("p => p.scrollHeight - p.clientHeight")
                assert room > 20, f"landscape picker should overflow and scroll; room {room}"
                await panel.evaluate("p => { p.scrollTop = 60; p.dispatchEvent(new Event('scroll')); }")
                await page.wait_for_timeout(300)
                state = await panel.evaluate("p => [p.hidden, p.scrollTop]")
                assert state == [False, 60], f"picker closed or snapped back: hidden, scrollTop = {state}"
                print(f"ok 844x390 picker (room {room}px)")
            finally:
                await ctx.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[sys.argv.index("--url") + 1].rstrip("/")))
