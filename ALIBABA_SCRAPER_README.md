# Alibaba Supplier Minisite Scraper

A Python + Playwright scraper for an Alibaba supplier minisite. It opens the
page in a **visible** Chromium browser, follows the redirect, waits for the
minisite to load, scrolls gradually to trigger lazy-loaded products, extracts
every product it can find, de-duplicates them, and writes the results to CSV.

It **does not** attempt to bypass CAPTCHA or any Alibaba security restriction.
If a verification page appears, the script pauses so you can solve it manually
in the open browser window, then continues.

## Outputs

| File                    | Description                              |
|-------------------------|------------------------------------------|
| `alibaba_products.csv`  | Extracted products (title, price, MOQ, product URL, image URL, supplier) |
| `alibaba_page.html`     | Final rendered HTML, for debugging       |
| `alibaba_page.png`      | Full-page screenshot                     |

## Install

```bash
# 1. (recommended) create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. install Python dependencies
pip install -r requirements.txt

# 3. install the Chromium browser Playwright drives
python -m playwright install chromium
```

On Linux you may also need the browser's system libraries:

```bash
python -m playwright install-deps chromium
# or: sudo playwright install-deps
```

## Run

```bash
python alibaba_scraper.py
```

Optionally pass a different minisite URL:

```bash
python alibaba_scraper.py "https://x.alibaba.com/B2BRxB?ck=minisite"
```

The script prints how many products were found when it finishes.

## Notes

- A **visible** browser is required by design (`headless=False`). Run it on a
  machine with a display, or under a virtual display (e.g. `xvfb-run`) on a
  headless server:

  ```bash
  xvfb-run -a python alibaba_scraper.py
  ```

- Selectors are intentionally **flexible** (attribute-contains matches like
  `[class*='price']`) because Alibaba's generated class names change often. If
  the site's markup changes substantially and results come back empty, inspect
  `alibaba_page.html` and adjust the selector lists in `extract_products`.
