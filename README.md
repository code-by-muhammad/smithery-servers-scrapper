# Smithery Scraper Toolkit (minimal guide)

Scrapes smithery.ai servers and tools (with parameters) and lets you re-scrape missing/failed lists.

## Install (once)
```bash
pip install -r requirements.txt  # includes playwright
playwright install chromium
```

## Core commands
- Scrape all/limited servers:
  ```bash
  python smithery_scraper.py --threads 5 [--page N | --limit N | --incremental]
  ```
- Find missing vs current listing:
  ```bash
  python missing_servers_checker.py --scraped-file scraped_servers.json \
    --output missing_servers.json --report audit_report.json --threads 10
  ```
- Re-scrape a URL list (missing/failed/etc.):
  ```bash
  python rescrape_missing_servers.py \
    --input missing_servers.json \
    --output rescraped_missing_servers.json \
    --jsonl rescraped_missing_servers.jsonl \
    --threads 10
  ```
- Re-scrape “problematic” format (expects extras like total_tools/actual_tools):
  ```bash
  python rescrape_problematic_servers.py
  ```

## Expected input formats
- `smithery_scraper.py`: no input list; scrapes site listing.
- `missing_servers_checker.py`: `scraped_file` must be a list of server objects with `server_url`.
- `rescrape_missing_servers.py`: input list can be
  - plain strings: `["https://smithery.ai/server/@user/foo", ...]`
  - or objects: `[{"server_url": "https://..."}, ...]`
- `rescrape_problematic_servers.py`: requires `problematic_servers.json` objects with `server_url`, `server_name`, `total_tools`, `actual_tools`, and `correct_servers.json` list to merge.

## Outputs (common)
- Full scrape: `smithery_servers.json` (or provided output) plus optional JSONL when `--incremental`.
- Missing checker: `missing_servers.json`, `audit_report.json`, optional `current_servers.json`.
- Rescrape missing: final JSON + JSONL; you can split success/failed manually if needed.
- Problematic rescrape: writes combined fixed file and stats.

## Notes
- Threads: 3–10 recommended; high values may hit rate limits/timeouts.
- The scraper now retries tool pages with a looser load condition and falls back to text parsing when tools aren’t clickable.
- Ensure Chromium is installed via Playwright before first run.***
