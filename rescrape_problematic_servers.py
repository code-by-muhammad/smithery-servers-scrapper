"""
Re-scrape all problematic servers with the fixed scraper
"""
from smithery_scraper import SmitheryScraper
import json
import time
import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# Fix Unicode encoding for Windows console
sys.stdout.reconfigure(encoding='utf-8')

# Load problematic servers (batch 1 for testing)
print("Loading problematic servers...")
with open('problematic_servers.json', 'r', encoding='utf-8') as f:
    problematic_servers = json.load(f)

print(f"Found {len(problematic_servers)} problematic servers to re-scrape")

# Also load correct servers to merge later
print("Loading correct servers...")
with open('correct_servers.json', 'r', encoding='utf-8') as f:
    correct_servers = json.load(f)

print(f"Found {len(correct_servers)} correct servers")

# Initialize incremental save file (clear if exists)
rescraped_jsonl_file = 'rescraped_servers.jsonl'
if os.path.exists(rescraped_jsonl_file):
    print(f"Clearing existing incremental save file: {rescraped_jsonl_file}")
    os.remove(rescraped_jsonl_file)

# Create scraper instance
scraper = SmitheryScraper()

# Thread-safe data collection
rescraped_data = []
data_lock = Lock()

# File lock for incremental saving
file_write_lock = Lock()

# Statistics
stats = {
    'total': len(problematic_servers),
    'completed': 0,
    'success': 0,
    'partial': 0,
    'failed': 0,
    'improved': 0
}
stats_lock = Lock()

def save_server_incrementally(server_data):
    """Save a single server to JSONL file immediately (thread-safe)"""
    with file_write_lock:
        with open(rescraped_jsonl_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(server_data, ensure_ascii=False) + '\n')

def rescrape_server(server_info):
    """Re-scrape a single problematic server"""
    server_url = server_info['server_url']
    server_name = server_info['server_name']
    expected_tools = server_info['total_tools']
    previous_tools = server_info['actual_tools']
    
    try:
        # Use the fixed scraper with retry logic
        server_data = scraper.scrape_single_server_with_browser(server_url, max_retries=3)
        
        if server_data:
            actual_tools = len(server_data['tools'])
            improvement = actual_tools - previous_tools
            
            # Update statistics
            with stats_lock:
                stats['completed'] += 1
                if actual_tools == expected_tools:
                    stats['success'] += 1
                    status = "SUCCESS"
                elif actual_tools > 0:
                    stats['partial'] += 1
                    status = "PARTIAL"
                else:
                    stats['failed'] += 1
                    status = "FAILED"
                
                if improvement > 0:
                    stats['improved'] += 1
            
            # Print progress
            progress = f"[{stats['completed']}/{stats['total']}]"
            print(f"{progress} {status}: {server_name[:40]:<40} {actual_tools}/{expected_tools} (+{improvement})")
            
            # Save immediately to file
            save_server_incrementally(server_data)
            
            return {
                'data': server_data,
                'status': status,
                'improvement': improvement,
                'previous': previous_tools
            }
        else:
            with stats_lock:
                stats['completed'] += 1
                stats['failed'] += 1
            print(f"[{stats['completed']}/{stats['total']}] FAILED: {server_name[:40]:<40} (scraper returned None)")
            return None
            
    except Exception as e:
        with stats_lock:
            stats['completed'] += 1
            stats['failed'] += 1
        print(f"[{stats['completed']}/{stats['total']}] ERROR: {server_name[:40]:<40} - {str(e)[:50]}")
        return None

# Start re-scraping
print("\n" + "="*80)
print("Starting re-scraping with 10 threads...")
print("="*80 + "\n")

start_time = time.time()

with ThreadPoolExecutor(max_workers=10) as executor:
    # Submit all tasks
    futures = {executor.submit(rescrape_server, server): server for server in problematic_servers}
    
    # Collect results as they complete
    for future in as_completed(futures):
        result = future.result()
        if result and result['data']:
            with data_lock:
                rescraped_data.append(result['data'])

elapsed_time = time.time() - start_time

# Print final statistics
print("\n" + "="*80)
print("RE-SCRAPING COMPLETE!")
print("="*80)
print(f"Total servers:     {stats['total']}")
print(f"Completed:         {stats['completed']}")
print(f"Success (100%):    {stats['success']}")
print(f"Partial (>0):      {stats['partial']}")
print(f"Failed (0 tools):  {stats['failed']}")
print(f"Improved:          {stats['improved']}")
print(f"Time elapsed:      {elapsed_time:.1f}s ({elapsed_time/60:.1f} minutes)")
print("="*80)

# Load re-scraped data from JSONL file (incremental saves)
print("\nLoading re-scraped servers from incremental save file...")
rescraped_data = []
if os.path.exists(rescraped_jsonl_file):
    with open(rescraped_jsonl_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    server_data = json.loads(line)
                    rescraped_data.append(server_data)
                except json.JSONDecodeError as e:
                    print(f"Warning: Failed to parse line in JSONL file: {e}")
    print(f"Loaded {len(rescraped_data)} servers from {rescraped_jsonl_file}")
else:
    print(f"Warning: Incremental save file {rescraped_jsonl_file} not found, using in-memory data")

# Merge with correct servers
print("\nMerging with correct servers...")
all_servers = correct_servers + rescraped_data

# Save combined results
output_file = 'smithery_servers_fixed.json'
print(f"Saving {len(all_servers)} servers to {output_file}...")
with open(output_file, 'w', encoding='utf-8') as f:
    json.dump(all_servers, f, indent=2, ensure_ascii=False)

print(f"\n[OK] Saved {len(all_servers)} servers to {output_file}")

# Save just the re-scraped data for comparison (convert from JSONL to JSON)
rescraped_file = 'rescraped_servers.json'
print(f"Saving {len(rescraped_data)} re-scraped servers to {rescraped_file}...")
with open(rescraped_file, 'w', encoding='utf-8') as f:
    json.dump(rescraped_data, f, indent=2, ensure_ascii=False)

print(f"[OK] Saved {len(rescraped_data)} re-scraped servers to {rescraped_file}")
print(f"[INFO] Incremental save file {rescraped_jsonl_file} is also available (JSONL format)")

# Save statistics
stats_file = 'rescrape_stats.json'
stats['elapsed_time_seconds'] = elapsed_time
stats['elapsed_time_minutes'] = elapsed_time / 60
with open(stats_file, 'w', encoding='utf-8') as f:
    json.dump(stats, f, indent=2)

print(f"[OK] Saved statistics to {stats_file}")

print("\n" + "="*80)
print("SUMMARY:")
print("="*80)
print(f"Success rate: {stats['success']/stats['total']*100:.1f}%")
print(f"Partial rate: {stats['partial']/stats['total']*100:.1f}%")
print(f"Failed rate:  {stats['failed']/stats['total']*100:.1f}%")
print(f"Improvement:  {stats['improved']/stats['total']*100:.1f}% of servers improved")
print("="*80)


