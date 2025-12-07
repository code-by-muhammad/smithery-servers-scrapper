"""
Utilities for checking which Smithery servers are missing from an existing scrape.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, List, Set, Tuple, Dict
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import sync_playwright

BASE_URL = "https://smithery.ai"


def parse_page_indicator(text: str) -> Tuple[int, int] | None:
    """Extract (current_page, total_pages) from body text like '1 / 48'."""
    match = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def normalize_server_url(url: str) -> str:
    """
    Normalize a Smithery server URL so comparisons are consistent.
    - Strips query/fragment
    - Strips trailing slash
    - Adds base URL if a relative server path is provided
    """
    if not url:
        return ""

    url = url.strip()
    if url.startswith("/server/"):
        url = f"{BASE_URL}{url}"

    parsed = urlsplit(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or "smithery.ai"
    path = parsed.path.rstrip("/")

    return urlunsplit((scheme, netloc, path, "", ""))


def load_scraped_server_urls(path: str | Path) -> Set[str]:
    """
    Load server URLs from a previously scraped JSON file.
    The file is expected to be a list of server objects with a `server_url` field.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("Scraped file must contain a JSON list of server objects")

    urls: Set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("server_url")
        if not raw:
            continue
        normalized = normalize_server_url(raw)
        if normalized:
            urls.add(normalized)

    return urls


def find_missing_servers(
    scraped_urls: Iterable[str], current_urls: Iterable[str]
) -> List[str]:
    """Return the sorted list of current server URLs that are missing from the scraped set."""
    scraped_set: Set[str] = set()
    for url in scraped_urls:
        normalized = normalize_server_url(url)
        if normalized:
            scraped_set.add(normalized)

    current_set: Set[str] = set()
    for url in current_urls:
        normalized = normalize_server_url(url)
        if normalized:
            current_set.add(normalized)

    return sorted(current_set - scraped_set)


