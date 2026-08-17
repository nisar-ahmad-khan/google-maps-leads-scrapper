"""Google Maps business lead scraper.

Usage:
    python scraper.py --query "coffee shops" --location "Austin, TX" --limit 50 --output leads.csv
    python scraper.py --query "plumbers" --location "Denver, CO" --no-details --output leads.json
    python scraper.py --query "dentists" --location "Miami, FL" --limit 100 --concurrency 8 --sort-by rating

Notes:
    Google serves a stripped-down "limited view" (no phone/website/review count,
    even on the place detail page) to sessions it flags as automated or that
    lack a signed-in account. When that happens those fields come back blank --
    it is a Google-side gate, not a scraper bug. Name, category, rating, address
    and the Maps URL are pulled from the results list and are unaffected.
"""

import argparse
import asyncio
import csv
import json
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# Playwright's Node driver process defaults to a ~2GB heap, which can be
# exhausted on long runs (hundreds of leads, many concurrent detail-page
# navigations). Give it more headroom unless the caller already set this.
os.environ.setdefault("NODE_OPTIONS", "--max-old-space-size=4096")

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

RESULTS_FEED_SELECTOR = 'div[role="feed"]'
RESULT_ROW_SELECTOR = 'div[role="feed"] > div'

STOPWORDS = {"in", "near", "the", "a", "an", "and", "or", "for", "of", "shops", "shop", "stores", "store"}

CARD_EXTRACT_JS = """
(rows) => rows.map(el => {
    const link = el.querySelector("a.hfpxzc");
    if (!link) return null;

    const name = el.querySelector(".qBF1Pd")?.textContent?.trim() || "";

    let category = "", address = "";
    for (const row of el.querySelectorAll(".W4Efsd")) {
        const nested = row.querySelector(".W4Efsd");
        if (nested) {
            const parts = nested.textContent.split("\\u00b7").map(s => s.trim()).filter(Boolean);
            if (parts.length > 0) category = parts[0];
            if (parts.length > 1) address = parts[parts.length - 1];
            break;
        }
    }

    let rating = "", reviewCount = "";
    const ratingImg = el.querySelector('span[role="img"][aria-label*="star" i]');
    if (ratingImg) {
        const label = ratingImg.getAttribute("aria-label") || "";
        const starMatch = label.match(/([\\d.]+)\\s*star/i);
        if (starMatch) rating = starMatch[1];
        const reviewMatch = label.match(/([\\d,]+)\\s*review/i);
        if (reviewMatch) reviewCount = reviewMatch[1].replace(/,/g, "");
        const containerText = ratingImg.closest(".AJB7ye")?.textContent || "";
        const parenMatch = containerText.match(/\\(([\\d,]+)\\)/);
        if (parenMatch) reviewCount = parenMatch[1].replace(/,/g, "");
    }

    return { name, category, address, rating, reviewCount, url: link.href };
}).filter(Boolean)
"""


@dataclass
class Business:
    name: str = ""
    category: str = ""
    rating: str = ""
    review_count: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""
    google_maps_url: str = ""


def build_search_url(query: str, location: str) -> str:
    search_term = f"{query} in {location}" if location else query
    return f"https://www.google.com/maps/search/{search_term.replace(' ', '+')}"


def query_stems(query: str) -> list[str]:
    words = [w.strip(".,'").lower() for w in query.split()]
    return [w[:5] if len(w) > 5 else w for w in words if w and w not in STOPWORDS and len(w) >= 3]


def is_relevant(row: dict, stems: list[str]) -> bool:
    if not stems:
        return True
    haystack = f"{row.get('category', '')} {row.get('name', '')}".lower()
    return any(stem in haystack for stem in stems)


async def dismiss_consent_dialog(page: Page) -> None:
    for text in ("Reject all", "Accept all", "I agree"):
        try:
            button = page.get_by_role("button", name=text, exact=False)
            if await button.count() > 0:
                await button.first.click(timeout=3000)
                return
        except PlaywrightTimeoutError:
            continue


