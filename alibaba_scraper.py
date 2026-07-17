#!/usr/bin/env python3
"""
Alibaba supplier minisite scraper.

Opens an Alibaba supplier minisite in a visible Chromium browser, follows the
redirect, waits for the page to load, gradually scrolls to trigger lazy-loaded
products, and extracts product data into a CSV.

This script does NOT attempt to bypass CAPTCHA or any Alibaba security
restriction. If a security/verification page is detected, it stops and asks
the user to solve it manually in the visible browser window.

Usage:
    python alibaba_scraper.py [URL]

Outputs:
    alibaba_products.csv  - extracted products
    alibaba_page.html     - final rendered HTML (for debugging)
    alibaba_page.png      - full-page screenshot
"""

import csv
import os
import re
import sys
import time
from urllib.parse import urljoin

from playwright.sync_api import (
    sync_playwright,
    Error as PWError,
    TimeoutError as PWTimeoutError,
)

DEFAULT_URL = "https://x.alibaba.com/B2BRxB?ck=minisite"

CSV_FILE = "alibaba_products.csv"
HTML_FILE = "alibaba_page.html"
PNG_FILE = "alibaba_page.png"

# CSS columns in output order.
FIELDNAMES = [
    "title",
    "price",
    "min_order",
    "product_url",
    "image_url",
    "supplier",
]

# Keywords that indicate a security / verification / captcha wall.
SECURITY_MARKERS = [
    "captcha",
    "punish",
    "slidingverify",
    "nc_1_wrapper",  # Alibaba "NoCaptcha" slider
    "verify.aliexpress",
    "unusual traffic",
    "please slide",
    "security check",
]


def looks_like_security_wall(page):
    """Best-effort detection of a verification/captcha page. We never solve it."""
    url = (page.url or "").lower()
    try:
        content = (page.content() or "").lower()
    except Exception:
        content = ""
    haystack = url + " " + content
    return any(marker in haystack for marker in SECURITY_MARKERS)