def _fetch_page_urls(
    page_num: int, delay_ms: int
) -> Tuple[int, Set[str], int | None, str | None]:
    """
    Fetch a single servers listing page and return URLs plus a detected total page count (if present).
    """
    urls: Set[str] = set()
    detected_total_pages: int | None = None
    error: str | None = None
    page_url = f"{BASE_URL}/servers?page={page_num}"

    try:
        print(f"[PAGE] Fetching listing page {page_num} ...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(page_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(delay_ms)

            body_text = page.locator("body").inner_text()
            page_info = parse_page_indicator(body_text)
            if page_info:
                _, detected_total_pages = page_info

            server_links = page.query_selector_all('a[href*="/server/"]')
            for link in server_links:
                href = link.get_attribute("href")
                if not href:
                    continue
                if not href.startswith("/server/") or href == "/servers":
                    continue
                href = href.split("?")[0]
                normalized = normalize_server_url(f"{BASE_URL}{href}")
                if normalized:
                    urls.add(normalized)

            browser.close()
        print(
            f"[PAGE] Page {page_num}: found {len(urls)} servers"
            + (f", total pages hinted: {detected_total_pages}" if detected_total_pages else "")
        )
    except Exception as exc:
        error = str(exc)
        print(f"[ERROR] Failed to load page {page_num}: {exc}")

    return page_num, urls, detected_total_pages, error


def fetch_current_server_urls(
    max_pages: int | None = None, delay_ms: int = 1500, threads: int = 1
) -> List[str]:
    """
    Crawl smithery.ai server listing pages to collect current server URLs only (no tool scraping).
    """
    urls: Set[str] = set()

    threads = max(1, min(threads, 10))

    # Fetch page 1 to seed URLs and try to detect total page count
    page_num, first_page_urls, detected_total, _ = _fetch_page_urls(1, delay_ms)
    urls.update(first_page_urls)
    print(f"[INFO] Seeded with {len(first_page_urls)} servers from page 1")

    if max_pages is not None:
        total_pages = max_pages
    elif detected_total:
        total_pages = detected_total
    else:
        total_pages = None

    # If we know total pages, fetch remaining pages in parallel
    if total_pages and total_pages > 1:
        pages_to_fetch = [n for n in range(2, total_pages + 1)]
        print(f"[INFO] Detected {total_pages} pages; fetching {len(pages_to_fetch)} more with {threads} threads")
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [
                executor.submit(_fetch_page_urls, page_num, delay_ms)
                for page_num in pages_to_fetch
            ]
            for future in as_completed(futures):
                p_num, page_urls, _, _ = future.result()
                new_additions = len(page_urls - urls)
                dup_against_global = len(page_urls) - new_additions
                urls.update(page_urls)
                print(
                    f"[INFO] Page {p_num}: +{new_additions} new, {dup_against_global} already seen; "
                    f"total unique so far {len(urls)}"
                )
    else:
        # Fallback: crawl sequentially until an empty page (threads not helpful without bounds)
        page = 2
        while True:
            p_num, page_urls, _, _ = _fetch_page_urls(page, delay_ms)
            if not page_urls:
                break
            new_additions = len(page_urls - urls)
            dup_against_global = len(page_urls) - new_additions
            urls.update(page_urls)
            print(
                f"[INFO] Page {p_num}: +{new_additions} new, {dup_against_global} already seen; "
                f"total unique so far {len(urls)}"
            )
            page += 1
            if max_pages and page > max_pages:
                break
            if page > 100:
                print("[WARNING] Reached safety limit of 100 pages.")
                break

    return sorted(urls)


def fetch_current_server_urls_with_errors(
    max_pages: int | None = None, delay_ms: int = 1500, threads: int = 1
) -> Tuple[List[str], List[Dict[str, str]]]:
    """
    Same as fetch_current_server_urls but also returns page-level errors.
    """
    errors: List[Dict[str, str]] = []
    urls: Set[str] = set()

    threads = max(1, min(threads, 10))

    page_num, first_page_urls, detected_total, error = _fetch_page_urls(1, delay_ms)
    urls.update(first_page_urls)
    print(f"[INFO] Seeded with {len(first_page_urls)} servers from page 1")
    if error:
        errors.append({"page": page_num, "error": error})

    if max_pages is not None:
        total_pages = max_pages
    elif detected_total:
        total_pages = detected_total
    else:
        total_pages = None

    if total_pages and total_pages > 1:
        pages_to_fetch = [n for n in range(2, total_pages + 1)]
        print(f"[INFO] Detected {total_pages} pages; fetching {len(pages_to_fetch)} more with {threads} threads")
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [
                executor.submit(_fetch_page_urls, page_num, delay_ms)
                for page_num in pages_to_fetch
            ]
            for future in as_completed(futures):
                p_num, page_urls, _, err = future.result()
                new_additions = len(page_urls - urls)
                dup_against_global = len(page_urls) - new_additions
                urls.update(page_urls)
                print(
                    f"[INFO] Page {p_num}: +{new_additions} new, {dup_against_global} already seen; "
                    f"total unique so far {len(urls)}"
                )
                if err:
                    errors.append({"page": p_num, "error": err})
    else:
        page = 2
        while True:
            p_num, page_urls, _, err = _fetch_page_urls(page, delay_ms)
            if not page_urls:
                break
            new_additions = len(page_urls - urls)
            dup_against_global = len(page_urls) - new_additions
            urls.update(page_urls)
            print(
                f"[INFO] Page {p_num}: +{new_additions} new, {dup_against_global} already seen; "
                f"total unique so far {len(urls)}"
            )
            if err:
                errors.append({"page": p_num, "error": err})
            page += 1
            if max_pages and page > max_pages:
                break
            if page > 100:
                errors.append({"page": page, "error": "safety limit reached"})
                print("[WARNING] Reached safety limit of 100 pages.")
                break

    return sorted(urls), errors


def build_audit_report(
    scraped_urls: Iterable[str],
    current_urls: Iterable[str],
    errors: List[Dict[str, str]],
) -> Dict[str, object]:
    missing = find_missing_servers(scraped_urls, current_urls)
    return {
        "scraped_count": len(set(scraped_urls)),
        "current_count": len(set(current_urls)),
        "missing_count": len(missing),
        "missing_urls": missing,
        "errors": errors,
    }


def _save_json(path: str | Path, data) -> None:
    path = Path(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(
        description="Find servers missing from an existing smithery scrape."
    )
    parser.add_argument(
        "--scraped-file",
        default="scraped_servers.json",
        help="Path to existing scraped JSON (default: scraped_servers.json)",
    )
    parser.add_argument(
        "--output",
        default="missing_servers.json",
        help="Where to write the missing server URLs (default: missing_servers.json)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit how many listing pages to scan (default: all until empty).",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=1500,
        help="Delay after loading each page to allow dynamic content (default: 1500ms).",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=10,
        help="Number of parallel threads to fetch listing pages (max 10, default 10).",
    )
    parser.add_argument(
        "--save-current",
        default=None,
        help="Optional path to save the full list of current server URLs.",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Optional path to write an audit report JSON (counts, missing URLs, errors).",
    )

    args = parser.parse_args()

    scraped_urls = load_scraped_server_urls(args.scraped_file)
    print(f"Loaded {len(scraped_urls)} scraped servers from {args.scraped_file}")

    if args.report:
        current_urls, errors = fetch_current_server_urls_with_errors(
            max_pages=args.max_pages, delay_ms=args.delay_ms, threads=args.threads
        )
    else:
        current_urls = fetch_current_server_urls(
            max_pages=args.max_pages, delay_ms=args.delay_ms, threads=args.threads
        )
        errors = []
    print(f"Found {len(current_urls)} servers currently listed on smithery.ai")

    missing_urls = find_missing_servers(scraped_urls, current_urls)
    print(f"Missing servers: {len(missing_urls)}")

    _save_json(args.output, missing_urls)
    print(f"[OK] Missing server URLs written to {args.output}")

    if args.save_current:
        _save_json(args.save_current, current_urls)
        print(f"[OK] Full current server URL list written to {args.save_current}")

    if args.report:
        report = build_audit_report(scraped_urls, current_urls, errors)
        _save_json(args.report, report)
        print(f"[OK] Audit report written to {args.report}")


if __name__ == "__main__":
    main()