async def scroll_results_feed(page: Page, target_count: int, max_idle_scrolls: int = 6) -> None:
    feed = page.locator(RESULTS_FEED_SELECTOR)
    await feed.wait_for(state="attached", timeout=15000)

    previous_count = 0
    idle_scrolls = 0

    while idle_scrolls < max_idle_scrolls:
        current_count = await page.locator(RESULT_ROW_SELECTOR).count()
        if current_count >= target_count:
            break

        await feed.evaluate("(el) => el.scrollBy(0, el.scrollHeight)")
        await page.wait_for_timeout(1500)

        current_count = await page.locator(RESULT_ROW_SELECTOR).count()
        if current_count <= previous_count:
            idle_scrolls += 1
        else:
            idle_scrolls = 0
        previous_count = current_count

        if await page.get_by_text("You've reached the end of the list").count() > 0:
            break


async def extract_list_rows(page: Page) -> list[dict]:
    rows = page.locator(RESULT_ROW_SELECTOR)
    data = await rows.evaluate_all(CARD_EXTRACT_JS)
    seen = set()
    unique = []
    for item in data:
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        unique.append(item)
    return unique


async def enrich_from_detail_page(page: Page, url: str, timeout_ms: int = 15000) -> dict:
    """Best-effort fetch of phone/website/full address/review count from a place's page.

    Returns empty strings for any field Google doesn't expose to this session
    (e.g. under the anonymous "limited view" gate) rather than raising.
    """
    result = {"phone": "", "website": "", "address": "", "review_count": ""}
    await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    await page.wait_for_selector("h1", timeout=timeout_ms)
    await page.wait_for_timeout(800)

    phone_btn = page.locator('button[data-item-id^="phone:tel:"]')
    if await phone_btn.count() > 0:
        item_id = await phone_btn.first.get_attribute("data-item-id") or ""
        result["phone"] = item_id.replace("phone:tel:", "").strip()

    website_link = page.locator('a[data-item-id="authority"]')
    if await website_link.count() > 0:
        result["website"] = await website_link.first.get_attribute("href") or ""

    address_btn = page.locator('button[data-item-id="address"]')
    if await address_btn.count() > 0:
        label = await address_btn.first.get_attribute("aria-label") or ""
        result["address"] = label.replace("Address:", "").strip()

    review_span = page.locator('.F7nice span[aria-label*="review" i]')
    if await review_span.count() > 0:
        label = await review_span.first.get_attribute("aria-label") or ""
        digits = "".join(ch for ch in label if ch.isdigit())
        if digits:
            result["review_count"] = digits

    return result


PAGE_RECYCLE_INTERVAL = 20  # close/reopen the worker's tab periodically to bound memory growth


async def fetch_details_worker(
    context: BrowserContext,
    queue: "asyncio.Queue[tuple[int, dict]]",
    output: dict[int, dict],
    max_attempts: int,
) -> None:
    page = await context.new_page()
    processed = 0
    try:
        while True:
            try:
                index, row = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            await asyncio.sleep(random.uniform(0.2, 0.9))

            details = {"phone": "", "website": "", "address": "", "review_count": ""}
            for attempt in range(1, max_attempts + 1):
                try:
                    details = await enrich_from_detail_page(page, row["url"])
                except PlaywrightTimeoutError:
                    details = {"phone": "", "website": "", "address": "", "review_count": ""}

                if details["phone"] or details["website"]:
                    break
                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** attempt, 6) + random.uniform(0, 0.5))

            output[index] = details
            queue.task_done()
            processed += 1

            if processed % PAGE_RECYCLE_INTERVAL == 0:
                await page.close()
                page = await context.new_page()
    finally:
        await page.close()


