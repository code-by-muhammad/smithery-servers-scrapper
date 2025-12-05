# Smithery.ai Server Scraper

Scrapes servers from [smithery.ai](https://smithery.ai/servers) with complete tool information including parameters.

## Features

- Scrapes all servers with pagination support
- Extracts all tools with complete parameters (inputSchema)
- Parallel scraping with configurable threads
- Incremental saving to prevent data loss
- Retry logic for failed attempts

## Installation

```bash
pip install playwright
playwright install chromium
```

## Usage

```bash
# Scrape all servers (single-threaded)
python smithery_scraper.py

# Scrape specific page
python smithery_scraper.py --page 1

# Scrape first N servers
python smithery_scraper.py --limit 20

# Use parallel threads (recommended: 3-5)
python smithery_scraper.py --threads 5

# Enable incremental saving (recommended for long runs)
python smithery_scraper.py --incremental

# Combine options
python smithery_scraper.py --page 2 --limit 10 --threads 5 --incremental
```

## Options

- `-p, --page`: Specific page number to scrape
- `-n, --limit`: Maximum number of servers to scrape
- `-t, --threads`: Number of parallel threads (default: 1, recommended: 3-5, max: 10)
- `-o, --output`: Output JSON file name (default: `smithery_servers.json`)
- `--incremental`: Enable incremental saving to JSONL file

## Output

Results are saved to `smithery_servers.json` (or specified filename) with complete server and tool information:

```json
{
  "server_name": "Linkup",
  "server_url": "https://smithery.ai/server/@LinkupPlatform/linkup-mcp-server",
  "connection_url": "https://server.smithery.ai/@LinkupPlatform/linkup-mcp-server/mcp",
  "homepage": "https://github.com/LinkupPlatform/linkup-mcp-server",
  "source_code": "https://github.com/LinkupPlatform/linkup-mcp-server",
  "authentication_method": "OAuth",
  "description": "Search the web in real time...",
  "total_tools": 2,
  "tools": [
    {
      "name": "linkup-search",
      "description": "Search the web in real time...",
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": {
            "type": "string",
            "description": "Natural language search query..."
          }
        },
        "required": ["query"]
      }
    }
  ]
}
```

## Requirements

- Python 3.8+
- Playwright
- Internet connection
