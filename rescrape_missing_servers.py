"""
Re-scrape servers listed in a simple URL list (e.g., missing_servers.json).
Avoids the problematic/correct merge flow; just writes fresh results.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import List, Dict, Any

from smithery_scraper import SmitheryScraper


def load_urls(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Input file must contain a JSON list of URLs or objects with server_url")

    urls: List[str] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            urls.append(item.strip())
        elif isinstance(item, dict) and item.get("server_url"):
            urls.append(str(item["server_url"]).strip())

    # preserve order, remove duplicates
    seen = set()
    uniq = []
    for url in urls:
        if url not in seen:
            uniq.append(url)
            seen.add(url)
    return uniq


def parse_args():
    parser = argparse.ArgumentParser(
        description="Re-scrape servers from a URL list (e.g., missing_servers.json)"
    )
    parser.add_argument(
        "--input",
        default="missing_servers.json",
        help="JSON file with a list of server URLs (default: missing_servers.json)",
    )
    parser.add_argument(
        "--output",
        default="rescraped_missing_servers.json",
        help="Where to write scraped server data (default: rescraped_missing_servers.json)",
    )
    parser.add_argument(
        "--jsonl",
        default="rescraped_missing_servers.jsonl",
        help="Incremental save JSONL path (default: rescraped_missing_servers.jsonl)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=10,
        help="Number of parallel threads (1-10, default 5)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Optional cap on how many URLs to process from the input list",
    )
    return parser.parse_args()


def main():
    # Fix Unicode for Windows just in case
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = parse_args()

    if args.threads < 1:
        print("[ERROR] threads must be >= 1")
        return
    if args.threads > 10:
        print("[WARNING] threads limited to 10")
        args.threads = 10

    urls = load_urls(args.input)
    if args.max:
        urls = urls[: args.max]

    print(f"Loaded {len(urls)} server URLs from {args.input}")

    # Prepare incremental file
    if os.path.exists(args.jsonl):
        print(f"Clearing existing incremental file: {args.jsonl}")
        os.remove(args.jsonl)

    scraper = SmitheryScraper()

    results: List[Dict[str, Any]] = []
    results_lock = Lock()
    file_lock = Lock()

    stats = {
        "total": len(urls),
        "completed": 0,
        "success": 0,
        "partial": 0,
        "failed": 0,
    }
    stats_lock = Lock()

    def save_jsonl(server_data: Dict[str, Any]):
        with file_lock:
            with open(args.jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(server_data, ensure_ascii=False) + "\n")

    def classify(server_data: Dict[str, Any]) -> str:
        tools_count = len(server_data.get("tools", []))
        expected = server_data.get("total_tools", 0)
        if tools_count == expected and expected > 0:
            return "SUCCESS"
        if tools_count > 0:
            return "PARTIAL"
        return "FAILED"

    def rescrape(url: str):
        try:
            server_data = scraper.scrape_single_server_with_browser(url, max_retries=3)
            if not server_data:
                status = "FAILED"
                tools_count = 0
            else:
                status = classify(server_data)
                tools_count = len(server_data.get("tools", []))

            with stats_lock:
                stats["completed"] += 1
                stats[status.lower()] += 1 if status.lower() in stats else 0

            # Print progress
            idx = stats["completed"]
            total = stats["total"]
            name = (server_data or {}).get("server_name", "Unknown")
            print(f"[{idx}/{total}] {status}: {name[:50]} ({tools_count} tools)")

            if server_data:
                save_jsonl(server_data)
                with results_lock:
                    results.append(server_data)
        except Exception as exc:
            with stats_lock:
                stats["completed"] += 1
                stats["failed"] += 1
            idx = stats["completed"]
            total = stats["total"]
            print(f"[{idx}/{total}] ERROR: {url} - {str(exc)[:100]}")

    start = time.time()

    if args.threads == 1:
        for url in urls:
            rescrape(url)
    else:
        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            futures = {executor.submit(rescrape, url): url for url in urls}
            for future in as_completed(futures):
                future.result()

    elapsed = time.time() - start

    # Load from JSONL to ensure we persist everything
    persisted = []
    if os.path.exists(args.jsonl):
        with open(args.jsonl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    persisted.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # Write final JSON
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(persisted or results, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 80)
    print("RE-SCRAPE COMPLETE")
    print("=" * 80)
    print(f"Total:     {stats['total']}")
    print(f"Completed: {stats['completed']}")
    print(f"Success:   {stats['success']}")
    print(f"Partial:   {stats['partial']}")
    print(f"Failed:    {stats['failed']}")
    print(f"Elapsed:   {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"[OK] Saved {len(persisted or results)} servers to {args.output}")
    print(f"[OK] Incremental JSONL at {args.jsonl}")


if __name__ == "__main__":
    main()


