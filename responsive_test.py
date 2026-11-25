"""Responsive design verification for the METAR app.

Loads the app at each of the 10 standard viewports, runs a query for KMYF,
captures full-page screenshots, and measures key element widths so we can
detect horizontal overflow, mis-sized charts, and clunky one-and-a-half-chart
forecast layouts without eyeballing.

Usage:
    python3 responsive_test.py [--url URL] [--out OUTDIR] [--view overview|all|both]
                               [--airport KMYF] [--hover]

`--hover` synthesizes a mid-chart mouse move so the crosshair / readout
state is captured (used to verify the TradingView-style top-of-chart
readout doesn't obscure data).
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

VIEWPORTS = [
    ("phone-narrow",          320,  568),
    ("phone-common",          360,  800),
    ("phone-modern",          390,  844),
    ("phone-large-tall",      430,  932),
    ("phone-landscape-short", 844,  390),
    ("tablet-portrait",       768, 1024),
    ("laptop-common",        1366,  768),
    ("laptop-modern",        1512,  982),
    ("desktop-common",       1920, 1080),
    ("desktop-ultrawide",    2560, 1080),
]


async def measure(page) -> dict:
    return await page.evaluate("""
() => {
  const docW   = document.documentElement.scrollWidth;
  const winW   = window.innerWidth;
  const overflow = docW - winW;  // > 0 means horizontal overflow
  const measureCharts = (selector) => Array.from(document.querySelectorAll(selector)).map(el => {
    const r = el.getBoundingClientRect();
    const svg = el.querySelector('svg');
    const sr = svg ? svg.getBoundingClientRect() : null;
    return {
      figW: Math.round(r.width),
      figH: Math.round(r.height),
      svgW: sr ? Math.round(sr.width)  : null,
      svgH: sr ? Math.round(sr.height) : null,
      overflowsParent: sr ? sr.width > r.width + 0.5 : false,
    };
  });
  const chartRows = (selector) => {
    const rects = Array.from(document.querySelectorAll(selector)).map(el => {
      const r = el.getBoundingClientRect();
      return {top:r.top, bottom:r.bottom, width:r.width};
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
    return {
      rows,
      completeInViewport: rects.filter(r => r.bottom <= window.innerHeight).length,
    };
  };
  const readouts = Array.from(document.querySelectorAll('.chart .readout text'))
    .map(t => {
      const svg = t.closest('svg');
      const g = t.closest('.crosshair');
      const vb = svg ? svg.viewBox.baseVal : null;
      let box = null;
      try {
        const b = t.getBBox();
        box = {x:b.x, y:b.y, width:b.width, height:b.height,
               right:b.x + b.width, bottom:b.y + b.height};
      } catch (_) {}
      const visible = g && getComputedStyle(g).opacity !== '0' && (t.textContent || '').trim();
      return {
        visible: !!visible,
        text: (t.textContent || '').trim(),
        lines: Number(t.dataset.lines || 0),
        box,
        viewBox: vb ? {x:vb.x, y:vb.y, width:vb.width, height:vb.height,
                       right:vb.x + vb.width, bottom:vb.y + vb.height} : null,
        fits: !visible || !box || !vb || (
          box.x >= vb.x - 1
          && box.y >= vb.y - 1
          && box.right <= vb.x + vb.width + 1
          && box.bottom <= vb.y + vb.height + 1
        ),
      };
    });
  const charts = measureCharts('.chart');
  const forecastCharts = measureCharts('#forecast-charts .chart');
  const forecastRows = chartRows('#forecast-charts .chart');
  const timelineCharts = measureCharts('.charts.timeline figure');
  const timelineRows = chartRows('.charts.timeline figure');
  const tableWrap = document.querySelector('.table-wrap');
  const table = tableWrap && tableWrap.querySelector('table');
  return {
    winW, docW, overflow,
    charts,
    forecastCharts,
    forecastRows,
    timelineCharts,
    timelineRows,
    chartCount: charts.length,
    forecastChartCount: forecastCharts.length,
    timelineChartCount: timelineCharts.length,
    readouts,
    table: table ? {
      wrapW: Math.round(tableWrap.getBoundingClientRect().width),
      tableW: Math.round(table.getBoundingClientRect().width),
      hasHorizScroll: tableWrap.scrollWidth > tableWrap.clientWidth + 0.5,
    } : null,
  };
}
""")


async def _switch_to_see_all(page) -> bool:
    """Click the See-all toggle if present. Returns True if switched."""
    btn = page.locator(".view-toggle button[data-mode='all']")
    if await btn.count() == 0:
        return False
    await btn.click()
    try:
        await page.wait_for_selector(".charts.timeline svg", timeout=15000)
    except Exception:
        return False
    await page.wait_for_timeout(200)
    return True


async def _hover_mid_chart(page) -> None:
    """Hover the first chart's hit rect so the crosshair + top readout
    become visible in screenshots. We hover the `.ch-hit` element
    explicitly because Playwright's `mouse.move` to absolute client
    coordinates doesn't always fire the SVG element's mousemove
    listener through the chart's transformed viewBox."""
    hit = page.locator(".chart svg .ch-hit").first
    if await hit.count() == 0:
        return
    await hit.hover()
    await page.wait_for_timeout(120)


async def _hover_all_readouts_fit(page) -> list[dict]:
    """Hard geometry check for every chart hover state.

    The screenshot captures one hovered chart, but layout regressions can be
    chart-specific because readout text differs by metric/model. This walks
    every `.ch-hit`, hovers it, then asserts the visible SVG readout bbox stays
    inside that SVG viewBox.
    """
    out = []
    hits = page.locator(".chart svg .ch-hit")
    for i in range(await hits.count()):
        hit = hits.nth(i)
        box = await hit.bounding_box()
        if not box:
            out.append({"idx": i, "fits": False, "visible": False,
                        "reason": "no-hit-box"})
            continue
        try:
            for frac in (0.08, 0.2, 0.35, 0.5, 0.65, 0.8, 0.92):
                await page.mouse.move(
                    box["x"] + box["width"] * frac,
                    box["y"] + box["height"] * 0.5,
                )
                await page.wait_for_timeout(25)
                out.append(await page.evaluate(
                    """
                    ({idx, frac}) => {
                  const hits = [...document.querySelectorAll('.chart svg .ch-hit')];
                  const svg = hits[idx]?.closest('svg');
                  const fig = svg?.closest('.chart');
                  const text = svg?.querySelector('.readout text');
                  const group = svg?.querySelector('.crosshair');
                  if (!svg || !text || !group) {
                    return {idx, frac, fits: true, visible: false, reason: 'no-readout'};
                  }
                  const visible = getComputedStyle(group).opacity !== '0'
                    && (text.textContent || '').trim().length > 0;
                  if (!visible) {
                    return {idx, frac, fits: true, visible: false, reason: 'not-visible'};
                  }
                  const b = text.getBBox();
                  const vb = svg.viewBox.baseVal;
                  const box = {
                    x: b.x, y: b.y, right: b.x + b.width,
                    bottom: b.y + b.height, width: b.width, height: b.height,
                  };
                  const view = {
                    x: vb.x, y: vb.y, right: vb.x + vb.width,
                    bottom: vb.y + vb.height, width: vb.width, height: vb.height,
                  };
                  const fits = box.x >= view.x - 1
                    && box.y >= view.y - 1
                    && box.right <= view.right + 1
                    && box.bottom <= view.bottom + 1;
                  return {
                    idx,
                    frac,
                    fits,
                    visible,
                    chart: fig?.querySelector('figcaption')?.textContent?.trim() || '',
                    lines: Number(text.dataset.lines || 0),
                    text: text.textContent,
                    box,
                    view,
                  };
                }
                    """,
                    {"idx": i, "frac": frac},
                ))
        except Exception as exc:
            out.append({"idx": i, "fits": False, "visible": False,
                        "reason": str(exc)})
    return out


async def _shot(page, outdir, w, h, label, view, hover):
    suffix = f"-{view}" + ("-hover" if hover else "")
    path = outdir / f"vw-{w}x{h}-{label}{suffix}.png"
    await page.screenshot(path=str(path), full_page=True)


async def run_for_view(browser, url, outdir, airport, view, hover):
    failures = 0
    results = []
    for label, w, h in VIEWPORTS:
        ctx = await browser.new_context(viewport={"width": w, "height": h})
        page = await ctx.new_page()
        await page.goto(url, wait_until="networkidle")
        await page.fill("#airport", airport)
        await page.click("#run")
        try:
            await page.wait_for_selector(".chart svg", timeout=90000)
            await page.wait_for_selector("#forecast-charts .chart svg", timeout=90000)
            await page.wait_for_function(
                "() => !document.querySelector('#run')?.disabled",
                timeout=120000,
            )
        except Exception:
            print(f"  [{label}] no chart rendered")
        await page.wait_for_timeout(300)
        if view == "all":
            await _switch_to_see_all(page)
        if hover:
            await _hover_mid_chart(page)
            hover_checks = await _hover_all_readouts_fit(page)
        else:
            hover_checks = []

        m = await measure(page)
        await _shot(page, outdir, w, h, label, view, hover)
        await ctx.close()

        forecast_dense = True
        if view == "overview" and w >= 768 and h >= 700:
            forecast_dense = (
                m["forecastChartCount"] >= 3
                and m["forecastRows"]["completeInViewport"] >= 3
                and m["forecastRows"]["rows"]
                and m["forecastRows"]["rows"][0]["count"] >= 2
            )
        timeline_dense = True
        if view == "all" and w >= 1500 and h >= 900:
            rows = m["timelineRows"]["rows"]
            timeline_dense = (
                m["timelineChartCount"] >= 3
                and rows
                and rows[0]["count"] == 2
                and m["timelineRows"]["completeInViewport"]
                    >= min(4, m["timelineChartCount"])
                and all(c["figH"] < 320 for c in m["timelineCharts"][:4])
            )
        ok = (m["overflow"] <= 0
              and not any(c["overflowsParent"] for c in m["charts"])
              and not any(c["overflowsParent"] for c in m["forecastCharts"])
              and not any(r["visible"] and not r["fits"] for r in m["readouts"])
              and not any(not h["fits"] for h in hover_checks)
              and forecast_dense
              and timeline_dense)
        status = "OK " if ok else "BAD"
        if not ok:
            failures += 1
        row = {"label": label, "viewport": f"{w}x{h}",
               "view": view, "hover": hover, "ok": ok,
               "hoverChecks": hover_checks, **m}
        results.append(row)
        charts_summary = " ".join(
            f"{c['figW']}px(svg={c['svgW']})" for c in m["charts"]
        ) or "(no charts)"
        forecast_summary = " ".join(
            f"{c['figW']}px" for c in m["forecastCharts"]
        ) or "(no forecast)"
        timeline_summary = " ".join(
            f"{c['figW']}x{c['figH']}" for c in m["timelineCharts"]
        ) or "(no timeline)"
        readout_summary = " ".join(
            f"{r['lines']}l" for r in m["readouts"] if r["visible"]
        ) or "(no hover)"
        print(f"  [{status}] [{view}{'+hover' if hover else ''}] "
              f"{label:<22} {w}x{h:<5}  doc={m['docW']:>5}  "
              f"overflow={m['overflow']:>3}  charts: {charts_summary}  "
              f"forecast: {forecast_summary}  timeline: {timeline_summary}  "
              f"readout: {readout_summary}")
    return failures, results


async def main(url: str, outdir: Path, *, view: str, airport: str,
               hover: bool) -> int:
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"output  → {outdir}")
    print(f"url     → {url}")
    print(f"airport → {airport}")
    print(f"view    → {view}")
    print(f"hover   → {hover}")
    views = ["overview", "all"] if view == "both" else [view]
    failures = 0
    all_results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for v in views:
            f, r = await run_for_view(browser, url, outdir, airport, v, hover)
            failures += f
            all_results.extend(r)
        await browser.close()

    summary_path = outdir / "summary.json"
    summary_path.write_text(json.dumps(all_results, indent=2))
    print(f"\nSummary written to {summary_path}")
    print(f"Failures: {failures}/{len(VIEWPORTS) * len(views)}")
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:5050/")
    ap.add_argument("--out", default="./screenshots")
    ap.add_argument("--view", default="both",
                    choices=["overview", "all", "both"])
    ap.add_argument("--airport", default="KMYF")
    ap.add_argument("--hover", action="store_true",
                    help="move mouse to middle of first chart "
                         "before screenshotting")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.url, Path(args.out),
                              view=args.view,
                              airport=args.airport,
                              hover=args.hover)))