def gradual_scroll(page, passes=8, step_pause=0.8):
    """
    Scroll down gradually multiple times so lazy-loaded products render.

    Each pass scrolls in several small steps to the current bottom, waits for
    content, and repeats until the page height stops growing (or `passes` is
    reached).
    """
    last_height = 0
    for i in range(passes):
        height = page.evaluate("document.body.scrollHeight")
        # Scroll down in small increments rather than one jump.
        steps = 10
        for s in range(1, steps + 1):
            page.evaluate(f"window.scrollTo(0, {height} * {s} / {steps});")
            time.sleep(step_pause / steps)
        # Nudge back up a little then down again — some lazy loaders need it.
        page.evaluate("window.scrollBy(0, -300);")
        time.sleep(0.3)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(step_pause)

        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except PWTimeoutError:
            pass

        new_height = page.evaluate("document.body.scrollHeight")
        print(f"  scroll pass {i + 1}/{passes}: height {height} -> {new_height}")
        if new_height == last_height and new_height == height:
            # Height stable for a full pass; assume all lazy content loaded.
            print("  page height stable, stopping scroll early")
            break
        last_height = height

    # Return to top so screenshot/HTML capture is consistent.
    page.evaluate("window.scrollTo(0, 0);")
    time.sleep(0.5)


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def extract_products(page, base_url):
    """
    Extract products using flexible selectors. Alibaba class names change
    frequently, so we try several container patterns and, for each container,
    several attribute/text patterns per field.
    """
    js = r"""
    () => {
        const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

        // Candidate container selectors — try each, keep the one that yields
        // the most matches. Kept broad on purpose.
        const containerSelectors = [
            "[data-spm*='product']",
            "[class*='product-card']",
            "[class*='productCard']",
            "[class*='product-item']",
            "[class*='productItem']",
            "[class*='gallery-card']",
            "[class*='offer-card']",
            "[class*='item-main']",
            "a[href*='/product-detail/']",
            "a[href*='/product/']",
            ".J-offer-wrapper",
            "[class*='card'] a[href*='alibaba.com']",
        ];

        let best = [];
        for (const sel of containerSelectors) {
            const nodes = Array.from(document.querySelectorAll(sel));
            if (nodes.length > best.length) best = nodes;
        }

        // Normalize: if we matched anchors directly, use their closest card-ish
        // ancestor as the container when possible.
        const containers = best.map((n) => {
            if (n.tagName === 'A') {
                const card = n.closest("[class*='card'], [class*='item'], li, div");
                return card || n;
            }
            return n;
        });

        const seen = new Set();
        const results = [];

        const pickText = (root, selectors) => {
            for (const sel of selectors) {
                const el = root.querySelector(sel);
                if (el && clean(el.textContent)) return clean(el.textContent);
            }
            return '';
        };

        for (const c of containers) {
            if (!c || seen.has(c)) continue;
            seen.add(c);

            // Product link
            let link = c.querySelector(
                "a[href*='/product-detail/'], a[href*='/product/'], a[href*='alibaba.com']"
            ) || (c.tagName === 'A' ? c : null);
            const href = link ? link.getAttribute('href') : '';

            // Title
            let title = pickText(c, [
                "[class*='title']",
                "[class*='subject']",
                "[class*='name']",
                "h2", "h3", "h4",
                "a[title]",
            ]);
            if (!title && link) {
                title = clean(link.getAttribute('title') || link.textContent);
            }

            // Price / price range
            const price = pickText(c, [
                "[class*='price']",
                "[class*='Price']",
                "[class*='amount']",
                "[class*='cost']",
            ]);

            // Minimum order quantity
            let moq = pickText(c, [
                "[class*='moq']",
                "[class*='MOQ']",
                "[class*='min-order']",
                "[class*='minOrder']",
                "[class*='quantity']",
            ]);
            if (!moq) {
                // fall back: scan text for "Min. Order" style phrases
                const txt = clean(c.textContent);
                const m = txt.match(/(min\.?\s*order[^,;]*?\d[\d,\.]*\s*\w+)/i)
                       || txt.match(/(\d[\d,\.]*\s*(pieces?|pcs?|sets?|units?|bags?|boxes?)\b[^,;]*min)/i);
                if (m) moq = clean(m[1]);
            }

            // Image
            let img = '';
            const imgEl = c.querySelector('img');
            if (imgEl) {
                img = imgEl.getAttribute('src')
                    || imgEl.getAttribute('data-src')
                    || imgEl.getAttribute('data-lazy-src')
                    || imgEl.getAttribute('data-original')
                    || '';
                if (!img) {
                    const srcset = imgEl.getAttribute('srcset') || imgEl.getAttribute('data-srcset');
                    if (srcset) img = srcset.split(',')[0].trim().split(' ')[0];
                }
            }

            // Supplier name
            const supplier = pickText(c, [
                "[class*='supplier']",
                "[class*='company']",
                "[class*='seller']",
                "[class*='store-name']",
                "[class*='shop-name']",
            ]);

            // Only keep rows that have at least a title or a product link.
            if (!title && !href) continue;

            results.push({
                title: title,
                price: price,
                min_order: moq,
                product_url: href,
                image_url: img,
                supplier: supplier,
            });
        }
        return results;
    }
    """
    raw = page.evaluate(js)

    products = []
    for r in raw:
        product_url = r.get("product_url") or ""
        image_url = r.get("image_url") or ""
        if product_url.startswith("//"):
            product_url = "https:" + product_url
        elif product_url and not product_url.startswith("http"):
            product_url = urljoin(base_url, product_url)
        if image_url.startswith("//"):
            image_url = "https:" + image_url
        elif image_url and not image_url.startswith("http"):
            image_url = urljoin(base_url, image_url)

        products.append(
            {
                "title": clean_text(r.get("title")),
                "price": clean_text(r.get("price")),
                "min_order": clean_text(r.get("min_order")),
                "product_url": product_url,
                "image_url": image_url,
                "supplier": clean_text(r.get("supplier")),
            }
        )
    return products