async def scrape(
    query: str,
    location: str,
    limit: int,
    headless: bool,
    fetch_details: bool,
    concurrency: int,
    relevance_filter: bool,
    max_attempts: int,
) -> list[Business]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        search_page = await browser.new_page(viewport={"width": 1400, "height": 1000}, locale="en-US")

        url = build_search_url(query, location)
        await search_page.goto(url, timeout=60000)
        await dismiss_consent_dialog(search_page)

        try:
            await search_page.wait_for_selector(RESULTS_FEED_SELECTOR, timeout=20000)
        except PlaywrightTimeoutError:
            await browser.close()
            return []

        scroll_target = int(limit * 1.3) + 5 if relevance_filter else limit
        await scroll_results_feed(search_page, target_count=scroll_target)
        rows = await extract_list_rows(search_page)
        await search_page.close()

        if relevance_filter:
            stems = query_stems(query)
            rows = [r for r in rows if is_relevant(r, stems)]

        rows = rows[:limit]

        detail_by_index: dict[int, dict] = {}
        if fetch_details and rows:
            queue: "asyncio.Queue[tuple[int, dict]]" = asyncio.Queue()
            for i, row in enumerate(rows):
                queue.put_nowait((i, row))

            context = await browser.new_context(viewport={"width": 1400, "height": 1000}, locale="en-US")
            workers = [
                asyncio.create_task(fetch_details_worker(context, queue, detail_by_index, max_attempts))
                for _ in range(min(concurrency, len(rows)))
            ]
            try:
                await asyncio.gather(*workers, return_exceptions=False)
            except Exception as exc:
                print(
                    f"Warning: detail-page fetching stopped early ({exc}). "
                    f"Keeping {len(detail_by_index)}/{len(rows)} enriched so far; "
                    "the rest will fall back to list-view fields.",
                    file=sys.stderr,
                )
                for w in workers:
                    w.cancel()
            try:
                await context.close()
            except Exception:
                pass

        try:
            await browser.close()
        except Exception:
            pass

    results = []
    for i, row in enumerate(rows):
        business = Business(
            name=row["name"],
            category=row["category"],
            rating=row["rating"],
            review_count=row["reviewCount"],
            address=row["address"],
            google_maps_url=row["url"],
        )
        details = detail_by_index.get(i)
        if details:
            business.phone = details["phone"]
            business.website = details["website"]
            if details["address"]:
                business.address = details["address"]
            if details["review_count"]:
                business.review_count = details["review_count"]
        results.append(business)

    return results


def sort_results(results: list[Business], sort_by: str) -> list[Business]:
    if sort_by == "rating":
        return sorted(results, key=lambda b: float(b.rating) if b.rating else -1, reverse=True)
    if sort_by == "reviews":
        return sorted(results, key=lambda b: int(b.review_count) if b.review_count else -1, reverse=True)
    if sort_by == "name":
        return sorted(results, key=lambda b: b.name.lower())
    return results


def export_csv(results: list[Business], output_path: Path) -> None:
    fieldnames = list(Business.__dataclass_fields__.keys())
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for business in results:
            writer.writerow(asdict(business))


def export_json(results: list[Business], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8") as f:
        json.dump([asdict(b) for b in results], f, indent=2, ensure_ascii=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape business leads from Google Maps.")
    parser.add_argument("--query", required=True, help='Search term, e.g. "coffee shops"')
    parser.add_argument("--location", default="", help='Location, e.g. "Austin, TX"')
    parser.add_argument("--limit", type=int, default=20, help="Max number of results to collect")
    parser.add_argument("--output", default="leads.csv", help="Output file path (.csv or .json)")
    parser.add_argument("--headless", dest="headless", action="store_true", default=True, help="Run browser headless (default)")
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="Show the browser window")
    parser.add_argument(
        "--details",
        dest="details",
        action="store_true",
        default=True,
        help="Visit each result's page for phone/website/full address (default, slower)",
    )
    parser.add_argument(
        "--no-details",
        dest="details",
        action="store_false",
        help="Skip per-result detail page visits (faster, list-view fields only)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Number of detail pages to fetch in parallel (default 5)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Max attempts per detail-page fetch before giving up on phone/website (default 2)",
    )
    parser.add_argument(
        "--relevance-filter",
        dest="relevance_filter",
        action="store_true",
        default=True,
        help="Drop results whose category/name don't match the query terms (default on)",
    )
    parser.add_argument(
        "--no-relevance-filter",
        dest="relevance_filter",
        action="store_false",
        help="Keep every result Google Maps returns, including unrelated ads/mismatches",
    )
    parser.add_argument(
        "--sort-by",
        choices=["none", "rating", "reviews", "name"],
        default="none",
        help="Sort output (default: keep Google Maps' original order)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_path = Path(args.output)

    print(f"Searching Google Maps for '{args.query}' in '{args.location or 'anywhere'}' (limit={args.limit})...")
    start = time.time()
    results = asyncio.run(
        scrape(
            args.query,
            args.location,
            args.limit,
            args.headless,
            args.details,
            args.concurrency,
            args.relevance_filter,
            args.retries,
        )
    )
    results = sort_results(results, args.sort_by)
    elapsed = time.time() - start

    if not results:
        print("No results found.")
        return 1

    if output_path.suffix.lower() == ".json":
        export_json(results, output_path)
    else:
        export_csv(results, output_path)

    print(f"Found {len(results)} businesses in {elapsed:.1f}s. Saved to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
