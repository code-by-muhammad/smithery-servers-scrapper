"""
Smithery.ai Server Scraper - Finalized Version
Scrapes all servers from smithery.ai with complete tool information including parameters
Includes all fixes and improvements for maximum accuracy on first scrape
"""
from playwright.sync_api import sync_playwright
import json
import re
import time
import argparse
import os
import sys
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

class SmitheryScraper:
    def __init__(self, incremental_save: bool = False, output_file: str = "smithery_servers.json"):
        self.base_url = "https://smithery.ai"
        self.servers_data = []
        self.lock = threading.Lock()  # Thread-safe access to servers_data
        self.incremental_save = incremental_save
        self.output_file = output_file
        self.jsonl_file = output_file.replace('.json', '.jsonl')
        self.file_write_lock = threading.Lock()
        
        # Statistics
        self.stats = {
            'total_servers': 0,
            'scraped': 0,
            'success': 0,
            'partial': 0,
            'failed': 0,
            'total_tools': 0
        }
        self.stats_lock = threading.Lock()
        
        # Initialize incremental save file if enabled
        if self.incremental_save and os.path.exists(self.jsonl_file):
            print(f"Clearing existing incremental save file: {self.jsonl_file}")
            os.remove(self.jsonl_file)
    
    def save_server_incrementally(self, server_data: Dict[str, Any]):
        """Save a single server to JSONL file immediately (thread-safe)"""
        if not self.incremental_save:
            return
        
        with self.file_write_lock:
            with open(self.jsonl_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(server_data, ensure_ascii=False) + '\n')
    
    def normalize_server(self, server: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure server has all required fields in consistent format"""
        normalized = {
            "server_name": server.get("server_name", ""),
            "server_url": server.get("server_url", ""),
            "connection_url": server.get("connection_url", ""),
            "homepage": server.get("homepage", ""),
            "source_code": server.get("source_code", ""),
            "authentication_method": server.get("authentication_method", ""),
            "description": server.get("description", ""),
            "total_tools": server.get("total_tools", 0),
            "tools": server.get("tools", [])
        }
        
        # Ensure tools is a list and normalize each tool
        if not isinstance(normalized["tools"], list):
            normalized["tools"] = []
        
        # Normalize each tool
        normalized_tools = []
        for tool in normalized["tools"]:
            if isinstance(tool, dict):
                normalized_tool = {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {})
                }
                normalized_tools.append(normalized_tool)
        
        normalized["tools"] = normalized_tools
        normalized["total_tools"] = len(normalized_tools)
        
        return normalized
    
    def parse_parameters_from_text(self, text: str, start_pos: int) -> tuple:
        """
        Parse parameters from text starting after "Parameters" keyword
        Returns (parameters_dict, required_list)
        
        Format:
        Parameters
        paramName*required
        string
        Description text
        
        nextParam
        integer
        Description
        """
        parameters = {}
        required_params = []
        
        lines = text[start_pos:].split('\n')
        
        current_param = None
        param_type = None
        param_desc = []
        desc_line_count = 0  # Track how many description lines we've collected
        
        for line in lines:
            line = line.strip()
            
            # Skip empty lines
            if not line:
                continue
            
            # Stop if we hit another major section
            if line in ['Connect', 'Details', 'Resources', 'Company', 'Capabilities', 'Get connection URL', 'Or add to your client']:
                break
            
            # Check if line looks like next tool name (ALL_CAPS with underscores)
            if line.isupper() and '_' in line and len(line) > 15:
                break
            
            # Check if this is a type keyword (should come right after param name)
            if line in ['string', 'integer', 'boolean', 'object', 'array', 'number']:
                if current_param and not param_type:
                    param_type = line
                    desc_line_count = 0  # Reset description line counter
                continue
            
            # Check if this is a parameter name
            # Parameter names are:
            # - Short (< 50 chars)
            # - Start with lowercase letter
            # - May contain *required
            # - Don't contain spaces (except for *required)
            # - Are not sentences (don't start with articles, etc.)
            
            word_count = len(line.split())
            looks_like_param_name = (
                line and 
                len(line) < 50 and 
                word_count <= 2 and  # Param names are 1-2 words max
                (line[0].islower() or '*required' in line) and
                ('*required' in line or ' ' not in line or line.count(' ') == 0)  # No spaces or only *required
            )
            
            if looks_like_param_name:
                # Save previous parameter if complete
                if current_param and param_type:
                    param_name_clean = current_param.replace('*required', '').strip()
                    parameters[param_name_clean] = {
                        "type": param_type,
                        "description": ' '.join(param_desc).strip()
                    }
                    if '*required' in current_param:
                        required_params.append(param_name_clean)
                
                # Start new parameter
                current_param = line
                param_type = None
                param_desc = []
                desc_line_count = 0
            
            # Otherwise it's part of the description
            elif current_param and param_type and line:
                # Don't limit description lines - collect all until next param
                param_desc.append(line)
                desc_line_count += 1
        
        # Save last parameter
        if current_param and param_type:
            param_name_clean = current_param.replace('*required', '').strip()
            parameters[param_name_clean] = {
                "type": param_type,
                "description": ' '.join(param_desc).strip()
            }
            if '*required' in current_param:
                required_params.append(param_name_clean)
        
        return parameters, required_params
    
    def extract_tool_with_params(self, page, tool_name: str) -> Dict[str, Any]:
        """
        Extract a tool's complete information including parameters after it's been clicked/expanded
        """
        # Wait for content to load
        page.wait_for_timeout(1000)
        
        # Get page text
        body_text = page.locator('body').inner_text()
        
        # Find the tool's description (text between tool name and Parameters or next tool)
        description = ""
        lines = body_text.split('\n')
        
        found_tool = False
        desc_lines = []
        
        for i, line in enumerate(lines):
            line_stripped = line.strip()
            
            if tool_name in line_stripped:
                found_tool = True
                continue
            
            if found_tool:
                # Stop at Parameters or next tool
                if line_stripped in ['Parameters', 'Connect', 'Details']:
                    break
                if line_stripped and len(line_stripped) > 30:
                    desc_lines.append(line_stripped)
                if len(desc_lines) >= 3:  # Got enough description
                    break
        
        description = ' '.join(desc_lines).strip()
        
        # Extract parameters
        parameters = {}
        required_params = []
        
        if 'Parameters' in body_text:
            param_start = body_text.find('Parameters')
            parameters, required_params = self.parse_parameters_from_text(body_text, param_start + len('Parameters'))
        
        # Build inputSchema
        input_schema = {
            "type": "object",
            "properties": parameters
        }
        
        if required_params:
            input_schema["required"] = required_params
        
        return {
            "name": tool_name,
            "description": description,
            "inputSchema": input_schema
        }

    def parse_tools_from_text(self, body_text: str) -> List[Dict[str, Any]]:
        """
        Fallback parser when clickable tool elements are absent.
        Heuristic: after the 'Tools' section header, treat alternating non-empty lines
        as tool names and subsequent non-empty lines as descriptions until a section break.
        """
        tools = []
        lines = [ln.strip() for ln in body_text.split("\n")]

        try:
            start_idx = lines.index("Tools")
        except ValueError:
            return tools

        i = start_idx + 1
        # Skip numeric lines like total count or pagination
        while i < len(lines) and (lines[i].isdigit() or "/" in lines[i]):
            i += 1

        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if line in ["Connect", "Details", "Company", "Capabilities"]:
                break
            # Skip section labels that may precede tools list
            if line in ["Resources", "Prompts"]:
                i += 1
                # Also skip any immediate numeric after heading
                if i < len(lines) and lines[i].isdigit():
                    i += 1
                continue

            name = line
            i += 1
            desc_lines = []
            while i < len(lines):
                desc_line = lines[i].strip()
                if not desc_line:
                    i += 1
                    continue
                if desc_line in ["Connect", "Details", "Resources", "Company", "Capabilities", "Prompts"]:
                    break
                if desc_line.isupper() and "_" in desc_line:
                    break
                # Heuristic: stop if looks like next name (short line, title case or contains parentheses)
                if len(desc_line.split()) <= 6 and desc_line[0].isupper():
                    break
                desc_lines.append(desc_line)
                i += 1
            description = " ".join(desc_lines).strip()
            tools.append(
                {
                    "name": name,
                    "description": description,
                    "inputSchema": {"type": "object", "properties": {}},
                }
            )
        return tools
    
    def scrape_tools_with_params(self, page, server_url: str, max_tools: int = None) -> List[Dict[str, Any]]:
        """
        Scrape tools with complete parameter information by clicking to expand each tool
        Handles pagination across multiple tool pages
        
        Args:
            max_tools: Maximum number of tools to extract. If None, extract all tools.
        """
        tools = []
        tool_page_num = 1
        
        while max_tools is None or len(tools) < max_tools:
            # Go to specific tools page
            tools_url = f"{server_url}?capability=tools&page={tool_page_num}"
            try:
                page.goto(tools_url, wait_until='networkidle', timeout=90000)  # 90 second timeout
                page.wait_for_timeout(5000)  # Wait 5 seconds for dynamic content
            except Exception as e:
                print(f"      [WARN] networkidle load failed for tools page {tool_page_num}: {e}. Retrying with domcontentloaded...")
                try:
                    page.goto(tools_url, wait_until='domcontentloaded', timeout=120000)
                    page.wait_for_timeout(7000)  # slightly longer wait after fallback
                except Exception as e2:
                    print(f"      [ERROR] Failed to load tools page {tool_page_num} even after fallback: {e2}")
                    break
            
            # Check pagination
            body_text = page.locator('body').inner_text()
            page_indicator = re.search(r'(\d+)\s*/\s*(\d+)', body_text)
            
            if page_indicator:
                current_page = int(page_indicator.group(1))
                total_pages = int(page_indicator.group(2))
                print(f"      Tool page {current_page}/{total_pages}")
            
            # Find all clickable tool elements on this page
            clickable_elements = page.query_selector_all('h3, h4, button, [role="button"]')
            
            tool_elements = []
            for elem in clickable_elements:
                try:
                    text = elem.inner_text().strip()
                    
                    # Check if this looks like a tool name
                    # Patterns: UPPERCASE_WITH_UNDERSCORES, lowercase_with_underscores, camelCase, names-with-hyphens, or descriptive names with tool ID in parens
                    
                    # First, exclude common non-tool patterns
                    excluded_patterns = [
                        'Developers', 'Details', 'Resources', 'Company', 'Capabilities',
                        'Get connection URL', 'Or add to your client', 'Quality Score',
                        'Monthly Tool Calls', 'Uptime', 'Local', 'Published', 'Connect',
                        'View more', 'Pricing', 'Login', 'Start for Free', 'Tools', 'Prompts'
                    ]
                    
                    if text in excluded_patterns:
                        continue
                    
                    # Skip if it looks like a number or pagination
                    if text.isdigit() or '/' in text and all(p.strip().isdigit() for p in text.split('/')):
                        continue
                    
                    # Check if it matches tool name patterns
                    is_tool_name = False
                    
                    if text and len(text) > 3:
                        # Pattern 1: ALL_CAPS_WITH_UNDERSCORES (YOUTUBE_GET_VIDEO)
                        if text.isupper() and '_' in text and len(text) < 100:
                            is_tool_name = True
                        
                        # Pattern 2: lowercase_with_underscores (search_engine)
                        elif text.islower() and '_' in text and len(text) < 100:
                            is_tool_name = True
                        
                        # Pattern 3: names-with-hyphens (linkup-search)
                        elif '-' in text and text.replace('-', '').replace('_', '').isalnum() and len(text) < 100:
                            is_tool_name = True
                        
                        # Pattern 4: camelCase (searchSymbol, cryptoCategories)
                        elif text[0].islower() and any(c.isupper() for c in text) and text.replace('_', '').isalnum() and len(text) < 100:
                            is_tool_name = True
                        
                        # Pattern 5: lowercase alphanumeric without spaces (simple tool names like 'fetch', 'search')
                        # FIX #1: Changed from len(text) > 5 to len(text) >= 4 to catch short tool names
                        elif text.islower() and text.isalnum() and len(text) >= 4 and len(text) < 100:
                            is_tool_name = True
                        
                        # Pattern 6: Descriptive name with tool ID in parentheses
                        # Examples: "Add Sheet (GOOGLESHEETS_ADD_SHEET)", "List comments (list_comments)", "Run Command (ssh_run)"
                        elif '(' in text and ')' in text and len(text) < 200:
                            # Extract content in LAST set of parentheses (handles cases like "Delete Dimension (Rows/Columns) (GOOGLESHEETS_DELETE_DIMENSION)")
                            paren_start = text.rfind('(')  # Use rfind to get the LAST occurrence
                            paren_end = text.rfind(')')
                            if paren_start > 0 and paren_end > paren_start:
                                in_parens = text[paren_start+1:paren_end].strip()
                                # Check if content in parens looks like a tool ID
                                # FIX #2: Accept both UPPERCASE and lowercase tool IDs in parentheses
                                # Accept: UPPERCASE_WITH_UNDERSCORES, lowercase_with_underscores, or names-with-hyphens
                                if ((in_parens.isupper() or in_parens.islower()) and ('_' in in_parens or '-' in in_parens)) or '-' in in_parens:
                                    is_tool_name = True
                    
                    if is_tool_name:
                        # Avoid duplicates
                        if not any(t['text'] == text for t in tool_elements):
                            tool_elements.append({'element': elem, 'text': text})
                except:
                    continue
            
            if not tool_elements:
                print(f"      No clickable tools found on page {tool_page_num}, attempting text fallback")
                # Fallback: parse tool names/descriptions from body text when click targets are absent
                body_text = page.locator('body').inner_text()
                fallback_tools = self.parse_tools_from_text(body_text)
                if fallback_tools:
                    tools.extend(fallback_tools)
                break
            
            print(f"      Found {len(tool_elements)} clickable tools on this page")
            
            # Click on each tool to expand and extract
            previous_element = None
            for i, tool_info in enumerate(tool_elements):
                if max_tools is not None and len(tools) >= max_tools:
                    break
                
                try:
                    progress = f"{len(tools)+1}/{max_tools}" if max_tools else f"{len(tools)+1}"
                    print(f"      Extracting tool {progress}: {tool_info['text'][:40]}...")
                    
                    # Collapse previous tool if any
                    if previous_element:
                        try:
                            previous_element.click()
                            page.wait_for_timeout(500)
                        except:
                            pass
                    
                    # Click to expand current tool
                    tool_info['element'].click()
                    page.wait_for_timeout(3000)  # Increased wait time for slow pages
                    
                    # Extract tool data
                    tool_data = self.extract_tool_with_params(page, tool_info['text'])
                    
                    if tool_data and tool_data.get('name'):
                        tools.append(tool_data)
                    
                    previous_element = tool_info['element']
                    
                except Exception as e:
                    print(f"        Error extracting tool: {e}")
                    continue
            
            # Check if we need to go to next page
            if max_tools is not None and len(tools) >= max_tools:
                break
            
            # Check for next page link (more reliable than pagination text)
            next_tool_page = page.query_selector(f'a[href*="capability=tools&page={tool_page_num + 1}"]')
            if not next_tool_page:
                # Also check for just page parameter
                next_tool_page = page.query_selector(f'a[href*="page={tool_page_num + 1}"]')
                if not next_tool_page:
                    print(f"      No more tool pages")
                    break
            
            tool_page_num += 1
            
            # Safety limit (increased to handle servers with many tools)
            if tool_page_num > 100:  # Allow up to 100 pages (~500 tools max)
                print(f"      [WARNING] Reached safety limit of 100 tool pages")
                break
        
        return tools
    
    def scrape_single_server_with_browser(self, server_url: str, max_retries: int = 3) -> Optional[Dict[str, Any]]:
        """
        Scrape a single server in its own browser instance (for parallel execution)
        With retry logic for failed attempts
        """
        for attempt in range(max_retries):
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    
                    # Increase timeout for slow pages
                    page.set_default_timeout(60000)  # 60 seconds
                    
                    server_data = self.scrape_server_page(page, server_url)
                    browser.close()
                    
                    if server_data:
                        # Normalize server data
                        server_data = self.normalize_server(server_data)
                        
                        # Update statistics
                        with self.stats_lock:
                            self.stats['scraped'] += 1
                            tools_count = len(server_data.get('tools', []))
                            expected_tools = server_data.get('total_tools', 0)
                            self.stats['total_tools'] += tools_count
                            
                            if tools_count == expected_tools and expected_tools > 0:
                                self.stats['success'] += 1
                            elif tools_count > 0:
                                self.stats['partial'] += 1
                            else:
                                self.stats['failed'] += 1
                        
                        # Save incrementally if enabled
                        self.save_server_incrementally(server_data)
                    
                    return server_data
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # Exponential backoff: 2s, 4s, 6s
                    print(f"  [RETRY] Attempt {attempt + 1} failed for {server_url}, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    print(f"  [ERROR] All {max_retries} attempts failed for {server_url}: {e}")
                    try:
                        browser.close()
                    except:
                        pass
                    
                    # Update statistics for failed server
                    with self.stats_lock:
                        self.stats['scraped'] += 1
                        self.stats['failed'] += 1
                    
                    return None
    
    def scrape_all_servers(self, max_pages: int = None, max_servers: int = None, num_threads: int = 1) -> List[Dict[str, Any]]:
        """
        Scrape all servers from all pages, a specific page, or up to a maximum number of servers
        
        Args:
            max_pages: If set, scrape only that specific page number
            max_servers: If set, scrape only up to this many servers (across multiple pages)
            num_threads: Number of parallel threads to use (default: 1, max recommended: 10)
        """
        # First, collect all server URLs
        all_server_urls = []
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            
            # If max_pages is set, scrape only that specific page
            # Otherwise, scrape all pages starting from page 1
            if max_pages:
                page_num = max_pages
                scrape_single_page = True
            else:
                page_num = 1
                scrape_single_page = False
            
            print(f"\n=== Collecting server URLs ===")
            
            while True:
                print(f"\nScanning page {page_num}...")
                url = f"{self.base_url}/servers?page={page_num}"
                try:
                    page.goto(url, wait_until='networkidle', timeout=60000)
                    page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"[ERROR] Failed to load page {page_num}: {e}")
                    break
                
                # Get all server links on this page
                server_links = page.query_selector_all('a[href*="/server/"]')
                
                if not server_links:
                    print(f"No servers found on page {page_num}. Stopping.")
                    break
                
                # Extract unique server URLs
                page_server_urls = set()
                for link in server_links:
                    href = link.get_attribute('href')
                    if href and href.startswith('/server/') and href != '/servers':
                        # Remove query parameters
                        href = href.split('?')[0]
                        full_url = f"{self.base_url}{href}"
                        page_server_urls.add(full_url)
                
                print(f"Found {len(page_server_urls)} unique servers on page {page_num}")
                all_server_urls.extend(sorted(page_server_urls))
                
                # Check if we've collected enough servers
                if max_servers and len(all_server_urls) >= max_servers:
                    all_server_urls = all_server_urls[:max_servers]
                    print(f"Reached server limit of {max_servers}. Stopping URL collection.")
                    break
                
                # If scraping a specific page, stop after that page
                if scrape_single_page:
                    print(f"Finished scanning page {page_num}.")
                    break
                
                # Try next page (keep going until we find an empty page)
                page_num += 1
                
                # Safety limit to prevent infinite loop
                if page_num > 100:
                    print(f"Reached safety limit of 100 pages. Stopping.")
                    break
            
            browser.close()
        
        # Update total servers count
        with self.stats_lock:
            self.stats['total_servers'] = len(all_server_urls)
        
        print(f"\n=== Scraping {len(all_server_urls)} servers with {num_threads} thread(s) ===")
        
        # Scrape servers in parallel
        if num_threads > 1:
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                # Submit all tasks
                future_to_url = {executor.submit(self.scrape_single_server_with_browser, url): url for url in all_server_urls}
                
                # Process completed tasks
                for future in as_completed(future_to_url):
                    url = future_to_url[future]
                    try:
                        server_data = future.result()
                        if server_data:
                            with self.lock:
                                self.servers_data.append(server_data)
                                tools_count = len(server_data.get('tools', []))
                                expected_tools = server_data.get('total_tools', 0)
                                progress = f"[{self.stats['scraped']}/{self.stats['total_servers']}]"
                                status = "✓" if tools_count == expected_tools else "~" if tools_count > 0 else "✗"
                                print(f"  {progress} {status} {server_data['server_name'][:40]:<40} ({tools_count}/{expected_tools} tools)")
                    except Exception as e:
                        print(f"  [ERROR] Error scraping {url}: {e}")
        else:
            # Single-threaded mode
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                
                for i, server_url in enumerate(all_server_urls, 1):
                    try:
                        server_data = self.scrape_server_page(page, server_url)
                        if server_data:
                            server_data = self.normalize_server(server_data)
                            self.servers_data.append(server_data)
                            
                            # Update statistics
                            with self.stats_lock:
                                self.stats['scraped'] += 1
                                tools_count = len(server_data.get('tools', []))
                                expected_tools = server_data.get('total_tools', 0)
                                self.stats['total_tools'] += tools_count
                                
                                if tools_count == expected_tools and expected_tools > 0:
                                    self.stats['success'] += 1
                                elif tools_count > 0:
                                    self.stats['partial'] += 1
                                else:
                                    self.stats['failed'] += 1
                            
                            # Save incrementally if enabled
                            self.save_server_incrementally(server_data)
                            
                            tools_count = len(server_data.get('tools', []))
                            expected_tools = server_data.get('total_tools', 0)
                            status = "✓" if tools_count == expected_tools else "~" if tools_count > 0 else "✗"
                            print(f"  [{i}/{len(all_server_urls)}] {status} {server_data['server_name'][:40]:<40} ({tools_count}/{expected_tools} tools)")
                    except Exception as e:
                        print(f"  [ERROR] Error scraping {server_url}: {e}")
                        with self.stats_lock:
                            self.stats['scraped'] += 1
                            self.stats['failed'] += 1
                
                browser.close()
        
        return self.servers_data
    
    def scrape_server_page(self, page, url: str) -> Dict[str, Any]:
        """
        Scrape individual server page with complete tool information including parameters
        """
        page.goto(url, wait_until='networkidle', timeout=60000)
        page.wait_for_timeout(2000)
        
        # Extract server slug from URL
        server_slug = url.split('/server/')[-1].split('?')[0]
        
        # Get server name from h1
        server_name = ""
        h1 = page.query_selector('h1')
        if h1:
            server_name = h1.inner_text().strip()
        
        # Get description from meta tag
        description = ""
        meta_desc = page.query_selector('meta[name="description"]')
        if meta_desc:
            description = meta_desc.get_attribute('content') or ''
        
        # Get homepage and source code from page text
        homepage = ""
        source_code = ""
        
        body_text = page.locator('body').inner_text()
        
        # Look for Homepage
        homepage_match = re.search(r'Homepage\s+([^\s\n]+\.[^\s\n]+)', body_text)
        if homepage_match:
            homepage = homepage_match.group(1)
            if not homepage.startswith('http'):
                homepage = 'https://' + homepage
        
        # Look for Source Code
        source_match = re.search(r'Source Code\s+([^\s\n]+)', body_text)
        if source_match:
            source_code = source_match.group(1)
            if not source_code.startswith('http'):
                source_code = 'https://github.com/' + source_code
        
        # Get connection URL
        connection_url = f"https://server.smithery.ai/{server_slug}/mcp"
        
        # Get authentication method (default to OAuth for smithery.ai)
        auth_method = "OAuth"
        
        # Get tool count from page
        total_tools = 0
        tool_count_match = re.search(r'Tools\s*(\d+)', body_text)
        if tool_count_match:
            total_tools = int(tool_count_match.group(1))

        # Scrape tools with parameters (all tools)
        tools = []
        if total_tools > 0:
            print(f"    Scraping ALL tools with parameters ({total_tools} total)...")
            tools = self.scrape_tools_with_params(page, url, max_tools=None)
        else:
            # Attempt scraping even when total_tools is unknown (count unavailable on page)
            print("    Tool count not found; attempting to scrape tools anyway...")
            tools = self.scrape_tools_with_params(page, url, max_tools=None)
            total_tools = len(tools)
        
        # Build server data
        server_data = {
            "server_name": server_name or "Unknown",
            "server_url": url,
            "connection_url": connection_url,
            "homepage": homepage,
            "source_code": source_code,
            "authentication_method": auth_method,
            "description": description,
            "total_tools": total_tools,
            "tools": tools,
        }
        
        return server_data
    
    def save_to_json(self, filename: Optional[str] = None):
        """Save scraped data to JSON file"""
        if filename is None:
            filename = self.output_file
        
        # If incremental save was used, load from JSONL and merge
        if self.incremental_save and os.path.exists(self.jsonl_file):
            print(f"\nLoading servers from incremental save file...")
            servers_from_jsonl = []
            with open(self.jsonl_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            server_data = json.loads(line)
                            servers_from_jsonl.append(server_data)
                        except json.JSONDecodeError as e:
                            print(f"Warning: Failed to parse line in JSONL file: {e}")
            
            # Merge with in-memory data (avoid duplicates)
            existing_urls = {s['server_url'] for s in self.servers_data}
            for server in servers_from_jsonl:
                if server['server_url'] not in existing_urls:
                    self.servers_data.append(server)
            
            print(f"Loaded {len(servers_from_jsonl)} servers from incremental save")
        
        # Normalize all servers before saving
        normalized_servers = [self.normalize_server(s) for s in self.servers_data]
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(normalized_servers, f, indent=2, ensure_ascii=False)
        
        print(f"\n[OK] Saved {len(normalized_servers)} servers to {filename}")
    
    def print_statistics(self):
        """Print scraping statistics"""
        with self.stats_lock:
            stats = self.stats.copy()
        
        print("\n" + "="*80)
        print("SCRAPING STATISTICS")
        print("="*80)
        print(f"Total servers found:    {stats['total_servers']}")
        print(f"Servers scraped:        {stats['scraped']}")
        print(f"Success (100% tools):   {stats['success']}")
        print(f"Partial (>0 tools):     {stats['partial']}")
        print(f"Failed (0 tools):       {stats['failed']}")
        print(f"Total tools extracted:  {stats['total_tools']}")
        
        if stats['scraped'] > 0:
            success_rate = (stats['success'] / stats['scraped']) * 100
            print(f"\nSuccess rate:           {success_rate:.1f}%")
        
        print("="*80)

def main():
    # Fix Unicode encoding for Windows console
    if sys.platform == 'win32':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except:
            pass
    
    parser = argparse.ArgumentParser(
        description='Scrape servers from smithery.ai with complete tool parameters (Finalized Version)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python smithery_scraper.py                      # Scrape all servers (single-threaded)
  python smithery_scraper.py --page 1             # Scrape only page 1
  python smithery_scraper.py --limit 20           # Scrape first 20 servers
  python smithery_scraper.py -n 5 -t 3            # Scrape first 5 servers with 3 threads
  python smithery_scraper.py -p 2 -n 10 -t 5      # Scrape up to 10 servers from page 2 with 5 threads
  python smithery_scraper.py --threads 5          # Scrape all servers with 5 parallel threads
  python smithery_scraper.py --incremental        # Enable incremental saving (recommended for long runs)
        '''
    )
    parser.add_argument(
        '-p', '--page',
        type=int,
        default=None,
        help='Specific page number to scrape (default: start from page 1)'
    )
    parser.add_argument(
        '-n', '--limit',
        type=int,
        default=None,
        help='Maximum number of servers to scrape (default: scrape all)'
    )
    parser.add_argument(
        '-t', '--threads',
        type=int,
        default=1,
        help='Number of parallel threads to use (default: 1, recommended: 3-5, max: 10)'
    )
    parser.add_argument(
        '-o', '--output',
        type=str,
        default='smithery_servers.json',
        help='Output JSON file name (default: smithery_servers.json)'
    )
    parser.add_argument(
        '--incremental',
        action='store_true',
        help='Enable incremental saving to JSONL file (recommended for long runs to prevent data loss)'
    )
    
    args = parser.parse_args()
    
    # Validate threads
    if args.threads < 1:
        print("[ERROR] Number of threads must be at least 1")
        return
    if args.threads > 10:
        print("[WARNING] Using more than 10 threads may cause rate limiting. Limiting to 10.")
        args.threads = 10
    
    scraper = SmitheryScraper(incremental_save=args.incremental, output_file=args.output)
    
    print("="*80)
    print("Smithery.ai Server Scraper - Finalized Version")
    print("="*80)
    print(f"Output file: {args.output}")
    if args.incremental:
        print(f"Incremental save: Enabled ({scraper.jsonl_file})")
    print("="*80)
    
    start_time = time.time()
    
    if args.page and args.limit:
        print(f"Scraping up to {args.limit} servers starting from page {args.page} with {args.threads} thread(s)...")
        servers = scraper.scrape_all_servers(max_pages=args.page, max_servers=args.limit, num_threads=args.threads)
    elif args.page:
        print(f"Scraping page {args.page} only with {args.threads} thread(s)...")
        servers = scraper.scrape_all_servers(max_pages=args.page, num_threads=args.threads)
    elif args.limit:
        print(f"Scraping first {args.limit} servers with {args.threads} thread(s)...")
        servers = scraper.scrape_all_servers(max_servers=args.limit, num_threads=args.threads)
    else:
        print(f"Scraping all servers from all pages with {args.threads} thread(s)...")
        servers = scraper.scrape_all_servers(num_threads=args.threads)
    
    elapsed_time = time.time() - start_time
    
    print("\n" + "="*80)
    print(f"Scraping complete! Total servers scraped: {len(servers)}")
    print(f"Time elapsed: {elapsed_time:.1f}s ({elapsed_time/60:.1f} minutes)")
    print("="*80)
    
    scraper.save_to_json()
    scraper.print_statistics()
    
    # Print sample with tools
    if servers:
        # Find a server with tools
        sample = servers[0]
        for server in servers:
            if server.get('tools') and server['tools'][0].get('inputSchema', {}).get('properties'):
                sample = server
                break
        
        print("\n=== Sample Server Data ===")
        print(json.dumps(sample, indent=2)[:2000])  # Limit output

if __name__ == '__main__':
    main()