def deduplicate(products):
    """Remove duplicates, keyed by product URL when present, else by title+image."""
    seen = set()
    unique = []
    for p in products:
        key = p["product_url"] or f"{p['title']}|{p['image_url']}"
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def _best_effort_debug_dump(page):
    """Try to save whatever HTML/screenshot the browser currently has. Never raises."""
    try:
        html = page.content()  # fetch first; don't create the file if this fails
        with open(HTML_FILE, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"  wrote partial debug HTML -> {HTML_FILE}")
    except Exception as exc:
        print(f"  (could not save debug HTML: {exc})")
    try:
        page.screenshot(path=PNG_FILE)
        print(f"  wrote debug screenshot -> {PNG_FILE}")
    except Exception as exc:
        print(f"  (could not save screenshot: {exc})")


def save_csv(products, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for p in products:
            writer.writerow(p)


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"Target URL: {url}")

    # Optional override for the Chromium binary. Useful on servers/CI where a
    # Chromium is pre-installed at a path that doesn't match Playwright's
    # bundled version (set PLAYWRIGHT_CHROMIUM_EXECUTABLE to that binary).
    exe_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None

    with sync_playwright() as p:
        launch_kwargs = dict(
            headless=False,  # visible browser as required
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        if exe_path:
            launch_kwargs["executable_path"] = exe_path
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport=None,  # use full window
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()

        print("Opening page (following redirects automatically)...")
        navigated = False
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            navigated = True
        except PWTimeoutError:
            # DOM didn't finish in time, but something may have loaded — keep going.
            print("Initial navigation timed out; continuing with whatever loaded.")
            navigated = True
        except PWError as exc:
            # Connection-level failure (DNS, proxy/tunnel, refused, blocked, ...).
            # The page never loaded, so extraction is impossible. Diagnose and
            # bail out cleanly instead of crashing with a raw traceback.
            msg = str(exc)
            print(f"\n[!] Navigation failed — the page could not be loaded:\n    {msg}")
            if "ERR_TUNNEL_CONNECTION_FAILED" in msg or "ERR_PROXY" in msg:
                print(
                    "    This is a proxy/tunnel rejection: outbound network access to\n"
                    "    the target host is being blocked (e.g. a firewall/egress policy\n"
                    "    returning 403 to the CONNECT). This is NOT something the scraper\n"
                    "    can work around. Run it on a network that can reach the host."
                )
            elif "ERR_NAME_NOT_RESOLVED" in msg:
                print("    DNS resolution failed — check the URL and your network/DNS.")
            elif "ERR_INTERNET_DISCONNECTED" in msg or "ERR_CONNECTION" in msg:
                print("    No usable network connection to the target host.")
            # Best-effort: save whatever the browser has, for debugging.
            _best_effort_debug_dump(page)
            print("\nNo page content was loaded; not writing a product CSV.")
            print("Found 0 products.")
            browser.close()
            sys.exit(2)

        # Follow-up wait for the minisite to settle.
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeoutError:
            pass

        print(f"Landed on: {page.url}")

        if looks_like_security_wall(page):
            print(
                "\n[!] A security / verification page was detected.\n"
                "    This script does NOT bypass CAPTCHA or security checks.\n"
                "    Please solve it manually in the open browser window.\n"
                "    You have 60 seconds; the script will then continue."
            )
            time.sleep(60)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeoutError:
                pass

        print("Scrolling gradually to load lazy content...")
        gradual_scroll(page, passes=8, step_pause=0.8)

        print("Extracting products...")
        products = extract_products(page, page.url)
        unique = deduplicate(products)

        print(f"Saving debug HTML -> {HTML_FILE}")
        try:
            with open(HTML_FILE, "w", encoding="utf-8") as fh:
                fh.write(page.content())
        except Exception as exc:
            print(f"  could not save HTML: {exc}")

        print(f"Saving screenshot -> {PNG_FILE}")
        try:
            page.screenshot(path=PNG_FILE, full_page=True)
        except Exception as exc:
            print(f"  full-page screenshot failed ({exc}); trying viewport shot")
            try:
                page.screenshot(path=PNG_FILE)
            except Exception as exc2:
                print(f"  screenshot failed: {exc2}")

        print(f"Saving products -> {CSV_FILE}")
        save_csv(unique, CSV_FILE)

        print(f"\nFound {len(unique)} unique products "
              f"({len(products)} before de-duplication).")

        browser.close()


if __name__ == "__main__":
    main()
