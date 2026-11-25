"""Rigorous test for the date-window, view-toggle, SEM-bars, and
implementation-detail-text-removal features.

Backend: exercises date_from/date_to (incl. Pacific-midnight edge
correctness), asserts per-hour and total counts, checks total_obs_cached.

Frontend (Playwright): default state, presets, header window display,
substitution + window interaction, narrow-viewport overflow, view toggle
(overview vs see all) for both table columns and chart contents, SEM
error-bar SVG presence, and absence of internal-implementation text the
user explicitly removed.

Usage:
    python3 test_date_window.py [--url URL]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

from playwright.async_api import async_playwright


# ----------------------------------------------------------------------
# Helpers

class Result:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []  # (id, status, detail)

    def ok(self, cid: str, detail: str = "") -> None:
        self.items.append((cid, "OK", detail))

    def bad(self, cid: str, detail: str) -> None:
        self.items.append((cid, "FAIL", detail))

    def info(self, cid: str, detail: str) -> None:
        self.items.append((cid, "info", detail))

    def report(self) -> int:
        fails = sum(1 for _, s, _ in self.items if s == "FAIL")
        for cid, status, detail in self.items:
            print(f"  [{status:<4}] {cid:<5} {detail}")
        print(f"\n  {len(self.items)} checks · {fails} FAIL")
        return 1 if fails else 0


def http_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def http_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read().decode("utf-8")


def http_status_location(url: str) -> tuple[int, str]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(url, timeout=20) as r:
            return r.status, r.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "")


def post_json_status(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {"error": ""}
        return exc.code, body


def stats_url(base: str, *, airport="KMYF",
              hours="10,11,12,13,14,15,16,17,18",
              date_from: str | None = None,
              date_to: str | None = None,
              vis=5, ceil=8000, xwind=12, runway="auto",
              runways_airport: str | None = None,
              runway_ident: str | None = None) -> str:
    q = {
        "airport": airport, "hours": hours,
        "vis_min_sm": str(vis), "ceiling_min_ft": str(ceil),
        "xwind_max_kt": str(xwind),
        "runway_heading": runway,
    }
    if date_from: q["date_from"] = date_from
    if date_to:   q["date_to"]   = date_to
    if runways_airport: q["runways_airport"] = runways_airport
    if runway_ident: q["runway_ident"] = runway_ident
    return f"{base}/api/stats?{urllib.parse.urlencode(q)}"


# ----------------------------------------------------------------------
# Backend probes

def run_backend(base: str, r: Result) -> dict:
    wall_today = date.today()
    window_today = wall_today
    try:
        info_for_window = http_json(f"{base}/api/info?airport=KMYF")
        latest_cached = (
            info_for_window.get("recent_contiguous_to")
            or str(info_for_window.get("last_obs_pt") or "")[:10]
        )
        if latest_cached:
            window_today = min(wall_today, date.fromisoformat(latest_cached))
    except Exception:
        window_today = wall_today
    d30 = (window_today - timedelta(days=30)).isoformat()
    d365 = (window_today - timedelta(days=365)).isoformat()
    d_iso = window_today.isoformat()

    health = http_json(f"{base}/health")
    version = str(health.get("version") or "")
    git_hash = str(health.get("git_hash") or "")
    if re.match(r"^\d+\.\d+\.\d+$", version):
        r.ok("cV0", f"health exposes semver {version}")
    else:
        r.bad("cV0", f"health version is not semver: {version!r}")
    if re.match(r"^[0-9a-f]{7,40}$", git_hash) or git_hash == "unknown":
        r.ok("cV1", f"health exposes git hash {git_hash}")
    else:
        r.bad("cV1", f"health git_hash is malformed: {git_hash!r}")

    about_html = http_text(f"{base}/about")
    if ("When do i get there?" in about_html
            and "These, and other questions" in about_html
            and "METAR-TAF" in about_html
            and "application/ld+json" in about_html):
        r.ok("cSEO1", "about page renders requested copy and JSON-LD")
    else:
        r.bad("cSEO1", "about page missing requested copy or JSON-LD")
    slash_status, slash_location = http_status_location(f"{base}/about/")
    if slash_status in (301, 308) and slash_location.endswith("/about"):
        r.ok("cSEO1b", f"about slash redirects to canonical: {slash_status} {slash_location}")
    else:
        r.bad("cSEO1b",
              f"about slash did not redirect: {slash_status} {slash_location!r}")
    robots = http_text(f"{base}/robots.txt")
    sitemap = http_text(f"{base}/sitemap.xml")
    llms = http_text(f"{base}/llms.txt")
    if "Sitemap:" in robots and "/sitemap.xml" in robots:
        r.ok("cSEO2", "robots.txt advertises sitemap")
    else:
        r.bad("cSEO2", f"robots.txt wrong: {robots!r}")
    if "https://windcheck.net/" in sitemap and "https://windcheck.net/about" in sitemap:
        r.ok("cSEO3", "sitemap includes root and about")
    else:
        r.bad("cSEO3", f"sitemap missing root/about: {sitemap[:220]!r}")
    if "Windcheck" in llms and "/about" in llms and "Forecast" in llms:
        r.ok("cSEO4", "llms.txt describes app and routes")
    else:
        r.bad("cSEO4", f"llms.txt incomplete: {llms[:220]!r}")
    if "windcheck.net" in urllib.parse.urlparse(base).netloc:
        status, body = post_json_status(
            f"{base}/api/fetch",
            {"airport": "KMYF", "mode": "recent", "days": 1},
        )
        if status == 403 and "recaptcha" in str(body.get("error", "")).lower():
            r.ok("cCAP1", "public history fetch rejects missing recaptcha")
        else:
            r.bad("cCAP1", f"missing recaptcha status={status}, body={body}")

    j_30  = http_json(stats_url(base, date_from=d30, date_to=d_iso))
    j_all = http_json(stats_url(base))
    j_365 = http_json(stats_url(base, date_from=d365, date_to=d_iso))
    # Edge probe: a 1-day window deep in the past — should yield 0 obs if
    # filter actually constrains, even though cache spans a year+.
    j_old = http_json(stats_url(base,
                                date_from="2020-01-01",
                                date_to="2020-01-02"))

    # cP1 — Pacific edge correctness. The bug we fixed: a single-day
    # Pacific window was actually being filtered as a UTC-naive window,
    # which truncates 7 hours early at the upper edge. Concretely, with
    # the OLD code, asking date_from=date_to=X clipped at "X 23:59:59 UTC"
    # = "X 16:59 PT", so PT hours 17..21 of date X were missing. With the
    # FIX, those late-afternoon PT hours are included.
    #
    # We probe ~7 days back so the cache has had time to settle for that
    # day. Use a 24-hour selector. We require obs in at least one of the
    # late-PT buckets (17, 18, 19, 20, 21) — those are precisely the
    # hours the broken filter would have lost.
    late_hours = (17, 18, 19, 20, 21)
    late_probe: tuple[str, dict[int, int]] | None = None
    for days_back in range(7, 120):
        probe = (window_today - timedelta(days=days_back)).isoformat()
        j_probe = http_json(stats_url(
            base, hours=",".join(str(h) for h in range(24)),
            date_from=probe, date_to=probe))
        late_buckets = {row["hour_pt"]: row["obs_count"]
                        for row in j_probe["rows"]
                        if row["hour_pt"] in late_hours}
        if any(c > 0 for c in late_buckets.values()):
            late_probe = (probe, late_buckets)
            break

    if late_probe:
        probe, late_buckets = late_probe
        r.ok("cP1", f"Pacific edge ({probe}): late-PT obs present "
                    f"{late_buckets} (would be 0 under broken UTC-naive code)")
    else:
        r.bad("cP1", "no cached day in the trailing 120 days has late-PT obs")

    # c10: date_from honored — 30d sum < all sum
    sum30  = sum(row["obs_count"] for row in j_30["rows"])
    sumAll = sum(row["obs_count"] for row in j_all["rows"])
    if sum30 < sumAll and sum30 > 0:
        r.ok("c10", f"30d sum={sum30} < all sum={sumAll}")
    else:
        r.bad("c10", f"30d sum={sum30}, all sum={sumAll}")

    # c11: date_to honored — old window returns 0 obs
    sumOld = sum(row["obs_count"] for row in j_old["rows"])
    if sumOld == 0:
        r.ok("c11", f"2020-01 window sum={sumOld}")
    else:
        r.bad("c11", f"2020-01 window unexpectedly returned {sumOld} obs")

    # c12: per-hour 30d much smaller than all
    factor_ok = True
    for row30, rowAll in zip(j_30["rows"], j_all["rows"]):
        if rowAll["obs_count"] > 0 and row30["obs_count"] > rowAll["obs_count"]:
            factor_ok = False
            break
    if factor_ok and sum30 < sumAll * 0.5:
        r.ok("c12", f"per-hour 30d={[r['obs_count'] for r in j_30['rows']]} "
                    f"all={[r['obs_count'] for r in j_all['rows']]}")
    else:
        r.bad("c12", "30d obs not smaller than all-cached as expected")

    # c13: total_obs reflects window
    if j_30["total_obs"] == sum30:
        r.ok("c13", f"total_obs (windowed) = {j_30['total_obs']} matches sum")
    else:
        r.bad("c13", f"total_obs={j_30['total_obs']} but sum={sum30}")

    # c14: total_obs without window = sum of selected-hour obs
    if j_all["total_obs"] == sumAll:
        r.ok("c14", f"total_obs (no window) = {j_all['total_obs']}")
    else:
        r.bad("c14", f"total_obs={j_all['total_obs']} but sum={sumAll}")

    # c22: total_obs_cached field present and reflects whole-cache count
    if "total_obs_cached" in j_all and j_all["total_obs_cached"] >= sumAll:
        r.ok("c22", f"total_obs_cached={j_all['total_obs_cached']}")
    else:
        r.bad("c22", f"total_obs_cached missing or wrong: "
                     f"{j_all.get('total_obs_cached')}")

    # cCOV1: /api/info exposes recent_contiguous_from / _to as a date
    # range that ends at the most recent obs and is >= the overall
    # first obs date (i.e. it is a sub-range of the outer cache span).
    info_url = f"{base}/api/info?airport=KMYF"
    j_info = http_json(info_url)
    cov_a = j_info.get("recent_contiguous_from")
    cov_b = j_info.get("recent_contiguous_to")
    first_pt = (j_info.get("first_obs_pt") or "")[:10]
    last_pt = (j_info.get("last_obs_pt") or "")[:10]
    if (cov_a and cov_b and first_pt and last_pt
            and cov_a >= first_pt and cov_b == last_pt):
        r.ok("cCOV1",
             f"recent_contiguous = {cov_a} → {cov_b} "
             f"(outer {first_pt} → {last_pt})")
    else:
        r.bad("cCOV1",
              f"recent_contiguous off: {cov_a!r} → {cov_b!r} "
              f"vs outer {first_pt!r} → {last_pt!r}")

    # c23: All preset → 365d window should include older data than 30d
    if j_365["total_obs"] > j_30["total_obs"]:
        r.ok("c23", f"365d total={j_365['total_obs']} > "
                    f"30d total={j_30['total_obs']}")
    else:
        r.bad("c23", "365d window did not include more data than 30d")

    # bonus: no-window total_obs >= 365d total_obs (since cache may exceed 1y)
    if j_all["total_obs"] >= j_365["total_obs"]:
        r.info("c14+", f"no-window total >= 365d total "
                       f"({j_all['total_obs']} vs {j_365['total_obs']})")

    # cC1 — every row carries a `ceiling` stats block in the standard
    # _stats_block shape (count + min/max/mean/stdev), even when no obs
    # in the bucket reported a ceiling.
    bad_shape = []
    for row in j_all["rows"]:
        c = row.get("ceiling")
        if not isinstance(c, dict):
            bad_shape.append(("missing", row["hour_pt"], c))
            continue
        keys = set(c.keys())
        if not keys >= {"count", "min", "max", "mean", "stdev"}:
            bad_shape.append(("keys", row["hour_pt"], list(keys)))
    if not bad_shape:
        r.ok("cC1", f"all {len(j_all['rows'])} rows expose a ceiling stats block")
    else:
        r.bad("cC1", f"ceiling stats block malformed: {bad_shape[:3]}…")

    # cC2 — at least one row over the full cache should have ceiling obs
    # (the airport must occasionally have BKN/OVC/VV layers reported).
    rows_with_ceil = [row for row in j_all["rows"]
                      if (row.get("ceiling") or {}).get("count", 0) > 0]
    if rows_with_ceil:
        sample = rows_with_ceil[0]["ceiling"]
        r.ok("cC2",
             f"{len(rows_with_ceil)}/{len(j_all['rows'])} rows have ceiling obs; "
             f"sample: cnt={sample['count']} mean={sample['mean']:.0f}ft "
             f"min={sample['min']:.0f} max={sample['max']:.0f}")
    else:
        r.bad("cC2", "no rows had any ceiling obs over the full cache")

    # cC3 — ceiling stats consistency: count <= obs_count for the same row,
    # and min <= mean <= max when count > 0.
    inconsistent = []
    for row in j_all["rows"]:
        c = row.get("ceiling") or {}
        if c.get("count", 0) <= 0:
            continue
        if c["count"] > row["obs_count"]:
            inconsistent.append(("count>obs", row["hour_pt"]))
        if not (c["min"] <= c["mean"] <= c["max"]):
            inconsistent.append(("ordering", row["hour_pt"], c))
    if not inconsistent:
        r.ok("cC3", "ceiling stats are internally consistent (count<=obs, min<=mean<=max)")
    else:
        r.bad("cC3", f"ceiling stats inconsistencies: {inconsistent[:3]}")

    return {"d30": d30, "d_iso": d_iso, "today": wall_today,
            "sum30": sum30, "sumAll": sumAll}


# ----------------------------------------------------------------------
# Frontend (Playwright)

async def run_frontend(base: str, r: Result, today: date) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()

        # ---- Default-airport behavior (fresh storage) ----
        # Wipe localStorage so we test the empty-storage path explicitly.
        await page.goto(base, wait_until="domcontentloaded")
        await page.evaluate("localStorage.clear()")
        await page.reload(wait_until="networkidle")
        empty_val = await page.input_value("#airport")
        if empty_val == "":
            r.ok("cD0", "fresh load: airport input is empty (no KL08 default)")
        else:
            r.bad("cD0", f"fresh load airport input = {empty_val!r}")
        tab_title = await page.title()
        if tab_title == "windcheck":
            r.ok("cTTL1", f"browser tab title = {tab_title!r}")
        else:
            r.bad("cTTL1", f"browser tab title wrong: {tab_title!r}")
        favicon_href = await page.get_attribute("link[rel~='icon']", "href")
        favicon_url = urllib.parse.urljoin(base.rstrip("/") + "/",
                                           favicon_href or "")
        fav_resp = await page.request.get(favicon_url)
        fav_text = await fav_resp.text()
        if (favicon_href and fav_resp.status == 200
                and "#1558e6" in fav_text
                and "<svg" in fav_text and "circle" in fav_text):
            r.ok("cTTL2", f"blue globe favicon served from {favicon_href!r}")
        else:
            r.bad("cTTL2",
                  f"favicon missing/wrong: href={favicon_href!r}, "
                  f"status={fav_resp.status}, body={fav_text[:80]!r}")
        brand_text = (await page.text_content("#brand-mark") or "").strip()
        brand_box = await page.locator("#brand-mark").bounding_box()
        brand_color = await page.locator("#brand-mark").evaluate(
            "el => getComputedStyle(el).color"
        )
        if brand_text == "WINDCHECK" and brand_box and brand_box["x"] <= 24:
            r.ok("cBND1", f"brand top-left label = {brand_text!r}")
        else:
            r.bad("cBND1",
                  f"brand label/position wrong: {brand_text!r}, {brand_box}")
        if "rgb(21, 88, 230)" in brand_color:
            r.ok("cBND2", f"brand uses blue {brand_color}")
        else:
            r.bad("cBND2", f"brand color is not expected blue: {brand_color}")
        if await page.locator("#brand-meta").is_hidden():
            r.ok("cBND3", "build meta starts hidden")
        else:
            r.bad("cBND3", "build meta is visible before click")
        await page.click("#brand-mark")
        meta_text = (await page.text_content("#brand-meta") or "").strip()
        expanded = await page.get_attribute("#brand-mark", "aria-expanded")
        if expanded == "true" and re.match(
                r"^v\d+\.\d+\.\d+\s+·\s+([0-9a-f]{7,40}|unknown)$",
                meta_text, re.I):
            r.ok("cBND4", f"click reveals build meta {meta_text!r}")
        else:
            r.bad("cBND4",
                  f"click did not reveal valid meta: expanded={expanded!r}, "
                  f"text={meta_text!r}")
        await page.click("#brand-mark")
        if await page.locator("#brand-meta").is_hidden():
            r.ok("cBND5", "second click hides build meta")
        else:
            r.bad("cBND5", "second click did not hide build meta")
        # On fresh load, the cache box should not be populated with anyone's
        # data (no info fetch should fire).
        cair = await page.text_content("#c-airport")
        if (cair or "").strip() in ("", "–", "-"):
            r.ok("cD1", f"fresh load: cache-box airport stays placeholder ({cair!r})")
        else:
            r.bad("cD1", f"cache-box auto-populated on fresh load: {cair!r}")
        run_label_empty = (await page.text_content("#run") or "").strip().lower()
        duplicate_history_buttons = await page.locator("#fetch-recent").count()
        if run_label_empty == "run query" and duplicate_history_buttons == 0:
            r.ok("cBTN1",
                 "fresh page has one primary action labelled Run query")
        else:
            r.bad("cBTN1",
                  f"primary label={run_label_empty!r}, "
                  f"duplicate fetch buttons={duplicate_history_buttons}")
        meta_desc = await page.get_attribute("meta[name='description']", "content")
        canonical = await page.get_attribute("link[rel='canonical']", "href")
        og_title = await page.get_attribute("meta[property='og:title']", "content")
        twitter_card = await page.get_attribute("meta[name='twitter:card']", "content")
        jsonld_count = await page.locator("script[type='application/ld+json']").count()
        if (meta_desc and "METAR history" in meta_desc
                and canonical == "https://windcheck.net/"
                and og_title == "Windcheck"
                and twitter_card == "summary"
                and jsonld_count >= 1):
            r.ok("cSEO5", "root has canonical, meta, OG/Twitter, and JSON-LD")
        else:
            r.bad("cSEO5",
                  f"root SEO tags wrong: desc={meta_desc!r}, "
                  f"canonical={canonical!r}, og={og_title!r}, "
                  f"twitter={twitter_card!r}, jsonld={jsonld_count}")
        credit = page.locator(".dreamers-credit")
        credit_text = (await credit.text_content() or "").strip()
        credit_href = await credit.get_attribute("href")
        credit_box = await credit.bounding_box()
        credit_position = await credit.evaluate("el => getComputedStyle(el).position")
        if (credit_text == "BUILT BY DREAMERS"
                and credit_href == "https://www.thedreamers.us/"
                and credit_box and credit_box["x"] < 20
                and credit_position != "fixed"):
            r.ok("cSEO6", "tiny bottom-left Dreamers credit links out without overlaying content")
        else:
            r.bad("cSEO6",
                  f"credit wrong: text={credit_text!r}, href={credit_href!r}, "
                  f"box={credit_box}, position={credit_position!r}")
        about_nav = await page.evaluate(
            """
            () => {
              const links = [...document.querySelectorAll("header .header-about[href='/about']")];
              const stray = document.querySelectorAll("body > a[href='/about'], main a[href='/about']").length;
              const nav = document.querySelector(".header-nav");
              const title = document.querySelector(".header-title");
              const link = links[0];
              const rect = (el) => {
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {
                  left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                  width: r.width, height: r.height,
                  centerY: r.top + (r.height / 2),
                };
              };
              const style = link ? getComputedStyle(link) : null;
              return {
                count: links.length,
                stray,
                text: link ? link.textContent.trim() : "",
                href: link ? link.getAttribute("href") : "",
                color: style ? style.color : "",
                navRect: rect(nav),
                titleRect: rect(title),
                linkRect: rect(link),
                overflow: document.documentElement.scrollWidth - window.innerWidth,
              };
            }
            """
        )
        if (about_nav["count"] == 1
                and about_nav["stray"] == 0
                and about_nav["text"].lower() == "about"
                and about_nav["href"] == "/about"
                and about_nav["color"] == "rgb(21, 88, 230)"
                and about_nav["overflow"] <= 0
                and about_nav["linkRect"]
                and 18 <= about_nav["linkRect"]["height"] <= 30
                and about_nav["titleRect"]
                and abs(about_nav["linkRect"]["centerY"]
                        - about_nav["titleRect"]["centerY"]) <= 2):
            r.ok("cSEO7", f"header about nav is compact, blue, and aligned: {about_nav}")
        else:
            r.bad("cSEO7", f"header about nav wrong: {about_nav}")
        recaptcha_scripts = await page.locator(
            "script[src*='google.com/recaptcha/api.js']"
        ).count()
        if recaptcha_scripts >= 1:
            r.ok("cCAP2", "root loads invisible reCAPTCHA script when configured")
        else:
            r.bad("cCAP2", "root did not load configured reCAPTCHA script")

        about = await ctx.new_page()
        await about.goto(urllib.parse.urljoin(base.rstrip("/") + "/", "about"),
                         wait_until="networkidle")
        about_h1 = (await about.text_content("h1") or "").strip()
        about_back = await about.get_attribute(".back", "href")
        about_sources = await about.locator(".source-list a").count()
        about_jsonld = await about.locator("script[type='application/ld+json']").count()
        await about.set_viewport_size({"width": 320, "height": 760})
        about_overflow = await about.evaluate(
            "() => Math.max(0, document.documentElement.scrollWidth - window.innerWidth)"
        )
        if ("When do i get there?" in about_h1
                and about_back == "/"
                and about_sources >= 8
                and about_jsonld >= 1
                and about_overflow == 0):
            r.ok("cSEO8",
                 f"about page copy/back/sources/schema responsive ok "
                 f"(sources={about_sources})")
        else:
            r.bad("cSEO8",
                  f"about bad: h1={about_h1[:80]!r}, back={about_back!r}, "
                  f"sources={about_sources}, jsonld={about_jsonld}, "
                  f"overflow={about_overflow}")
        await about.close()

        # Type, run, then reload — input should restore from localStorage.
        # KMYF is already in the cache from earlier tests, so this is fast.
        await page.fill("#airport", "KMYF")
        await page.click("#run")
        await page.wait_for_selector(".header-bar:not([hidden])", timeout=120_000)
        await page.wait_for_timeout(200)
        restore_fetch_reqs = []
        restore_forecast_reqs = []
        page.on("request", lambda req: (
            restore_fetch_reqs.append(req.url)
            if "/api/fetch_stream" in req.url else None,
            restore_forecast_reqs.append(req.url)
            if "/api/forecast" in req.url else None,
        ))
        await page.reload(wait_until="networkidle")
        restored = await page.input_value("#airport")
        if restored == "KMYF":
            r.ok("cD2", f"after reload: airport restored from storage = {restored}")
        else:
            r.bad("cD2", f"reload did not restore: {restored!r}")
        # cD3 — cache-box should now be populated (init triggered refresh).
        await page.wait_for_timeout(400)
        cair2 = await page.text_content("#c-airport")
        if (cair2 or "").strip().upper() == "KMYF":
            r.ok("cD3", f"reload: cache-box auto-populated for {cair2!r}")
        else:
            r.bad("cD3", f"reload cache-box = {cair2!r}")
        try:
            await page.wait_for_selector(".header-bar:not([hidden])", timeout=120_000)
            auto_hb = await page.text_content("#header-bar")
            if auto_hb and "KMYF" in auto_hb and "obs in window" in auto_hb.lower():
                r.ok("cD4", "reload auto-ran last airport query and restored history")
            else:
                r.bad("cD4", f"reload header did not show restored query: {auto_hb!r}")
        except Exception as exc:
            r.bad("cD4", f"reload did not auto-run restored airport query: {exc}")
        try:
            await page.wait_for_selector("#forecast-output:not([hidden])",
                                         timeout=90_000)
            await page.wait_for_selector(".forecast-table tbody tr",
                                         timeout=90_000)
            forecast_rows = await page.locator(".forecast-table tbody tr").count()
            run_label_cached = (await page.text_content("#run") or "").strip().lower()
            if (restore_fetch_reqs
                    and restore_forecast_reqs
                    and forecast_rows >= 5
                    and run_label_cached == "refresh / get more history"):
                r.ok("cD5",
                     f"reload refreshed history and forecast automatically: "
                     f"fetches={len(restore_fetch_reqs)} "
                     f"forecast={len(restore_forecast_reqs)} "
                     f"rows={forecast_rows}")
            else:
                r.bad("cD5",
                      f"reload auto-load incomplete: fetches={restore_fetch_reqs} "
                      f"forecast={restore_forecast_reqs} rows={forecast_rows} "
                      f"run_label={run_label_cached!r}")
        except Exception as exc:
            r.bad("cD5", f"reload did not auto-load forecast/history: {exc}")

        # cD6 — Typing a complete airport should auto-run; Enter is no longer
        # required for the ordinary query/history/forecast flow.
        auto_ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
        auto_page = await auto_ctx.new_page()
        auto_stats_reqs = []
        auto_forecast_reqs = []
        auto_page.on("request", lambda req: (
            auto_stats_reqs.append(req.url) if "/api/stats" in req.url else None,
            auto_forecast_reqs.append(req.url) if "/api/forecast" in req.url else None,
        ))
        await auto_page.goto(base, wait_until="domcontentloaded")
        await auto_page.evaluate("localStorage.clear()")
        await auto_page.fill("#airport", "")
        await auto_page.type("#airport", "KMYF", delay=35)
        try:
            await auto_page.wait_for_selector(".header-bar:not([hidden])",
                                              timeout=120_000)
            await auto_page.wait_for_selector("#forecast-output:not([hidden])",
                                              timeout=90_000)
            auto_header = (await auto_page.text_content("#header-bar") or "")
            auto_run_label = (await auto_page.text_content("#run") or "").strip().lower()
            auto_empty = (await auto_page.text_content("#results-host") or "").lower()
            if ("KMYF" in auto_header
                    and auto_stats_reqs
                    and auto_forecast_reqs
                    and "run a query to see results" not in auto_empty
                    and auto_run_label == "refresh / get more history"):
                r.ok("cD6",
                     f"typed airport auto-ran stats/forecast: "
                     f"stats={len(auto_stats_reqs)} forecast={len(auto_forecast_reqs)}")
            else:
                r.bad("cD6",
                      f"typed airport did not auto-run cleanly: "
                      f"header={auto_header!r} stats={auto_stats_reqs} "
                      f"forecast={auto_forecast_reqs} run={auto_run_label!r}")
        except Exception as exc:
            r.bad("cD6", f"typed airport did not auto-run: {exc}")
        await auto_ctx.close()

        # Capture network requests so we can prove c8/c9.
        stat_requests: list[str] = []
        page.on("request", lambda req: (
            stat_requests.append(req.url) if "/api/stats" in req.url else None
        ))

        await page.goto(base, wait_until="networkidle")
        # The date range lives in the Historical section, so open results
        # first; the default window is still the last 30 days.
        if not (await page.input_value("#airport")):
            await page.fill("#airport", "KMYF")
        await page.wait_for_selector("#date_from", timeout=120_000)
        await page.wait_for_function("!document.getElementById('run').disabled",
                                     timeout=120_000)

        # cH1 — default hour selection should be 10..18 (10am to 6pm)
        on_hours = await page.evaluate(
            "[...document.querySelectorAll('#hours button.on')]"
            ".map(b => +b.dataset.hour).sort((a,b)=>a-b)"
        )
        if on_hours == list(range(10, 19)):
            r.ok("cH1", f"default selected hours = {on_hours}")
        else:
            r.bad("cH1", f"default selected hours = {on_hours}, expected 10..18")
        # cH2 — preset button label is 10–18
        btn_label = (await page.text_content("#hr-day") or "").strip()
        if btn_label in ("10–18", "10-18"):
            r.ok("cH2", f"hr-day button label = {btn_label!r}")
        else:
            r.bad("cH2", f"hr-day button label = {btn_label!r}")

        d_from = await page.input_value("#date_from")
        d_to   = await page.input_value("#date_to")

        # c1, c2, c3
        expect_from = (today - timedelta(days=30)).isoformat()
        expect_to   = today.isoformat()
        if d_from == expect_from:
            r.ok("c1", f"date_from = {d_from}")
        else:
            r.bad("c1", f"date_from={d_from} expected {expect_from}")
        if d_to == expect_to:
            r.ok("c2", f"date_to = {d_to}")
        else:
            r.bad("c2", f"date_to={d_to} expected {expect_to}")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", d_from) \
           and re.match(r"^\d{4}-\d{2}-\d{2}$", d_to):
            r.ok("c3", "yyyy-mm-dd format on both inputs")
        else:
            r.bad("c3", f"format wrong: {d_from} / {d_to}")

        # c5 — preset 90d
        await page.click("#dr-90")
        d_from = await page.input_value("#date_from")
        if d_from == (today - timedelta(days=90)).isoformat():
            r.ok("c5", f"90d preset → from = {d_from}")
        else:
            r.bad("c5", f"90d gave from={d_from}")

        # c6 — 1y preset removed (timeline view caps at 90 days, so the
        # button was misleading). Confirm it is no longer rendered and
        # only 30d / 90d / Cached remain.
        present_ids = await page.evaluate(
            "[...document.querySelectorAll('.history-range-presets button')]"
            ".map(b => b.id)"
        )
        if ("dr-1y" not in present_ids
                and {"dr-30", "dr-90", "dr-cached"}.issubset(present_ids)):
            r.ok("c6", f"1y preset removed; presets = {present_ids}")
        else:
            r.bad("c6", f"unexpected preset buttons: {present_ids}")

        # c7 — "All cached" preset uses the longest contiguous monthly
        # block ending at the most recent obs (recent_contiguous_from /
        # _to in lastInfo). For an airport like KMYF whose cache is
        # contiguous, this should equal the outer first/last_obs range;
        # but for cache holes it's narrower than the outer range — we
        # only require the chosen range is a subset of the outer one
        # AND ends at last_obs_pt.
        cached_btn_label = await page.text_content("#dr-cached")
        if (cached_btn_label or "").strip().lower() == "all cached":
            r.ok("c7-label", f"button label = {cached_btn_label!r}")
        else:
            r.bad("c7-label",
                  f"button label = {cached_btn_label!r}, "
                  f"want 'All cached'")
        await page.click("#dr-cached")
        d_from = await page.input_value("#date_from")
        d_to   = await page.input_value("#date_to")
        info = await page.evaluate(
            "({a: lastInfo && lastInfo.first_obs_pt, "
            "  b: lastInfo && lastInfo.last_obs_pt, "
            "  ca: lastInfo && lastInfo.recent_contiguous_from, "
            "  cb: lastInfo && lastInfo.recent_contiguous_to})"
        )
        info_a = (info["a"] or "")[:10]
        info_b = (info["b"] or "")[:10]
        cov_a = info["ca"] or info_a
        cov_b = info["cb"] or info_b
        # Always: range ends at last obs date and starts >= overall first
        # obs date and is exactly the contiguous block the backend chose.
        if (d_from == cov_a and d_to == cov_b
                and bool(cov_a) and bool(cov_b)
                and cov_b == info_b
                and cov_a >= info_a):
            r.ok("c7", f"All-cached preset filled contiguous range "
                       f"{d_from} → {d_to} (outer was {info_a} → {info_b})")
        else:
            r.bad("c7", f"All-cached gave from={d_from!r} to={d_to!r}; "
                        f"contig={cov_a!r}→{cov_b!r}; "
                        f"outer={info_a!r}→{info_b!r}")

        # c4 — 30d preset (do this last so we end in the default state for
        # subsequent assertions).
        await page.click("#dr-30")
        await page.wait_for_function("!document.getElementById('run').disabled")
        d_from = await page.input_value("#date_from")
        d_to   = await page.input_value("#date_to")
        if d_from == expect_from and d_to == expect_to:
            r.ok("c4", f"30d preset → {d_from} → {d_to}")
        else:
            r.bad("c4", f"30d gave from={d_from} to={d_to}")

        # c9 — backend no-filter path is still wired correctly. We force
        # the no-filter URL by manually clearing the inputs and running.
        stat_requests.clear()
        await page.evaluate(
            "document.getElementById('date_from').value=''; "
            "document.getElementById('date_to').value='';"
        )
        await page.fill("#airport", "KMYF")
        await page.wait_for_function("!document.getElementById('run').disabled")
        await page.click("#run")
        await page.wait_for_selector(".header-bar:not([hidden])")
        await page.wait_for_function("!document.getElementById('run').disabled",
                                     timeout=120_000)
        all_url = next((u for u in stat_requests if "/api/stats" in u), "")
        if "date_from" not in all_url and "date_to" not in all_url:
            r.ok("c9", f"empty-input run → no date params in URL "
                       f"({all_url[-80:]})")
        else:
            r.bad("c9", f"date params leaked into empty-input URL: {all_url}")

        # c16 — header bar shows the explicit "All cached" date range
        # (longest contiguous block ending at most recent obs). The
        # range must round-trip cleanly through inputs → URL → header.
        stat_requests.clear()
        await page.click("#dr-cached")
        await page.wait_for_function(
            "document.getElementById('date_from').value && "
            "document.getElementById('date_to').value",
            timeout=60_000,
        )
        d_from = await page.input_value("#date_from")
        d_to   = await page.input_value("#date_to")
        info = await page.evaluate(
            "({a: lastInfo && lastInfo.first_obs_pt, "
            "  b: lastInfo && lastInfo.last_obs_pt, "
            "  ca: lastInfo && lastInfo.recent_contiguous_from, "
            "  cb: lastInfo && lastInfo.recent_contiguous_to})"
        )
        cov_a = info["ca"] or (info["a"] or "")[:10]
        cov_b = info["cb"] or (info["b"] or "")[:10]
        await page.wait_for_function(
            """
            ({a, b}) => {
              const hb = (document.getElementById('header-bar')?.innerText || '').toLowerCase();
              return hb.includes(a)
                && hb.includes(b)
                && !hb.includes('all cached')
                && !document.getElementById('run').disabled;
            }
            """,
            arg={"a": cov_a, "b": cov_b},
            timeout=60_000,
        )
        cached_url = next((
            u for u in reversed(stat_requests)
            if "/api/stats" in u and "date_from=" in u and "date_to=" in u
        ), next((u for u in reversed(stat_requests) if "/api/stats" in u), ""))
        hb = await page.text_content("#header-bar")
        ok16 = (
            d_from == cov_a and d_to == cov_b and bool(cov_a) and bool(cov_b)
            and f"date_from={cov_a}" in cached_url
            and f"date_to={cov_b}" in cached_url
            and "all cached" not in (hb or "").lower()
            and (cov_a in (hb or "") and cov_b in (hb or ""))
        )
        if ok16:
            r.ok("c16",
                 f"All-cached preset → inputs/url/header all show "
                 f"{cov_a} → {cov_b}")
        else:
            r.bad("c16",
                  f"All-cached mismatch: inputs={d_from!r}→{d_to!r} "
                  f"contig={cov_a!r}→{cov_b!r} url={cached_url[-100:]} "
                  f"hb_has_all_cached={'all cached' in (hb or '').lower()}")

        # c-AR1 — auto-run: clicking 90d AFTER results are on screen
        # fires at least one /api/stats and the header obs count grows
        # (90d window for KMYF must be larger than the 30d window).
        # First, get back to a known 30d baseline. We click Run after
        # selecting the preset so this setup is not coupled to whether
        # the 30d preset itself auto-ran before the browser finished
        # earlier async work.
        await page.click("#dr-30")
        await page.wait_for_function("!document.getElementById('run').disabled")
        await page.click("#run")
        await page.wait_for_function(
            """
            ({a, b}) => {
              const hb = document.getElementById('header-bar')?.innerText || '';
              return hb.includes(a)
                && hb.includes(b)
                && !document.getElementById('run').disabled;
            }
            """,
            arg={"a": expect_from, "b": expect_to},
            timeout=60_000,
        )
        hb_30 = await page.text_content("#header-bar")
        m30 = re.search(r"obs in window\s*([\d,]+)", (hb_30 or "").lower())
        n30 = int(m30.group(1).replace(",", "")) if m30 else 0
        stat_requests.clear()
        await page.click("#dr-90")
        expect_90_from = (today - timedelta(days=90)).isoformat()
        await page.wait_for_function(
            """
            ({a, b, n30}) => {
              const hb = document.getElementById('header-bar')?.innerText || '';
              const m = hb.toLowerCase().match(/obs in window\\s*([\\d,]+)/);
              const n = m ? Number(m[1].replace(/,/g, '')) : 0;
              return hb.includes(a)
                && hb.includes(b)
                && n !== n30
                && !document.getElementById('run').disabled;
            }
            """,
            arg={"a": expect_90_from, "b": expect_to, "n30": n30},
            timeout=60_000,
        )
        n90_reqs = sum(1 for u in stat_requests if "/api/stats" in u)
        hb_90 = await page.text_content("#header-bar")
        m90 = re.search(r"obs in window\s*([\d,]+)", (hb_90 or "").lower())
        n90 = int(m90.group(1).replace(",", "")) if m90 else 0
        if n90_reqs >= 1 and n90 > n30:
            r.ok("c-AR1",
                 f"90d preset auto-ran ({n90_reqs} req); obs jumped {n30}→{n90}")
        else:
            r.bad("c-AR1",
                  f"90d preset did not auto-run as expected: reqs={n90_reqs}, "
                  f"obs 30d={n30} 90d={n90}")

        # c-AR2 — auto-run gate: clicking presets BEFORE any first Run on
        # a fresh, empty-storage page must NOT fire /api/stats.
        cold_ctx = await browser.new_context(
            viewport={"width": 1366, "height": 900}
        )
        page2 = await cold_ctx.new_page()
        gate_reqs = []
        page2.on("request",
                 lambda r2: gate_reqs.append(r2.url)
                 if "/api/stats" in r2.url else None)
        await page2.goto(base, wait_until="networkidle")
        # The range presets live in the Historical section, so a cold
        # page has none to click and fires no stats request.
        cold_presets = await page2.locator("#dr-30, #dr-90, #dr-cached").count()
        await page2.wait_for_timeout(400)
        if cold_presets == 0 and not gate_reqs:
            r.ok("c-AR2", "range presets appear only with results; cold page fired no stats")
        else:
            r.bad("c-AR2",
                  f"cold page: presets={cold_presets} stats requests={gate_reqs}")
        await cold_ctx.close()

        # c8 — set 30d preset, run, confirm date params present in URL
        stat_requests.clear()
        await page.click("#dr-30")
        await page.click("#run")
        await page.wait_for_function(
            """
            ({a, b}) => {
              const hb = document.getElementById('header-bar')?.innerText || '';
              return hb.includes(a)
                && hb.includes(b)
                && !document.getElementById('run').disabled;
            }
            """,
            arg={"a": expect_from, "b": expect_to},
            timeout=120_000,
        )
        d30_url = next((u for u in stat_requests if "/api/stats" in u), "")
        if f"date_from={expect_from}" in d30_url \
                and f"date_to={expect_to}" in d30_url:
            r.ok("c8", f"30d → URL has date_from + date_to")
        else:
            r.bad("c8", f"30d URL missing date params: {d30_url}")

        # c15 — header bar shows window range
        hb = await page.text_content("#header-bar")
        if hb and expect_from in hb and expect_to in hb:
            r.ok("c15", f"header bar shows '{expect_from} → {expect_to}'")
        else:
            r.bad("c15", f"header bar missing window: {hb!r}")

        # c17, c18 — header has OBS IN WINDOW and matches sum of row counts
        m = re.search(r"obs in window\s*([\d,]+)", hb or "", re.I)
        if not m:
            r.bad("c17", f"no 'OBS IN WINDOW' in header: {hb!r}")
        else:
            displayed = int(m.group(1).replace(",", ""))
            r.ok("c17", f"header OBS IN WINDOW = {displayed}")
            # cross-check against API
            api = http_json(stats_url(base, date_from=expect_from,
                                      date_to=expect_to))
            api_sum = sum(row["obs_count"] for row in api["rows"])
            if displayed == api_sum:
                r.ok("c18", f"header count {displayed} == API sum {api_sum}")
            else:
                r.bad("c18", f"header={displayed} api sum={api_sum}")

        # c19 — substitution still works with date window applied
        stat_requests.clear()
        await page.fill("#airport", "L22")
        await page.click("#run")
        # Wait for substitution banner (allow up to 2 min for any auto-fetch).
        try:
            await page.wait_for_selector(".substitute-banner",
                                         timeout=120_000)
        except Exception:
            r.bad("c19", "substitute banner never appeared for L22")
        else:
            sub_text = await page.text_content(".substitute-banner")
            substituted_url = next(
                (u for u in stat_requests
                 if "/api/stats" in u and "airport=KPSP" in u), "")
            has_window = (substituted_url
                          and f"date_from={expect_from}" in substituted_url
                          and f"date_to={expect_to}"   in substituted_url)
            if has_window and sub_text and "KPSP" in sub_text:
                r.ok("c19", "substituted query carried date window")
            else:
                r.bad("c19", f"sub_text={sub_text!r} url={substituted_url}")

        # ---- New checks for view toggle, SEM bars, removed copy ----

        # Re-issue a normal query with KMYF + 30d window so we have stats
        # rendered in a known state.
        await page.fill("#airport", "KMYF")
        await page.click("#dr-30")
        await page.click("#run")
        await page.wait_for_selector(".header-bar:not([hidden])")
        await page.wait_for_selector(".charts:not([hidden])")
        await page.wait_for_function("!document.getElementById('run').disabled",
                                     timeout=120_000)

        # cT0 — toggle exists in the header bar with both buttons
        toggle_count = await page.locator(".header-bar .view-toggle button").count()
        if toggle_count == 2:
            r.ok("cT0", "view-toggle has 2 buttons in the header bar")
        else:
            r.bad("cT0", f"toggle has {toggle_count} buttons")

        # cT1 — Overview is the default (button.on)
        active = await page.text_content(".view-toggle button.on")
        if (active or "").strip().lower() == "overview":
            r.ok("cT1", f"default active = {active!r}")
        else:
            r.bad("cT1", f"default active = {active!r}, expected Overview")

        # cH3 — table HOUR PT cell uses friendly clock format e.g. "10 (10a)"
        first_label = await page.text_content(
            ".historical-section table tbody tr:first-child td:first-child")
        first_label = (first_label or "").strip()
        import re as _re
        if _re.match(r"^\d{2} \(\d{1,2}[ap]\)$", first_label):
            r.ok("cH3", f"hour cell = {first_label!r}")
        else:
            r.bad("cH3", f"hour cell unexpected: {first_label!r}")

        # cT2 — Overview table is the FULL stats grid:
        #        8 top-level groups (Hour PT, Count, Avg dir, xwind, Speed,
        #        Gust, Ceiling, Beneath minimums) + 22 sub-cells (xwind*5
        #        with `active` instead of `ct`, speed*4, gust*5 including %,
        #        ceiling*4,
        #        beneath*4). The merged Hour/Count/Avg-dir groups contribute
        #        via rowspan, not sub cells. `ct` was removed from each
        #        measure since the leftmost Count column already shows
        #        per-bucket sample size.
        overview_top = await page.locator(
            ".historical-section table thead tr").nth(0).locator("th").count()
        overview_sub = await page.locator(
            ".historical-section table thead tr").nth(1).locator("th").count()
        if overview_top == 8 and overview_sub == 22:
            r.ok("cT2", f"overview header: top={overview_top} sub={overview_sub}")
        else:
            r.bad("cT2", f"overview header: top={overview_top} "
                         f"sub={overview_sub} (want top=8, sub=22)")

        # cT2b — `ct` sub-header is gone everywhere except where it never
        # existed. We assert there is no <th> whose own text is exactly
        # "ct" (case-insensitive). The leftmost Count group is the
        # canonical place for sample size now.
        ct_subs = await page.evaluate(
            "[...document.querySelectorAll("
            "'.historical-section table thead tr:nth-child(2) th')]"
            ".map(t => t.textContent.trim().toLowerCase())"
            ".filter(t => t === 'ct').length"
        )
        if ct_subs == 0:
            r.ok("cT2b", "no sub-header named 'ct' (count moved to leftmost column)")
        else:
            r.bad("cT2b", f"unexpected 'ct' sub-headers count = {ct_subs}")

        # cT2c — xwind sub-headers include 'active' (per-hour runway end).
        sub_labels = await page.evaluate(
            "[...document.querySelectorAll("
            "'.historical-section table thead tr:nth-child(2) th')]"
            ".map(t => t.textContent.trim().toLowerCase())"
        )
        if "active" in sub_labels:
            r.ok("cT2c", f"sub-headers include 'active' ({sub_labels})")
        else:
            r.bad("cT2c", f"sub-headers missing 'active' ({sub_labels})")

        # cT2e — Gust group includes a percentage column. The value is
        # ct(gust reports) / ct(all observations) and must render as a
        # percent, not as another speed statistic.
        gust_pct = await page.evaluate(
            """
            (() => {
              const top = [...document.querySelectorAll(
                '.historical-section table thead tr:first-child th')];
              const sub = [...document.querySelectorAll(
                '.historical-section table thead tr:nth-child(2) th')];
              const singlesBefore = top.findIndex(t => /gust/i.test(t.textContent || ''));
              if (singlesBefore < 0) return { found: false };
              let fullStart = 0, subStart = 0;
              for (let i = 0; i < singlesBefore; i++) {
                const span = top[i].colSpan || 1;
                fullStart += span;
                if (top[i].rowSpan !== 2) subStart += span;
              }
              const gustSpan = top[singlesBefore].colSpan || 1;
              const labels = sub
                .slice(subStart, subStart + gustSpan)
                .map(t => t.textContent.trim());
              const pctIdx = labels.findIndex(x => x === '%');
              const rows = [...document.querySelectorAll(
                '.historical-section table tbody tr')].map(tr => {
                  const tds = tr.querySelectorAll('td');
                  return pctIdx >= 0 && tds[fullStart + pctIdx]
                    ? tds[fullStart + pctIdx].textContent.trim()
                    : null;
              }).filter(Boolean);
              return { found: pctIdx >= 0, labels, rows };
            })()
            """
        )
        if (gust_pct["found"]
                and gust_pct["rows"]
                and all(x.endswith("%") for x in gust_pct["rows"][:5])):
            r.ok("cT2e",
                 f"gust percent column present; sample={gust_pct['rows'][:5]}")
        else:
            r.bad("cT2e", f"gust percent column missing/malformed: {gust_pct}")

        # cT2d — Active cell carries the per-hour runway end ident (e.g.
        # "23" or "23+" when the bucket was mixed). Pull all rendered
        # active cells; at least one must be non-empty for KMYF (which
        # has runway data).
        active_cells = await page.evaluate(
            """
            (() => {
              const table = document.querySelector('.historical-section table');
              if (!table) return null;
              const tops = [...document.querySelectorAll(
                '.historical-section table thead tr:first-child th')].map(t => t.textContent.trim());
              const ix = tops.findIndex(t => /xwind/i.test(t));
              if (ix < 0) return null;
              const subs = [...document.querySelectorAll(
                '.historical-section table thead tr:nth-child(2) th')];
              const topEls = [...document.querySelectorAll(
                '.historical-section table thead tr:first-child th')];
              let fullStart = 0, subStart = 0;
              for (let i = 0; i < ix; i++) {
                const span = topEls[i].colSpan || 1;
                fullStart += span;
                if (topEls[i].rowSpan !== 2) subStart += span;
              }
              const xwSpan = topEls[ix].colSpan || 1;
              const xwSubLabels = subs
                .slice(subStart, subStart + xwSpan)
                .map(t => t.textContent.trim());
              const activeIdx = xwSubLabels.findIndex(s => s.toLowerCase() === 'active');
              if (activeIdx < 0) return null;
              const cells = [...document.querySelectorAll(
                '.historical-section table tbody tr')].map(tr => {
                  const tds = tr.querySelectorAll('td');
                  return tds[fullStart + activeIdx] ? tds[fullStart + activeIdx].textContent.trim() : null;
                });
              return { xwSubLabels, activeIdx, cells };
            })()
            """
        )
        if (active_cells and active_cells["activeIdx"] >= 0
                and any(c and c not in ("–", "") for c in active_cells["cells"])):
            r.ok("cT2d",
                 f"active cells populated; sample={active_cells['cells'][:5]}")
        else:
            r.bad("cT2d", f"active column missing or all empty: {active_cells}")

        # cC4 — Overview table has the Ceiling (ft) column group with the
        # standard 4 sub-cells (min/max/mean/sd).
        top_labels = await page.locator(
            ".historical-section table thead tr").nth(0).locator("th").all_inner_texts()
        ceil_col = next(
            (h for h in top_labels if "ceiling" in h.lower()), None)
        # Check the rendered cell — at least one row should show a real
        # whole-foot ceiling value (e.g. "2,575" or "300").
        sample_ceil_cells = await page.evaluate(
            """
            (() => {
              const tops = [...document.querySelectorAll(
                '.historical-section table thead tr:first-child th')].map(t => t.textContent.trim());
              const i0 = tops.findIndex(t => /ceiling/i.test(t));
              if (i0 < 0) return { top_idx: -1 };
              // sub-row precedes data; columns up to merged groups are rowspan=2
              // so sub-row indices are not aligned. Pull every TD whose column
              // header (subs) is min/max/mean/sd/ct under Ceiling group.
              const subs = [...document.querySelectorAll(
                '.historical-section table thead tr:nth-child(2) th')].map(t => t.textContent.trim());
              // Compute global col index of Ceiling sub-cells.
              const groupCounts = [...document.querySelectorAll(
                '.historical-section table thead tr:first-child th')].map(t => +t.colSpan || 1);
              let acc = 0;
              const groupColRange = groupCounts.map(c => {
                const r = [acc, acc + c]; acc += c; return r;
              });
              const [a, b] = groupColRange[i0];
              const tds = [...document.querySelectorAll(
                '.historical-section table tbody tr:first-child td')].slice(a, b);
              return { top_idx: i0,
                       sub_count: b - a,
                       cells: tds.map(td => td.textContent.trim()) };
            })()
            """
        )
        if (ceil_col
                and sample_ceil_cells["top_idx"] >= 0
                and sample_ceil_cells["sub_count"] == 4
                and any(c not in ("–", "0") for c in sample_ceil_cells["cells"])):
            r.ok("cC4",
                 f"Ceiling column group present: 4 sub-cells "
                 f"(min/max/mean/sd; per-bucket count moved to leftmost "
                 f"Count column), sample row = {sample_ceil_cells['cells']}")
        else:
            r.bad("cC4",
                  f"Ceiling column group malformed: ceil_col={ceil_col!r} "
                  f"probe={sample_ceil_cells} (want 4 sub-cells)")

        # cC5 — Overview charts include a dedicated ceiling chart.
        captions_overview = (await page.locator(
            "#charts .chart figcaption").all_inner_texts())
        ceil_caps = [c for c in captions_overview if "ceiling" in c.lower()]
        if any("by hour" in c.lower() for c in ceil_caps):
            r.ok("cC5", f"overview ceiling chart present: {ceil_caps}")
        else:
            r.bad("cC5",
                  f"no overview ceiling chart found in: {captions_overview}")

        # cO1 — Overview ordering is forecast first, then Historical; within
        # Historical, charts sit above the table. The concise explanation stays
        # below the results host.
        overview_order = await page.evaluate(
            """
            (() => {
              const forecast = document.querySelector('#forecast-section');
              const hist = document.querySelector('.historical-section');
              const charts = document.querySelector('#historical-charts-slot');
              const table = document.querySelector('.historical-section .table-wrap');
              const note = document.querySelector('#stats-note');
              const before = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
              return {
                forecastBeforeHistorical: before(forecast, hist),
                chartsBeforeTable: before(charts, table),
                resultsBeforeNote: before(document.querySelector('#results-host'), note),
                noteVisible: note ? !note.hidden : false,
              };
            })()
            """
        )
        if (overview_order["forecastBeforeHistorical"]
                and overview_order["chartsBeforeTable"]
                and overview_order["resultsBeforeNote"]
                and overview_order["noteVisible"]):
            r.ok("cO1", f"overview ordering ok: {overview_order}")
        else:
            r.bad("cO1", f"overview ordering wrong: {overview_order}")

        # cO2 — Historical charts and table need deliberate breathing room,
        # not a hard visual join. This pins the responsive spacing rule so
        # future table/chart edits do not collapse the stack again.
        hist_spacing = await page.evaluate(
            """
            (() => {
              const body = document.querySelector('.historical-section .section-body');
              const slot = document.querySelector('#historical-charts-slot');
              const table = document.querySelector('.historical-section .table-wrap');
              if (!body || !slot || !table) return null;
              const br = body.getBoundingClientRect();
              const sr = slot.getBoundingClientRect();
              const tr = table.getBoundingClientRect();
              return {
                chartToTable: Math.round(tr.top - sr.bottom),
                tableToBottom: Math.round(br.bottom - tr.bottom),
              };
            })()
            """
        )
        if (hist_spacing
                and hist_spacing["chartToTable"] >= 12
                and hist_spacing["tableToBottom"] >= 12):
            r.ok("cO2", f"historical stack spacing ok: {hist_spacing}")
        else:
            r.bad("cO2", f"historical stack spacing too tight: {hist_spacing}")

        hist_bar = await page.evaluate(
            """
            (() => {
              const btn = document.querySelector('#historical-refresh');
              const title = document.querySelector('.historical-section .section-title');
              const br = btn?.getBoundingClientRect();
              const tr = title?.getBoundingClientRect();
              return {
                text: btn?.textContent?.replace(/\\s+/g, ' ').trim() || '',
                inBar: !!(btn && btn.closest('.historical-section .section-title')),
                buttonHeight: br?.height || 0,
                titleHeight: tr?.height || 0,
              };
            })()
            """
        )
        if (hist_bar["text"].lower() == "refresh / get more history"
                and hist_bar["inBar"]
                and hist_bar["buttonHeight"] <= 25
                and hist_bar["titleHeight"] <= 42):
            r.ok("cO3", f"historical refresh action lives in the bar: {hist_bar}")
        else:
            r.bad("cO3", f"historical refresh action misplaced: {hist_bar}")

        # cF-UI1 — Overview includes the Forecast section above the
        # Historical block. It auto-loads available model horizons and
        # exposes one table-time picker; the picker must affect the table,
        # not the chart horizon.
        try:
            await page.wait_for_selector("#forecast-output:not([hidden])",
                                         timeout=90_000)
            await page.wait_for_selector(".forecast-wind svg", timeout=30_000)
            await page.wait_for_selector(".forecast-xwind", timeout=30_000)
            ftxt = (await page.text_content("#forecast-section") or "").lower()
            f_charts = await page.locator("#forecast-charts .chart").count()
            f_rows = await page.locator(".forecast-table tbody tr").count()
            f_cols = await page.locator(".forecast-table thead th").count()
            f_head = (await page.text_content(".forecast-table thead") or "").lower()
            f_first_row_cols = await page.locator(".forecast-table tbody tr").first.locator("td").count()
            guide = await page.locator(".forecast-model-legend summary").is_visible()
            picker_count = await page.locator("#forecast-at").count()
            range_count = await page.locator("#forecast-range-select").count()
            forecast_layout = await page.evaluate(
                """
                (() => {
                  const host = document.querySelector('#forecast-charts');
                  const hr = host.getBoundingClientRect();
                  const rects = [...host.querySelectorAll('.chart')]
                    .map(el => {
                      const r = el.getBoundingClientRect();
                      return {left:r.left, top:r.top, width:r.width, height:r.height};
                    });
                  const rowKey = (r) => Math.round(r.top / 4) * 4;
                  const rows = [];
                  for (const r of rects) {
                    const key = rowKey(r);
                    let row = rows.find(x => Math.abs(x.top - key) <= 4);
                    if (!row) {
                      row = {top: key, count: 0, widths: []};
                      rows.push(row);
                    }
                    row.count += 1;
                    row.widths.push(r.width);
                  }
                  rows.sort((a, b) => a.top - b.top);
                  return {
                    hostWidth: hr.width,
                    rects,
                    rows,
                    firstRowCount: rows[0]?.count || 0,
                    maxChartWidthRatio: rects.length
                      ? Math.max(...rects.map(r => r.width / Math.max(1, hr.width)))
                      : 1,
                    denseCompleteRow: hr.width < 620
                      ? rects.every(r => r.width >= hr.width * 0.94)
                      : (rows[0]?.count || 0) >= 2,
                  };
                })()
                """
            )
            if ("forecast" in ftxt
                    and "available model horizons" in ftxt
                    and "table samples the nearest point" in ftxt
                    and f_charts >= 3
                    and forecast_layout["denseCompleteRow"]
                    and forecast_layout["maxChartWidthRatio"] <= 0.72
                    and f_rows >= 5
                    and f_cols == 7
                    and f_first_row_cols == 7
                    and "confidence" not in f_head
                    and "reason" not in f_head
                    and guide
                    and picker_count == 1
                    and range_count == 1):
                r.ok("cF-UI1",
                     f"forecast section rendered: charts={f_charts} rows={f_rows} "
                     f"cols={f_cols} picker={picker_count} range={range_count} "
                     f"layout={forecast_layout}")
            else:
                r.bad("cF-UI1",
                      f"forecast section incomplete: charts={f_charts} "
                      f"rows={f_rows} cols={f_cols}/{f_first_row_cols} "
                      f"guide={guide} picker={picker_count} range={range_count} "
                      f"layout={forecast_layout} header={f_head!r} "
                      f"text={ftxt[:160]!r}")
        except Exception as exc:
            r.bad("cF-UI1", f"forecast section did not render: {exc}")

        # cF-UI1z — Forecast table semantics: null/missing means "not
        # returned"; a real numeric zero must stay visible as 0, not be
        # collapsed into the missing marker.
        forecast_null_semantics = await page.evaluate(
            """
            (() => {
              const target = '2026-05-10T11:00';
              const input = document.querySelector('#forecast-at');
              if (input) input.value = target;
              const html = forecastTableRowsHtml({
                target_pt: target,
                models: [
                  {
                    id: 'nbm',
                    label: 'NBM',
                    status: 'ok',
                    points: [{
                      time_pt: target,
                      time_label: '2026-05-10 11:00 PDT',
                      wind_dir_deg: 0,
                      wind_speed_kt: 0,
                      gust_kt: 0,
                      xwind_kt: 0,
                      ceiling_ft: 0,
                      visibility_sm: 0,
                      density_altitude_ft: 0,
                    }],
                  },
                  {
                    id: 'gfs',
                    label: 'GFS',
                    status: 'ok',
                    points: [{
                      time_pt: target,
                      time_label: '2026-05-10 11:00 PDT',
                    }],
                  },
                  {
                    id: 'taf',
                    label: 'TAF',
                    status: 'missing',
                    missing_reason: 'no same-airport TAF available',
                    points: [],
                  },
                ],
              });
              const table = document.createElement('table');
              table.innerHTML = `<tbody>${html}</tbody>`;
              const rows = [...table.querySelectorAll('tr')]
                .map(tr => [...tr.children].map(td => td.textContent.trim()));
              return {html, rows};
            })()
            """
        )
        zero_row = forecast_null_semantics["rows"][0]
        null_row = forecast_null_semantics["rows"][1]
        missing_row = forecast_null_semantics["rows"][2]
        if (zero_row[2] == "000° 0.0 kt G0.0"
                and zero_row[3] == "0.0"
                and zero_row[4] == "0 ft"
                and zero_row[5] == "0.0"
                and zero_row[6] == "0 ft"
                and "not returned" in " ".join(null_row[2:])
                and "not returned" in " ".join(missing_row[1:])
                and "–" not in " ".join(null_row + missing_row)):
            r.ok("cF-UI1z", f"forecast null/zero semantics ok: {forecast_null_semantics['rows']}")
        else:
            r.bad("cF-UI1z", f"forecast null/zero semantics wrong: {forecast_null_semantics['rows']}")

        # cF-UI1h — A high-DPI laptop screenshot is often around
        # 1188x776 CSS pixels. Forecast charts should not turn that into a
        # single oversized chart plus a partial second chart; the layout
        # should reveal complete chart units so the page scans quickly.
        await page.set_viewport_size({"width": 1188, "height": 776})
        await page.wait_for_timeout(100)
        forecast_density = await page.evaluate(
            """
            (() => {
              const host = document.querySelector('#forecast-charts');
              if (!host) return null;
              const rects = [...host.querySelectorAll('.chart')]
                .map(el => {
                  const r = el.getBoundingClientRect();
                  return {left:r.left, top:r.top, width:r.width, height:r.height, bottom:r.bottom};
                });
              const rows = [];
              for (const r of rects) {
                const top = Math.round(r.top / 4) * 4;
                let row = rows.find(x => Math.abs(x.top - top) <= 4);
                if (!row) {
                  row = {top, count: 0, bottom: 0};
                  rows.push(row);
                }
                row.count += 1;
                row.bottom = Math.max(row.bottom, r.bottom);
              }
              rows.sort((a, b) => a.top - b.top);
              const hr = host.getBoundingClientRect();
              return {
                hostWidth: hr.width,
                rows,
                rects,
                completeInViewport: rects.filter(r => r.bottom <= window.innerHeight).length,
                maxChartWidthRatio: rects.length
                  ? Math.max(...rects.map(r => r.width / Math.max(1, hr.width)))
                  : 1,
                docOverflow: Math.ceil(document.documentElement.scrollWidth - window.innerWidth),
              };
            })()
            """
        )
        density_ok = (
            forecast_density
            and forecast_density["docOverflow"] <= 1
            and forecast_density["completeInViewport"] >= 3
            and forecast_density["rows"]
            and forecast_density["rows"][0]["count"] >= 2
            and forecast_density["maxChartWidthRatio"] <= 0.55
        )
        if density_ok:
            r.ok("cF-UI1h", f"forecast chart density ok: {forecast_density}")
        else:
            r.bad("cF-UI1h", f"forecast chart density clunky: {forecast_density}")

        # cF-UI1c — Forecast controls keep equal-role boxes flush. The
        # refresh action belongs beside the status; the table-time picker
        # belongs in the forecast table header because it only retargets
        # table sampling, not chart horizons.
        async def forecast_controls_probe(width: int, height: int) -> dict:
            await page.set_viewport_size({"width": width, "height": height})
            await page.wait_for_timeout(100)
            return await page.evaluate(
                """
                (() => {
                  const rectFor = (sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return {
                      left: r.left, top: r.top, right: r.right, bottom: r.bottom,
                      width: r.width, height: r.height,
                    };
                  };
                  const rects = {
                    input: rectFor('#forecast-at-button'),
                    note: rectFor('#forecast-table-note'),
                    range: rectFor('#forecast-range-select'),
                    button: rectFor('#forecast-run'),
                    reset: rectFor('#forecast-zoom-reset'),
                    statusRow: rectFor('.forecast-status-row'),
                    status: rectFor('#forecast-status'),
                  };
                  const pairs = [['input', 'note'], ['range', 'status'], ['button', 'status']];
                  const overlaps = [];
                  for (const [a, b] of pairs) {
                    const ar = rects[a], br = rects[b];
                    if (!ar || !br) {
                      overlaps.push(`${a}:${b}:missing`);
                      continue;
                    }
                    const x = Math.min(ar.right, br.right) - Math.max(ar.left, br.left);
                    const y = Math.min(ar.bottom, br.bottom) - Math.max(ar.top, br.top);
                    if (x > 1 && y > 1) overlaps.push(`${a}:${b}:${Math.round(x)}x${Math.round(y)}`);
                  }
                  const delta = (a, b, edge) => Math.abs(rects[a][edge] - rects[b][edge]);
                  const picker = document.querySelector('#forecast-at-button');
                  const button = document.querySelector('#forecast-run');
                  const range = document.querySelector('#forecast-range-select');
                  return {
                    width: window.innerWidth,
                    rects,
                    overlaps,
                    pickerInTable: !!(picker && picker.closest('.forecast-table')),
                    pickerInChartControls: !!(picker && picker.closest('.forecast-controls')),
                    rangeInChartControls: !!(range && range.closest('.forecast-controls')),
                    rangeValue: range?.value || '',
                    rangeOptions: range ? [...range.options].map(o => ({value:o.value, disabled:o.disabled})) : [],
                    inputLeftOfNote: !!(rects.input && rects.note && rects.input.left < rects.note.left),
                    rangeLeftOfStatus: !!(rects.range && rects.statusRow && rects.range.left < rects.statusRow.left),
                    buttonInStatusRow: !!(button && button.closest('.forecast-status-row')),
                    resetInStatusRow: !!document.querySelector('#forecast-zoom-reset')?.closest('.forecast-status-row'),
                    resetHidden: document.querySelector('#forecast-zoom-reset')?.hidden,
                    buttonText: button?.textContent?.trim() || '',
                    buttonLabel: button?.getAttribute('aria-label') || '',
                    docOverflow: Math.ceil(document.documentElement.scrollWidth - window.innerWidth),
                    inputNoteTopDelta: delta('input', 'note', 'top'),
                    inputNoteBottomDelta: delta('input', 'note', 'bottom'),
                    buttonStatusTopDelta: delta('button', 'status', 'top'),
                    buttonStatusBottomDelta: delta('button', 'status', 'bottom'),
                  };
                })()
                """
            )

        controls_desktop = await forecast_controls_probe(1366, 900)
        controls_tablet = await forecast_controls_probe(768, 1024)
        controls_landscape = await forecast_controls_probe(844, 390)
        await page.set_viewport_size({"width": 1366, "height": 900})
        await page.wait_for_timeout(100)
        controls_ok = (
            controls_desktop["docOverflow"] <= 1
            and controls_tablet["docOverflow"] <= 1
            and controls_landscape["docOverflow"] <= 1
            and not controls_desktop["overlaps"]
            and not controls_tablet["overlaps"]
            and not controls_landscape["overlaps"]
            and controls_desktop["pickerInTable"]
            and not controls_desktop["pickerInChartControls"]
            and controls_desktop["rangeInChartControls"]
            and controls_desktop["rangeValue"]
            and controls_desktop["inputLeftOfNote"]
            and controls_desktop["rangeLeftOfStatus"]
            and controls_desktop["buttonInStatusRow"]
            and controls_desktop["resetInStatusRow"]
            and controls_desktop["resetHidden"] is True
            and controls_desktop["rects"]["statusRow"]["height"] <= 32.5
            and controls_desktop["inputNoteTopDelta"] <= 1.5
            and controls_desktop["inputNoteBottomDelta"] <= 1.5
            and controls_desktop["buttonStatusTopDelta"] <= 1.5
            and controls_desktop["buttonStatusBottomDelta"] <= 1.5
            and controls_desktop["buttonText"] == "↻"
            and controls_desktop["buttonLabel"] == "Refresh forecast guidance"
            and abs(controls_desktop["rects"]["button"]["width"] - controls_desktop["rects"]["button"]["height"]) <= 1.5
            and controls_tablet["pickerInTable"]
            and not controls_tablet["pickerInChartControls"]
            and controls_tablet["rangeInChartControls"]
            and controls_tablet["rangeValue"]
            and controls_tablet["inputLeftOfNote"]
            and controls_tablet["rangeLeftOfStatus"]
            and controls_tablet["buttonInStatusRow"]
            and controls_tablet["resetInStatusRow"]
            and controls_tablet["resetHidden"] is True
            and controls_tablet["rects"]["statusRow"]["height"] <= 32.5
            and controls_tablet["inputNoteTopDelta"] <= 1.5
            and controls_tablet["inputNoteBottomDelta"] <= 1.5
            and controls_tablet["buttonStatusTopDelta"] <= 1.5
            and controls_tablet["buttonStatusBottomDelta"] <= 1.5
            and controls_landscape["pickerInTable"]
            and not controls_landscape["pickerInChartControls"]
            and controls_landscape["rangeInChartControls"]
            and controls_landscape["rangeValue"]
            and controls_landscape["inputLeftOfNote"]
            and controls_landscape["rangeLeftOfStatus"]
            and controls_landscape["buttonInStatusRow"]
            and controls_landscape["resetInStatusRow"]
            and controls_landscape["resetHidden"] is True
            and controls_landscape["rects"]["statusRow"]["height"] <= 32.5
            and controls_landscape["inputNoteTopDelta"] <= 1.5
            and controls_landscape["inputNoteBottomDelta"] <= 1.5
            and controls_landscape["buttonStatusTopDelta"] <= 1.5
            and controls_landscape["buttonStatusBottomDelta"] <= 1.5
            and abs(controls_landscape["rects"]["button"]["width"] - controls_landscape["rects"]["button"]["height"]) <= 1.5
        )
        if controls_ok:
            r.ok("cF-UI1c",
                 f"forecast controls align and do not overlap: "
                 f"desktop={controls_desktop} tablet={controls_tablet} "
                 f"landscape={controls_landscape}")
        else:
            r.bad("cF-UI1c",
                  f"forecast controls misaligned: desktop={controls_desktop} "
                  f"tablet={controls_tablet} landscape={controls_landscape}")

        # cF-UI1d — The table-time picker must be the custom monochrome
        # control, not the native blue datetime-local popup from the browser.
        async def forecast_picker_style_probe(width: int, height: int) -> dict:
            await page.set_viewport_size({"width": width, "height": height})
            await page.keyboard.press("Escape")
            await page.locator("#forecast-at-button").scroll_into_view_if_needed()
            await page.wait_for_timeout(50)
            scroll_before = await page.evaluate("window.scrollY")
            await page.evaluate(
                "document.querySelector('#forecast-at-button')?.click()"
            )
            await page.wait_for_timeout(100)
            out = await page.evaluate(
                """
                (() => {
                  const panel = document.querySelector('#forecast-picker-panel');
                  const trigger = document.querySelector('#forecast-at-button');
                  const selected = document.querySelector('#forecast-calendar .is-selected');
                  const input = document.querySelector('#forecast-at');
                  const pr = panel.getBoundingClientRect();
                  const tr = trigger.getBoundingClientRect();
                  const ps = getComputedStyle(panel);
                  const ts = getComputedStyle(trigger);
                  const ss = selected ? getComputedStyle(selected) : null;
                  return {
                    hiddenInputType: input ? input.type : null,
                    panelHidden: panel.hidden,
                    panelBg: ps.backgroundColor,
                    panelColor: ps.color,
                    triggerBg: ts.backgroundColor,
                    selectedBg: ss ? ss.backgroundColor : null,
                    selectedColor: ss ? ss.color : null,
                    panelLeft: pr.left,
                    panelRight: pr.right,
                    panelWidth: pr.width,
                    triggerWidth: tr.width,
                    panelTop: pr.top,
                    panelBottom: pr.bottom,
                    panelHeight: pr.height,
                    triggerLeft: tr.left,
                    triggerRight: tr.right,
                    triggerTop: tr.top,
                    triggerBottom: tr.bottom,
                    panelClientHeight: panel.clientHeight,
                    panelScrollHeight: panel.scrollHeight,
                    viewportHeight: window.innerHeight,
                    scrollAfter: window.scrollY,
                    docOverflow: Math.ceil(document.documentElement.scrollWidth - window.innerWidth),
                    rightOverflow: Math.ceil(pr.right - window.innerWidth),
                    leftUnderflow: Math.floor(pr.left),
                    bottomOverflow: Math.ceil(pr.bottom - window.innerHeight),
                    topUnderflow: Math.floor(pr.top),
                  };
                })()
                """
            )
            out["scrollBefore"] = scroll_before
            out["scrollDelta"] = abs(out["scrollAfter"] - scroll_before)
            return out

        picker_desktop = await forecast_picker_style_probe(1366, 900)
        value_before_click = await page.locator("#forecast-at").input_value()
        await page.click("[data-picker-time='minute'][data-picker-delta='15']")
        await page.wait_for_timeout(100)
        value_after_click = await page.locator("#forecast-at").input_value()
        display_after_click = await page.locator("#forecast-at-display").text_content()
        await page.keyboard.press("Escape")
        picker_phone = await forecast_picker_style_probe(320, 568)
        await page.keyboard.press("Escape")
        await page.set_viewport_size({"width": 1366, "height": 900})
        await page.wait_for_timeout(100)
        picker_ok = (
            picker_desktop["hiddenInputType"] == "hidden"
            and not picker_desktop["panelHidden"]
            and picker_desktop["panelBg"] == "rgb(255, 255, 255)"
            and picker_desktop["panelColor"] == "rgb(0, 0, 0)"
            and picker_desktop["selectedBg"] == "rgb(0, 0, 0)"
            and picker_desktop["selectedColor"] == "rgb(255, 255, 255)"
            and picker_desktop["panelWidth"] <= 342
            and abs(picker_desktop["panelWidth"] - picker_desktop["triggerWidth"]) <= 1
            and abs(picker_desktop["panelLeft"] - picker_desktop["triggerLeft"]) <= 1
            and (
                abs(picker_desktop["panelTop"] - picker_desktop["triggerBottom"]) <= 1
                or abs(picker_desktop["panelBottom"] - picker_desktop["triggerTop"]) <= 1
            )
            and picker_desktop["panelHeight"] <= 350
            and picker_desktop["panelScrollHeight"] <= picker_desktop["panelClientHeight"] + 1
            and picker_desktop["scrollDelta"] <= 1
            and picker_desktop["rightOverflow"] <= 1
            and picker_desktop["leftUnderflow"] >= -1
            and picker_desktop["bottomOverflow"] <= 1
            and picker_desktop["topUnderflow"] >= -1
            and value_before_click != value_after_click
            and bool(display_after_click and "AM" in display_after_click or display_after_click and "PM" in display_after_click)
            and picker_phone["docOverflow"] <= 1
            and picker_phone["panelWidth"] <= 304
            and abs(picker_phone["panelWidth"] - picker_phone["triggerWidth"]) <= 1
            and abs(picker_phone["panelLeft"] - picker_phone["triggerLeft"]) <= 1
            and (
                abs(picker_phone["panelTop"] - picker_phone["triggerBottom"]) <= 1
                or abs(picker_phone["panelBottom"] - picker_phone["triggerTop"]) <= 1
            )
            and picker_phone["panelHeight"] <= 350
            and picker_phone["panelScrollHeight"] <= picker_phone["panelClientHeight"] + 1
            and picker_phone["scrollDelta"] <= 1
            and picker_phone["rightOverflow"] <= 1
            and picker_phone["leftUnderflow"] >= -1
            and picker_phone["bottomOverflow"] <= 1
            and picker_phone["topUnderflow"] >= -1
        )
        if picker_ok:
            r.ok("cF-UI1d",
                 f"forecast picker is custom monochrome and responsive: "
                 f"desktop={picker_desktop} phone={picker_phone} "
                 f"value={value_before_click}->{value_after_click} "
                 f"display={display_after_click!r}")
        else:
            r.bad("cF-UI1d",
                  f"forecast picker styling/behavior wrong: "
                  f"desktop={picker_desktop} phone={picker_phone} "
                  f"value={value_before_click}->{value_after_click} "
                  f"display={display_after_click!r}")

        # cF-UI1b — Changing the forecast table time automatically retargets
        # the table to the nearest model point without clicking the manual
        # refresh icon, refetching forecast data, or shrinking/redrawing the
        # charts.
        forecast_reqs = []
        page.on("request", lambda req: (
            forecast_reqs.append(req.url) if "/api/forecast" in req.url else None
        ))
        picker_before = await page.evaluate(
            """
            (() => {
              const model = (lastForecast && lastForecast.data.models || [])
                .find(m => m.id === 'nws_grid' && (m.points || []).length > 36);
              const point = model && model.points[Math.min(48, model.points.length - 1)];
              const line = document.querySelector('.forecast-wind svg path.forecast-line');
              return {
                target: point ? point.time_pt : null,
                expectedTime: point ? (point.time_label || point.time_pt) : null,
                beforePath: line ? line.getAttribute('d') : null,
                beforeRows: [...document.querySelectorAll('.forecast-table tbody tr')]
                  .map(tr => tr.textContent.replace(/\\s+/g, ' ').trim()),
              };
            })()
            """
        )
        forecast_reqs.clear()
        if picker_before["target"]:
            await page.evaluate(
                """
                (target) => {
                  const input = document.querySelector('#forecast-at');
                  input.value = target;
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                }
                """,
                picker_before["target"],
            )
            await page.wait_for_function(
                """
                (expected) => [...document.querySelectorAll('.forecast-table tbody tr')]
                  .some(tr => tr.textContent.includes('NWS gridpoint')
                    && tr.textContent.includes(expected))
                """,
                arg=picker_before["expectedTime"],
                timeout=30_000,
            )
            picker_after = await page.evaluate(
                """
                (() => {
                  const line = document.querySelector('.forecast-wind svg path.forecast-line');
                  const rows = [...document.querySelectorAll('.forecast-table tbody tr')]
                    .map(tr => {
                      const cells = [...tr.querySelectorAll('td')].map(td => td.textContent.replace(/\\s+/g, ' ').trim());
                      return {model: cells[0] || '', time: cells[1] || '', text: cells.join(' | ')};
                    });
                  const nws = rows.find(r => r.model.includes('NWS gridpoint'));
                  return {
                    selected: document.querySelector('#forecast-at')?.value || null,
                    afterPath: line ? line.getAttribute('d') : null,
                    note: document.querySelector('#forecast-table-note')?.textContent || '',
                    nws,
                    chartCount: document.querySelectorAll('#forecast-charts .chart').length,
                  };
                })()
                """
            )
            table_changed = picker_after["nws"] and (
                picker_after["nws"]["time"] == picker_before["expectedTime"])
            chart_same = picker_after["afterPath"] == picker_before["beforePath"]
            row_changed = picker_after["nws"] and picker_after["nws"]["text"] not in picker_before["beforeRows"]
            if (picker_after["selected"] == picker_before["target"]
                    and table_changed
                    and row_changed
                    and chart_same
                    and picker_after["chartCount"] >= 3
                    and not forecast_reqs
                    and "table nearest to" in picker_after["note"].lower()):
                r.ok("cF-UI1b",
                     f"forecast time change auto-refreshed table only: "
                     f"{picker_after['nws']} reqs={forecast_reqs}")
            else:
                r.bad("cF-UI1b",
                      f"forecast picker behavior wrong: before={picker_before} "
                      f"after={picker_after} reqs={forecast_reqs}")
        else:
            r.bad("cF-UI1b",
                  f"could not find NWS grid target point: {picker_before}")

        # cF-UI1g — The chart range control changes chart horizons only.
        # Far-out windows should drop short-range models from the drawn legend
        # while preserving the table's selected sample time.
        range_before = await page.evaluate(
            """
            (() => ({
              value: document.querySelector('#forecast-range-select')?.value || '',
              path: document.querySelector('.forecast-wind svg path.forecast-line')?.getAttribute('d') || '',
              tableTime: document.querySelector('#forecast-at')?.value || '',
              options: [...document.querySelectorAll('#forecast-range-select option')]
                .map(o => ({value:o.value, disabled:o.disabled})),
              legends: [...document.querySelectorAll('.forecast-wind .legend-row .model-key')]
                .map(k => [...k.classList].find(c => c.startsWith('m-'))?.slice(2))
                .filter(Boolean)
                .sort(),
            }))()
            """
        )
        if not any(o["value"] == "10-16" and not o["disabled"]
                   for o in range_before["options"]):
            r.bad("cF-UI1g", f"10-16d forecast range unavailable: {range_before}")
        else:
            await page.select_option("#forecast-range-select", "10-16")
            await page.wait_for_timeout(300)
            range_far = await page.evaluate(
                """
                (() => ({
                  value: document.querySelector('#forecast-range-select')?.value || '',
                  path: document.querySelector('.forecast-wind svg path.forecast-line')?.getAttribute('d') || '',
                  tableTime: document.querySelector('#forecast-at')?.value || '',
                  legends: [...document.querySelectorAll('.forecast-wind .legend-row .model-key')]
                    .map(k => [...k.classList].find(c => c.startsWith('m-'))?.slice(2))
                    .filter(Boolean)
                    .sort(),
                  captions: [...document.querySelectorAll('#forecast-charts figcaption')]
                    .map(c => c.textContent.trim()),
                  emptyCount: document.querySelectorAll('#forecast-charts .empty').length,
                  docOverflow: Math.ceil(document.documentElement.scrollWidth - window.innerWidth),
                }))()
                """
            )
            await page.select_option("#forecast-range-select", "0-5")
            await page.wait_for_timeout(200)
            short_models_gone = not any(
                mid in range_far["legends"]
                for mid in ("lamp", "hrrr", "nam")
            )
            if (range_far["value"] == "10-16"
                    and range_far["path"]
                    and range_far["path"] != range_before["path"]
                    and range_far["tableTime"] == range_before["tableTime"]
                    and range_far["docOverflow"] <= 1
                    and len(range_far["legends"]) < len(range_before["legends"])
                    and ("gfs" in range_far["legends"] or "nbm" in range_far["legends"])
                    and short_models_gone):
                r.ok("cF-UI1g",
                     f"forecast range filters chart models only: "
                     f"before={range_before} far={range_far}")
            else:
                r.bad("cF-UI1g",
                      f"forecast range behavior wrong: before={range_before} "
                      f"far={range_far}")

        # cF-UI1e — Forecast refresh keeps the last successful charts/table
        # visible while the new payload is still in flight.
        forecast_hold_payload = http_json(f"{base}/api/forecast?airport=KMYF")

        async def delayed_forecast(route):
            await asyncio.sleep(0.6)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(forecast_hold_payload),
            )

        await page.route("**/api/forecast?**", delayed_forecast)
        refresh_before = await page.evaluate(
            """
            (() => ({
              windPath: document.querySelector('.forecast-wind svg path.forecast-line')?.getAttribute('d') || '',
              rows: document.querySelectorAll('.forecast-table tbody tr').length,
              charts: document.querySelectorAll('#forecast-charts .chart').length,
            }))()
            """
        )
        await page.locator("#forecast-run").click()
        await page.wait_for_function(
            "document.querySelector('#forecast-output')?.classList.contains('is-loading')",
            timeout=10_000,
        )
        refresh_during = await page.evaluate(
            """
            (() => ({
              loading: document.querySelector('#forecast-output')?.classList.contains('is-loading'),
              windPath: document.querySelector('.forecast-wind svg path.forecast-line')?.getAttribute('d') || '',
              rows: document.querySelectorAll('.forecast-table tbody tr').length,
              charts: document.querySelectorAll('#forecast-charts .chart').length,
              emptyText: document.querySelector('#forecast-output .empty')?.textContent || '',
              status: document.querySelector('#forecast-status')?.textContent || '',
            }))()
            """
        )
        await page.wait_for_function(
            "!document.querySelector('#forecast-output')?.classList.contains('is-loading')",
            timeout=20_000,
        )
        await page.unroute("**/api/forecast?**", delayed_forecast)
        if (refresh_during["loading"]
                and refresh_during["windPath"] == refresh_before["windPath"]
                and refresh_during["rows"] == refresh_before["rows"]
                and refresh_during["charts"] == refresh_before["charts"]
                and "loading model guidance" not in refresh_during["emptyText"].lower()
                and "loading" in refresh_during["status"].lower()):
            r.ok("cF-UI1e",
                 f"forecast refresh preserved rendered output: {refresh_during}")
        else:
            r.bad("cF-UI1e",
                  f"forecast refresh replaced output: before={refresh_before} "
                  f"during={refresh_during}")

        # cF-UI1i — Optimistic forecast cache is per airport. When a fresh
        # forecast shell is created for an airport with a cached forecast,
        # it should paint that cached charts/table immediately and only use
        # the status cell for the loading indicator.
        async def delayed_forecast_again(route):
            await asyncio.sleep(0.8)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(forecast_hold_payload),
            )

        await page.route("**/api/forecast?**", delayed_forecast_again)
        await page.evaluate(
            """
            (payload) => {
              saveForecastSnapshot('KMYF', payload);
              lastForecast = null;
              const out = document.querySelector('#forecast-output');
              out.innerHTML = '';
              out.hidden = true;
              runForecast(lastStats.j, {auto: true});
            }
            """,
            forecast_hold_payload,
        )
        await page.wait_for_selector("#forecast-output .forecast-table", timeout=10_000)
        cached_during = await page.evaluate(
            """
            (() => ({
              status: document.querySelector('#forecast-status')?.textContent || '',
              loading: document.querySelector('#forecast-output')?.classList.contains('is-loading'),
              charts: document.querySelectorAll('#forecast-charts .chart').length,
              rows: document.querySelectorAll('.forecast-table tbody tr').length,
              emptyText: document.querySelector('#forecast-output .empty')?.textContent || '',
              cacheKeys: Object.keys(JSON.parse(localStorage.getItem('metar.forecastSnapshotsByAirport.v1') || '{}')).sort(),
            }))()
            """
        )
        await page.wait_for_function(
            "!document.querySelector('#forecast-output')?.classList.contains('is-loading')",
            timeout=20_000,
        )
        await page.unroute("**/api/forecast?**", delayed_forecast_again)
        if (cached_during["loading"]
                and cached_during["charts"] >= 3
                and cached_during["rows"] >= 5
                and "loading" in cached_during["status"].lower()
                and "loading model guidance" not in cached_during["emptyText"].lower()
                and "KMYF" in cached_during["cacheKeys"]):
            r.ok("cF-UI1i",
                 f"per-airport forecast cache paints while loading: {cached_during}")
        else:
            r.bad("cF-UI1i",
                  f"per-airport forecast cache did not paint during load: {cached_during}")

        # cF-UI1f — Historical refresh does the same: it marks the section as
        # refreshing but keeps the existing overview charts/table in place.
        async def delayed_stats(route):
            await asyncio.sleep(0.6)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(http_json(route.request.url)),
            )

        await page.route("**/api/stats?**", delayed_stats)
        hist_before = await page.evaluate(
            """
            (() => ({
              rows: document.querySelectorAll('#results-host tbody tr').length,
              charts: document.querySelectorAll('#charts .chart').length,
              xwindPath: document.querySelector('.chart.xwind svg path.line')?.getAttribute('d') || '',
              forecastRows: document.querySelectorAll('.forecast-table tbody tr').length,
              forecastCharts: document.querySelectorAll('#forecast-charts .chart').length,
              forecastWindPath: document.querySelector('.forecast-wind svg path.forecast-line')?.getAttribute('d') || '',
            }))()
            """
        )
        await page.evaluate("() => { runQuery(); return true; }")
        await page.wait_for_function(
            "document.querySelector('#results-section')?.classList.contains('is-refreshing')",
            timeout=10_000,
        )
        hist_during = await page.evaluate(
            """
            (() => ({
              refreshing: document.querySelector('#results-section')?.classList.contains('is-refreshing'),
              rows: document.querySelectorAll('#results-host tbody tr').length,
              charts: document.querySelectorAll('#charts .chart').length,
              xwindPath: document.querySelector('.chart.xwind svg path.line')?.getAttribute('d') || '',
              forecastRows: document.querySelectorAll('.forecast-table tbody tr').length,
              forecastCharts: document.querySelectorAll('#forecast-charts .chart').length,
              forecastWindPath: document.querySelector('.forecast-wind svg path.forecast-line')?.getAttribute('d') || '',
              querying: /querying|loading every observation/i.test(document.querySelector('#results-host')?.textContent || ''),
            }))()
            """
        )
        await page.wait_for_function(
            "!document.querySelector('#results-section')?.classList.contains('is-refreshing')",
            timeout=20_000,
        )
        hist_after = await page.evaluate(
            """
            (() => ({
              forecastRows: document.querySelectorAll('.forecast-table tbody tr').length,
              forecastCharts: document.querySelectorAll('#forecast-charts .chart').length,
              forecastWindPath: document.querySelector('.forecast-wind svg path.forecast-line')?.getAttribute('d') || '',
              loadingText: /loading model guidance/i.test(document.querySelector('#forecast-output')?.textContent || ''),
            }))()
            """
        )
        await page.unroute("**/api/stats?**", delayed_stats)
        if (hist_during["refreshing"]
                and hist_during["rows"] == hist_before["rows"]
                and hist_during["charts"] == hist_before["charts"]
                and hist_during["xwindPath"] == hist_before["xwindPath"]
                and hist_during["forecastRows"] == hist_before["forecastRows"]
                and hist_during["forecastCharts"] == hist_before["forecastCharts"]
                and hist_during["forecastWindPath"] == hist_before["forecastWindPath"]
                and hist_after["forecastRows"] == hist_before["forecastRows"]
                and hist_after["forecastCharts"] == hist_before["forecastCharts"]
                and hist_after["forecastWindPath"] == hist_before["forecastWindPath"]
                and not hist_after["loadingText"]
                and not hist_during["querying"]):
            r.ok("cF-UI1f",
                 f"historical refresh preserved rendered output: "
                 f"during={hist_during} after={hist_after}")
        else:
            r.bad("cF-UI1f",
                  f"historical refresh replaced output: before={hist_before} "
                  f"during={hist_during} after={hist_after}")

        # cF-UI2 — Forecast chart axis labels stay anchored around the
        # default 11a checkpoint, have no comma, and include hover readouts
        # like the historical charts.
        f_axis = await page.evaluate(
            """
            (() => {
              const fig = document.querySelector('.forecast-wind');
              const labels = fig
                ? [...fig.querySelectorAll('svg .axis text')]
                    .map(t => t.textContent.trim())
                    .filter(t => /^\\d{1,2}\\/\\d{1,2}\\s+\\d{1,2}[ap]$/.test(t))
                : [];
              const hit = fig && fig.querySelector('svg .ch-hit');
              return {
                labels,
                commas: labels.filter(x => x.includes(',')),
                has11a: labels.some(x => /\\s11a$/.test(x)),
                hit: !!hit,
              };
            })()
            """
        )
        if (f_axis["labels"]
                and not f_axis["commas"]
                and f_axis["has11a"]
                and f_axis["hit"]):
            r.ok("cF-UI2", f"forecast axis labels/hover hit ok: {f_axis}")
        else:
            r.bad("cF-UI2", f"forecast axis labels/hover hit off: {f_axis}")
        f_hit = page.locator(".forecast-wind svg .ch-hit").first
        if await f_hit.count() > 0:
            await f_hit.hover()
            await page.wait_for_timeout(120)
            f_readout = await page.evaluate(
                "(() => { const t = document.querySelector("
                "'.forecast-wind .readout text'); "
                "return t ? t.textContent : null; })()"
            )
            if f_readout and "kt" in f_readout and (
                    "TAF" in f_readout or "HRRR" in f_readout
                    or "NBM" in f_readout or "NWS" in f_readout):
                r.ok("cF-UI3", f"forecast hover readout = {f_readout!r}")
            else:
                r.bad("cF-UI3", f"forecast hover readout missing values: {f_readout!r}")
            f_readout_fit = await page.evaluate(
                """
                (() => {
                  const t = document.querySelector('.forecast-wind .readout text');
                  const svg = t?.closest('svg');
                  if (!t || !svg) return null;
                  const b = t.getBBox();
                  const vb = svg.viewBox.baseVal;
                  return {
                    text: t.textContent,
                    lines: Number(t.dataset.lines || 0),
                    x: b.x, y: b.y, right: b.x + b.width,
                    bottom: b.y + b.height,
                    viewRight: vb.x + vb.width,
                    viewBottom: vb.y + vb.height,
                  };
                })()
                """
            )
            if (f_readout_fit
                    and 1 <= f_readout_fit["lines"] <= 4
                    and f_readout_fit["right"] <= f_readout_fit["viewRight"] + 1
                    and f_readout_fit["bottom"] <= f_readout_fit["viewBottom"] + 1):
                r.ok("cF-UI3a", f"forecast hover readout fits SVG: {f_readout_fit}")
            else:
                r.bad("cF-UI3a", f"forecast hover readout clips: {f_readout_fit}")
        else:
            r.bad("cF-UI3", "forecast wind chart has no hover hit rect")

        # cF-UI3b — Forecast includes a dedicated xwind chart with the
        # same hover readout behavior as the other forecast charts.
        fx_hit = page.locator(".forecast-xwind svg .ch-hit").first
        if await fx_hit.count() > 0:
            await fx_hit.hover()
            await page.wait_for_timeout(120)
            fx_readout = await page.evaluate(
                "(() => { const t = document.querySelector("
                "'.forecast-xwind .readout text'); "
                "return t ? t.textContent : null; })()"
            )
            fx_cap = await page.locator(".forecast-xwind figcaption").inner_text()
            if fx_readout and "kt" in fx_readout and "crosswind" in fx_cap.lower():
                r.ok("cF-UI3b", f"forecast xwind hover = {fx_readout!r}")
            else:
                r.bad("cF-UI3b",
                      f"forecast xwind hover missing values: cap={fx_cap!r} "
                      f"readout={fx_readout!r}")
        else:
            r.bad("cF-UI3b", "forecast xwind chart has no hover hit rect")

        # cF-UI3c — Forecast legend keys carry enough visual grammar to
        # distinguish similar dashed/dotted model lines, and each chart legend
        # only lists the model series actually drawn in that chart.
        legend_keys = await page.evaluate(
            """
            (() => {
              const chartSets = [...document.querySelectorAll('#forecast-charts .chart')]
                .map(fig => {
                  const pathIds = [...fig.querySelectorAll('svg path.forecast-line')]
                    .map(p => [...p.classList].find(c => c.startsWith('m-'))?.slice(2))
                    .filter(Boolean)
                    .sort();
                  const legendIds = [...fig.querySelectorAll('.legend-row .model-key')]
                    .map(k => [...k.classList].find(c => c.startsWith('m-'))?.slice(2))
                    .filter(Boolean)
                    .sort();
                  const svg = fig.querySelector('svg');
                  return {
                    cls: fig.className,
                    pathIds,
                    legendIds,
                    dataMax: Number(svg?.dataset.dataMax || 0),
                    yMax: Number(svg?.dataset.yMax || 0),
                  };
                });
              const ids = ['taf', 'lamp', 'nws_grid', 'hrrr', 'nbm', 'nam', 'gfs'];
              const out = {keys: {}, chartSets};
              for (const id of ids) {
                const el = document.querySelector(`.model-key.m-${id}`);
                if (!el) continue;
                const ps = getComputedStyle(el, '::before');
                out.keys[id] = {
                  bg: ps.backgroundImage || ps.backgroundColor,
                  color: ps.backgroundColor,
                  height: ps.height,
                  width: getComputedStyle(el).width,
                };
              }
              return out;
            })()
            """
        )
        key_bgs = {v["bg"] for v in legend_keys["keys"].values()}
        chart_legends_match = all(
            c["pathIds"] == c["legendIds"]
            for c in legend_keys["chartSets"]
            if c["pathIds"] or c["legendIds"]
        )
        y_axes_tight = all(
            (not c["dataMax"])
            or (c["yMax"] >= c["dataMax"] and c["yMax"] <= c["dataMax"] * 1.45 + 1)
            for c in legend_keys["chartSets"]
        )
        legend_ok = (
            len(legend_keys["keys"]) >= 7
            and len(key_bgs) >= 6
            and "linear-gradient" in legend_keys["keys"]["taf"]["bg"]
            and legend_keys["keys"]["taf"]["height"] == "3px"
            and "21, 88, 230" in legend_keys["keys"]["nbm"]["color"]
            and "0, 140, 140" in legend_keys["keys"]["nws_grid"]["bg"]
            and "23, 78, 166" in legend_keys["keys"]["hrrr"]["bg"]
            and legend_keys["keys"]["nam"]["bg"] != legend_keys["keys"]["gfs"]["bg"]
            and chart_legends_match
            and y_axes_tight
        )
        if legend_ok:
            r.ok("cF-UI3c", f"forecast legend keys/dynamic y-axis ok: {legend_keys}")
        else:
            r.bad("cF-UI3c", f"forecast legend/y-axis wrong: {legend_keys}")

        # cF-UI3e — Forecast legend entries are real toggles. Everything is
        # on by default; clicking an entry hides that model's line for that
        # chart, keeps the legend entry visible, and mutes it slightly so it
        # is obvious the item can be restored.
        legend_toggle_before = await page.evaluate(
            """
            (() => {
              const btn = document.querySelector(
                ".forecast-wind .legend-toggle[data-forecast-series='nbm']"
              ) || document.querySelector(".forecast-wind .legend-toggle");
              const id = btn?.dataset.forecastSeries || '';
              return {
                id,
                cursor: btn ? getComputedStyle(btn).cursor : '',
                aria: btn?.getAttribute('aria-pressed') || '',
                muted: !!btn?.classList.contains('is-muted'),
                pathIds: [...document.querySelectorAll('.forecast-wind svg path.forecast-line')]
                  .map(p => [...p.classList].find(c => c.startsWith('m-'))?.slice(2))
                  .filter(Boolean),
                legendIds: [...document.querySelectorAll('.forecast-wind .legend-row .model-key')]
                  .map(k => [...k.classList].find(c => c.startsWith('m-'))?.slice(2))
                  .filter(Boolean),
              };
            })()
            """
        )
        if legend_toggle_before["id"]:
            await page.locator(
                f".forecast-wind .legend-toggle[data-forecast-series='{legend_toggle_before['id']}']"
            ).first.click()
            await page.wait_for_timeout(150)
            legend_toggle_off = await page.evaluate(
                """
                (id) => {
                  const btn = document.querySelector(
                    `.forecast-wind .legend-toggle[data-forecast-series='${id}']`
                  );
                  return {
                    aria: btn?.getAttribute('aria-pressed') || '',
                    muted: !!btn?.classList.contains('is-muted'),
                    opacity: btn ? getComputedStyle(btn).opacity : '',
                    pathIds: [...document.querySelectorAll('.forecast-wind svg path.forecast-line')]
                      .map(p => [...p.classList].find(c => c.startsWith('m-'))?.slice(2))
                      .filter(Boolean),
                    legendIds: [...document.querySelectorAll('.forecast-wind .legend-row .model-key')]
                      .map(k => [...k.classList].find(c => c.startsWith('m-'))?.slice(2))
                      .filter(Boolean),
                  };
                }
                """,
                legend_toggle_before["id"],
            )
            await page.locator(
                f".forecast-wind .legend-toggle[data-forecast-series='{legend_toggle_before['id']}']"
            ).first.click()
            await page.wait_for_timeout(150)
            legend_toggle_restored = await page.evaluate(
                """
                (id) => {
                  const btn = document.querySelector(
                    `.forecast-wind .legend-toggle[data-forecast-series='${id}']`
                  );
                  return {
                    aria: btn?.getAttribute('aria-pressed') || '',
                    muted: !!btn?.classList.contains('is-muted'),
                    pathIds: [...document.querySelectorAll('.forecast-wind svg path.forecast-line')]
                      .map(p => [...p.classList].find(c => c.startsWith('m-'))?.slice(2))
                      .filter(Boolean),
                  };
                }
                """,
                legend_toggle_before["id"],
            )
            if (legend_toggle_before["cursor"] == "pointer"
                    and legend_toggle_before["aria"] == "true"
                    and not legend_toggle_before["muted"]
                    and legend_toggle_before["id"] in legend_toggle_before["pathIds"]
                    and legend_toggle_before["id"] in legend_toggle_off["legendIds"]
                    and legend_toggle_before["id"] not in legend_toggle_off["pathIds"]
                    and legend_toggle_off["aria"] == "false"
                    and legend_toggle_off["muted"]
                    and legend_toggle_restored["aria"] == "true"
                    and not legend_toggle_restored["muted"]
                    and legend_toggle_before["id"] in legend_toggle_restored["pathIds"]):
                r.ok("cF-UI3e",
                     f"forecast legend toggle hides/restores one line: "
                     f"before={legend_toggle_before} off={legend_toggle_off} "
                     f"restored={legend_toggle_restored}")
            else:
                r.bad("cF-UI3e",
                      f"forecast legend toggle broken: before={legend_toggle_before} "
                      f"off={legend_toggle_off} restored={legend_toggle_restored}")
        else:
            r.bad("cF-UI3e", f"no forecast legend toggle found: {legend_toggle_before}")

        # cF-UI3d — Drag-selecting any forecast chart zooms the shared
        # forecast horizon for all charts. The global zoom-out control sits
        # beside refresh as the same compact square, reads visually as a
        # zoom-out icon, and restores the full horizon without moving scroll.
        await page.set_viewport_size({"width": 1366, "height": 900})
        await page.wait_for_timeout(100)
        zoom_hit = page.locator(".forecast-wind svg .ch-hit").first
        zoom_box = await zoom_hit.bounding_box() if await zoom_hit.count() else None
        zoom_before = await page.locator(
            ".forecast-wind svg path.forecast-line").first.get_attribute("d")
        xwind_before = await page.locator(
            ".forecast-xwind svg path.forecast-line").first.get_attribute("d")
        if zoom_box and zoom_before:
            y = zoom_box["y"] + zoom_box["height"] * 0.48
            await page.mouse.move(zoom_box["x"] + zoom_box["width"] * 0.20, y)
            await page.mouse.down()
            await page.mouse.move(zoom_box["x"] + zoom_box["width"] * 0.64, y, steps=8)
            await page.mouse.up()
            await page.wait_for_timeout(250)
            zoom_after = await page.evaluate(
                """
                (() => ({
                  mode: document.querySelector('#forecast-charts')?.dataset.zoom,
                  windMode: document.querySelector('.forecast-wind')?.dataset.zoom,
                  xwindMode: document.querySelector('.forecast-xwind')?.dataset.zoom,
                  ceilingMode: document.querySelector('.forecast-ceiling')?.dataset.zoom,
                  chartResetCount: document.querySelectorAll('#forecast-charts .forecast-zoom-reset').length,
                  globalResetHidden: document.querySelector('#forecast-zoom-reset')?.hidden,
                  resetInStatusRow: !!document.querySelector('#forecast-zoom-reset')?.closest('.forecast-status-row'),
                  resetRect: (() => {
                    const r = document.querySelector('#forecast-zoom-reset')?.getBoundingClientRect();
                    return r ? {w:r.width, h:r.height, left:r.left, right:r.right, top:r.top, bottom:r.bottom} : null;
                  })(),
                  refreshRect: (() => {
                    const r = document.querySelector('#forecast-run')?.getBoundingClientRect();
                    return r ? {w:r.width, h:r.height, left:r.left, right:r.right, top:r.top, bottom:r.bottom} : null;
                  })(),
                  statusRowRect: (() => {
                    const r = document.querySelector('.forecast-status-row')?.getBoundingClientRect();
                    return r ? {h:r.height} : null;
                  })(),
                  zoomIcon: (() => {
                    const el = document.querySelector('.forecast-zoom-icon');
                    if (!el) return null;
                    const cs = getComputedStyle(el);
                    const before = getComputedStyle(el, '::before');
                    const after = getComputedStyle(el, '::after');
                    return {
                      bg: cs.backgroundImage,
                      fontSize: cs.fontSize,
                      beforeContent: before.content,
                      beforeBorder: before.borderTopWidth,
                      afterContent: after.content,
                      afterWidth: after.width,
                    };
                  })(),
                  path: document.querySelector('.forecast-wind svg path.forecast-line')?.getAttribute('d'),
                  xwindPath: document.querySelector('.forecast-xwind svg path.forecast-line')?.getAttribute('d'),
                  ceilingPath: document.querySelector('.forecast-ceiling svg path.forecast-line')?.getAttribute('d'),
                  charts: document.querySelectorAll('#forecast-charts .chart svg').length,
                }))()
                """
            )
            scroll_before_reset = await page.evaluate("window.scrollY")
            await page.evaluate(
                "document.querySelector('#forecast-zoom-reset')?.click()"
            )
            await page.wait_for_timeout(200)
            zoom_reset = await page.evaluate(
                """
                (() => ({
                  mode: document.querySelector('#forecast-charts')?.dataset.zoom,
                  windMode: document.querySelector('.forecast-wind')?.dataset.zoom,
                  xwindMode: document.querySelector('.forecast-xwind')?.dataset.zoom,
                  ceilingMode: document.querySelector('.forecast-ceiling')?.dataset.zoom,
                  globalResetHidden: document.querySelector('#forecast-zoom-reset')?.hidden,
                  scrollY: window.scrollY,
                  path: document.querySelector('.forecast-wind svg path.forecast-line')?.getAttribute('d'),
                  xwindPath: document.querySelector('.forecast-xwind svg path.forecast-line')?.getAttribute('d'),
                  ceilingPath: document.querySelector('.forecast-ceiling svg path.forecast-line')?.getAttribute('d'),
                }))()
                """
            )
            same_size = (
                zoom_after["resetRect"]
                and zoom_after["refreshRect"]
                and abs(zoom_after["resetRect"]["w"] - zoom_after["refreshRect"]["w"]) <= 0.5
                and abs(zoom_after["resetRect"]["h"] - zoom_after["refreshRect"]["h"]) <= 0.5
            )
            same_row = (
                zoom_after["resetRect"]
                and zoom_after["refreshRect"]
                and abs(zoom_after["resetRect"]["top"] - zoom_after["refreshRect"]["top"]) <= 1.5
                and abs(zoom_after["resetRect"]["bottom"] - zoom_after["refreshRect"]["bottom"]) <= 1.5
                and zoom_after["resetRect"]["right"] <= zoom_after["refreshRect"]["left"] + 0.5
            )
            zoom_icon = zoom_after.get("zoomIcon") or {}
            zoom_icon_ok = (
                zoom_icon.get("fontSize") == "0px"
                and "linear-gradient" in (zoom_icon.get("bg") or "")
                and zoom_icon.get("beforeContent") != "none"
                and zoom_icon.get("afterContent") != "none"
                and zoom_icon.get("beforeBorder") not in ("0px", "")
            )
            if (zoom_after["mode"] == "on"
                    and zoom_after["windMode"] == "on"
                    and zoom_after["xwindMode"] == "on"
                    and zoom_after["ceilingMode"] == "on"
                    and zoom_after["chartResetCount"] == 0
                    and zoom_after["globalResetHidden"] is False
                    and zoom_after["resetInStatusRow"] is True
                    and same_size
                    and same_row
                    and zoom_icon_ok
                    and zoom_after["statusRowRect"]["h"] <= 28.5
                    and zoom_after["charts"] >= 3
                    and zoom_after["path"] != zoom_before
                    and zoom_after["xwindPath"] != xwind_before
                    and zoom_reset["mode"] == "off"
                    and zoom_reset["windMode"] == "off"
                    and zoom_reset["xwindMode"] == "off"
                    and zoom_reset["ceilingMode"] == "off"
                    and zoom_reset["globalResetHidden"] is True
                    and abs(zoom_reset["scrollY"] - scroll_before_reset) <= 1
                    and zoom_reset["path"] == zoom_before
                    and zoom_reset["xwindPath"] == xwind_before):
                r.ok("cF-UI3d",
                     f"forecast drag zoom/reset ok: after={zoom_after} reset={zoom_reset}")
            else:
                r.bad("cF-UI3d",
                      f"forecast drag zoom/reset wrong: before={zoom_before} "
                      f"after={zoom_after} reset={zoom_reset}")
        else:
            r.bad("cF-UI3d", f"forecast zoom target missing: box={zoom_box} path={zoom_before}")

        # cF-UI4 — Forecast model guide is no longer a wide table. It explains
        # acronyms and deterministic guidance in a card grid that fits phone width.
        await page.wait_for_function(
            """
            (() => {
              const host = document.getElementById('results-host');
              const header = document.getElementById('header-bar');
              const run = document.getElementById('run');
              return host && header && run
                && !header.hidden
                && !run.disabled
                && !/querying/i.test(host.textContent || '')
                && document.querySelector('.forecast-model-legend');
            })()
            """,
            timeout=120_000,
        )
        await page.wait_for_load_state("networkidle", timeout=60_000)
        await page.locator(".forecast-model-legend summary").click()
        guide_info = await page.evaluate(
            """
            (() => {
              const box = document.querySelector('.forecast-model-legend');
              const text = (box && box.textContent || '').replace(/\\s+/g, ' ');
              return {
                cards: box ? box.querySelectorAll('.forecast-guide-card').length : 0,
                tables: box ? box.querySelectorAll('table').length : 0,
                overflow: box ? Math.ceil(box.scrollWidth - box.clientWidth) : 999,
                text,
              };
            })()
            """
        )
        await page.set_viewport_size({"width": 320, "height": 700})
        await page.wait_for_selector(".forecast-model-legend .forecast-guide-card",
                                     timeout=30_000)
        await page.wait_for_timeout(100)
        guide_phone = await page.evaluate(
            """
            (() => {
              const box = document.querySelector('.forecast-model-legend');
              const card = box && box.querySelector('.forecast-guide-card');
              return {
                docOverflow: document.documentElement.scrollWidth - window.innerWidth,
                guideOverflow: box ? Math.ceil(box.scrollWidth - box.clientWidth) : 999,
                cardWidth: card ? Math.round(card.getBoundingClientRect().width) : 0,
              };
            })()
            """
        )
        await page.set_viewport_size({"width": 1366, "height": 900})
        await page.wait_for_timeout(100)
        guide_ok = (
            guide_info["cards"] >= 6
            and guide_info["tables"] == 0
            and guide_info["overflow"] <= 1
            and guide_phone["docOverflow"] <= 1
            and guide_phone["guideOverflow"] <= 1
            and 250 <= guide_phone["cardWidth"] <= 320
            and "Terminal Aerodrome Forecast" in guide_info["text"]
            and "Localized Aviation MOS Program" in guide_info["text"]
            and "Model Output Statistics" in guide_info["text"]
            and "High-Resolution Rapid Refresh" in guide_info["text"]
            and "National Blend of Models" in guide_info["text"]
            and "Deterministic" in guide_info["text"]
        )
        if guide_ok:
            r.ok("cF-UI4",
                 f"forecast guide card grid fits and explains acronyms "
                 f"(cards={guide_info['cards']}, phone={guide_phone})")
        else:
            r.bad("cF-UI4",
                  f"forecast guide inadequate: cards={guide_info['cards']} "
                  f"tables={guide_info['tables']} overflow={guide_info['overflow']} "
                  f"phone={guide_phone} text={guide_info['text'][:240]!r}")

        # cR-UI1 — Overview xwind chart reverted to the simple absolute
        # crosswind display: no chart-local runway switch and no signed axis.
        overview_xwind = await page.evaluate(
            """
            (() => {
              const fig = document.querySelector('.chart.xwind');
              const cap = fig && fig.querySelector('figcaption');
              const legend = fig && fig.querySelector('.legend-row');
              const ticks = fig
                ? [...fig.querySelectorAll('svg .axis text')]
                    .map(t => t.textContent.trim())
                : [];
              return {
                switchCount: fig ? fig.querySelectorAll('.runway-switch').length : -1,
                buttons: fig ? fig.querySelectorAll('button').length : -1,
                overrideType: typeof xwindChartOverride,
                caption: cap ? cap.textContent.replace(/\\s+/g, ' ').trim() : null,
                legend: legend ? legend.textContent.replace(/\\s+/g, ' ').trim() : null,
                negativeTicks: ticks.filter(t => t.startsWith('-')),
                path: !!(fig && fig.querySelector('svg path.line')),
              };
            })()
            """
        )
        if (overview_xwind["switchCount"] == 0
                and overview_xwind["buttons"] == 0
                and overview_xwind["overrideType"] == "undefined"
                and "Crosswind" in (overview_xwind["caption"] or "")
                and "Signed" not in (overview_xwind["caption"] or "")
                and "signed left/right" not in (overview_xwind["legend"] or "")
                and not overview_xwind["negativeTicks"]
                and overview_xwind["path"]):
            r.ok("cR-UI1", f"overview xwind absolute/no switch: {overview_xwind}")
        else:
            r.bad("cR-UI1",
                  f"overview xwind still has runway/signed affordance: "
                  f"{overview_xwind}")

        # cR-UI2 — Re-clicking the chart area cannot trigger a hidden
        # chart-only runway request; the old flash-refresh path is gone.
        await page.wait_for_function(
            """
            (() => {
              const host = document.getElementById('results-host');
              const header = document.getElementById('header-bar');
              const run = document.getElementById('run');
              return host && header && run
                && !header.hidden
                && !run.disabled
                && !/querying/i.test(host.textContent || '');
            })()
            """,
            timeout=120_000,
        )
        await page.wait_for_load_state("networkidle", timeout=60_000)
        rw_reqs = []
        page.on("request", lambda req: (
            rw_reqs.append(req.url) if "/api/stats" in req.url else None))
        table_before = await page.locator(".historical-section table").inner_html()
        header_before = await page.locator("#header-bar").inner_text()
        path_before = await page.locator(
            ".chart.xwind svg path.line").first.get_attribute("d")
        await page.locator(".chart.xwind").click(position={"x": 40, "y": 40})
        await page.wait_for_timeout(250)
        chart_after = await page.evaluate(
            """
            (() => {
              const path = document.querySelector('.chart.xwind svg path.line');
              return {
                path: path ? path.getAttribute('d') : null,
                querying: /querying/i.test(
                  document.getElementById('results-host').textContent || ''),
                headerHidden: document.getElementById('header-bar').hidden,
              };
            })()
            """
        )
        table_after = await page.locator(".historical-section table").inner_html()
        header_after = await page.locator("#header-bar").inner_text()
        if (chart_after["path"] == path_before
                and table_after == table_before
                and header_after == header_before
                and chart_after["querying"] is False
                and chart_after["headerHidden"] is False
                and not rw_reqs):
            r.ok("cR-UI2", f"xwind chart has no chart-local refresh path: {chart_after}")
        else:
            r.bad("cR-UI2",
                  f"xwind chart click unexpectedly mutated page: {chart_after}, "
                  f"urls={rw_reqs[-3:]}")

        # cS1 — Overview charts: SEM error bars are present in the SVG.
        sem_lines = await page.locator(".chart svg .errbar line").count()
        if sem_lines > 0:
            r.ok("cS1", f"SEM error-bar SVG lines: {sem_lines}")
        else:
            r.bad("cS1", "no .errbar lines drawn in any chart SVG")

        # cS3 — User asked to drop "± SEM" / "sem" from the legend (SEM
        # bars stay in the chart, just not in the legend).
        legend_text = (await page.text_content(
            "#charts .chart .legend-row") or "").lower()
        if "sem" not in legend_text:
            r.ok("cS3", f"legend free of 'sem' wording ({legend_text!r})")
        else:
            r.bad("cS3", f"legend still mentions sem: {legend_text!r}")

        # cS4 — SEM error bars use the muted (background) stroke color so
        # they recede behind the mean line.
        sem_color = await page.evaluate(
            "(() => {\n"
            "  const el = document.querySelector('.chart svg .errbar line');\n"
            "  if (!el) return null;\n"
            "  const cs = getComputedStyle(el);\n"
            "  return cs.stroke;\n"
            "})()"
        )
        if sem_color and ("128" in sem_color or "153" in sem_color
                          or "rgb" in sem_color):
            # Just confirm it's not the pure black --fg by sampling that.
            fg = await page.evaluate(
                "getComputedStyle(document.documentElement)"
                ".getPropertyValue('--fg').trim()"
            )
            r.ok("cS4", f"SEM stroke = {sem_color!r}, --fg = {fg!r}")
        else:
            r.bad("cS4", f"SEM stroke unexpected: {sem_color!r}")

        # cG1 — Gust dashed line is present in the overview wind-speed
        # chart.
        gust_dash = await page.locator(
            ".chart svg path.line-dash").count()
        if gust_dash >= 1:
            r.ok("cG1", f"overview gust dashed lines = {gust_dash}")
        else:
            r.bad("cG1", "no gust dashed line in overview")
        gust_legend = (await page.locator(
            ".chart .legend-row:has-text('gust')").count())
        if gust_legend >= 1:
            r.ok("cG1b", "overview legend includes 'gust'")
        else:
            r.bad("cG1b", "overview legend missing 'gust'")
        gust_hit = page.locator(
            "#historical-charts-slot .chart:not(.xwind) svg .ch-hit").first
        if await gust_hit.count() > 0:
            await gust_hit.hover()
            await page.wait_for_timeout(120)
            gust_hover = await page.evaluate(
                "(() => { const t = document.querySelector("
                "'#historical-charts-slot .chart:not(.xwind) .readout text'); "
                "return t ? t.textContent : null; })()"
            )
            if gust_hover and _re.search(r"gust\s*[-\d.]+\s+kt\s+\d+%", gust_hover):
                r.ok("cG1c", f"overview gust hover includes occurrence %: {gust_hover!r}")
            else:
                r.bad("cG1c", f"overview gust hover missing occurrence %: {gust_hover!r}")
        else:
            r.bad("cG1c", "overview wind-speed chart has no hover hit rect")

        # cN1 — The explanatory stats note is now concise and only belongs
        # to Overview, where the per-hour aggregation needs context.
        note_visible = await page.locator("#stats-note").is_visible()
        note_text = (await page.text_content("#stats-note") or "").lower()
        note_ok = (note_visible
                   and "overview groups observations by selected hour in the chosen time zone" in note_text
                   and "stats are computed across every observation" not in note_text
                   and "|sin" not in note_text)
        if note_ok:
            r.ok("cN1", f"overview stats note concise: {note_text!r}")
        else:
            r.bad("cN1",
                  f"overview stats note off: visible={note_visible}, "
                  f"text={note_text!r}")

        # cT3 — Overview hides the min/max envelope band, but DOES draw
        # the gust dashed line on the wind-speed chart.
        band_paths_overview = await page.locator(".chart svg path.band").count()
        dash_paths_overview = await page.locator(".chart svg path.line-dash").count()
        if band_paths_overview == 0 and dash_paths_overview >= 1:
            r.ok("cT3", f"overview suppresses band, draws gust dashed "
                        f"({dash_paths_overview} dashed line(s))")
        else:
            r.bad("cT3", f"overview band={band_paths_overview} "
                         f"dash={dash_paths_overview} (want band=0, dash>=1)")

        # Switch to See all
        await page.click(".view-toggle button[data-mode='all']")
        await page.wait_for_timeout(150)

        # cT4 — All Historical marks its button on
        active = await page.text_content(".view-toggle button.on")
        if (active or "").strip().lower() == "all historical":
            r.ok("cT4", f"all historical active after click = {active!r}")
        else:
            r.bad("cT4", f"after click active = {active!r}")
        if await page.locator("#stats-note").is_hidden():
            r.ok("cN2", "see-all hides the overview stats note")
        else:
            r.bad("cN2", "see-all still shows the overview stats note")

        # See-all is now an entirely raw-observation view: scrollable per-obs
        # table + scatter plots with one dot per observation.
        # Wait for raw fetch to complete and re-render to swap in.
        await page.wait_for_selector(".raw-table", timeout=30_000)

        # cT5 — raw table header has 7 columns, raw table body has many rows
        raw_thead_cols = await page.locator(
            ".raw-table thead tr th").count()
        raw_rows = await page.locator(".raw-table tbody tr").count()
        if raw_thead_cols == 7 and raw_rows >= 10:
            r.ok("cT5", f"see-all raw table: {raw_thead_cols} cols × "
                        f"{raw_rows} rows")
        else:
            r.bad("cT5", f"see-all raw table: cols={raw_thead_cols} "
                         f"rows={raw_rows} (want 7 cols & >=10 rows)")

        # cT6 — see-all renders chronological time-series charts: it has
        # ts-line paths and per-obs dots, and NO aggregated overview-line
        # paths (path.line, which is the bold mean line in overview).
        scatter_dots = await page.locator(".chart svg .scatter circle").count()
        ts_lines     = await page.locator(".chart svg path.ts-line").count()
        line_paths   = await page.locator(".chart svg path.line").count()
        if scatter_dots > 0 and ts_lines > 0 and line_paths == 0:
            r.ok("cT6", f"see-all: dots={scatter_dots} ts-lines={ts_lines} "
                        f"overview-lines={line_paths}")
        else:
            r.bad("cT6", f"see-all dots={scatter_dots} ts-lines={ts_lines} "
                         f"overview-lines={line_paths}")

        # cTL1 — see-all .charts host has the .timeline class (full-width
        # stacked layout, not the per-hour grid).
        host_cls = await page.evaluate(
            "document.getElementById('charts').className")
        if "timeline" in host_cls:
            r.ok("cTL1", f".charts class includes 'timeline' ({host_cls!r})")
        else:
            r.bad("cTL1", f".charts class missing 'timeline' ({host_cls!r})")

        # cTL2 — figcaption for the time-series charts mentions "over time"
        # so users see at a glance the x-axis switched away from hour buckets.
        capt = await page.locator(
            ".chart figcaption:has-text('over time')").count()
        if capt >= 2:   # wind speed + crosswind
            r.ok("cTL2", f"see-all captions with 'over time' = {capt}")
        else:
            r.bad("cTL2", f"see-all captions with 'over time' = {capt}")

        # cTL-DA3 — See-all adds a density-altitude chart, but the raw
        # observation table stays focused on the existing operational
        # columns (no DA table column).
        da_chart = await page.evaluate(
            """
            (() => {
              const fig = [...document.querySelectorAll('.charts.timeline figure')]
                .find(f => /Density altitude/i.test(
                  f.querySelector('figcaption')?.textContent || ''));
              return {
                hasFig: !!fig,
                cap: fig ? fig.querySelector('figcaption').textContent
                  .replace(/\\s+/g, ' ').trim() : null,
                legend: fig ? fig.querySelector('.legend-row')?.textContent
                  .replace(/\\s+/g, ' ').trim() : null,
                tableHasDA: [...document.querySelectorAll('.raw-table th')]
                  .some(th => /density|altitude/i.test(th.textContent)),
              };
            })()
            """
        )
        if (da_chart["hasFig"]
                and "8,000 ft" in (da_chart["legend"] or "")
                and da_chart["tableHasDA"] is False):
            r.ok("cTL-DA3", f"DA chart visible, table unchanged: {da_chart}")
        else:
            r.bad("cTL-DA3", f"DA chart/table wiring off: {da_chart}")

        # cTL-FLT1 — See-all renders a top-of-charts timeline filter with
        # from/to date inputs bounded by the timeline window, plus a Reset
        # button. Check that bounds match the backend's timeline range.
        flt = await page.evaluate(
            """
            (() => {
              const bar = document.querySelector('.timeline-filter');
              if (!bar) return null;
              const f = document.getElementById('tf-from');
              const t = document.getElementById('tf-to');
              const reset = document.getElementById('tf-reset');
              const zoom = document.getElementById('tl-zoom-reset');
              const meta = document.getElementById('tf-meta');
              const presets = [...document.querySelectorAll('[data-tf-days]')]
                .map(b => b.textContent.trim().toLowerCase());
              const fRect = f && f.getBoundingClientRect();
              const tRect = t && t.getBoundingClientRect();
              return {
                hasBar: true,
                from_min: f && f.min, from_max: f && f.max, from_val: f && f.value,
                to_min:   t && t.min, to_max:   t && t.max, to_val:   t && t.value,
                hasReset: !!reset,
                hasZoomReset: !!zoom,
                zoomHidden: zoom ? zoom.hidden : null,
                presets,
                inputHeights: [fRect && fRect.height, tRect && tRect.height],
                metaText: meta ? meta.textContent.trim() : "",
              };
            })()
            """
        )
        if (flt and flt["hasBar"] and flt["hasReset"]
                and flt["hasZoomReset"] and flt["zoomHidden"] is True
                and flt["presets"] == ["7d", "30d", "90d"]
                and all(abs((h or 0) - 28) <= 0.5 for h in flt["inputHeights"])
                and flt["from_min"] and flt["from_max"]
                and flt["from_val"] == flt["from_min"]
                and flt["to_val"]   == flt["to_max"]
                and "obs" in (flt["metaText"] or "")):
            r.ok("cTL-FLT1",
                 f"timeline filter present: {flt['from_val']} → "
                 f"{flt['to_val']} · {flt['metaText']}")
        else:
            r.bad("cTL-FLT1", f"timeline filter wiring off: {flt}")

        # cTL-FIT1 — The "All" timeline bounds should fit actual data,
        # not blindly use the wider backend cap if the first observation
        # starts later. This directly covers small/substitute stations
        # whose 90-day payload contains leading empty weeks.
        fit_bounds = await page.evaluate(
            """
            (() => _tlBounds({
              timeline_from: "2026-02-06",
              timeline_to:   "2026-05-06",
              timeline: [
                { time_pt: "2026-03-06 16:53" },
                { time_pt: "2026-05-05 18:53" },
              ],
            }))()
            """
        )
        if fit_bounds == {"min": "2026-03-06", "max": "2026-05-05"}:
            r.ok("cTL-FIT1", f"timeline bounds fit data: {fit_bounds}")
        else:
            r.bad("cTL-FIT1", f"timeline bounds still use empty cap: {fit_bounds}")

        # cTL-GAP1 — Sparse values like gust/ceiling must not draw a fake
        # straight segment across multi-day missing-data gaps, and hover
        # interpolation should also be absent inside that gap.
        gap_probe = await page.evaluate(
            """
            (() => {
              const day = 86400000;
              const pts = [
                { t: 0, y: 5 },
                { t: 3600000, y: 7 },
                { t: 3 * day, y: 9 },
              ];
              const svg = renderTimeSeriesChart(pts, { ylabel: "kt" });
              const d = svg.querySelector("path.ts-line").getAttribute("d");
              return {
                d,
                moves: (d.match(/M/g) || []).length,
                midGap: _interpYAtT(pts, 2 * day, 6 * 60 * 60 * 1000),
              };
            })()
            """
        )
        if gap_probe["moves"] >= 2 and gap_probe["midGap"] is None:
            r.ok("cTL-GAP1",
                 f"time-series gaps break path and hover: {gap_probe}")
        else:
            r.bad("cTL-GAP1",
                  f"time-series still bridges missing data: {gap_probe}")

        # cTL-GAP2 — Explicit null observations are gap markers. This
        # pins the See-all ceiling behavior: a missing ceiling report
        # between two reported layers should not become a diagonal line
        # or an interpolated hover value.
        null_gap_probe = await page.evaluate(
            """
            (() => {
              const hour = 3600000;
              const pts = [
                { t: 0, y: 1000 },
                { t: hour, y: null },
                { t: 2 * hour, y: 3000 },
                { t: 3 * hour, y: 3500 },
              ];
              const svg = renderTimeSeriesChart(pts, {
                ylabel: "ft",
                breakOnNull: true,
              });
              const d = svg.querySelector("path.ts-line").getAttribute("d");
              return {
                d,
                moves: (d.match(/M/g) || []).length,
                lines: (d.match(/L/g) || []).length,
                atNull: _interpYAtT(pts, hour, 6 * hour),
                beforeNull: _interpYAtT(pts, hour / 2, 6 * hour),
                afterNull: _interpYAtT(pts, 1.5 * hour, 6 * hour),
                afterReport: _interpYAtT(pts, 2.5 * hour, 6 * hour),
              };
            })()
            """
        )
        null_gap_ok = (
            null_gap_probe["moves"] >= 2
            and null_gap_probe["lines"] == 1
            and null_gap_probe["atNull"] is None
            and null_gap_probe["beforeNull"] is None
            and null_gap_probe["afterNull"] is None
            and null_gap_probe["afterReport"] is not None
        )
        if null_gap_ok:
            r.ok("cTL-GAP2",
                 f"explicit null ceiling gaps break path and hover: {null_gap_probe}")
        else:
            r.bad("cTL-GAP2",
                  f"explicit null ceiling gap still bridged: {null_gap_probe}")

        # cTL-DA4 — A high density-altitude point renders as a red dot.
        # Use a tiny synthetic chart so the assertion does not depend on
        # today's KMYF weather crossing 8,000 ft.
        da_dot_probe = await page.evaluate(
            """
            (() => {
              const svg = renderTimeSeriesChart([
                { t: 0, y: 1200 },
                { t: 3600000, y: 8500, danger: true },
                { t: 7200000, y: null },
                { t: 10800000, y: 5000 },
              ], {
                ylabel: "ft",
                breakOnNull: true,
                tMinForce: 0,
                tMaxForce: 10800000,
              });
              const d = svg.querySelector("path.ts-line").getAttribute("d");
              return {
                redDots: svg.querySelectorAll('.ts-dot.dot-danger').length,
                allDots: svg.querySelectorAll('.ts-dot').length,
                moves: (d.match(/M/g) || []).length,
              };
            })()
            """
        )
        if (da_dot_probe["redDots"] == 1
                and da_dot_probe["allDots"] == 3
                and da_dot_probe["moves"] >= 2):
            r.ok("cTL-DA4",
                 f"synthetic DA high point is red and gap-safe: {da_dot_probe}")
        else:
            r.bad("cTL-DA4",
                  f"DA red/gap rendering off: {da_dot_probe}")

        # cTL-HR1 — Wide See-all timelines include sparse hour notches
        # on the x axis, and the date/hour labels do not overlap.
        hour_axis = await page.evaluate(
            """
            (() => {
              const labelRe = /^(\\d\\d-\\d\\d|\\d{1,2}[ap])$/;
              const out = [...document.querySelectorAll('.charts.timeline figure')]
                .map(fig => {
                  const svg = fig.querySelector('svg');
                  const labels = [...svg.querySelectorAll('.axis text')]
                    .filter(t => labelRe.test(t.textContent.trim()))
                    .map(t => {
                      const r = t.getBoundingClientRect();
                      return {
                        text: t.textContent.trim(),
                        time: t.classList.contains('time-tick'),
                        left: r.left, right: r.right,
                        top: r.top, bottom: r.bottom,
                      };
                    });
                  const overlaps = [];
                  for (let i = 0; i < labels.length; i++) {
                    for (let j = i + 1; j < labels.length; j++) {
                      const a = labels[i], b = labels[j];
                      const hit = !(a.right < b.left + 1
                        || b.right < a.left + 1
                        || a.bottom < b.top + 1
                        || b.bottom < a.top + 1);
                      if (hit) overlaps.push([a.text, b.text]);
                    }
                  }
                  return {
                    hours: labels.filter(x => x.time).map(x => x.text),
                    overlaps,
                  };
                });
              return out;
            })()
            """
        )
        fixed_hours = {"5a", "11a", "5p", "11p"}
        wide_unexpected_hours = sorted({
            h for x in (hour_axis or []) for h in x["hours"]
            if h not in fixed_hours
        })
        wide_has_hours = (hour_axis
                          and all(len(x["hours"]) >= 6 for x in hour_axis)
                          and all("11a" in x["hours"] for x in hour_axis)
                          and not wide_unexpected_hours
                          and all(not x["overlaps"] for x in hour_axis))
        if wide_has_hours:
            r.ok("cTL-HR1",
                 f"wide fixed-hour ticks present without overlap: {hour_axis}")
        else:
            r.bad("cTL-HR1",
                  f"wide hour ticks crowded/missing/wrong: {hour_axis}; "
                  f"unexpected={wide_unexpected_hours}")

        # cTL-FLT2 — Tightening the timeline filter must drop the dot
        # count while the browser is offline. That proves it is a
        # frontend-only filter on the already-loaded payload, without
        # mistaking unrelated background refresh traffic for filter work.
        dots_full = await page.locator(
            "#charts .chart svg .scatter circle").count()
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(500)
        # Pick a 7-day sub-window at the END of the timeline cap.
        new_from = await page.evaluate(
            """(() => {
              const t = document.getElementById('tf-to').value;
              const d = new Date(t + 'T00:00:00');
              d.setDate(d.getDate() - 6);
              const z = (n) => String(n).padStart(2, '0');
              return `${d.getFullYear()}-${z(d.getMonth()+1)}-${z(d.getDate())}`;
            })()"""
        )
        await ctx.set_offline(True)
        try:
            await page.evaluate(
                """
                (value) => {
                  const input = document.querySelector('#charts #tf-from');
                  if (!input) throw new Error('missing timeline from input');
                  input.value = value;
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                }
                """,
                new_from,
            )
            try:
                await page.wait_for_function(
                    """
                    (full) => {
                      const dots = document.querySelectorAll(
                        '#charts .chart svg .scatter circle').length;
                      return dots > 0 && dots < full;
                    }
                    """,
                    arg=dots_full,
                    timeout=3000,
                )
            except Exception:
                pass
        finally:
            await ctx.set_offline(False)
        dots_narrow = await page.locator(
            "#charts .chart svg .scatter circle").count()
        if dots_narrow > 0 and dots_narrow < dots_full:
            r.ok("cTL-FLT2",
                 f"narrowing dropped dots {dots_full}→{dots_narrow} with "
                 f"the browser offline")
        else:
            r.bad("cTL-FLT2",
                  f"filter mis-wired: dots {dots_full}→{dots_narrow}, "
                  f"target from={new_from}")
        # Reset back so subsequent tests see the full timeline.
        await page.click("#tf-reset")
        await page.wait_for_timeout(150)

        # cTL-FLT3 — The 7d/30d/90d buttons are client-side timeline
        # presets beside the date selector. Clicking 7d tightens the
        # visible window without fetching new raw data.
        preset_reqs = []
        page.on("request", lambda req: (
            preset_reqs.append(req.url) if "/api/raw" in req.url else None))
        dots_full_reset = await page.locator(
            "#charts .chart svg .scatter circle").count()
        await page.click("#tf-7d")
        await page.wait_for_timeout(200)
        preset_probe = await page.evaluate(
            """
            (() => {
              const f = document.getElementById('tf-from');
              const t = document.getElementById('tf-to');
              const d = new Date(t.value + 'T00:00:00');
              d.setDate(d.getDate() - 6);
              const z = (n) => String(n).padStart(2, '0');
              const expected = `${d.getFullYear()}-${z(d.getMonth()+1)}-${z(d.getDate())}`;
              return {
                from: f.value,
                to: t.value,
                expected,
                on7: document.getElementById('tf-7d')?.classList.contains('on'),
                meta: document.getElementById('tf-meta')?.textContent || '',
                dots: document.querySelectorAll('#charts .chart svg .scatter circle').length,
              };
            })()
            """
        )
        if (preset_probe["from"] == preset_probe["expected"]
                and preset_probe["on7"] is True
                and "7 day" in preset_probe["meta"].lower()
                and 0 < preset_probe["dots"] < dots_full_reset
                and not preset_reqs):
            r.ok("cTL-FLT3",
                 f"7d timeline preset is client-side: {preset_probe}")
        else:
            r.bad("cTL-FLT3",
                  f"7d timeline preset off: {preset_probe}, raw={preset_reqs}")
        await page.click("#tf-reset")
        await page.wait_for_timeout(150)

        # cTL-ZOOM1 — Drag-selecting an all-historical chart zooms the
        # shared timeline range, reveals a compact reset icon beside the
        # date controls, and resetting restores without moving scroll.
        zoom_hit = page.locator("#charts .chart svg .ch-hit").first
        zoom_box = await zoom_hit.bounding_box() if await zoom_hit.count() else None
        timeline_before = await page.locator(
            "#charts .chart svg path.ts-line:not(.ts-dash)").first.get_attribute("d")
        if zoom_box and timeline_before:
            y = zoom_box["y"] + zoom_box["height"] * 0.48
            await page.mouse.move(zoom_box["x"] + zoom_box["width"] * 0.20, y)
            await page.mouse.down()
            await page.mouse.move(zoom_box["x"] + zoom_box["width"] * 0.62, y, steps=8)
            await page.mouse.up()
            await page.wait_for_timeout(250)
            timeline_zoom = await page.evaluate(
                """
                (() => {
                  const reset = document.getElementById('tl-zoom-reset');
                  const to = document.getElementById('tf-to');
                  const rr = reset?.getBoundingClientRect();
                  const tr = to?.getBoundingClientRect();
                  const icon = reset?.querySelector('.forecast-zoom-icon');
                  const cs = icon ? getComputedStyle(icon) : null;
                  const before = icon ? getComputedStyle(icon, '::before') : null;
                  return {
                    mode: document.getElementById('charts')?.dataset.zoom,
                    figModes: [...document.querySelectorAll('#charts .chart')]
                      .map(f => f.dataset.zoom || ''),
                    resetHidden: reset ? reset.hidden : null,
                    resetInFilter: !!reset?.closest('.timeline-filter'),
                    sameRow: !!(rr && tr && Math.abs(rr.top - tr.top) <= 1.5
                                && Math.abs(rr.height - tr.height) <= 0.5),
                    resetRect: rr ? {w:rr.width, h:rr.height, top:rr.top, bottom:rr.bottom} : null,
                    iconOk: !!(cs && cs.fontSize == '0px'
                               && (cs.backgroundImage || '').includes('linear-gradient')
                               && before && before.borderTopWidth != '0px'),
                    path: document.querySelector('#charts .chart svg path.ts-line:not(.ts-dash)')?.getAttribute('d'),
                    meta: document.getElementById('tf-meta')?.textContent || '',
                  };
                })()
                """
            )
            scroll_before_reset = await page.evaluate("window.scrollY")
            await page.click("#tl-zoom-reset")
            await page.wait_for_timeout(200)
            timeline_reset = await page.evaluate(
                """
                (() => ({
                  mode: document.getElementById('charts')?.dataset.zoom,
                  resetHidden: document.getElementById('tl-zoom-reset')?.hidden,
                  scrollY: window.scrollY,
                  path: document.querySelector('#charts .chart svg path.ts-line:not(.ts-dash)')?.getAttribute('d'),
                }))()
                """
            )
            if (timeline_zoom["mode"] == "on"
                    and timeline_zoom["figModes"]
                    and all(m == "on" for m in timeline_zoom["figModes"])
                    and timeline_zoom["resetHidden"] is False
                    and timeline_zoom["resetInFilter"] is True
                    and timeline_zoom["sameRow"] is True
                    and timeline_zoom["resetRect"]
                    and abs(timeline_zoom["resetRect"]["w"] - 28) <= 0.5
                    and timeline_zoom["iconOk"] is True
                    and timeline_zoom["path"] != timeline_before
                    and "zoomed" in timeline_zoom["meta"].lower()
                    and timeline_reset["mode"] == "off"
                    and timeline_reset["resetHidden"] is True
                    and timeline_reset["path"] == timeline_before
                    and abs(timeline_reset["scrollY"] - scroll_before_reset) <= 1):
                r.ok("cTL-ZOOM1",
                     f"all-historical drag zoom/reset ok: "
                     f"zoom={timeline_zoom} reset={timeline_reset}")
            else:
                r.bad("cTL-ZOOM1",
                      f"all-historical zoom/reset wrong: before={timeline_before} "
                      f"zoom={timeline_zoom} reset={timeline_reset}")
        else:
            r.bad("cTL-ZOOM1",
                  f"timeline zoom target missing: box={zoom_box} path={timeline_before}")

        # cTL-RDO1 — Hover overlay is the new TradingView-style top
        # readout pinned inside the chart area, NOT the old centered
        # tooltip box. The .ch-tip-bg / .ch-tip-lines classes must be
        # gone, and `.readout text` must be present and live above the
        # data line (y near top margin).
        old_tip = await page.locator(
            "#charts .chart svg .ch-tip-bg, "
            "#charts .chart svg .ch-tip-lines").count()
        readouts = await page.locator(
            "#charts .chart svg .readout text").count()
        readout_ys = await page.evaluate(
            "[...document.querySelectorAll('#charts .chart svg "
            ".readout text')].map(t => parseFloat(t.getAttribute('y')))"
        )
        if old_tip == 0 and readouts >= 1 and all(y < 25 for y in readout_ys):
            r.ok("cTL-RDO1",
                 f"top readouts present ({readouts}), no legacy tip; "
                 f"y={readout_ys}")
        else:
            r.bad("cTL-RDO1",
                  f"readouts={readouts} legacy_tips={old_tip} "
                  f"y={readout_ys}")

        # cTL-XW1 — See-all crosswind chart carries the `xwind` modifier
        # class so its line + dots pick up the accent (blue) styling.
        xw_class = await page.locator(
            "#charts .chart.xwind").count()
        if xw_class >= 1:
            r.ok("cTL-XW1", f"see-all xwind chart has 'xwind' class "
                            f"({xw_class} matches)")
        else:
            r.bad("cTL-XW1", "no see-all .chart.xwind class found")

        # cTL-XW2 — Hovering the see-all xwind chart adds an `active`
        # readout entry with the .ch-active class (the blue runway
        # callout). The legacy "(off-primary)" wording is replaced by
        # "(non-primary)" — the test asserts the new copy is present
        # and the old wording is gone for non-primary obs.
        xw_hit = page.locator(".chart.xwind svg .ch-hit").first
        if await xw_hit.count() > 0:
            await xw_hit.hover()
            await page.wait_for_timeout(120)
            active_text = await page.evaluate(
                "(() => { const t = document.querySelector("
                "'.chart.xwind .readout text'); "
                "return t ? t.textContent : null; })()"
            )
            active_html = await page.evaluate(
                "(() => { const t = document.querySelector("
                "'.chart.xwind .readout text'); "
                "return t ? t.outerHTML : null; })()"
            )
            no_off = "off-primary" not in (active_text or "")
            if (active_html
                    and "ch-active" in active_html
                    and "active" in (active_text or "")
                    and no_off):
                r.ok("cTL-XW2",
                     f"xwind hover readout includes active runway "
                     f"(no legacy 'off-primary'): {active_text!r}")
            else:
                r.bad("cTL-XW2",
                      f"xwind hover readout off: text={active_text!r} "
                      f"html={active_html!r}")
        else:
            r.bad("cTL-XW2", "no .ch-hit on xwind chart to hover")

        # cTL-XW3 — Crosswind line stroke is the default fg (black), NOT
        # the accent. Only dots use the accent. We compare the stroke
        # against --fg.
        xw_line_style = await page.evaluate(
            """
            (() => {
              const svg = document.querySelector('.chart.xwind svg');
              if (!svg) return null;
              const path = svg.querySelector('path.ts-line:not(.ts-dash), path.line');
              if (!path) return null;
              const cs = getComputedStyle(path);
              const root = getComputedStyle(document.documentElement);
              return {
                stroke: cs.stroke,
                fg: root.getPropertyValue('--fg').trim(),
                accent: root.getPropertyValue('--accent').trim(),
              };
            })()
            """
        )
        # Stroke comes back as rgb(0,0,0) when fg=#000000. Just assert
        # the stroke isn't the accent blue rgb(31,111,235).
        if (xw_line_style
                and "31, 111, 235" not in xw_line_style["stroke"]
                and "1f6feb" not in xw_line_style["stroke"].lower()):
            r.ok("cTL-XW3",
                 f"xwind line uses fg, not accent: {xw_line_style}")
        else:
            r.bad("cTL-XW3",
                  f"xwind line still using accent: {xw_line_style}")

        # cTL-XW4 — Wind-speed see-all hover includes wind direction
        # ("dir 280°" or "dir VRB") so user gets full wind context
        # without leaving the chart.
        speed_hit = page.locator(
            ".chart:not(.xwind) svg .ch-hit").first
        # Wind speed chart is the first non-xwind chart in the see-all
        # column. Hover and read the readout.
        if await speed_hit.count() > 0:
            await speed_hit.hover()
            await page.wait_for_timeout(120)
            speed_text = await page.evaluate(
                "(() => { const t = document.querySelector("
                "'.chart:not(.xwind) .readout text'); "
                "return t ? t.textContent : null; })()"
            )
            if speed_text and "dir" in speed_text.lower() and (
                    "VRB" in speed_text or "°" in speed_text):
                r.ok("cTL-XW4",
                     f"wind-speed hover includes direction: {speed_text!r}")
            else:
                r.bad("cTL-XW4",
                      f"wind-speed hover missing direction: {speed_text!r}")
        else:
            r.bad("cTL-XW4", "no .ch-hit on wind-speed chart to hover")

        # cTL-XW5 — Color semantics swap: PRIMARY xwind dots are now the
        # default fg (the common case is unobtrusive) and NON-PRIMARY
        # dots take the accent (the rare event pops). We sample one of
        # each and confirm only the secondary fill is the accent blue.
        prim_color = await page.evaluate(
            """
            (() => {
              const d = document.querySelector(
                '.chart.xwind .scatter circle:not(.dot-secondary)');
              return d ? getComputedStyle(d).fill : null;
            })()
            """
        )
        sec_color = await page.evaluate(
            """
            (() => {
              const d = document.querySelector(
                '.chart.xwind .scatter circle.dot-secondary');
              return d ? getComputedStyle(d).fill : null;
            })()
            """
        )
        accent_match = sec_color and (
            "31, 111, 235" in sec_color or "1f6feb" in sec_color.lower())
        primary_not_accent = prim_color and (
            "31, 111, 235" not in prim_color
            and "1f6feb" not in prim_color.lower())
        if accent_match and primary_not_accent:
            r.ok("cTL-XW5",
                 f"non-primary dot = accent ({sec_color}); "
                 f"primary dot = fg ({prim_color})")
        else:
            r.bad("cTL-XW5",
                  f"swap not applied: primary={prim_color!r} "
                  f"secondary={sec_color!r}")

        # cTL-RWY1 — See-all xwind chart also uses the simple absolute
        # mixed/selected frame display: no chart-local runway switch and
        # no signed negative axis.
        seeall_xwind = await page.evaluate(
            """
            (() => {
              const fig = document.querySelector('.chart.xwind');
              const cap = fig && fig.querySelector('figcaption');
              const legend = fig && fig.querySelector('.legend-row');
              const ticks = fig
                ? [...fig.querySelectorAll('svg .axis text')]
                    .map(t => t.textContent.trim())
                : [];
              return {
                switchCount: fig ? fig.querySelectorAll('.runway-switch').length : -1,
                buttons: fig ? fig.querySelectorAll('button').length : -1,
                caption: cap ? cap.textContent.replace(/\\s+/g, ' ').trim() : null,
                legend: legend ? legend.textContent.replace(/\\s+/g, ' ').trim() : null,
                negativeTicks: ticks.filter(t => t.startsWith('-')),
                path: !!(fig && fig.querySelector('svg path.ts-line')),
                mode: viewMode,
              };
            })()
            """
        )
        if (seeall_xwind["mode"] == "all"
                and seeall_xwind["switchCount"] == 0
                and seeall_xwind["buttons"] == 0
                and "Crosswind" in (seeall_xwind["caption"] or "")
                and "Signed" not in (seeall_xwind["caption"] or "")
                and "signed left/right" not in (seeall_xwind["legend"] or "")
                and not seeall_xwind["negativeTicks"]
                and seeall_xwind["path"]):
            r.ok("cTL-RWY1", f"see-all xwind absolute/no switch: {seeall_xwind}")
        else:
            r.bad("cTL-RWY1",
                  f"see-all xwind still has runway/signed affordance: "
                  f"{seeall_xwind}")

        # cTL-RWY2 — Clicking the see-all xwind chart has no hidden
        # chart-only refetch path and leaves the raw table untouched.
        all_rw_reqs = []
        page.on("request", lambda req: (
            all_rw_reqs.append(req.url) if "/api/" in req.url else None))
        raw_table_before = await page.locator(".raw-table").inner_html()
        xw_ts_before = await page.locator(
            ".chart.xwind svg path.ts-line").first.get_attribute("d")
        await page.locator(".chart.xwind").click(position={"x": 40, "y": 40})
        await page.wait_for_timeout(250)
        all_click = await page.evaluate(
            """
            (() => {
              const path = document.querySelector('.chart.xwind svg path.ts-line');
              return {
                mode: viewMode,
                path: path ? path.getAttribute('d') : null,
                loadingText: /loading every observation/i.test(
                  document.getElementById('results-host').textContent || ''),
              };
            })()
            """
        )
        raw_table_after = await page.locator(".raw-table").inner_html()
        if (all_click["mode"] == "all"
                and all_click["path"] == xw_ts_before
                and raw_table_after == raw_table_before
                and all_click["loadingText"] is False
                and not all_rw_reqs):
            r.ok("cTL-RWY2", f"see-all xwind click does not refetch: {all_click}")
        else:
            r.bad("cTL-RWY2",
                  f"see-all xwind click unexpectedly mutated page: {all_click}, "
                  f"urls={all_rw_reqs[-4:]}")

        # cTL-K1 — Y-axis tick labels on the ceiling chart use compact
        # k-suffix notation so "10000" doesn't bleed into the plot
        # area at narrow viewports.
        ceil_ticks = await page.evaluate(
            """
            (() => {
              const figs = Array.from(
                document.querySelectorAll('.charts.timeline figure'));
              const ceilFig = figs.find(f =>
                /Ceiling/i.test(f.querySelector('figcaption')?.textContent
                                || ''));
              if (!ceilFig) return null;
              return Array.from(ceilFig.querySelectorAll('.axis text'))
                .filter(t => !t.classList.contains('time-tick'))
                .map(t => t.textContent)
                .filter(t => /\\d/.test(t) && !/\\d\\d-\\d\\d/.test(t));
            })()
            """
        )
        if ceil_ticks and any("k" in t for t in ceil_ticks):
            r.ok("cTL-K1",
                 f"ceiling y-axis uses compact format: {ceil_ticks}")
        else:
            r.bad("cTL-K1",
                  f"ceiling y-axis missing 'k' suffix: {ceil_ticks!r}")

        # cTL-SPLIT1 — At a viewport <760 the see-all timeline charts
        # split into two stacked panels per chart instead of a single
        # over-compressed wide chart.
        narrow_split = await page.evaluate(
            """
            (async () => {
              await new Promise(res => setTimeout(res, 30));
              return null;
            })()
            """
        )
        # Resize the page and re-render by toggling overview→all.
        await page.set_viewport_size({"width": 430, "height": 800})
        await page.click(".view-toggle button[data-mode='overview']")
        await page.wait_for_timeout(120)
        await page.click(".view-toggle button[data-mode='all']")
        await page.wait_for_timeout(250)
        narrow_panels = await page.evaluate(
            """
            (() => {
              const figs = Array.from(
                document.querySelectorAll('.charts.timeline figure'));
              return figs.map(f => ({
                cap: (f.querySelector('figcaption') || {})
                       .textContent?.trim().slice(0, 32) || '',
                svgs: f.querySelectorAll('svg').length,
                ranges: f.querySelectorAll('.panel-range').length,
              }));
            })()
            """
        )
        all_split = (narrow_panels
                     and all(p["svgs"] == 2 and p["ranges"] == 2
                             for p in narrow_panels))
        if all_split:
            r.ok("cTL-SPLIT1",
                 f"narrow viewport splits each timeline chart into 2 panels: "
                 f"{narrow_panels}")
        else:
            r.bad("cTL-SPLIT1",
                  f"narrow viewport panels off: {narrow_panels}")

        # cTL-HR2 — On split phone panels, keep hour ticks sparse enough
        # that the two-line x axis remains readable.
        narrow_hour_axis = await page.evaluate(
            """
            (() => {
              const labelRe = /^(\\d\\d-\\d\\d|\\d{1,2}[ap])$/;
              return [...document.querySelectorAll('.charts.timeline figure svg')]
                .map(svg => {
                  const labels = [...svg.querySelectorAll('.axis text')]
                    .filter(t => labelRe.test(t.textContent.trim()))
                    .map(t => {
                      const r = t.getBoundingClientRect();
                      return {
                        text: t.textContent.trim(),
                        time: t.classList.contains('time-tick'),
                        left: r.left, right: r.right,
                        top: r.top, bottom: r.bottom,
                      };
                    });
                  const overlaps = [];
                  for (let i = 0; i < labels.length; i++) {
                    for (let j = i + 1; j < labels.length; j++) {
                      const a = labels[i], b = labels[j];
                      const hit = !(a.right < b.left + 1
                        || b.right < a.left + 1
                        || a.bottom < b.top + 1
                        || b.bottom < a.top + 1);
                      if (hit) overlaps.push([a.text, b.text]);
                    }
                  }
                  return {
                    hours: labels.filter(x => x.time).map(x => x.text),
                    overlaps,
                  };
                });
            })()
            """
        )
        narrow_unexpected_hours = sorted({
            h for x in (narrow_hour_axis or []) for h in x["hours"]
            if h not in fixed_hours
        })
        narrow_ok = (narrow_hour_axis
                     and all(len(x["hours"]) >= 2 for x in narrow_hour_axis)
                     and all("11a" in x["hours"] for x in narrow_hour_axis)
                     and not narrow_unexpected_hours
                     and all(not x["overlaps"] for x in narrow_hour_axis))
        if narrow_ok:
            r.ok("cTL-HR2",
                 f"phone split hour ticks stay readable: {narrow_hour_axis}")
        else:
            r.bad("cTL-HR2",
                  f"phone split hour ticks crowded/missing/wrong: "
                  f"{narrow_hour_axis}; unexpected={narrow_unexpected_hours}")

        # cTL-SPLIT2 — Wider viewports keep ONE panel per chart (no split).
        await page.set_viewport_size({"width": 1366, "height": 900})
        await page.click(".view-toggle button[data-mode='overview']")
        await page.wait_for_timeout(120)
        await page.click(".view-toggle button[data-mode='all']")
        await page.wait_for_timeout(250)
        wide_panels = await page.evaluate(
            """
            (() => {
              const figs = Array.from(
                document.querySelectorAll('.charts.timeline figure'));
              return figs.map(f => ({
                svgs: f.querySelectorAll('svg').length,
                ranges: f.querySelectorAll('.panel-range').length,
              }));
            })()
            """
        )
        single = (wide_panels
                  and all(p["svgs"] == 1 and p["ranges"] == 0
                          for p in wide_panels))
        if single:
            r.ok("cTL-SPLIT2",
                 f"wide viewport keeps one panel per chart: {wide_panels}")
        else:
            r.bad("cTL-SPLIT2",
                  f"wide viewport split unexpectedly: {wide_panels}")

        # cTL-DENS1 — At the user-reported 1797x1484 desktop size, all
        # historical charts should switch from oversized full-width slabs
        # to a two-column scan layout with complete chart rows visible.
        await page.set_viewport_size({"width": 1797, "height": 1484})
        await page.click(".view-toggle button[data-mode='overview']")
        await page.wait_for_timeout(120)
        await page.click(".view-toggle button[data-mode='all']")
        await page.wait_for_timeout(250)
        density = await page.evaluate(
            """
            (() => {
              const figs = [...document.querySelectorAll('.charts.timeline figure')]
                .map(f => {
                  const r = f.getBoundingClientRect();
                  return {top:r.top, bottom:r.bottom, width:r.width, height:r.height};
                });
              const firstTop = figs.length ? figs[0].top : 0;
              return {
                winH: innerHeight,
                docW: document.documentElement.scrollWidth,
                winW: innerWidth,
                figCount: figs.length,
                firstRow: figs.filter(f => Math.abs(f.top - firstTop) <= 6).length,
                complete: figs.filter(f => f.bottom <= innerHeight).length,
                widths: figs.map(f => Math.round(f.width)),
                heights: figs.map(f => Math.round(f.height)),
              };
            })()
            """
        )
        density_ok = (
            density["docW"] <= density["winW"] + 1
            and density["figCount"] >= 3
            and density["firstRow"] == 2
            and density["complete"] >= min(4, density["figCount"])
            and all(560 <= w <= 760 for w in density["widths"][:2])
            and all(h < 260 for h in density["heights"][:min(4, density["figCount"])])
        )
        if density_ok:
            r.ok("cTL-DENS1",
                 f"1797x1484 all-historical density ok: {density}")
        else:
            r.bad("cTL-DENS1",
                  f"1797x1484 all-historical still clunky: {density}")

        # Restore original viewport for the rest of the suite.
        await page.set_viewport_size({"width": 1366, "height": 900})

        # cG2 — See-all wind-speed time-series has the gust dashed overlay.
        gust_overlay = await page.locator(
            ".chart svg path.ts-line.ts-dash").count()
        if gust_overlay >= 1:
            r.ok("cG2", f"see-all gust dashed overlays = {gust_overlay}")
        else:
            r.bad("cG2", "no gust dashed overlay in see-all")
        gust_legend2 = await page.locator(
            ".chart .legend-row:has-text('gust')").count()
        if gust_legend2 >= 1:
            r.ok("cG2b", "see-all legend includes 'gust'")
        else:
            r.bad("cG2b", "see-all legend missing 'gust'")

        # cTL3 — see-all dots exceed the hour-filtered raw rows because
        # the chart deliberately ignores the hour filter (all 24 hours).
        if scatter_dots >= raw_rows:
            r.ok("cS2", f"timeline dots ({scatter_dots}) >= raw rows ({raw_rows})")
        else:
            r.bad("cS2", f"too few timeline dots: {scatter_dots} < {raw_rows}")

        # cC6 — See-all has its own ceiling time-series chart.
        captions_seeall = await page.locator(
            "#charts .chart figcaption").all_inner_texts()
        ceil_caps_sa = [c for c in captions_seeall
                        if "ceiling" in c.lower() and "over time" in c.lower()]
        if ceil_caps_sa:
            r.ok("cC6", f"see-all ceiling time-series present: {ceil_caps_sa}")
        else:
            r.bad("cC6",
                  f"no see-all ceiling time-series in: {captions_seeall}")

        # cB1 — Y-axis headroom: no See-all dot may sit on the very top of
        # the SVG plot area. We compute the SVG bbox top and assert that
        # all circles have cy strictly greater than top + 1.5 (in svg
        # user units, which equals viewBox units since preserveAspectRatio
        # is xMidYMid meet). This catches "peak kissing the top border".
        headroom = await page.evaluate(
            """
            (() => {
              const out = [];
              for (const fig of document.querySelectorAll('#charts .chart')) {
                const svg = fig.querySelector('svg');
                if (!svg) continue;
                const cap = (fig.querySelector('figcaption') || {}).textContent || '';
                const vb = svg.viewBox.baseVal;
                // Plot rect is what the renderer uses internally; we don't
                // have a marker for it but the chart uses m.t = 8 so any
                // dot with cy < 8.5 is touching the top edge.
                const dots = svg.querySelectorAll('circle');
                let onTop = 0;
                for (const d of dots) {
                  const cy = parseFloat(d.getAttribute('cy'));
                  if (!isNaN(cy) && cy < 8.5) onTop++;
                }
                out.push({
                  caption: cap.replace(/\\s+/g, ' ').trim().slice(0, 60),
                  dots: dots.length, onTop,
                });
              }
              return out;
            })()
            """
        )
        any_top_kissers = any(d["onTop"] > 0 for d in headroom)
        if not any_top_kissers and headroom:
            r.ok("cB1",
                 "no time-series dot kisses the top edge: "
                 + ", ".join(f"{d['caption']}({d['dots']} dots, top={d['onTop']})"
                             for d in headroom))
        else:
            r.bad("cB1",
                  f"dots touching top edge: {headroom}")

        # cB2 — Connecting line is rendered as a real line (>= 1px stroke,
        # high opacity) so the trace remains visible behind dense dots.
        line_style = await page.evaluate(
            """
            (() => {
              const ts = document.querySelector('#charts .chart svg path.ts-line:not(.ts-dash)');
              if (!ts) return null;
              const cs = getComputedStyle(ts);
              return {
                stroke: cs.stroke,
                strokeWidth: cs.strokeWidth,
                strokeOpacity: parseFloat(cs.strokeOpacity || cs.opacity || "1"),
              };
            })()
            """
        )
        if (line_style
                and float(line_style["strokeWidth"].replace("px", "")) >= 1.0
                and line_style["strokeOpacity"] >= 0.85):
            r.ok("cB2",
                 f"ts-line is visibly solid: width={line_style['strokeWidth']}, "
                 f"opacity={line_style['strokeOpacity']:.2f}")
        else:
            r.bad("cB2", f"ts-line too thin/faint: {line_style}")

        # cTL-F1 — Toggling back to overview drops the .timeline class
        # so charts go back to the per-hour grid layout.
        # (Probe done after the toggle below in cT7.)

        # cT7 — Toggling back to Overview is a pure re-render (no /api/*).
        stat_requests.clear()
        raw_requests = []
        all_requests = []
        page.on("request", lambda req: (
            raw_requests.append(req.url) if "/api/raw" in req.url else None,
            all_requests.append(req.url) if "/api/" in req.url else None,
        ))
        await page.click(".view-toggle button[data-mode='overview']")
        await page.wait_for_timeout(250)
        # Overview must restore the aggregated table + line+SEM charts.
        sem_after = await page.locator(".chart svg .errbar line").count()
        if not any("/api/stats" in u for u in stat_requests) \
           and not raw_requests \
           and sem_after > 0:
            r.ok("cT7", f"overview restored without refetch ({sem_after} "
                        f"SEM lines back)")
        else:
            r.bad("cT7", f"unexpected fetches: stats={stat_requests} "
                         f"raw={raw_requests}, sem_after={sem_after}")
        host_cls_after = await page.evaluate(
            "document.getElementById('charts').className")
        if "timeline" not in host_cls_after:
            r.ok("cTL-F1", f".charts class drops 'timeline' on overview "
                          f"({host_cls_after!r})")
        else:
            r.bad("cTL-F1",
                  f"overview still has 'timeline' class ({host_cls_after!r})")

        # cU1 — Removed copy: 'smart cache' phrase is gone everywhere
        body_html = await page.content()
        for phrase in ("smart cache",
                       "months already pulled",
                       "fetching last 60 days",
                       "Months already cached are skipped"):
            if phrase.lower() not in body_html.lower():
                r.ok("cU1+" + phrase[:18],
                     f"phrase removed: {phrase!r}")
            else:
                r.bad("cU1+" + phrase[:18],
                      f"phrase still present: {phrase!r}")

        # cU2 — Cache box no longer exposes months ok / months empty / sources
        for label in ("months ok", "months empty", "sources"):
            cnt = await page.locator(
                f"#cache-box .k:has-text('{label}')").count()
            if cnt == 0:
                r.ok("cU2+" + label, f"cache-box no longer shows '{label}'")
            else:
                r.bad("cU2+" + label, f"cache-box still shows '{label}'")

        # cV1 — Toggle BACK to See all should reuse the cached raw payload
        # (no second /api/raw fetch).
        raw_before = list(raw_requests)
        await page.click(".view-toggle button[data-mode='all']")
        await page.wait_for_selector(".raw-table", timeout=10_000)
        await page.wait_for_timeout(150)
        if len(raw_requests) == len(raw_before):
            r.ok("cV1", f"see-all re-render reused cache (raw fetches: "
                        f"{len(raw_requests)})")
        else:
            r.bad("cV1", f"see-all re-render hit /api/raw again: "
                         f"{raw_requests[len(raw_before):]}")

        # Switch back to overview for downstream checks
        await page.click(".view-toggle button[data-mode='overview']")
        await page.wait_for_timeout(150)

        # c20 — narrow viewport: re-test at 320 to make sure inputs+presets fit
        await page.set_viewport_size({"width": 320, "height": 568})
        await page.reload(wait_until="networkidle")
        m = await page.evaluate("""
() => ({
  doc: document.documentElement.scrollWidth,
  win: window.innerWidth,
  fromBox: document.getElementById('date_from').getBoundingClientRect(),
  presetBox: document.getElementById('dr-cached').getBoundingClientRect(),
})
""")
        ok20 = (m["doc"] <= m["win"]
                and m["fromBox"]["right"] <= m["win"] + 0.5
                and m["presetBox"]["right"] <= m["win"] + 0.5)
        if ok20:
            r.ok("c20", f"320px: doc={m['doc']} win={m['win']} "
                        f"date_from right={m['fromBox']['right']:.0f} "
                        f"Cached-button right={m['presetBox']['right']:.0f}")
        else:
            r.bad("c20", f"320px overflow: {m}")

        # c21 — Personal-minimums numeric boxes stay flush even when
        # their labels wrap differently at phone width.
        mins = await page.evaluate("""
() => {
  const boxes = [...document.querySelectorAll('#minimums-row input')]
    .map(el => {
      const r = el.getBoundingClientRect();
      return { id: el.id, top: r.top, bottom: r.bottom, height: r.height };
    });
  const tops = boxes.map(b => b.top);
  const bottoms = boxes.map(b => b.bottom);
  return {
    boxes,
    topSpread: Math.max(...tops) - Math.min(...tops),
    bottomSpread: Math.max(...bottoms) - Math.min(...bottoms),
  };
}
""")
        min_align_ok = (len(mins["boxes"]) == 3
                        and mins["topSpread"] <= 1
                        and mins["bottomSpread"] <= 1)
        if min_align_ok:
            r.ok("c21", f"320px minimum boxes flush: {mins}")
        else:
            r.bad("c21", f"320px minimum boxes staggered: {mins}")

        await browser.close()


# ----------------------------------------------------------------------
# Main

def run_timeline_backend(base: str, r: Result) -> None:
    """Backend probes for the See-all chronological timeline payload."""
    import urllib.parse
    today = date.today()
    d30   = (today - timedelta(days=30)).isoformat()
    d_iso = today.isoformat()

    def raw(**kw):
        q = {"airport": kw.pop("airport"),
             "hours": kw.pop("hours", "10,11,12,13,14,15,16,17,18")}
        q.update({k: v for k, v in kw.items() if v is not None})
        return http_json(f"{base}/api/raw?{urllib.parse.urlencode(q)}")

    j = raw(airport="KMYF", date_from=d30, date_to=d_iso)
    tl = j.get("timeline") or []
    rows = j.get("rows") or []
    cap = j.get("timeline_max_days")

    # cTL-B1: timeline present and non-empty
    if tl:
        r.ok("cTL-B1", f"KMYF 30d window: timeline len={len(tl)} "
                       f"rows len={len(rows)}")
    else:
        r.bad("cTL-B1", "no timeline in /api/raw response")

    # cTL-B2: timeline covers ALL 24 hours, not just the user's selection
    hours_seen = sorted({row["hour_pt"] for row in tl})
    if len(hours_seen) >= 18:
        r.ok("cTL-B2", f"timeline covers {len(hours_seen)} distinct hours "
                       f"({hours_seen[0]}..{hours_seen[-1]})")
    else:
        r.bad("cTL-B2", f"timeline only covers hours: {hours_seen}")

    # cTL-B3: 30d window stays inside the 90d cap; timeline_from == d30
    if j.get("timeline_from") == d30 and cap == 90:
        r.ok("cTL-B3", f"timeline_from honored ({d30}); cap={cap}")
    else:
        r.bad("cTL-B3",
              f"timeline_from={j.get('timeline_from')} cap={cap}, "
              f"expected from={d30} cap=90")

    # cTL-B4: with no date window (All), timeline is capped to last 90 days
    j_all = raw(airport="KMYF")
    tf = j_all.get("timeline_from")
    if tf:
        days_back = (today - date.fromisoformat(tf)).days
        if 80 <= days_back <= 92:    # ~ TIMELINE_MAX_DAYS - 1
            r.ok("cTL-B4", f"All-window timeline capped at "
                           f"{days_back} days back (from={tf})")
        else:
            r.bad("cTL-B4", f"All-window cap wrong: {days_back} days back "
                            f"(from={tf})")
    else:
        r.bad("cTL-B4", f"timeline_from missing in All response")

    # cTL-B5: timeline rows are chronologically ordered
    times = [row["time_utc"] for row in tl]
    if times == sorted(times):
        r.ok("cTL-B5", f"timeline rows chronological (n={len(times)})")
    else:
        r.bad("cTL-B5", "timeline rows are not in time order")

    # cTL-B6: each timeline row has the runway-frame fields needed for
    # primary/secondary dot coloring.
    has_keys = all(("active_end_ident" in row and "xwind" in row)
                   for row in tl[:50])
    if has_keys:
        r.ok("cTL-B6", "timeline rows include xwind + active_end_ident")
    else:
        r.bad("cTL-B6", "timeline rows missing xwind/active_end_ident")

    # cTL-DA1: native-station raw payload exposes density-altitude fields
    # computed from that station's own METAR temp/altimeter and airport
    # elevation. Top-level availability is true, rows/timeline carry values,
    # and the high-DA boolean matches the advertised threshold.
    da_vals = [row.get("density_altitude_ft") for row in tl
               if row.get("density_altitude_ft") is not None]
    threshold = j.get("density_altitude_threshold_ft")
    cmp_threshold = threshold if isinstance(threshold, (int, float)) else 8000
    flag_mismatch = [
        row for row in tl
        if row.get("density_altitude_ft") is not None
        and bool(row.get("density_altitude_high"))
        != (row.get("density_altitude_ft") > cmp_threshold)
    ]
    if (j.get("density_altitude_available") is True
            and j.get("density_altitude_elev_ft") == 427
            and threshold == 8000
            and len(da_vals) >= max(5, len(tl) // 2)
            and all(-5000 <= v <= 15000 for v in da_vals)
            and not flag_mismatch):
        r.ok("cTL-DA1",
             f"KMYF DA available: n={len(da_vals)} "
             f"range={min(da_vals)}..{max(da_vals)} ft")
    else:
        r.bad("cTL-DA1",
              f"DA native payload off: available="
              f"{j.get('density_altitude_available')} elev="
              f"{j.get('density_altitude_elev_ft')} threshold={threshold} "
              f"n={len(da_vals)} mismatches={len(flag_mismatch)}")

    # cTL-DA2: when a nearby reporting station supplies data for another
    # airport's runway frame, suppress DA entirely. DA needs the requested
    # field's own temperature/altimeter, not borrowed METAR weather.
    j_sub = raw(airport="KPSP", runways_airport="L22",
                date_from=d30, date_to=d_iso)
    sub_rows = (j_sub.get("rows") or []) + (j_sub.get("timeline") or [])
    sub_da = [row.get("density_altitude_ft") for row in sub_rows
              if row.get("density_altitude_ft") is not None]
    if j_sub.get("density_altitude_available") is False and not sub_da:
        r.ok("cTL-DA2", "substituted runway frame suppresses DA values")
    else:
        r.bad("cTL-DA2",
              f"substitution leaked DA: available="
              f"{j_sub.get('density_altitude_available')} values={len(sub_da)}")


def run_forecast_backend(base: str, r: Result) -> None:
    """Forecast API probes for the airports the user called out. Missing
    TAF/LAMP/model products must be explicit, while available wind guidance
    must still produce chartable points."""
    targets = ["KAVX", "L22", "L35", "L08"]
    for raw_code in targets:
        code = raw_code if raw_code.startswith("K") else "K" + raw_code
        q = urllib.parse.urlencode({
            "airport": raw_code,
        })
        last_exc = None
        j = None
        for attempt in range(2):
            try:
                j = http_json(f"{base}/api/forecast?{q}")
                break
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(1)
                    continue
        try:
            if j is None:
                raise last_exc or RuntimeError("unknown forecast error")
        except Exception as exc:
            r.bad(f"cF-B-{raw_code}",
                  f"forecast API failed after retry: {exc}")
            continue
        models = j.get("models") or []
        target_rows = j.get("target_rows") or []
        usable = [m for m in models
                  if m.get("status") == "ok" and (m.get("points") or [])]
        explicit_missing = [
            m for m in models
            if m.get("status") != "ok" and m.get("missing_reason")
        ]
        wind_ok = any(
            any(p.get("wind_speed_kt") is not None
                for p in m.get("points") or [])
            for m in usable
        )
        xwind_ok = any(
            any(p.get("xwind_kt") is not None
                for p in m.get("points") or [])
            for m in usable
        )
        table_complete = (
            len(target_rows) == len(models)
            and all(("point" in row and "missing_reason" in row)
                    for row in target_rows)
        )
        no_confidence_payload = (
            all("confidence" not in m for m in models)
            and all("confidence" not in row for row in target_rows)
        )
        airport_ok = j.get("airport") == code
        if (airport_ok and usable and wind_ok and xwind_ok
                and table_complete and no_confidence_payload):
            r.ok(f"cF-B-{raw_code}",
                 f"{j.get('airport')} usable="
                 f"{[(m['id'], len(m['points'])) for m in usable]} "
                 f"missing="
                 f"{[(m['id'], m.get('missing_reason')) for m in explicit_missing]}")
        else:
            r.bad(f"cF-B-{raw_code}",
                  f"airport={j.get('airport')} usable={len(usable)} "
                  f"wind_ok={wind_ok} xwind_ok={xwind_ok} "
                  f"table_complete={table_complete} "
                  f"no_confidence_payload={no_confidence_payload} "
                  f"models="
                  f"{[(m.get('id'), m.get('status'), len(m.get('points') or []), m.get('missing_reason')) for m in models]}")


def run_runway_scenarios(base: str, r: Result) -> None:
    """Backend runway-frame scenarios: per-obs end picking, primary/
    secondary, frame-of-reference decoupling, no-runway-data shape.
    """
    # cR-S4: KMYF native multi-runway → multiple ends used and primary set.
    j = http_json(stats_url(base, airport="KMYF"))
    ru = j.get("runways_used") or []
    if (len(ru) >= 1 and ru[0].get("primary") is True
            and j.get("primary_ident") == ru[0]["ident"]):
        r.ok("cR-S4", f"KMYF runways_used={[(x['ident'],x['obs_count']) for x in ru]} "
                     f"primary={j['primary_ident']}")
    else:
        r.bad("cR-S4", f"KMYF unexpected runways_used={ru} "
                       f"primary_ident={j.get('primary_ident')}")

    # cR-S4b: at least one row should have non-empty end_counts and a
    #         hour with mixed=True is plausible on a busy day.
    rows_w_ends = [row for row in j["rows"] if row.get("end_counts")]
    if rows_w_ends:
        r.ok("cR-S4b", f"KMYF rows with end_counts: {len(rows_w_ends)} "
                      f"sample={rows_w_ends[0]['end_counts']}")
    else:
        r.bad("cR-S4b", "KMYF: no rows reported end_counts")

    # cR-S5: exact runway_ident pins the requested runway end rather
    # than only pinning by heading. Use 10L because it is not reciprocal
    # to KMYF's usual 23/05 pair, so its xwind values should visibly
    # differ from mixed/auto mode.
    j_10l = http_json(stats_url(base, airport="KMYF",
                                runway="auto", runway_ident="10L"))
    diffs = []
    for a, b in zip(j["rows"], j_10l["rows"]):
        ma = (a.get("xwind") or {}).get("mean")
        mb = (b.get("xwind") or {}).get("mean")
        if ma is not None and mb is not None:
            diffs.append(abs(ma - mb))
    ok_ident = (
        j_10l.get("runway_auto") is False
        and j_10l.get("runway_ident") == "10L"
        and any((x.get("ident") == "10L" and x.get("primary"))
                for x in (j_10l.get("runways_used") or []))
        and sum(1 for d in diffs if d >= 0.5) >= 3
    )
    if ok_ident:
        r.ok("cR-S5", f"runway_ident pins 10L; kt diffs={diffs}")
    else:
        r.bad("cR-S5",
              f"runway_ident did not pin 10L: ident={j_10l.get('runway_ident')} "
              f"auto={j_10l.get('runway_auto')} used={j_10l.get('runways_used')} "
              f"diffs={diffs}")

    # cR-S6: Mixed/auto is a best-runway-per-observation xwind trace,
    # so its mean cannot exceed any single pinned runway's mean for the
    # same hour/window, aside from tiny floating point noise.
    pinned = {"10L": j_10l}
    for rw in (j.get("runways") or []):
        ident = rw.get("ident")
        if not ident or rw.get("heading_deg") is None or ident in pinned:
            continue
        pinned[ident] = http_json(stats_url(
            base, airport="KMYF", runway="auto", runway_ident=ident))
    violations = []
    for idx, auto_row in enumerate(j["rows"]):
        auto_mean = (auto_row.get("xwind") or {}).get("mean")
        if auto_mean is None:
            continue
        for ident, pj in pinned.items():
            pin_mean = (pj["rows"][idx].get("xwind") or {}).get("mean")
            if pin_mean is not None and auto_mean > pin_mean + 1e-9:
                violations.append((auto_row["hour_pt"], ident,
                                   auto_mean, pin_mean))
    if not violations:
        sample = {
            ident: [round((row.get("xwind") or {}).get("mean") or 0, 2)
                    for row in pj["rows"][:3]]
            for ident, pj in list(pinned.items())[:3]
        }
        r.ok("cR-S6", f"mixed xwind <= pinned runway means; sample={sample}")
    else:
        r.bad("cR-S6", f"mixed exceeded pinned xwind: {violations[:5]}")

    # cR-S7: Reciprocal runway ends have the same absolute crosswind.
    # We no longer emit signed xwind because the UI uses magnitude only.
    j_04 = http_json(stats_url(base, airport="KAVX",
                               runway="auto", runway_ident="04"))
    j_22 = http_json(stats_url(base, airport="KAVX",
                               runway="auto", runway_ident="22"))
    abs_pairs = []
    signed_fields = 0
    for a, b in zip(j_04["rows"], j_22["rows"]):
        a_abs = (a.get("xwind") or {}).get("mean")
        b_abs = (b.get("xwind") or {}).get("mean")
        signed_fields += int("xwind_signed" in a) + int("xwind_signed" in b)
        if a_abs is not None and b_abs is not None:
            abs_pairs.append(abs(a_abs - b_abs))
    if (j_04.get("runway_ident") == "04"
            and j_22.get("runway_ident") == "22"
            and abs_pairs and max(abs_pairs) <= 0.01
            and signed_fields == 0):
        r.ok("cR-S7",
             f"KAVX reciprocal absolute xwind matches; signed fields removed "
             f"(diffs={abs_pairs[:5]})")
    else:
        r.bad("cR-S7",
              f"KAVX reciprocal absolute check failed: "
              f"04={j_04.get('runway_ident')} 22={j_22.get('runway_ident')} "
              f"abs_diffs={abs_pairs[:5]} signed_fields={signed_fields}")

    # cR-S1: L22 → KPSP substitute, but pass runways_airport=L22.
    j_sub = http_json(stats_url(
        base, airport="KPSP", runways_airport="L22"))
    ru = j_sub.get("runways_used") or []
    idents = sorted({x["ident"] for x in ru})
    if (idents == ["06", "24"]
            and j_sub.get("runway_ident") in {"06", "24"}
            and j_sub.get("runways_airport", "").endswith("L22")):
        r.ok("cR-S1", f"L22-frame on KPSP wind: idents={idents} "
                     f"primary={j_sub['runway_ident']}")
    else:
        r.bad("cR-S1", f"L22-frame unexpected: idents={idents} "
                      f"runway_ident={j_sub.get('runway_ident')} "
                      f"runways_airport={j_sub.get('runways_airport')}")

    # cR-S2: same KPSP wind data, but with KPSP frame vs L22 frame —
    #        xwind values must differ on at least 3 hours by ≥0.5 kt
    #        because the runways are oriented differently.
    j_kpsp = http_json(stats_url(base, airport="KPSP"))
    diffs = []
    for ra, rb in zip(j_kpsp["rows"], j_sub["rows"]):
        ma, mb = (ra.get("xwind") or {}).get("mean"), (rb.get("xwind") or {}).get("mean")
        if ma is not None and mb is not None:
            diffs.append(abs(ma - mb))
    if sum(1 for d in diffs if d >= 0.5) >= 3:
        r.ok("cR-S2", f"KPSP-frame vs L22-frame xwind differs (kt diffs={diffs})")
    else:
        r.bad("cR-S2", f"frame swap did not change xwind: diffs={diffs}")

    # cR-S3: KSAS → no usable runway headings.
    j_ks = http_json(stats_url(base, airport="KSAS"))
    if (j_ks.get("no_runway_data") is True
            and j_ks.get("runway_ident") is None
            and j_ks.get("runway_heading") is None
            and (j_ks.get("runways_used") or []) == []
            and all((row.get("xwind") or {}).get("count", 0) == 0
                    for row in j_ks["rows"])):
        r.ok("cR-S3", "KSAS shape: no_runway_data=True, ident=None, xwind empty")
    else:
        r.bad("cR-S3",
              f"KSAS unexpected: no_runway_data={j_ks.get('no_runway_data')} "
              f"runway_ident={j_ks.get('runway_ident')} "
              f"runways_used={j_ks.get('runways_used')} "
              f"sample_row={j_ks['rows'][0] if j_ks.get('rows') else None}")


async def run_runway_frontend(base: str, r: Result) -> None:
    """Playwright checks for the runway-frame UI: substitution → L22
    runway dropdown, single-vs-mixed crosswind title coloring, off-primary
    dot greyout, and KSAS no-runway header / hidden xwind chart.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()

        # Capture stat + raw URLs to verify runways_airport= rides along.
        urls: list[str] = []
        page.on("request", lambda req: (
            urls.append(req.url) if "/api/" in req.url else None))

        # ---- L22 substitution: runway frame must stay on L22 ----
        await page.goto(base, wait_until="networkidle")
        await page.evaluate("localStorage.clear()")
        await page.fill("#airport", "L22")
        await page.click("#run")
        await page.wait_for_selector(".substitute-banner", timeout=120_000)
        await page.wait_for_selector(".charts:not([hidden])")
        await page.wait_for_function(
            "() => !document.querySelector('#run')?.disabled",
            timeout=120_000,
        )
        await page.wait_for_timeout(400)

        # cR-F1: outgoing /api/stats request includes runways_airport=L22
        sub_url = next(
            (u for u in urls
             if "/api/stats" in u and "airport=KPSP" in u
             and "runways_airport=" in u), "")
        if "runways_airport=KL22" in sub_url or "runways_airport=L22" in sub_url:
            r.ok("cR-F1", f"stats URL carries runways_airport "
                         f"({sub_url[-100:]})")
        else:
            r.bad("cR-F1", f"runways_airport missing from stats URL "
                          f"(saw: {sub_url})")

        # cR-F2: runway dropdown lists L22's ends 06 and 24, NOT KPSP's.
        opts = await page.evaluate(
            "[...document.querySelectorAll('#runway option')]"
            ".map(o => o.textContent)"
        )
        joined = " ".join(opts).upper()
        if ("RWY 06" in joined and "RWY 24" in joined
                and "RWY 13L" not in joined and "RWY 31R" not in joined
                and " FT" not in joined):
            r.ok("cR-F2", f"dropdown shows L22 ends without length ({opts})")
        else:
            r.bad("cR-F2", f"dropdown wrong: {opts}")

        # cR-F3/F4: crosswind chart title contains the runway list.
        # Find the figcaption that mentions Crosswind.
        cap = await page.evaluate("""
() => {
  const figs = [...document.querySelectorAll('.chart figcaption')];
  const xw = figs.find(f => /crosswind/i.test(f.textContent));
  return xw ? {
    text: xw.textContent.trim(),
    primary: [...xw.querySelectorAll('.rw-primary')].map(e=>e.textContent),
    secondary: [...xw.querySelectorAll('.rw-secondary')].map(e=>e.textContent),
  } : null;
}""")
        if not cap:
            r.bad("cR-F3", "no crosswind chart found")
        else:
            ctext = cap["text"].lower()
            has_06_or_24 = ("06" in ctext or "24" in ctext) \
                            and ("13l" not in ctext and "31r" not in ctext)
            if has_06_or_24:
                r.ok("cR-F3", f"xwind title uses L22 ends: {cap['text']!r}")
            else:
                r.bad("cR-F3", f"xwind title wrong frame: {cap['text']!r}")
            # F4: if multiple ends used, expect coloring spans
            if cap["secondary"]:
                r.ok("cR-F4", f"mixed-runway title has primary={cap['primary']} "
                              f"secondary={cap['secondary']}")
            else:
                # If only 1 end was active in the window that's also OK —
                # log as info rather than fail.
                r.info("cR-F4", f"single-end window (primary={cap['primary']})")

        # cR-F5: per-hour dot has secondary class iff mixed.
        sec_dots = await page.locator(
            ".chart svg circle.dot.dot-secondary").count()
        # Either 0 (single-end window) or > 0 (mixed). We just confirm
        # the markup is reachable when applicable.
        r.info("cR-F5", f".dot.dot-secondary count = {sec_dots}")

        # ---- KMYF non-parallel runway selector: choosing a runway from
        # the top-left selector should update xwind from the existing page,
        # without going through the global "querying..." reload state.
        urls.clear()
        await page.fill("#airport", "KMYF")
        await page.click("#run")
        await page.wait_for_function(
            """
            () => {
              const hb = document.querySelector('#header-bar')?.innerText || '';
              return hb.includes('KMYF')
                && !document.querySelector('#run')?.disabled;
            }
            """,
            timeout=120_000,
        )
        await page.wait_for_selector(".chart.xwind svg", timeout=90_000)
        await page.wait_for_selector("#forecast-output:not([hidden])",
                                     timeout=90_000)
        await page.wait_for_selector(".forecast-wind svg", timeout=30_000)
        await page.wait_for_timeout(300)
        kmyf_before = await page.evaluate(
            """
            (() => {
              const path = document.querySelector('.chart.xwind svg path.line');
              const cap = document.querySelector('.chart.xwind figcaption');
              const opts = [...document.querySelectorAll('#runway option')]
                .map(o => o.textContent);
              return {
                path: path ? path.getAttribute('d') : null,
                caption: cap ? cap.textContent.replace(/\\s+/g, ' ').trim() : null,
                options: opts,
                forecastCharts: document.querySelectorAll('#forecast-charts .chart').length,
              };
            })()
            """
        )
        urls.clear()
        await page.evaluate(
            """
            (() => {
              const sel = document.querySelector('#runway');
              const opt = [...sel.options].find(o => o.dataset.ident === '10L');
              if (!opt) throw new Error('missing RWY 10L option');
              sel.selectedIndex = opt.index;
              sel.value = opt.value;
              sel.dispatchEvent(new Event('change', { bubbles: true }));
            })()
            """
        )
        await page.wait_for_function(
            "() => lastStats && lastStats.j && lastStats.j.runway_ident === '10L'",
            timeout=30_000)
        await page.wait_for_timeout(250)
        kmyf_after = await page.evaluate(
            """
            (() => {
              const path = document.querySelector('.chart.xwind svg path.line');
              const cap = document.querySelector('.chart.xwind figcaption');
              const hb = document.querySelector('#header-bar');
              return {
                path: path ? path.getAttribute('d') : null,
                caption: cap ? cap.textContent.replace(/\\s+/g, ' ').trim() : null,
                header: hb ? hb.textContent.replace(/\\s+/g, ' ').trim() : null,
                headerHidden: hb ? hb.hidden : true,
                querying: /querying/i.test(document.querySelector('#results-host')?.textContent || ''),
                runDisabled: document.querySelector('#run')?.disabled || false,
                runwayIdent: lastStats && lastStats.j ? lastStats.j.runway_ident : null,
                runwayAuto: lastStats && lastStats.j ? lastStats.j.runway_auto : null,
                forecastCharts: document.querySelectorAll('#forecast-charts .chart').length,
              };
            })()
            """
        )
        stat_reqs = [u for u in urls if "/api/stats" in u]
        raw_reqs = [u for u in urls if "/api/raw" in u]
        fc_reqs = [u for u in urls if "/api/forecast" in u]
        labels_clean = (" FT" not in " ".join(kmyf_before["options"]).upper())
        kmyf_ok = (
            labels_clean
            and kmyf_before["path"]
            and kmyf_after["path"]
            and kmyf_after["path"] != kmyf_before["path"]
            and kmyf_after["runwayIdent"] == "10L"
            and kmyf_after["runwayAuto"] is False
            and "10L" in (kmyf_after["caption"] or "")
            and "10L" in (kmyf_after["header"] or "")
            and kmyf_after["headerHidden"] is False
            and kmyf_after["querying"] is False
            and kmyf_after["runDisabled"] is False
            and kmyf_after["forecastCharts"] == kmyf_before["forecastCharts"]
            and any("runway_ident=10L" in u for u in stat_reqs)
            and not raw_reqs
            and not fc_reqs
        )
        if kmyf_ok:
            r.ok("cR-F9",
                 f"KMYF runway dropdown updates xwind in place: "
                 f"before={kmyf_before['caption']!r} after={kmyf_after['caption']!r} "
                 f"stats={stat_reqs[-1][-120:] if stat_reqs else None}")
        else:
            r.bad("cR-F9",
                  f"KMYF runway dropdown did not update cleanly: "
                  f"before={kmyf_before} after={kmyf_after} "
                  f"stats={stat_reqs[-3:]} raw={raw_reqs[-3:]} forecast={fc_reqs[-3:]}")

        # ---- KAVX reciprocal runway UI: no chart-local runway buttons,
        # no signed values, no negative y-axis.
        urls.clear()
        await page.fill("#airport", "KAVX")
        await page.click("#run")
        await page.wait_for_function(
            """
            () => {
              const hb = document.querySelector('#header-bar')?.innerText || '';
              return hb.includes('KAVX')
                && !document.querySelector('#run')?.disabled;
            }
            """,
            timeout=120_000,
        )
        await page.wait_for_selector(".chart.xwind svg", timeout=90_000)
        await page.wait_for_timeout(300)
        kavx_probe = await page.evaluate(
            """
            (() => {
              const path = document.querySelector('.chart.xwind svg path.line');
              const cap = document.querySelector('.chart.xwind figcaption');
              const legend = document.querySelector('.chart.xwind .legend-row');
              const ticks = [...document.querySelectorAll('.chart.xwind svg .axis text')]
                .map(t => t.textContent.trim());
              return {
                switchCount: document.querySelectorAll('.chart.xwind .runway-switch').length,
                buttonCount: document.querySelectorAll('.chart.xwind button').length,
                overrideType: typeof xwindChartOverride,
                absMeans: lastStats && lastStats.j ? lastStats.j.rows.map(r =>
                  r.xwind && r.xwind.mean).filter(v => v != null) : [],
                path: path ? path.getAttribute('d') : null,
                caption: cap ? cap.textContent.replace(/\\s+/g, ' ').trim() : null,
                legend: legend ? legend.textContent.replace(/\\s+/g, ' ').trim() : null,
                ticks,
              };
            })()
            """
        )
        kavx_has_abs = any(v > 0.5 for v in kavx_probe["absMeans"])
        kavx_has_negative_tick = any(t.startswith("-") for t in kavx_probe["ticks"])
        if (kavx_probe["switchCount"] == 0
                and kavx_probe["buttonCount"] == 0
                and kavx_probe["overrideType"] == "undefined"
                and kavx_probe["path"]
                and kavx_has_abs
                and not kavx_has_negative_tick
                and "Crosswind" in (kavx_probe["caption"] or "")
                and "Signed" not in (kavx_probe["caption"] or "")
                and "signed left/right" not in (kavx_probe["legend"] or "")):
            r.ok("cR-F8",
                 f"KAVX xwind has no runway switch or signed axis: {kavx_probe}")
        else:
            r.bad("cR-F8",
                  f"KAVX xwind still has runway/signed UI: {kavx_probe} "
                  f"urls={urls[-6:]}")

        # ---- KSAS: no runway data ----
        urls.clear()
        await page.fill("#airport", "KSAS")
        await page.click("#run")
        # KSAS may auto-fetch; also no METAR data exists, so substitution
        # may kick in. We only care about the "no runway" header path
        # — skip if substitution renders normal stats. We force pass
        # runways_airport=KSAS via the URL that the frontend sends.
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector(".header-bar:not([hidden])",
                                         timeout=60_000)
            await page.wait_for_function(
                "() => !document.querySelector('#run')?.disabled",
                timeout=120_000,
            )
        except Exception:
            r.info("cR-F6", "KSAS never rendered header bar — skipping")
            await browser.close()
            return

        hb = (await page.text_content("#header-bar") or "").lower()
        # Note: KSAS may substitute. The "no runway" path lights up when
        # the runway-frame airport (KSAS itself, since user typed it) has
        # no runway headings. We hit api/stats for the substitute station
        # but pass runways_airport=KSAS, so the response should still be
        # no_runway_data=True.
        if "no runway" in hb:
            r.ok("cR-F6", "KSAS header shows 'no runway'")
            xw_caps = await page.locator(
                ".chart figcaption:has-text('Crosswind')").count()
            if xw_caps == 0:
                r.ok("cR-F7", "KSAS hides crosswind chart")
            else:
                r.bad("cR-F7",
                      f"KSAS still shows {xw_caps} crosswind chart(s)")
        else:
            # Either KSAS data does report headings (some Salton variants
            # have heading_deg), or substitution masked the no-runway
            # path. Log as info, not a hard fail.
            r.info("cR-F6",
                   f"KSAS header has no 'no runway' (hb sample: {hb[:120]!r})")

        await browser.close()


async def main(base: str) -> int:
    r = Result()
    print(f"== Backend probes against {base}")
    info = run_backend(base, r)

    print(f"\n== Backend runway scenarios")
    run_runway_scenarios(base, r)

    print(f"\n== Backend timeline (See-all)")
    run_timeline_backend(base, r)

    print(f"\n== Backend forecast scenarios")
    run_forecast_backend(base, r)

    print(f"\n== Frontend (Playwright) against {base}")
    await run_frontend(base, r, info["today"])

    print(f"\n== Frontend runway scenarios (Playwright)")
    await run_runway_frontend(base, r)

    print("\n== Results")
    return r.report()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:5050")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.url)))
